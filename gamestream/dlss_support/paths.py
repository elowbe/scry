"""Discovery and validation of the native DLSS 5 runtime.

The runtime binaries are NVIDIA/ReShade/RenoDX components that this repository
neither ships nor redistributes; ``install_runtime.py`` fetches them, or the
user points at an existing DLSS 5 Visual Enhancer installation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PROJECT_ROOT
PACKAGE_ROOT = PROJECT_ROOT
CONFIG_FILE = PACKAGE_ROOT / "config.json"

RUNTIME_ENV = "DLSS5_RUNTIME_DIR"
FFMPEG_ENV = "DLSS5_FFMPEG_DIR"

# The worker keeps NVIDIA's image name because the signed-snippet caller
# contract checks it; it is an executable, not a library.
WORKER_NAME = "nvngx.dll"

REQUIRED_RUNTIME_FILES = (
    WORKER_NAME,
    "dxgi.dll",
    "renodx-dlss5.addon64",
    "nvngx_dlss.dll",
    "nvngx_dlssnr.dll",
)
LINUX_REQUIRED_RUNTIME_FILES = (
    "nvngx_dlss.dll",
    "nvngx_dlssnr.dll",
)

SETUP_HINT = """Run scry-server setup --dlss-dll /path/to/nvngx_dlssnr.dll,
or set dlss.runtime_dir in your Scry configuration to an existing runtime directory."""


class RuntimeMissing(RuntimeError):
    """Raised when the native runtime is absent or incomplete."""


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    """Resolved locations of every binary a render session needs.

    ffmpeg is resolved on first use: the image node never needs it, and failing
    its lookup there would block a perfectly usable install.
    """

    root: Path
    # Excluded from equality so the layout stays hashable and comparable.
    config: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def ffmpeg(self) -> Path:
        return _resolve_ffmpeg(self.config, self.root)[0]

    @property
    def ffprobe(self) -> Path:
        return _resolve_ffmpeg(self.config, self.root)[1]

    @property
    def worker(self) -> Path:
        return self.root / WORKER_NAME

    @property
    def addon(self) -> Path:
        return self.root / "renodx-dlss5.addon64"

    @property
    def neural_runtime(self) -> Path:
        return self.root / "nvngx_dlssnr.dll"

    @property
    def reshade_log(self) -> Path:
        return self.root / "ReShade.log"

    def require_ffmpeg(self) -> "RuntimeLayout":
        """Resolve ffmpeg now, so a missing binary fails before a long render."""
        _resolve_ffmpeg(self.config, self.root)
        return self

    def validate(self, *, platform_name: str | None = None) -> "RuntimeLayout":
        host = sys.platform if platform_name is None else platform_name
        required = (
            LINUX_REQUIRED_RUNTIME_FILES
            if host.startswith("linux") or host == "win32"
            else REQUIRED_RUNTIME_FILES
        )
        missing = [
            str(self.root / name)
            for name in required
            if not (self.root / name).is_file()
        ]
        if missing:
            raise RuntimeMissing(
                "The DLSS 5 runtime in "
                f"{self.root} is incomplete. Missing:\n  "
                + "\n  ".join(missing)
                + "\n\n"
                + SETUP_HINT
            )
        return self


def _read_config() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeMissing(f"{CONFIG_FILE} is not readable JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def write_config(
    runtime_dir: Path,
    ffmpeg_dir: Path | None = None,
    *,
    backend: str | None = None,
    proton_path: Path | None = None,
    proton_compat_data: Path | None = None,
    steam_dir: Path | None = None,
) -> Path:
    """Persist the chosen runtime location for later ComfyUI sessions."""
    payload = {"runtime_dir": str(Path(runtime_dir).resolve())}
    if ffmpeg_dir is not None:
        payload["ffmpeg_dir"] = str(Path(ffmpeg_dir).resolve())
    if backend is not None:
        payload["backend"] = backend
    if proton_path is not None:
        payload["proton_path"] = str(Path(proton_path).resolve())
    if proton_compat_data is not None:
        payload["proton_compat_data"] = str(Path(proton_compat_data).resolve())
    if steam_dir is not None:
        payload["steam_dir"] = str(Path(steam_dir).resolve())
    CONFIG_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return CONFIG_FILE


def _runtime_candidates(config: dict) -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get(RUNTIME_ENV)
    if env_value:
        candidates.append(Path(env_value))
    configured = config.get("runtime_dir")
    if configured:
        candidates.append(Path(configured))
    candidates.append(PACKAGE_ROOT / ".state/runtime")
    return candidates


def _ffmpeg_candidates(config: dict, runtime_root: Path) -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get(FFMPEG_ENV)
    if env_value:
        candidates.append(Path(env_value))
    configured = config.get("ffmpeg_dir")
    if configured:
        candidates.append(Path(configured))
    candidates.append(PACKAGE_ROOT / "ffmpeg" / "bin")
    # Layout of a DLSS 5 Visual Enhancer install: bin/runtime and bin/ffmpeg/bin.
    candidates.append(runtime_root.parent / "ffmpeg" / "bin")
    return candidates


def _resolve_ffmpeg(config: dict, runtime_root: Path) -> tuple[Path, Path]:
    # Video handling stays native on Linux; only the D3D12 worker goes through
    # Proton. Never accidentally try to execute the bundled Windows ffmpeg.
    binary_names = (
        (("ffmpeg.exe", "ffprobe.exe"), ("ffmpeg", "ffprobe"))
        if sys.platform == "win32"
        else (("ffmpeg", "ffprobe"),)
    )
    for directory in _ffmpeg_candidates(config, runtime_root):
        for ffmpeg_name, ffprobe_name in binary_names:
            ffmpeg = directory / ffmpeg_name
            ffprobe = directory / ffprobe_name
            if ffmpeg.is_file() and ffprobe.is_file():
                return ffmpeg, ffprobe
    on_path = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if all(on_path):
        return Path(on_path[0]), Path(on_path[1])
    raise RuntimeMissing(
        "Native ffmpeg/ffprobe were not found. Install them with your operating "
        f"system package manager, set {FFMPEG_ENV} to a directory containing them, "
        "or put them on PATH."
    )


def find_runtime(override: str | os.PathLike[str] | None = None) -> RuntimeLayout:
    """Locate the runtime; ``override`` wins, then env var, config, bundled dir."""
    config = _read_config()
    candidates = [Path(override)] if override else _runtime_candidates(config)

    marker = "nvngx_dlssnr.dll"
    for candidate in candidates:
        root = candidate.expanduser()
        if (root / marker).is_file():
            return RuntimeLayout(root=root.resolve(), config=config).validate()

    searched = "\n  ".join(str(path) for path in candidates)
    raise RuntimeMissing(
        "No DLSS 5 runtime was found. Searched:\n  " + searched + "\n\n" + SETUP_HINT
    )
