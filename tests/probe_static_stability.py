"""GPU check: static-only mode must leave motion regions exactly unmodified."""
from dataclasses import replace
import json
import os
from pathlib import Path

import numpy as np

from gamestream.config import load_config
from gamestream.dlss import ParallelDlssFilter
from gamestream.temporal import StaticRegionStabilizer


class CheckedStabilizer(StaticRegionStabilizer):
    region = None
    checks = 0
    def process(self, source, output):
        result = super().process(source, output)
        if self.region is not None:
            y, x = self.region
            np.testing.assert_array_equal(result[y, x], output[y, x])
            self.checks += 1
        return result


def main():
    os.environ['GAMESTREAM_DLSS_PREFIX'] = str(Path('.state/dlss-static-probe').resolve())
    height, width = 360, 640
    y, x = np.mgrid[:height, :width]
    source = np.empty((height, width, 4), np.uint8)
    source[..., 0] = (x * 7 + y * 3) % 200
    source[..., 1] = (x * 2 + y * 11) % 200
    source[..., 2] = (x * 13 + y * 5) % 200
    source[..., 3] = 255
    for neural_first in (False, True):
        config = replace(load_config().dlss, workers=2, upscaling_factor=2,
                         temporal_stability=True, temporal_method='static_regions',
                         neural_before_upscale=neural_first)
        pool = ParallelDlssFilter(config, width, height)
        checked = CheckedStabilizer()
        pool.workers[0]._stabilizer = checked
        assert pool.workers[0]._guide is None
        try:
            for _ in range(4): pool.process(0, source.tobytes())
            local = source.copy()
            local[100:150, 200:250, :3] = 240
            checked.region = (slice(184, 316), slice(384, 516))
            pool.process(0, local.tobytes())  # moving object appears
            pool.process(0, source.tobytes())  # background revealed
            checked.region = (slice(None), slice(None))
            pool.process(0, np.roll(source, (2, 5), axis=(0, 1)).tobytes())
            fade = source.copy()
            fade[..., :3] += 1
            pool.process(0, fade.tobytes())
            pool.process(0, np.full_like(source, 255).tobytes())
            print(json.dumps({'neural_first': neural_first, 'motion_checks': checked.checks,
                              'changed_region_max_deviation': 0}), flush=True)
        finally:
            pool.close()


if __name__ == '__main__':
    main()
