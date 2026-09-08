"""Opt-in capture motion probe: keep an animated game focused on the host.

Counts distinct image contents, not repeated frames from PipeWire keepalive.
No screen images are saved. A genuinely static scene should fail this probe.
"""
import argparse
import hashlib
import selectors
import subprocess
import time

from gamestream.config import load_config
from gamestream.media import build_raw_capture_command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=10)
    args = parser.parse_args()
    config = load_config().stream
    width, height = 640, 360
    frame_size = width * height * 4
    process = subprocess.Popen(build_raw_capture_command(config, width, height), stdout=subprocess.PIPE)
    poll = selectors.DefaultSelector()
    poll.register(process.stdout, selectors.EVENT_READ)
    pending = bytearray()
    counts = []
    hashes = set()
    frames = 0
    interval = time.monotonic()
    started = None
    deadline = interval + args.seconds + 15
    try:
        while time.monotonic() < deadline:
            if not poll.select(timeout=1):
                if process.poll() is not None:
                    raise RuntimeError(f'Capture process exited {process.returncode}')
                continue
            chunk = process.stdout.read1(65536)
            if not chunk:
                raise RuntimeError('Capture stream ended')
            pending.extend(chunk)
            while len(pending) >= frame_size:
                frame = pending[:frame_size]
                del pending[:frame_size]
                hashes.add(hashlib.blake2s(frame, digest_size=8).digest())
                frames += 1
                now = time.monotonic()
                if started is None:
                    started = interval = now
                if now - interval >= 1:
                    counts.append(len(hashes))
                    print(f'second={len(counts)} frames={frames} distinct_images={len(hashes)}', flush=True)
                    hashes.clear()
                    frames = 0
                    interval = now
            if len(counts) >= args.seconds:
                # Ignore startup; require motion in every subsequent interval.
                if min(counts[1:] or counts) <= 1:
                    raise RuntimeError('Repeated image detected; animated fullscreen capture did not pass')
                print('PASS: changing image contents throughout the capture interval', flush=True)
                return
        raise RuntimeError('Timed out waiting for captured frames')
    finally:
        poll.close()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


if __name__ == '__main__':
    main()
