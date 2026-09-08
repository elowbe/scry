import os
import threading

from gamestream.portal_capture import capture_pipeline, pump_frames, write_frame


def test_backpressure_preserves_complete_frame_bytes():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    stopped = threading.Event()
    frame = bytes(range(256)) * 4096
    writer = threading.Thread(target=write_frame, args=(write_fd, frame, stopped))
    writer.start()
    received = bytearray()
    try:
        while len(received) < len(frame):
            received.extend(os.read(read_fd, 4096))
        writer.join(1)
        assert not writer.is_alive()
        assert received == frame
    finally:
        stopped.set()
        writer.join(1)
        os.close(read_fd)
        os.close(write_fd)


def test_shutdown_interrupts_blocked_capture_write():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    stopped = threading.Event()
    writer = threading.Thread(target=write_frame, args=(write_fd, bytes(1024 * 1024), stopped))
    writer.start()
    try:
        stopped.set()
        writer.join(.5)
        assert not writer.is_alive()
    finally:
        stopped.set()
        writer.join(1)
        os.close(read_fd)
        os.close(write_fd)


def test_static_frames_repeat_and_slow_encoder_does_not_build_backlog(monkeypatch):
    stopped = threading.Event()
    samples = iter([None, b'first', None, b'latest', None])
    emitted = []
    clock = [0.0]
    monkeypatch.setattr('gamestream.portal_capture.time.monotonic', lambda: clock[0])
    waits = []

    def wait(delay):
        waits.append(delay)
        clock[0] += delay
        if len(waits) == 5:
            stopped.set()

    monkeypatch.setattr(stopped, 'wait', wait)

    def write(_fd, data, _stopped):
        emitted.append(data)
        clock[0] += .5  # Much slower than the target frame period.

    monkeypatch.setattr('gamestream.portal_capture.write_frame', write)
    pump_frames(lambda: next(samples), 1, 60, stopped)
    assert emitted == [b'first', b'first', b'latest', b'latest']
    assert len(waits) == 5
    assert all(0 <= delay <= 1 / 60 for delay in waits)


def test_capture_does_not_use_shared_keepalive_or_timestamp_based_rate_conversion():
    description = capture_pipeline(2560, 1440)
    assert 'keepalive-time=0' in description
    assert 'videorate' not in description
    assert 'max-buffers=1 drop=true' in description


def test_timestamp_discontinuity_with_system_gstreamer():
    import subprocess
    from pathlib import Path
    import pytest

    python = Path('/usr/bin/python3')
    if not python.exists():
        pytest.skip('System Python is unavailable')
    available = subprocess.run([str(python), '-c',
        "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; "
        "Gst.init(None); assert all(Gst.ElementFactory.find(n) for n in "
        "('appsrc', 'appsink', 'videorate', 'videoconvert', 'videoscale'))"],
        capture_output=True, timeout=10)
    if available.returncode:
        pytest.skip('System GStreamer test plugins are unavailable')
    result = subprocess.run([str(python), '-m', 'tests.probe_capture_timestamps'],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
