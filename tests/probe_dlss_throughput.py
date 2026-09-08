"""Opt-in benchmark for the persistent DLSS stream worker.

Runs synthetic changing frames through the exact RGBA transport used by the
live streamer. No captured desktop image is read or written.
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import statistics
import time

import numpy as np

from gamestream.config import load_config
from gamestream.dlss import ParallelDlssFilter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--workers", type=int, choices=range(1, 5))
    parser.add_argument("--factor", type=float, choices=(1., 1.5, 1.724, 2., 3.))
    parser.add_argument("--neural-first", action="store_true")
    parser.add_argument("--independent-frames", action="store_true", help="Disable temporal stability to benchmark parallel workers")
    parser.add_argument("--temporal-method", choices=("static_regions", "optical_flow"), default="static_regions")
    args = parser.parse_args()
    os.environ["GAMESTREAM_DLSS_PREFIX"] = str(Path(".state/dlss-benchmark-prefix").resolve())
    os.environ["GAMESTREAM_DLSS_PROFILE"] = "1"

    config = load_config().dlss
    config.neural_before_upscale = args.neural_first
    config.temporal_stability = not args.independent_frames
    config.temporal_method = args.temporal_method
    if args.workers:
        config.workers = args.workers
    if args.factor:
        config.upscaling_factor = args.factor
    width = round(2560 / config.upscaling_factor / 2) * 2
    height = round(1440 / config.upscaling_factor / 2) * 2
    pool = ParallelDlssFilter(config, width, height)
    executors = [ThreadPoolExecutor(max_workers=1) for _ in pool.workers]
    samples: list[float] = []
    try:
        frame = np.zeros((height, width, 4), dtype=np.uint8)
        frame[..., 3] = 255
        frames = []
        for index in range(min(args.frames, 16)):
            frame[..., 0] = (index * 17) & 255
            frame[..., 1] = np.arange(width, dtype=np.uint8)
            frame[..., 2] = np.arange(height, dtype=np.uint8)[:, None]
            frames.append(frame.tobytes())

        # Pay the model's one-time initialization before measuring steady state.
        warmups = [
            executor.submit(pool.process, index, frames[index % len(frames)])
            for index, executor in enumerate(executors)
        ]
        for future in warmups:
            future.result()

        pending = deque()
        started = time.perf_counter()
        for index in range(args.frames):
            frame_bytes = frames[index % len(frames)]
            worker_index = index % pool.worker_count
            pending.append(executors[worker_index].submit(pool.process, worker_index, frame_bytes))
            if len(pending) >= pool.worker_count:
                samples.append(pending.popleft().result().processing_ms)
        while pending:
            samples.append(pending.popleft().result().processing_ms)
        elapsed = time.perf_counter() - started
        print(
            f"frames={args.frames} elapsed={elapsed:.3f}s throughput={args.frames / elapsed:.2f}fps "
            f"workers={pool.worker_count} mean={statistics.fmean(samples):.2f}ms "
            f"p95={np.percentile(samples, 95):.2f}ms"
        )
        for line in pool.worker_logs:
            if "gamestream-profile" in line:
                print(line)
    finally:
        for executor in executors:
            executor.shutdown(wait=False, cancel_futures=True)
        if all(worker.index for worker in pool.workers):
            pool.close()
        else:
            pool.abort()


if __name__ == "__main__":
    main()
