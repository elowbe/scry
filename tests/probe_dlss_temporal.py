"""Opt-in GPU A/B: repeatable pan with known motion, no desktop capture.

Compare motion-compensated output variation after warmup. This measures temporal
inconsistency on a synthetic chart, not game image quality or streaming FPS.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path

import numpy as np

from gamestream.config import load_config
from gamestream.dlss import ParallelDlssFilter
from gamestream.temporal import TemporalFrame


class HistoryWithoutMotion:
    """Ablation: isolate why changing only Reset is insufficient."""
    def __init__(self, width, height):
        self.zero = np.zeros((height, width, 2), np.float16)
        self.first = True
    def process(self, rgba):
        first, self.first = self.first, False
        return TemporalFrame(self.zero, first, 0.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--factor', type=float, choices=(1, 2), default=2)
    parser.add_argument('--neural-first', action='store_true')
    parser.add_argument('--frames', type=int, default=36)
    args = parser.parse_args()
    if args.frames < 16:
        parser.error('--frames must be at least 16')
    os.environ['GAMESTREAM_DLSS_PREFIX'] = str(Path('.state/dlss-temporal-probe').resolve())
    width, height = 640, 360
    y, x = np.mgrid[:height, :width + args.frames * 2]
    base = np.empty((*x.shape, 4), np.uint8)
    for channel in range(3):
        signal = (110 + 40 * np.sin(x / (7 + channel * 9))
                  + 35 * np.cos(y / (11 + channel * 4))
                  + 30 * np.sin((x + y) / (3 + channel * 2)))
        base[..., channel] = np.clip(signal, 0, 255)
    base[..., 3] = 255
    results = []
    for mode in ('reset_every_frame', 'history_without_motion', 'motion_guided'):
        config = replace(load_config().dlss, workers=1, upscaling_factor=args.factor,
                         neural_before_upscale=args.neural_first,
                         temporal_method="optical_flow", temporal_stability=mode != 'reset_every_frame')
        pool = ParallelDlssFilter(config, width, height)
        if mode == 'history_without_motion':
            pool.workers[0]._guide = HistoryWithoutMotion(width, height)
        errors, times, previous = [], [], None
        try:
            for index in range(args.frames):
                frame = base[:, index * 2:index * 2 + width].copy()
                result = pool.process(0, frame.tobytes())
                output = result.rgba[..., :3].astype(np.float32)
                if index >= 12:
                    shift = int(2 * args.factor)
                    error = np.abs(output[32:-32, 32:-32-shift] - previous[32:-32, 32+shift:-32])
                    errors.append(float(error.mean()))
                    times.append(result.processing_ms)
                previous = output
            results.append({'mode': mode, 'warped_mae_8bit': float(np.mean(errors)),
                            'p95_warped_mae_8bit': float(np.percentile(errors, 95)),
                            'mean_processing_ms': float(np.mean(times))})
        finally:
            pool.close()
    print(json.dumps({'factor': args.factor, 'neural_first': args.neural_first,
                      'frames': args.frames, 'results': results}, indent=2))


if __name__ == '__main__':
    main()
