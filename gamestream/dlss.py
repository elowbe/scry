"""Persistent, RGBA8 DLSS 5 frame middleman for live streaming.

The bundled D3D12/NGX implementation is adapted from the independent
MIT ComfyUI bridge. Scry - Server replaces its offline RGB32F pipe host with a compact
RGBA8 protocol and supplies FP16 optical-flow motion to a persistent worker.
It never reads or changes a game file or game compatibility prefix.
"""

from __future__ import annotations

import os
import json
import sys
import mmap
import tempfile
if sys.platform != "win32":
    import fcntl
from pathlib import Path
import select
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, replace

from .config import DlssConfig, PROJECT_ROOT
from .errors import PreflightError

VIDEO_HEADER = struct.Struct("<4sIIIIIIIIIIII4f")
FRAME_HEADER = struct.Struct("<4sII")
RESULT_HEADER = struct.Struct("<4sIII")
ERROR_LENGTH = struct.Struct("<I")
VIDEO_MAGIC = b"DNR4"
FRAME_MAGIC = b"FRM3"
RESULT_MAGIC = b"OUT3"
END_MAGIC = b"END3"
ORDERED_BRIDGE = PROJECT_ROOT / "native/bin/dlss5nr_bridge.dll"
FAST_HOST = PROJECT_ROOT / "native/bin/dlss5nr_stream_host.exe"


def _pipe_ready(stream, timeout):
    if sys.platform != "win32":
        return bool(select.select([stream], [], [], timeout)[0])
    import ctypes
    import msvcrt
    from ctypes import wintypes
    peek = ctypes.windll.kernel32.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                     wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    peek.restype = wintypes.BOOL
    available = wintypes.DWORD()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not peek(msvcrt.get_osfhandle(stream.fileno()), None, 0, None,
                    ctypes.byref(available), None):
            return True  # read reports EOF / broken pipe
        if available.value:
            return True
        time.sleep(0.001)
    return False


def _read_exact(stream, size: int, timeout: float | None = None) -> bytes:
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    deadline = time.monotonic() + timeout if timeout is not None else None
    while offset < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _pipe_ready(stream, remaining):
                raise TimeoutError(f"DLSS stream host timed out after {timeout:g} seconds")
        count = stream.readinto(view[offset:])
        if not count:
            raise EOFError(f"DLSS stream host stopped after {offset} of {size} bytes")
        offset += count
    return bytes(data)


def _write_exact(stream, data, *, flush: bool = True) -> None:
    view = memoryview(data).cast("B")
    offset = 0
    while offset < len(view):
        count = stream.write(view[offset:])
        if not count:
            raise BrokenPipeError(f"DLSS stream host accepted {offset} of {len(view)} bytes")
        offset += count
    if flush:
        stream.flush()


def _should_swap_red_blue(frame, reference) -> bool:
    """Detect the two resource channel layouts used by DLSS NR runtime builds."""
    import numpy as np

    sample_h = min(96, frame.shape[0], reference.shape[0])
    sample_w = min(96, frame.shape[1], reference.shape[1])
    frame_y = np.linspace(0, frame.shape[0] - 1, sample_h, dtype=np.int64)
    frame_x = np.linspace(0, frame.shape[1] - 1, sample_w, dtype=np.int64)
    ref_y = np.linspace(0, reference.shape[0] - 1, sample_h, dtype=np.int64)
    ref_x = np.linspace(0, reference.shape[1] - 1, sample_w, dtype=np.int64)
    output = frame[np.ix_(frame_y, frame_x)].astype(np.float32)
    source = reference[np.ix_(ref_y, ref_x)].astype(np.float32)
    raw_score = float(np.mean(np.abs(output - source)))
    swap_score = float(np.mean(np.abs(output[..., ::-1] - source)))
    return swap_score < raw_score


@dataclass(frozen=True, slots=True)
class DlssFrameResult:
    rgba: object
    processing_ms: float


class LiveDlssFilter:
    """One unknown-duration, fail-fast feature-18 worker with no frame backlog."""

    def __init__(
        self,
        config: DlssConfig,
        input_width: int,
        input_height: int,
        *,
        worker_index: int = 0,
        runtime_owner: LiveDlssFilter | None = None,
    ):
        if not FAST_HOST.is_file():
            raise PreflightError(f"Optimized DLSS stream host is missing: {FAST_HOST}")
        os.environ["DLSS5_FRAME_TIMEOUT"] = str(config.frame_timeout_seconds)
        try:
            import numpy as np
            from .dlss_support.diagnostics import LogRing, drain, ensure_supported, verify_direct_feature_18
            from .dlss_support.launcher import (
                WorkerLaunch,
                acquire_worker_slot,
                build_worker_launch,
                prepare_worker_launch,
                release_worker_slot,
                stop_proton_prefix,
                terminate_worker,
            )
            from .dlss_support.paths import find_runtime
            from .dlss_support.settings import DlssOptions, resolve_output_size
        except Exception as exc:
            raise PreflightError(f"DLSS5 bridge could not be imported: {exc}") from exc

        self._np = np
        self._release_worker_slot = release_worker_slot
        self._stop_proton_prefix = stop_proton_prefix
        self._terminate_worker = terminate_worker
        self._verify = verify_direct_feature_18
        self.input_width = int(input_width)
        self.input_height = int(input_height)
        self.options = DlssOptions.create(
            upscaling_factor=config.upscaling_factor,
            motion_mode="optical_flow" if config.temporal_stability and config.temporal_method == "optical_flow" else "none",
            warmup_frames=0,
            nr_style=config.nr_style,
            nr_preset=config.nr_preset,
            nr_intensity=config.nr_intensity,
            local_tone_strength=config.local_tone_strength,
            local_structure_strength=config.local_structure_strength,
            skin_structure_strength=config.skin_structure_strength,
            automatic_mask=config.automatic_mask,
        )
        self.output_width, self.output_height = resolve_output_size(
            self.input_width, self.input_height, self.options.upscaling_factor
        )
        self._guide = None
        self._stabilizer = None
        if config.temporal_stability:
            from .temporal import StaticRegionStabilizer, TemporalGuide
            if config.temporal_method == "optical_flow":
                self._guide = TemporalGuide(self.input_width, self.input_height)
            else:
                self._stabilizer = StaticRegionStabilizer()
        self._last_completed_at = None
        self._history_gap_seconds = max(0.5, 3.0 / config.target_fps)
        self.index = 0
        self.closed = False
        self.last_processing_ms = 0.0
        self._frame_timeout = config.frame_timeout_seconds
        self._swap_rb: bool | None = None
        self._slot_acquired = False
        self._prefix_lock = None
        self._worker = None
        self._log_thread = None
        self._logs = LogRing()
        self._mapping = None
        self._mapping_path = None
        self.worker_index = worker_index
        self._runtime_owner = runtime_owner

        try:
            if runtime_owner is None:
                layout = find_runtime(str(config.runtime_dir) if config.runtime_dir else None)
                ensure_supported(layout)
                # Never share the ComfyUI prefix: its startup cleanup terminates
                # every Wine worker in that prefix. One owner prepares and locks
                # the prefix; sibling workers then join the same Wine session.
                environment = dict(os.environ)
                compat = Path(environment.get("GAMESTREAM_DLSS_PREFIX", str((config.runtime_dir or PROJECT_ROOT / ".state/runtime").parent / "dlss-prefix"))).resolve()
                compat.mkdir(parents=True, exist_ok=True)
                self._prefix_lock = (compat / ".gamestream.lock").open("a+b")
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        self._prefix_lock.seek(0)
                        self._prefix_lock.write(b"0")
                        self._prefix_lock.flush()
                        self._prefix_lock.seek(0)
                        msvcrt.locking(self._prefix_lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(self._prefix_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PreflightError("Another Scry - Server DLSS worker is using this runtime prefix") from exc
                environment["DLSS5_PROTON_COMPAT_DATA"] = str(compat)
                support_file = (config.runtime_dir or PROJECT_ROOT / ".state/runtime").parent / "dlss-support.json"
                if support_file.is_file():
                    support = json.loads(support_file.read_text())
                    if support.get("proton_path"):
                        environment.setdefault("DLSS5_PROTON_PATH", support["proton_path"])
                original = build_worker_launch(layout, env=environment)
                if original.backend == "proton" and len(original.command) < 4:
                    raise PreflightError("The optimized DLSS stream host currently requires Linux/Proton")
                bridge = ORDERED_BRIDGE
                if not bridge.is_file():
                    raise PreflightError(f"DLSS5 D3D12 bridge is missing: {bridge}")
                self.launch = WorkerLaunch(
                    command=((*original.command[:2], str(FAST_HOST), str(layout.root), str(bridge))
                             if original.backend == "proton" else
                             (str(FAST_HOST), str(layout.root), str(bridge))),
                    cwd=FAST_HOST.parent,
                    env={**(original.env or environment), "DLSS5NR_CALLER_SHIM": str(FAST_HOST.parent / "nvngx.dll_comfy.dll")},
                    backend=original.backend,
                    start_new_session=original.start_new_session,
                )
                self._slot_acquired = acquire_worker_slot(self.launch)
                prepare_worker_launch(self.launch)
            else:
                if (input_width, input_height) != (
                    runtime_owner.input_width,
                    runtime_owner.input_height,
                ):
                    raise ValueError("DLSS pool workers must use the same input dimensions")
                self.launch = runtime_owner.launch
            descriptor, self._mapping_path = tempfile.mkstemp(prefix="gamestream-frame-")
            try:
                os.ftruncate(descriptor, (2 * self.input_width * self.input_height + self.output_width * self.output_height) * 4)
                self._mapping = mmap.mmap(descriptor, 0)
            finally:
                os.close(descriptor)
            self.launch = replace(self.launch, env={**self.launch.env,
                "GAMESTREAM_NEURAL_FIRST": "1" if config.neural_before_upscale else "0",
                "GAMESTREAM_FRAME_MAP": (self._mapping_path if sys.platform == "win32" else "Z:" + self._mapping_path.replace("/", "\\"))})
            self._worker = subprocess.Popen(
                self.launch.command,
                cwd=str(self.launch.cwd),
                env=self.launch.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=self.launch.start_new_session,
            )
            self._log_thread = threading.Thread(
                target=drain,
                args=(self._worker.stderr, self._logs),
                name=f"dlss5-stream-log-{worker_index}",
                daemon=True,
            )
            self._log_thread.start()
            native = self.options.native()
            header = VIDEO_HEADER.pack(
                VIDEO_MAGIC,
                self.input_width,
                self.input_height,
                self.output_width,
                self.output_height,
                0,
                0,
                int(self.options.mode["perf_quality"]),
                int(native["profile"]),
                int(native["preset"]),
                int(native["style"]),
                int(native["auto_mask"]),
                int(native["ui_correction"]),
                float(native["intensity"]),
                float(native["local_tone"]),
                float(native["local_structure"]),
                float(native["skin_structure"]),
            )
            _write_exact(self._worker.stdin, header)
        except Exception as exc:
            self.abort()
            if isinstance(exc, PreflightError):
                raise
            raise PreflightError(f"DLSS5 feature-18 stream host could not start: {exc}") from exc

    @property
    def worker_logs(self) -> list[str]:
        return self._logs.snapshot()

    def process(self, frame: bytes) -> DlssFrameResult:
        if self.closed or not self._worker:
            raise RuntimeError("DLSS filter is closed")
        expected_input = self.input_width * self.input_height * 4
        if len(frame) != expected_input:
            raise ValueError(f"DLSS input has {len(frame)} bytes; expected {expected_input}")
        started = time.perf_counter()
        try:
            source = self._np.frombuffer(frame, dtype=self._np.uint8).reshape(
                self.input_height, self.input_width, 4)
            if (self._last_completed_at is not None
                    and started - self._last_completed_at > self._history_gap_seconds):
                self.reset_history()
            guide = self._guide.process(source) if self._guide is not None else None
            self._mapping[:expected_input] = frame
            motion_offset = expected_input + self.output_width * self.output_height * 4
            # DNR4 appends render-sized FP16 backward XY motion to the mapping.
            self._mapping[motion_offset:] = memoryview(guide.motion).cast("B") if guide else bytes(expected_input)
            _write_exact(
                self._worker.stdin,
                FRAME_HEADER.pack(FRAME_MAGIC, self.index, int(guide.reset) if guide else 1),
                flush=True,
            )
            magic, index, ok, byte_count = RESULT_HEADER.unpack(
                _read_exact(self._worker.stdout, RESULT_HEADER.size, self._frame_timeout)
            )
            if magic != RESULT_MAGIC or index != self.index:
                raise RuntimeError("DLSS stream host returned a malformed frame header")
            if not ok:
                (length,) = ERROR_LENGTH.unpack(
                    _read_exact(self._worker.stdout, ERROR_LENGTH.size, self._frame_timeout)
                )
                if length > 65535:
                    raise RuntimeError("DLSS stream host returned an invalid error length")
                detail = _read_exact(
                    self._worker.stdout, length, self._frame_timeout
                ).decode("utf-8", errors="replace")
                raise RuntimeError(f"DLSS feature-18 failed on frame {self.index}: {detail}")
            expected_output = self.output_width * self.output_height * 4
            if byte_count != expected_output:
                raise RuntimeError(f"DLSS output has {byte_count} bytes; expected {expected_output}")
            output = self._mapping[expected_input:expected_input + expected_output]
        except Exception as exc:
            details = "\n".join(self.worker_logs[-30:])
            raise RuntimeError(f"DLSS stream exchange failed: {exc}" + (f"\n{details}" if details else "")) from exc

        rgba = self._np.frombuffer(output, dtype=self._np.uint8).reshape(
            self.output_height, self.output_width, 4
        )
        source = self._np.frombuffer(frame, dtype=self._np.uint8).reshape(
            self.input_height, self.input_width, 4
        )
        if self._swap_rb is None:
            self._swap_rb = _should_swap_red_blue(rgba[..., :3], source[..., :3])
        if self._swap_rb:
            rgba = rgba.copy()
            rgba[..., [0, 2]] = rgba[..., [2, 0]]
        if self._stabilizer is not None:
            rgba = self._stabilizer.process(source, rgba)
        self.index += 1
        self._last_completed_at = time.perf_counter()
        self.last_processing_ms = (self._last_completed_at - started) * 1000.0
        return DlssFrameResult(rgba, self.last_processing_ms)

    def reset_history(self):
        self._last_completed_at = None
        if self._guide is not None:
            self._guide.reset()
        if self._stabilizer is not None:
            self._stabilizer.reset()
        self._swap_rb = None

    def close(self) -> dict | None:
        if self.closed:
            return None
        worker = self._worker
        if worker and worker.stdin and not worker.stdin.closed:
            worker.stdin.close()
        if worker and worker.stdout:
            try:
                marker = _read_exact(worker.stdout, 4, 2.0)
                if marker != END_MAGIC:
                    raise RuntimeError("DLSS stream host did not send END3")
            except EOFError:
                pass
        if worker:
            self._terminate_worker(worker, self.launch, timeout=2.0)
        if self._log_thread:
            self._log_thread.join(timeout=3.0)
        self.closed = True
        self._worker = None
        self._close_mapping()
        self._release_worker_slot(self._slot_acquired)
        self._slot_acquired = False
        if self._prefix_lock:
            self._prefix_lock.close()
            self._prefix_lock = None
        return self._verify(self.worker_logs, self.index)

    def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        worker = self._worker
        if worker and hasattr(self, "launch"):
            self._terminate_worker(worker, self.launch, timeout=2.0)
            if self._runtime_owner is None:
                self._stop_proton_prefix(self.launch, timeout=3.0)
        if self._log_thread:
            self._log_thread.join(timeout=3.0)
        self._worker = None
        self._close_mapping()
        if self._slot_acquired:
            self._release_worker_slot(self._slot_acquired)
            self._slot_acquired = False
        if self._prefix_lock:
            self._prefix_lock.close()
            self._prefix_lock = None

    def _close_mapping(self):
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        if self._mapping_path:
            Path(self._mapping_path).unlink(missing_ok=True)
            self._mapping_path = None


class ParallelDlssFilter:
    """One coherent temporal stream, or opt-in independent parallel frames."""

    def __init__(self, config: DlssConfig, input_width: int, input_height: int):
        self.closed = False
        self.workers: list[LiveDlssFilter] = []
        owner = LiveDlssFilter(config, input_width, input_height)
        self.workers.append(owner)
        try:
            for index in range(1, 1 if config.temporal_stability else config.workers):
                self.workers.append(
                    LiveDlssFilter(
                        config,
                        input_width,
                        input_height,
                        worker_index=index,
                        runtime_owner=owner,
                    )
                )
        except Exception:
            self.abort()
            raise
        self.input_width = owner.input_width
        self.input_height = owner.input_height
        self.output_width = owner.output_width
        self.output_height = owner.output_height

    @property
    def worker_count(self) -> int:
        return len(self.workers)

    @property
    def worker_logs(self) -> list[str]:
        return [
            f"worker {index}: {line}"
            for index, worker in enumerate(self.workers)
            for line in worker.worker_logs
        ]

    def process(self, worker_index: int, frame: bytes) -> DlssFrameResult:
        return self.workers[worker_index].process(frame)

    def reset_history(self):
        for worker in self.workers:
            worker.reset_history()

    def close(self) -> list[dict | None]:
        if self.closed:
            return []
        self.closed = True
        reports = []
        # The runtime owner holds the Wine-prefix lock, so close it last.
        for worker in reversed(self.workers):
            reports.append(worker.close())
        return reports

    def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        for worker in reversed(self.workers):
            worker.abort()


class WarmDlssCache:
    """Own at most one idle pool; never lend a worker to concurrent tracks."""
    def __init__(self):
        self.lock = threading.RLock()
        self.idle = None
        self.key = None
        self.busy = False

    @staticmethod
    def signature(config, width, height):
        from dataclasses import asdict
        values = asdict(config)
        for key in ("target_fps", "keep_warm", "fail_closed"):
            values.pop(key)
        return (width, height, tuple(sorted(values.items())))

    def acquire(self, config, width, height):
        key = self.signature(config, width, height)
        with self.lock:
            if self.busy:
                raise PreflightError("DLSS workers are still in use by the previous stream")
            if self.idle is not None:
                pool, self.idle = self.idle, None
                if self.key == key and all(w._worker.poll() is None for w in pool.workers):
                    pool.reset_history()
                    self.busy = True
                    return pool
                pool.abort()
            pool = ParallelDlssFilter(config, width, height)
            self.busy = True
            return pool

    def release(self, config, pool):
        with self.lock:
            if self.idle is not None:
                self.idle.abort()
            self.idle = pool
            self.busy = False
            self.key = self.signature(config, pool.input_width, pool.input_height)

    def discard(self, pool):
        with self.lock:
            pool.abort()
            self.busy = False

    def prewarm(self, config):
        from concurrent.futures import ThreadPoolExecutor
        d = config.dlss
        if not d.keep_warm:
            return
        width = max(64, round(config.stream.width / d.upscaling_factor / 2) * 2)
        height = max(64, round(config.stream.height / d.upscaling_factor / 2) * 2)
        with self.lock:
            if self.busy or (self.idle and self.key == self.signature(d, width, height)):
                return
            pool = self.acquire(d, width, height)
            try:
                frame = bytes((64, 128, 192, 255)) * (width * height)
                with ThreadPoolExecutor(max_workers=pool.worker_count) as executor:
                    futures = [executor.submit(pool.process, i, frame) for i in range(pool.worker_count)]
                    for future in futures:
                        future.result()
                pool.reset_history()  # Synthetic warmup must not become capture history.
                self.release(d, pool)
            except BaseException:
                self.discard(pool)
                raise

    def clear(self):
        with self.lock:
            if self.idle is not None:
                self.idle.abort()
                self.idle = None


WARM_DLSS = WarmDlssCache()
