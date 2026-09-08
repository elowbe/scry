"""FFmpeg/NVENC capture feeding pre-encoded H.264 packets into WebRTC."""

from __future__ import annotations

import asyncio
import logging
import queue as thread_queue
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from .config import AppConfig, StreamConfig, capture_backend
from .dlss import ParallelDlssFilter, WARM_DLSS, _write_exact
from .errors import PreflightError
from .platform_support import resolve_encoder, system_python
import sys

logger = logging.getLogger(__name__)
VIDEO_STALL_SECONDS = 15

try:
    from aiortc import MediaStreamTrack
    from aiortc.mediastreams import MediaStreamError
except ImportError:  # lets doctor/config/catalog run before optional wheels install
    class MediaStreamTrack:  # type: ignore[no-redef]
        kind = "unknown"

        def __init__(self) -> None:
            pass

        def stop(self) -> None:
            pass

    class MediaStreamError(Exception):  # type: ignore[no-redef]
        pass


def _capture_input(config: StreamConfig, width: int, height: int) -> list[str]:
    if config.capture_backend in {"pipewire", "gnome"}:
        return ["-f", "rawvideo", "-pixel_format", "rgba", "-video_size",
                f"{config.width}x{config.height}", "-framerate", str(config.fps), "-i", "pipe:0"]
    if config.capture_backend == "gdigrab":
        return ["-f", "gdigrab", "-draw_mouse", "1", "-framerate", str(config.fps), "-i", "desktop"]
    if config.capture_backend == "x11grab":
        return [
            "-thread_queue_size", "2",
            "-f", "x11grab",
            "-draw_mouse", "1",
            "-framerate", str(config.fps),
            "-i", config.display,
        ]
    if config.capture_backend == "kmsgrab":
        return [
            "-device", "/dev/dri/card0",
            "-f", "kmsgrab",
            "-framerate", str(config.fps),
            "-i", "-",
        ]
    raise ValueError(f"Unsupported capture backend {config.capture_backend!r}")


def _scale_filter(config: StreamConfig, width: int, height: int, pixel_format: str) -> str:
    if config.capture_backend == "kmsgrab":
        return (
            f"hwmap=derive_device=cuda,scale_cuda={width}:{height}:format={pixel_format}"
        )
    return (
        f"scale={width}:{height}:flags=fast_bilinear:in_range=full:out_range={config.color_range}:"
        f"out_color_matrix={config.color_space},format={pixel_format}"
    )


def rate_control_args(config: StreamConfig) -> list[str]:
    args = ["-rc", config.rate_control, "-b:v", f"{config.bitrate_mbps}M"]
    if config.rate_control == "vbr":
        args += ["-cq", str(config.cq)]
    cap = config.max_bitrate_mbps
    if cap:
        args += ["-maxrate", f"{cap}M", "-bufsize",
                 str(round(cap * 1_000_000 * config.vbv_ms / 1000))]
    else:
        args += ["-maxrate", "0", "-bufsize", "0"]
    return args


def _encoder_command(config, command):
    encoder = resolve_encoder(config.encoder)
    command[command.index("-c:v") + 1] = encoder
    if encoder == "h264_nvenc":
        return command
    remove = {"-rc", "-cq", "-rc-lookahead", "-spatial-aq", "-aq-strength",
              "-zerolatency", "-delay", "-forced-idr"}
    result = []
    index = 0
    while index < len(command):
        flag = command[index]
        if flag in remove:
            index += 2
            continue
        if flag in {"-preset", "-tune"}:
            result.extend([flag, "veryfast" if flag == "-preset" else "zerolatency"])
            index += 2
            continue
        result.append(flag)
        index += 1
    if config.rate_control == "vbr":
        result[-3:-3] = ["-crf", str(config.cq)]
    return result


def build_encoded_capture_command(config: StreamConfig) -> list[str]:
    config = replace(config, capture_backend=capture_backend(config))
    return _encoder_command(config, [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "32", "-analyzeduration", "0",
        *_capture_input(config, config.width, config.height),
        *config.extra_capture_args,
        "-an",
        "-vf", _scale_filter(config, config.width, config.height, "nv12"),
        "-c:v", config.encoder,
        "-preset", config.encoder_preset,
        "-tune", "ull",
        *rate_control_args(config),
        "-g", str(config.fps * config.keyframe_seconds),
        "-keyint_min", str(config.fps * config.keyframe_seconds),
        "-bf", "0",
        "-rc-lookahead", "0",
        "-spatial-aq", str(int(config.spatial_aq)),
        "-aq-strength", "8",
        "-zerolatency", "1",
        "-delay", "0",
        "-forced-idr", "1",
        "-profile:v", "baseline",
        "-level:v", "5.1",
        "-color_range", config.color_range,
        "-colorspace", config.color_space,
        "-color_primaries", config.color_space,
        "-color_trc", config.color_space,
        "-flush_packets", "1",
        "-f", "h264",
        "pipe:1",
    ])


def build_raw_capture_command(config: StreamConfig, width: int, height: int) -> list[str]:
    config = replace(config, capture_backend=capture_backend(config))
    if config.capture_backend in {"pipewire", "gnome"}:
        return [system_python(), str(Path(__file__).with_name("portal_capture.py")),
                "--width", str(width), "--height", str(height), "--fps", str(config.fps),
                "--method", "gnome" if config.capture_backend == "gnome" else "portal",
                "--monitor", config.capture_monitor]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "32", "-analyzeduration", "0",
        *_capture_input(config, width, height),
        *config.extra_capture_args,
        "-an",
        "-vf", _scale_filter(config, width, height, "rgba"),
        "-pix_fmt", "rgba",
        "-f", "rawvideo",
        "pipe:1",
    ]


def build_raw_encoder_command(config: StreamConfig, width: int, height: int) -> list[str]:
    # DLSS and standard video share the user's encoder quality controls.
    return _encoder_command(config, [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "32", "-analyzeduration", "0",
        "-f", "rawvideo", "-pixel_format", "rgba",
        "-video_size", f"{width}x{height}", "-framerate", str(config.fps),
        "-i", "pipe:0",
        "-an",
        "-vf", _scale_filter(config, config.width, config.height, "nv12"),
        "-c:v", config.encoder, "-preset", config.encoder_preset, "-tune", "ull",
        *rate_control_args(config),
        "-g", str(config.fps * config.keyframe_seconds), "-keyint_min", str(config.fps * config.keyframe_seconds), "-bf", "0",
        "-rc-lookahead", "0", "-spatial-aq", str(int(config.spatial_aq)),
        "-zerolatency", "1", "-delay", "0", "-forced-idr", "1",
        "-profile:v", "baseline", "-level:v", "5.1",
        "-color_range", config.color_range, "-colorspace", config.color_space,
        "-color_primaries", config.color_space, "-color_trc", config.color_space,
        "-flush_packets", "1",
        "-f", "h264", "pipe:1",
    ])


def _read_exact(handle, size: int) -> bytes:
    data = bytearray(size)
    view = memoryview(data)
    offset = 0
    while offset < size:
        chunk = handle.readinto(view[offset:])
        if not chunk:
            raise EOFError(f"capture ended after {offset} of {size} frame bytes")
        offset += chunk
    return bytes(data)


@dataclass(slots=True)
class PipelineStats:
    dlss_enabled: bool = False
    output_fps: int = 60
    dlss_processing_ms: float = 0.0
    frames_filtered: int = 0
    last_error: str = ""


class EncodedVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, app_config: AppConfig, dlss_enabled: bool, *, start: bool = True):
        super().__init__()
        self.app_config = app_config
        self.config = replace(app_config.stream, capture_backend=capture_backend(app_config.stream))
        self.dlss_enabled = dlss_enabled
        self.output_fps = (
            min(app_config.dlss.target_fps, self.config.fps)
            if dlss_enabled
            else self.config.fps
        )
        self.stats = PipelineStats(dlss_enabled=dlss_enabled, output_fps=self.output_fps)
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._ready = asyncio.Event()
        self._logs = deque(maxlen=20)
        self._threads: list[threading.Thread] = []
        self.capture: subprocess.Popen | None = None
        self.encoder: subprocess.Popen | None = None
        self.filter: ParallelDlssFilter | None = None
        try:
            if start:
                self._start()
        except Exception:
            self.stop()
            raise

    @classmethod
    async def create(cls, config: AppConfig, dlss_enabled: bool):
        track = cls(config, dlss_enabled, start=False)
        task = asyncio.create_task(asyncio.to_thread(track._start))
        try:
            await asyncio.shield(task)
            await track.wait_ready()
            return track
        except BaseException:
            # _start may still be opening Proton. Let it finish so cleanup
            # cannot race a process spawned after cancellation.
            try:
                await task
            except Exception:
                pass
            track.stop()
            raise

    @staticmethod
    def _spawn(command: list[str], *, stdin=None) -> subprocess.Popen:
        try:
            return subprocess.Popen(
                command,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            raise PreflightError(f"Could not start {command[0]}: {exc}") from exc

    def _start(self) -> None:
        if self.dlss_enabled:
            if self.config.capture_backend == "kmsgrab":
                raise PreflightError(
                    "The DLSS5 CPU-frame bridge requires x11grab or PipeWire; "
                    "use standard streaming with kmsgrab"
                )
            factor = self.app_config.dlss.upscaling_factor
            input_width = max(64, round(self.config.width / factor / 2) * 2)
            input_height = max(64, round(self.config.height / factor / 2) * 2)
            self.filter = WARM_DLSS.acquire(self.app_config.dlss, input_width, input_height)
            dlss_stream = replace(self.config, fps=self.output_fps)
            self.encoder = self._spawn(
                build_raw_encoder_command(
                    dlss_stream, self.filter.output_width, self.filter.output_height
                ),
                stdin=subprocess.PIPE,
            )
            self.capture = self._spawn(
                build_raw_capture_command(dlss_stream, input_width, input_height)
            )
            self._thread("dlss-filter", self._filter_loop)
            output = self.encoder.stdout
            self._thread("capture-log", self._log_loop, self.capture.stderr, "capture")
            self._thread("encoder-log", self._log_loop, self.encoder.stderr, "encoder")
        elif self.config.capture_backend in {"pipewire", "gnome"}:
            self.capture = self._spawn(build_raw_capture_command(self.config, self.config.width, self.config.height))
            self._thread("capture-log", self._log_loop, self.capture.stderr, "portal")
            self.encoder = self._spawn(build_encoded_capture_command(self.config), stdin=self.capture.stdout)
            self.capture.stdout.close()
            output = self.encoder.stdout
            self._thread("encoder-log", self._log_loop, self.encoder.stderr, "encoder")
        else:
            self.capture = self._spawn(build_encoded_capture_command(self.config))
            output = self.capture.stdout
            self._thread("capture-log", self._log_loop, self.capture.stderr, "capture")
        self._thread("h264-demux", self._demux_loop, output)

    def _thread(self, name: str, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _log_loop(self, handle, label: str) -> None:
        if not handle:
            return
        for raw in iter(handle.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._logs.append(f"{label}: {line}")
                logger.warning("media %s: %s", label, line)
                if "error" in line.casefold() or "failed" in line.casefold():
                    self.stats.last_error = line[-500:]

    def _filter_loop(self) -> None:
        assert self.capture and self.capture.stdout and self.encoder and self.encoder.stdin and self.filter
        size = self.filter.input_width * self.filter.input_height * 4
        executors = [ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"dlss-{index}")
                     for index in range(self.filter.worker_count)]
        pending = deque()
        next_worker = 0
        encoded = thread_queue.Queue(maxsize=1)
        writer_done = threading.Event()
        def feed_encoder():
            try:
                while not self._stopped.is_set():
                    try:
                        frame = encoded.get(timeout=.1)
                    except thread_queue.Empty:
                        if writer_done.is_set():
                            break
                        continue
                    _write_exact(self.encoder.stdin, frame)
            except (BrokenPipeError, OSError) as exc:
                if not self._stopped.is_set():
                    self.stats.last_error = f"DLSS encoder input failed: {exc}"
            finally:
                writer_done.set()
        writer = threading.Thread(target=feed_encoder, name="dlss-encoder-feed", daemon=True)
        writer.start()
        try:
            while not self._stopped.is_set():
                raw = _read_exact(self.capture.stdout, size)
                future = executors[next_worker].submit(self.filter.process, next_worker, raw)
                pending.append(future)
                next_worker = (next_worker + 1) % self.filter.worker_count
                if len(pending) < self.filter.worker_count:
                    continue
                result = pending.popleft().result()
                self.stats.dlss_processing_ms = result.processing_ms
                self.stats.frames_filtered += 1
                while not self._stopped.is_set():
                    if writer_done.is_set():
                        raise RuntimeError("DLSS encoder stopped accepting frames")
                    try:
                        encoded.put(result.rgba, timeout=.1)
                        break
                    except thread_queue.Full:
                        continue
        except (BrokenPipeError, EOFError, OSError, RuntimeError) as exc:
            if not self._stopped.is_set():
                self.stats.last_error = f"DLSS pipeline stopped: {exc}"
                logger.exception("DLSS frame pipeline stopped")
        finally:
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=True)
            writer_done.set()
            writer.join(timeout=2)
            try:
                self.encoder.stdin.close()
            except OSError:
                pass

    def _demux_loop(self, output) -> None:
        container = None
        try:
            import av

            container = av.open(
                output,
                mode="r",
                format="h264",
                options={
                    # Retain the SPS/PPS and first IDR read during probing.
                    # nobuffer discards them, leaving a fresh decoder unable
                    # to decode until another complete keyframe arrives.
                    "flags": "low_delay",
                    "probesize": "2048",
                    "analyzeduration": "0",
                    "fpsprobesize": "0",
                },
            )
            index = 0
            for packet in container.demux(video=0):
                if self._stopped.is_set():
                    break
                if not packet.size:
                    continue
                packet.pts = index
                packet.dts = index
                packet.time_base = Fraction(1, self.output_fps)
                index += 1
                self.loop.call_soon_threadsafe(self._ready.set)
                pending = asyncio.run_coroutine_threadsafe(self.queue.put(packet), self.loop)
                while not self._stopped.is_set():
                    try:
                        pending.result(timeout=0.5)
                        break
                    except FutureTimeout:
                        continue
                if self._stopped.is_set() and not pending.done():
                    pending.cancel()
                    break
        except Exception as exc:
            if not self._stopped.is_set():
                self.stats.last_error = f"H.264 stream stopped: {exc}"
                logger.exception("H.264 demux stopped")
        finally:
            if container is not None:
                container.close()
            def finish() -> None:
                try:
                    self.queue.put_nowait(None)
                except asyncio.QueueFull:
                    try:
                        self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self.queue.put_nowait(None)
            if not self._stopped.is_set() and not self.stats.last_error:
                self.stats.last_error = "Video capture ended before the stream was stopped"
            self.loop.call_soon_threadsafe(self._ready.set)
            self.loop.call_soon_threadsafe(finish)

    async def wait_ready(self, timeout: float = 120) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise PreflightError("Capture startup timed out. Select a monitor in the host screen-sharing dialog. " + " | ".join(self._logs)) from exc
        if self.stats.last_error:
            raise PreflightError(self.stats.last_error + " | " + " | ".join(self._logs))

    async def recv(self):
        try:
            packet = await asyncio.wait_for(self.queue.get(), VIDEO_STALL_SECONDS)
        except asyncio.TimeoutError as exc:
            self.stats.last_error = (
                f"Video stalled: no encoded frame received for {VIDEO_STALL_SECONDS} seconds. "
                + " | ".join(self._logs)[-2000:]
            )
            logger.error(self.stats.last_error)
            await asyncio.to_thread(self.stop)
            raise MediaStreamError from exc
        if packet is None:
            raise MediaStreamError
        return packet

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        processes = [p for p in (self.capture, self.encoder) if p]
        for process in processes:
            if process.poll() is None:
                process.terminate()
        def reap() -> None:
            for process in processes:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        threading.Thread(target=reap, name="media-cleanup", daemon=True).start()
        def finish() -> None:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.loop.call_soon_threadsafe(finish)
        if self.filter:
            try:
                # Finish all in-flight exchanges before caching their workers.
                # Only the filter thread waits on executor futures.
                filter_thread = next((t for t in self._threads if t.name == "dlss-filter"), None)
                if filter_thread and filter_thread is not threading.current_thread():
                    filter_thread.join(timeout=self.app_config.dlss.frame_timeout_seconds + 1)
                if (self.app_config.dlss.keep_warm and not self.stats.last_error
                        and filter_thread and not filter_thread.is_alive()):
                    WARM_DLSS.release(self.app_config.dlss, self.filter)
                else:
                    WARM_DLSS.discard(self.filter)
                self.filter = None
            except Exception:
                logger.exception("DLSS worker cleanup failed")
        super().stop()


class SwitchableVideoTrack(MediaStreamTrack):
    """One RTP sender across quality/filter changes, with continuous timestamps."""
    kind = "video"

    def __init__(self, source, recover=None):
        super().__init__()
        self.source = source
        self.available = asyncio.Event()
        self.available.set()
        self.closed = False
        self.started = time.monotonic()
        self.last_pts = -1
        self.recover = recover
        self._recovery_task = None

    def replace(self, source):
        previous, self.source = self.source, source
        self.available.set()
        return previous

    def suspend(self):
        previous, self.source = self.source, None
        self.available.clear()
        return previous

    async def recv(self):
        while not self.closed:
            await self.available.wait()
            if self.closed:
                break
            source = self.source
            try:
                packet = await source.recv()
            except MediaStreamError:
                if source is not self.source:
                    continue
                if self.recover and not self.closed:
                    self._recovery_task = asyncio.create_task(self.recover(source))
                    try:
                        await self._recovery_task
                    except asyncio.CancelledError:
                        if not self.closed:
                            raise
                    finally:
                        self._recovery_task = None
                    continue
                raise
            if source is not self.source:
                continue
            # Wall-clock cadence prevents a slow capture/filter from building
            # an ever-increasing playout delay against synthetic 60 fps PTS.
            self.last_pts = max(self.last_pts + 1, round((time.monotonic() - self.started) * 90000))
            packet.pts = packet.dts = self.last_pts
            packet.time_base = Fraction(1, 90000)
            return packet
        raise MediaStreamError

    def stop(self):
        self.closed = True
        self.available.set()
        if self._recovery_task:
            self._recovery_task.cancel()
        if self.source:
            self.source.stop()
        super().stop()


class PcmAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, config: StreamConfig):
        super().__init__()
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-fflags", "nobuffer", "-probesize", "32", "-analyzeduration", "0",
            "-f", "pulse", "-fragment_size", "3840", "-thread_queue_size", "1",
            "-i", config.audio_source, "-vn", "-ac", "2", "-ar", "48000",
            "-sample_fmt", "s16", "-f", "s16le", "pipe:1",
        ]
        if sys.platform == "win32":
            command = [sys.executable, "-m", "gamestream.windows_audio", "--source", config.audio_source]
        try:
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as exc:
            raise PreflightError(f"Could not start audio capture: {exc}") from exc
        self.last_error = ""
        self._audio_stopped = False
        self._log_thread = threading.Thread(target=self._log_audio, name="audio-log", daemon=True)
        self._log_thread.start()
        self.pts = 0
        self.loop = asyncio.get_running_loop()

    def _log_audio(self) -> None:
        for raw in iter(self.process.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line:
                self.last_error = line[-500:]
                logger.warning("audio capture: %s", line)

    async def recv(self):
        import av

        assert self.process.stdout
        try:
            data = await self.loop.run_in_executor(None, _read_exact, self.process.stdout, 960 * 2 * 2)
        except EOFError as exc:
            self.last_error = "Audio capture ended: " + self.last_error
            logger.error(self.last_error)
            raise MediaStreamError from exc
        frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
        frame.planes[0].update(data)
        frame.sample_rate = 48000
        frame.time_base = Fraction(1, 48000)
        frame.pts = self.pts
        self.pts += 960
        return frame

    def stop(self) -> None:
        if self._audio_stopped:
            return
        self._audio_stopped = True
        if self.process.poll() is None:
            self.process.terminate()
        def reap():
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        threading.Thread(target=reap, name="audio-cleanup", daemon=True).start()
        super().stop()


class StreamPipeline:
    def __init__(self, config: AppConfig, dlss_enabled: bool):
        try:
            self.video = EncodedVideoTrack(config, dlss_enabled)
        except Exception:
            if not dlss_enabled or config.dlss.fail_closed:
                raise
            logger.exception("DLSS unavailable; fail_closed is false, using the standard path")
            self.video = EncodedVideoTrack(config, False)
        try:
            self.audio = PcmAudioTrack(config.stream)
        except PreflightError as exc:
            logger.warning("Audio capture unavailable: %s", exc)
            self.audio = None

    @classmethod
    async def create(cls, config: AppConfig, dlss_enabled: bool):
        pipeline = cls.__new__(cls)
        pipeline.video = await EncodedVideoTrack.create(config, dlss_enabled)
        try:
            pipeline.audio = PcmAudioTrack(config.stream)
        except PreflightError:
            logger.exception("Audio capture unavailable")
            pipeline.audio = None
        return pipeline

    @property
    def stats(self) -> dict:
        raw = self.video.stats
        return {
            "dlss_enabled": raw.dlss_enabled,
            "output_fps": raw.output_fps,
            "dlss_processing_ms": round(raw.dlss_processing_ms, 2),
            "frames_filtered": raw.frames_filtered,
            "last_error": (raw.last_error + " | " + " | ".join(self.video._logs)[-2000:]) if raw.last_error and self.video._logs else raw.last_error,
        }

    def close(self) -> None:
        self.video.stop()
        if self.audio:
            self.audio.stop()
