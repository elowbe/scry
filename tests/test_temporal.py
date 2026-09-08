from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from gamestream.temporal import TemporalGuide
from gamestream.config import DlssConfig
from gamestream.dlss import ParallelDlssFilter, WarmDlssCache


def texture(width=320, height=180):
    rng = np.random.default_rng(12)
    gray = cv2.GaussianBlur(rng.integers(0, 256, (height, width), np.uint8), (5, 5), 0)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGBA)


def test_backward_motion_uses_input_pixels_and_preserves_pan_history():
    image = texture()
    guide = TemporalGuide(320, 180, flow_width=160)
    assert guide.process(image).reset
    shifted = cv2.warpAffine(image, np.float32([[1, 0, 6], [0, 1, -2]]), (320, 180))
    result = guide.process(shifted)
    assert not result.reset
    assert result.motion.dtype == np.float16
    assert result.motion.shape == (180, 320, 2)
    np.testing.assert_allclose(np.median(result.motion[25:-25, 25:-25], axis=(0, 1)), [-6, 2], atol=.65)


def test_static_frames_keep_history_and_cuts_reset_it():
    guide = TemporalGuide(320, 180)
    black = np.zeros((180, 320, 4), np.uint8)
    assert guide.process(black).reset
    still = guide.process(black)
    assert not still.reset and not still.motion.any()
    assert guide.process(np.full_like(black, 255)).reset
    assert not guide.process(np.full_like(black, 255)).reset
    guide.reset()
    assert guide.process(black).reset


def test_temporal_pool_owns_one_consecutive_history_even_if_workers_requested():
    class Worker:
        def __init__(self, config, width, height, **kwargs):
            self.input_width = self.output_width = width
            self.input_height = self.output_height = height
        def reset_history(self):
            self.reset = True
    with patch('gamestream.dlss.LiveDlssFilter', Worker):
        config = DlssConfig(workers=4, temporal_stability=True)
        pool = ParallelDlssFilter(config, 320, 180)
        assert pool.worker_count == 1
        pool.reset_history()
        assert pool.workers[0].reset
        assert ParallelDlssFilter(replace(config, temporal_stability=False), 320, 180).worker_count == 4


def test_temporal_mode_invalidates_warm_cache():
    config = DlssConfig(temporal_stability=True)
    assert WarmDlssCache.signature(config, 320, 180) != WarmDlssCache.signature(
        replace(config, temporal_stability=False), 320, 180)


def test_transport_sends_motion_and_reset_only_at_discontinuities(monkeypatch):
    import io
    import gamestream.dlss as dlss
    image = texture()
    worker = dlss.LiveDlssFilter.__new__(dlss.LiveDlssFilter)
    worker.closed = False
    worker._np = np
    worker.input_width = worker.output_width = 320
    worker.input_height = worker.output_height = 180
    worker._mapping = bytearray(image.nbytes * 3)
    worker._frame_timeout = 1
    worker._stabilizer = None
    worker._guide = TemporalGuide(320, 180)
    worker._last_completed_at = None
    worker._history_gap_seconds = .5
    worker._swap_rb = False
    worker.index = 0
    worker._worker = SimpleNamespace(stdin=io.BytesIO())
    monkeypatch.setattr(dlss, '_read_exact', lambda stream, size, timeout=None: stream.read(size))

    def process(frame):
        worker._worker.stdout = io.BytesIO(dlss.RESULT_HEADER.pack(dlss.RESULT_MAGIC, worker.index, 1, image.nbytes))
        worker.process(frame.tobytes())
        return dlss.FRAME_HEADER.unpack(worker._worker.stdin.getvalue()[-dlss.FRAME_HEADER.size:])[2]

    assert process(image) == 1
    shifted = cv2.warpAffine(image, np.float32([[1, 0, 6], [0, 1, 0]]), (320, 180))
    assert process(shifted) == 0
    motion = np.frombuffer(worker._mapping, dtype='<f2', offset=image.nbytes * 2).reshape(180, 320, 2)
    assert np.median(motion[25:-25, 25:-25, 0]) == pytest.approx(-6, abs=.65)
    worker.reset_history()
    assert process(shifted) == 1
    assert not np.frombuffer(worker._mapping, dtype='<f2', offset=image.nbytes * 2).any()
    worker._last_completed_at = 0
    assert process(shifted) == 1
    # Neither the original renderer nor static-only smoothing may retain NGX
    # history or supply nonzero flow, even during repeated/moving frames.
    from gamestream.temporal import StaticRegionStabilizer
    worker._guide = None
    for stabilizer in (None, StaticRegionStabilizer()):
        worker._stabilizer = stabilizer
        assert process(image) == 1
        assert process(shifted) == 1
        assert not np.frombuffer(worker._mapping, dtype='<f2', offset=image.nbytes * 2).any()


def test_cut_between_unrelated_textures_with_similar_brightness_resets():
    rng = np.random.default_rng(999)
    guide = TemporalGuide(320, 180)
    def random_frame():
        gray = cv2.GaussianBlur(rng.integers(0, 256, (180, 320), np.uint8), (5, 5), 0)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGBA)
    guide.process(random_frame())
    assert guide.process(random_frame()).reset
