"""Read-only host capability checks used by the CLI and browser dashboard."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .catalog import GameCatalog
from .config import AppConfig, PROJECT_ROOT, capture_backend
from .launcher import discover_proton, steam_command
from .platform_support import resolve_encoder, system_python


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    label: str
    status: str
    detail: str
    required: bool = True

    def public(self) -> dict:
        return asdict(self)


def _command_ok(command: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = result.stdout.strip().splitlines()
    return result.returncode == 0, (" | ".join(output[-3:]) if output else f"exit {result.returncode}")


def run_checks(config: AppConfig, *, probe_capture: bool = False) -> list[Check]:
    checks: list[Check] = []
    catalog = GameCatalog(config)
    steam_root = catalog.steam_root
    checks.append(
        Check(
            "python",
            "Python 3.11+",
            "ok" if sys.version_info >= (3, 11) else "error",
            sys.version.split()[0],
        )
    )
    missing = [
        package
        for package in ("aiohttp", "aiortc", "av", "numpy", "cryptography", "cv2",
                        *( ("pynput", "vgamepad", "pyaudiowpatch") if sys.platform == "win32" else ("evdev",)))
        if importlib.util.find_spec(package) is None
    ]
    checks.append(
        Check(
            "python_packages",
            "WebRTC runtime",
            "error" if missing else "ok",
            "Missing: " + ", ".join(missing) if missing else "All Python packages available",
        )
    )
    ffmpeg = shutil.which("ffmpeg")
    checks.append(
        Check(
            "ffmpeg",
            "FFmpeg",
            "ok" if ffmpeg else "error",
            ffmpeg or "ffmpeg is not on PATH",
        )
    )
    if ffmpeg:
        try:
            encoder = resolve_encoder(config.stream.encoder)
        except (ValueError, RuntimeError) as exc:
            encoder = "libx264"
            checks.append(Check("encoder_discovery", "H.264 encoder", "error", str(exc)))
        ok, detail = _command_ok(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=size=256x256:rate=1", "-frames:v", "1",
                "-c:v", encoder, "-f", "null", "-",
            ],
            timeout=15,
        )
        checks.append(
            Check(
                "encoder",
                f"H.264 encoder ({config.stream.encoder})",
                "ok" if ok else "error",
                "One-frame encode succeeded" if ok else detail,
            )
        )
    try:
        steam_available = bool(steam_command(config))
    except RuntimeError:
        steam_available = False
    checks.append(
        Check(
            "steam",
            "Steam libraries",
            "ok" if steam_root and steam_available else "warning",
            str(steam_root) if steam_root else "Steam is optional; desktop streaming is available without it.",
            required=False,
        )
    )
    proton = discover_proton(config)
    checks.append(
        Check(
            "proton",
            "Proton runtime",
            "ok" if proton or sys.platform == "win32" else "warning",
            str(proton) if proton else "Only needed for Windows games / DLSS on Linux. Setup can install the DLSS runtime.",
            required=False,
        )
    )
    uinput = Path("/dev/uinput")
    uinput_ok = sys.platform == "win32" or uinput.exists() and os.access(uinput, os.W_OK)
    checks.append(
        Check(
            "uinput",
            "Keyboard, mouse & controller",
            "ok" if uinput_ok or not config.input.enabled else "error",
            (
                "Windows native input (ViGEmBus required for controller)" if sys.platform == "win32" else "/dev/uinput is writable"
                if uinput_ok
                else "Install config/70-game-stream-uinput.rules and load the uinput module"
            ),
            required=config.input.enabled,
        )
    )
    tls_ok = config.server.cert_file.is_file() and config.server.key_file.is_file()
    checks.append(
        Check(
            "tls",
            "HTTPS certificate",
            "ok" if tls_ok else "error",
            "Certificate and private key exist" if tls_ok else "Run: game-stream init",
        )
    )
    node = config.dlss.node_path
    runtime = config.dlss.runtime_dir or node / "runtime"
    dlss_files = [
        runtime / "nvngx_dlss.dll",
        runtime / "nvngx_dlssnr.dll",
        PROJECT_ROOT / "native/bin/dlss5nr_bridge.dll",
        PROJECT_ROOT / "native/bin/dlss5nr_stream_host.exe",
    ]
    dlss_ok = all(path.is_file() for path in dlss_files)
    checks.append(
        Check(
            "dlss",
            "Optional DLSS5 middleman",
            "ok" if dlss_ok else "warning",
            (
                "Optimized RGBA8 bridge host and proprietary runtime found; live validation occurs when enabled"
                if dlss_ok
                else "Optional bridge/runtime is incomplete; standard streaming remains available"
            ),
            required=False,
        )
    )
    try:
        backend = capture_backend(config.stream)
    except ValueError as exc:
        checks.append(Check("capture", "Desktop capture", "error", str(exc)))
        return checks
    if backend in {"pipewire", "gnome"}:
        ok, detail = _command_ok([system_python(), str(Path(__file__).with_name("portal_capture.py")), "--check", "--method", "gnome" if backend == "gnome" else "portal"])
        checks.append(Check("capture", "GNOME unattended / PipeWire" if backend == "gnome" else "Wayland portal / PipeWire", "ok" if ok else "error", detail))
        if probe_capture and ok:
            # Only an explicit CLI probe should open a host sharing dialog.
            import selectors
            import time
            process = subprocess.Popen([
                system_python(), str(Path(__file__).with_name("portal_capture.py")),
                "--width", "640", "--height", "360", "--fps", "30",
                "--method", "gnome" if backend == "gnome" else "portal",
                "--monitor", config.stream.capture_monitor,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, "frame")
            selector.register(process.stderr, selectors.EVENT_READ, "log")
            frame_bytes, logs = 0, []
            deadline = time.monotonic() + 120
            try:
                while frame_bytes < 640 * 360 * 4 and time.monotonic() < deadline:
                    for key, _ in selector.select(1):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                        elif key.data == "frame":
                            frame_bytes += len(data)
                        else:
                            logs.append(data.decode(errors="replace").strip())
                    if process.poll() is not None:
                        break
            finally:
                selector.close()
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()
            ok = frame_bytes >= 640 * 360 * 4
            checks.append(Check("capture_frame", "Wayland desktop frame", "ok" if ok else "error",
                                "PipeWire RGBA frame received" if ok else " | ".join(logs[-5:]) or "No frame received; approve screen sharing on the host"))
        return checks
    if probe_capture and ffmpeg:
        if backend == "gdigrab":
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "gdigrab",
                       "-i", "desktop", "-frames:v", "1", "-f", "null", "-"]
        elif backend == "x11grab":
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "x11grab",
                "-i", config.stream.display,
                "-frames:v", "1", "-f", "null", "-",
            ]
        else:
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-device", "/dev/dri/card0",
                "-f", "kmsgrab", "-i", "-", "-frames:v", "1", "-f", "null", "-",
            ]
        ok, detail = _command_ok(command, timeout=10)
        checks.append(
            Check(
                "capture",
                f"{config.stream.width}x{config.stream.height} capture",
                "ok" if ok else "error",
                "Desktop frame captured" if ok else detail,
            )
        )
    return checks


def ready(checks: list[Check]) -> bool:
    return all(item.status != "error" for item in checks if item.required)
