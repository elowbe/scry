"""Typed TOML configuration with safe, deterministic defaults."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not (PROJECT_ROOT / "web/index.html").is_file():
    PROJECT_ROOT = Path(sys.prefix) / "share/scry"
    user_config = Path(os.environ.get("APPDATA", Path.home())) if sys.platform == "win32" else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    DEFAULT_CONFIG = user_config / "scry/config.toml"
else:
    DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


@dataclass(slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    public_host: str = ""
    cert_file: Path = PROJECT_ROOT / ".state" / "tls.crt"
    key_file: Path = PROJECT_ROOT / ".state" / "tls.key"
    token_file: Path = PROJECT_ROOT / ".state" / "access.token"
    static_dir: Path = PROJECT_ROOT / "web"
    ice_urls: list[str] = field(default_factory=list)
    ice_username: str = ""
    ice_credential: str = ""


@dataclass(slots=True)
class StreamConfig:
    width: int = 2560
    height: int = 1440
    fps: int = 60
    bitrate_mbps: int = 60
    max_bitrate_mbps: int = 90
    rate_control: str = "vbr"
    cq: int = 18
    vbv_ms: int = 100
    keyframe_seconds: int = 1
    spatial_aq: bool = True
    jitter_ms: int = 20
    pacing_ms: int = 4
    display: str = ":0.0"
    capture_backend: str = "auto"
    capture_monitor: str = ""
    audio_source: str = "@DEFAULT_MONITOR@"
    encoder: str = "auto"
    encoder_preset: str = "p4"
    color_range: str = "tv"
    color_space: str = "bt709"
    extra_capture_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SteamConfig:
    root: Path | None = None
    command: str = "steam"
    proton_path: Path | None = None
    require_proton: bool = False
    launch_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DlssConfig:
    node_path: Path = PROJECT_ROOT
    runtime_dir: Path | None = None
    upscaling_factor: float = 1.0
    target_fps: int = 60
    workers: int = 2
    temporal_stability: bool = False
    temporal_method: str = "static_regions"
    neural_before_upscale: bool = False
    nr_style: str = "Natural"
    nr_preset: str = "Default"
    nr_intensity: float = 1.0
    local_tone_strength: float = 1.0
    local_structure_strength: float = 1.25
    skin_structure_strength: float = 2.0
    automatic_mask: bool = True
    keep_warm: bool = True
    fail_closed: bool = True
    frame_timeout_seconds: float = 5.0


@dataclass(slots=True)
class UbisoftGameConfig:
    id: str
    name: str
    executable: Path
    compat_data: Path
    uplay_id: str = ""
    launch_args: list[str] = field(default_factory=list)
    artwork: Path | None = None


@dataclass(slots=True)
class InputConfig:
    enabled: bool = True
    grab_timeout_seconds: int = 15
    mouse_sensitivity: float = 1.0


@dataclass(slots=True)
class AppConfig:
    source: Path
    server: ServerConfig = field(default_factory=ServerConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    steam: SteamConfig = field(default_factory=SteamConfig)
    dlss: DlssConfig = field(default_factory=DlssConfig)
    input: InputConfig = field(default_factory=InputConfig)
    ubisoft_games: list[UbisoftGameConfig] = field(default_factory=list)


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    source = Path(path or os.environ.get("SCRY_CONFIG", os.environ.get("GAMESTREAM_CONFIG", DEFAULT_CONFIG))).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raw = {}

    def _path(value):
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else source.parent / candidate).resolve()

    server_raw = _table(raw, "server")
    stream_raw = _table(raw, "stream")
    steam_raw = _table(raw, "steam")
    dlss_raw = _table(raw, "dlss")
    input_raw = _table(raw, "input")

    server = ServerConfig(
        host=str(server_raw.get("host", "0.0.0.0")),
        port=_bounded_int(server_raw.get("port", 8443), "server.port", 1, 65535),
        public_host=str(server_raw.get("public_host", "")),
        cert_file=_path(server_raw.get("cert_file", source.parent / ".state/tls.crt")),
        key_file=_path(server_raw.get("key_file", source.parent / ".state/tls.key")),
        token_file=_path(server_raw.get("token_file", source.parent / ".state/access.token")),
        static_dir=_path(server_raw.get("static_dir", PROJECT_ROOT / "web")),
        ice_urls=[str(item) for item in server_raw.get("ice_urls", [])],
        ice_username=str(server_raw.get("ice_username", "")),
        ice_credential=str(server_raw.get("ice_credential", "")),
    )
    stream = StreamConfig(
        width=_bounded_int(stream_raw.get("width", 2560), "stream.width", 640, 7680),
        height=_bounded_int(stream_raw.get("height", 1440), "stream.height", 360, 4320),
        fps=_bounded_int(stream_raw.get("fps", 60), "stream.fps", 24, 240),
        bitrate_mbps=_bounded_int(
            stream_raw.get("bitrate_mbps", 60), "stream.bitrate_mbps", 0, 500
        ),
        max_bitrate_mbps=_bounded_int(
            stream_raw.get("max_bitrate_mbps", 90),
            "stream.max_bitrate_mbps",
            0,
            500,
        ),
        display=str(stream_raw.get("display", os.environ.get("DISPLAY", ":0.0"))),
        capture_backend=str(stream_raw.get("capture_backend", "auto")),
        capture_monitor=str(stream_raw.get("capture_monitor", "")),
        audio_source=str(stream_raw.get("audio_source", "@DEFAULT_MONITOR@")),
        encoder=str(stream_raw.get("encoder", "auto")),
        encoder_preset=str(stream_raw.get("encoder_preset", "p4")),
        color_range=str(stream_raw.get("color_range", "tv")),
        color_space=str(stream_raw.get("color_space", "bt709")),
        extra_capture_args=[str(item) for item in stream_raw.get("extra_capture_args", [])],
    )
    if stream.encoder not in {"auto", "h264_nvenc", "libx264"}:
        raise ValueError("stream.encoder must be auto, h264_nvenc or libx264")
    if stream.width % 2 or stream.height % 2:
        raise ValueError("stream dimensions must both be even")
    if stream.max_bitrate_mbps and stream.max_bitrate_mbps < stream.bitrate_mbps:
        raise ValueError("stream.max_bitrate_mbps cannot be lower than bitrate_mbps")
    if stream.capture_backend not in {"auto", "gnome", "pipewire", "x11grab", "kmsgrab", "gdigrab"}:
        raise ValueError("stream.capture_backend must be auto, gnome, pipewire, x11grab or kmsgrab")

    steam_root = steam_raw.get("root")
    proton_path = steam_raw.get("proton_path")
    from .catalog import discover_steam_root
    detected_steam = discover_steam_root(_path(steam_root) if steam_root else None)
    default_steam_command = str(detected_steam / "steam.exe") if sys.platform == "win32" and detected_steam else "steam"
    steam = SteamConfig(
        root=_path(steam_root) if steam_root else None,
        command=str(steam_raw.get("command", default_steam_command)),
        proton_path=_path(proton_path) if proton_path else None,
        require_proton=bool(steam_raw.get("require_proton", False)),
        launch_args=[str(item) for item in steam_raw.get("launch_args", [])],
    )

    runtime_dir = dlss_raw.get("runtime_dir")
    dlss = DlssConfig(
        node_path=_path(
            dlss_raw.get(
                "node_path",
                PROJECT_ROOT,
            )
        ),
        runtime_dir=_path(runtime_dir) if runtime_dir else source.parent / ".state/runtime",
        upscaling_factor=float(dlss_raw.get("upscaling_factor", 1.0)),
        target_fps=_bounded_int(dlss_raw.get("target_fps", min(60, stream.fps)), "dlss.target_fps", 1, 240),
        workers=_bounded_int(dlss_raw.get("workers", 2), "dlss.workers", 1, 4),
        fail_closed=bool(dlss_raw.get("fail_closed", True)),
        frame_timeout_seconds=float(dlss_raw.get("frame_timeout_seconds", 5.0)),
    )
    if dlss.upscaling_factor not in {1.0, 1.5, 1.724, 2.0, 3.0}:
        raise ValueError("dlss.upscaling_factor must be 1, 1.5, 1.724, 2, or 3")
    if not 5.0 <= dlss.frame_timeout_seconds <= 60.0:
        raise ValueError("dlss.frame_timeout_seconds must be between 5 and 60")
    if dlss.target_fps > stream.fps:
        raise ValueError("dlss.target_fps cannot exceed stream.fps")

    # Use the same strict allowlist for file and live controls.
    from .settings import apply_settings, STREAM_FIELDS, DLSS_FIELDS
    validated = apply_settings(AppConfig(source=source, stream=stream, dlss=dlss), {
        "stream": {k: v for k, v in stream_raw.items() if k in STREAM_FIELDS},
        "dlss": {k: v for k, v in dlss_raw.items() if k in DLSS_FIELDS},
    })
    stream, dlss = validated.stream, validated.dlss

    input_config = InputConfig(
        enabled=bool(input_raw.get("enabled", True)),
        grab_timeout_seconds=_bounded_int(
            input_raw.get("grab_timeout_seconds", 15),
            "input.grab_timeout_seconds",
            1,
            120,
        ),
        mouse_sensitivity=float(input_raw.get("mouse_sensitivity", 1.0)),
    )
    if not 0.1 <= input_config.mouse_sensitivity <= 5.0:
        raise ValueError("input.mouse_sensitivity must be between 0.1 and 5.0")

    ubisoft_games: list[UbisoftGameConfig] = []
    for index, game in enumerate(raw.get("ubisoft_games", [])):
        if not isinstance(game, dict):
            raise ValueError(f"ubisoft_games[{index}] must be a table")
        try:
            game_id = str(game["id"])
            name = str(game["name"])
            executable = _path(game["executable"])
            compat_data = _path(game["compat_data"])
        except KeyError as exc:
            raise ValueError(f"ubisoft_games[{index}] is missing {exc.args[0]}") from exc
        artwork = game.get("artwork")
        ubisoft_games.append(
            UbisoftGameConfig(
                id=game_id,
                name=name,
                executable=executable,
                compat_data=compat_data,
                uplay_id=str(game.get("uplay_id", "")),
                launch_args=[str(item) for item in game.get("launch_args", [])],
                artwork=_path(artwork) if artwork else None,
            )
        )

    return AppConfig(
        source=source,
        server=server,
        stream=stream,
        steam=steam,
        dlss=dlss,
        input=input_config,
        ubisoft_games=ubisoft_games,
    )


def capture_backend(config: StreamConfig) -> str:
    if sys.platform == "win32":
        if config.capture_backend not in {"auto", "gdigrab"}:
            raise ValueError("Windows capture_backend must be auto or gdigrab")
        return "gdigrab"
    wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
    if config.capture_backend == "auto":
        if wayland and "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":"):
            return "gnome"
        return "pipewire" if wayland else "x11grab"
    if config.capture_backend == "x11grab" and wayland:
        raise ValueError("x11grab cannot reliably capture a Wayland desktop; set stream.capture_backend = 'auto' or 'pipewire'")
    return config.capture_backend
