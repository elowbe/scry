"""Platform-specific launch support for the DLSS workers.

The protocol client is native Python on every platform. Windows starts the
upstream worker directly. Linux runs the bundled open-source direct bridge in
an isolated Proton prefix with VKD3D-Proton and DXVK-NVAPI enabled.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .paths import PACKAGE_ROOT, RuntimeLayout

BACKEND_ENV = "DLSS5_BACKEND"
PROTON_ENV = "DLSS5_PROTON_PATH"
COMPAT_DATA_ENV = "DLSS5_PROTON_COMPAT_DATA"
STEAM_ENV = "DLSS5_STEAM_DIR"

BACKENDS = ("auto", "native", "proton")
DEFAULT_COMPAT_DATA = PACKAGE_ROOT / "proton_compat_data"
REQUIRED_DLL_OVERRIDES = (
    "d3d12",
    "d3d12core",
    "nvapi64",
    "dxgi",
)
LINUX_BRIDGE_ROOT = PACKAGE_ROOT / "native"
LINUX_HOST = LINUX_BRIDGE_ROOT / "bin" / "dlss5nr_stream_host.exe"
LINUX_BRIDGE = LINUX_BRIDGE_ROOT / "bin" / "dlss5nr_bridge.dll"
LINUX_CALLER_SHIM = LINUX_BRIDGE_ROOT / "bin" / "nvngx.dll_comfy.dll"

_bootstrap_lock = threading.Lock()
_bootstrapped_prefixes: set[tuple[str, str]] = set()
_worker_slot = threading.Lock()


@dataclass(frozen=True, slots=True)
class WorkerLaunch:
    """Everything :class:`subprocess.Popen` needs to start the worker."""

    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str] | None
    backend: str
    start_new_session: bool = False

    @property
    def description(self) -> str:
        if self.backend == "proton":
            return f"Proton ({self.command[0]})"
        return "native Windows"


def _configured(config: Mapping[str, object], env: Mapping[str, str], key: str, name: str) -> str:
    value = env.get(name) or config.get(key) or ""
    return str(value).strip()


def _steam_roots(env: Mapping[str, str]) -> list[Path]:
    roots: list[Path] = []
    explicit = env.get(STEAM_ENV)
    if explicit:
        roots.append(Path(explicit).expanduser())

    home = Path(env.get("HOME") or Path.home())
    xdg_data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    roots.extend(
        [
            xdg_data / "Steam",
            home / ".steam" / "root",
            home / ".steam" / "steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
            home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
        ]
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        marker = str(root)
        if marker not in seen:
            seen.add(marker)
            unique.append(root)
    return unique


def _proton_candidates(env: Mapping[str, str]) -> list[Path]:
    candidates: list[Path] = []
    for steam in _steam_roots(env):
        common = steam / "steamapps" / "common"
        # Experimental is the best default for a brand-new NGX feature.
        candidates.append(common / "Proton - Experimental" / "proton")
        candidates.extend(sorted(common.glob("Proton */proton"), key=_natural_key, reverse=True))
        candidates.extend(
            sorted(
                (steam / "compatibilitytools.d").glob("*/proton"),
                key=_natural_key,
                reverse=True,
            )
        )
    on_path = shutil.which("proton", path=env.get("PATH"))
    if on_path:
        candidates.append(Path(on_path))
    return candidates


def _natural_key(path: Path) -> tuple[tuple[int, object], ...]:
    """Order Proton 10 after Proton 9 while still handling custom tool names."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", str(path))
    )


def _as_proton_script(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded / "proton" if expanded.is_dir() else expanded


def find_proton(
    config: Mapping[str, object], env: Mapping[str, str] | None = None
) -> Path:
    """Resolve Proton from an override, config, PATH, or common Steam installs."""
    environment = os.environ if env is None else env
    explicit = _configured(config, environment, "proton_path", PROTON_ENV)
    if explicit:
        proton = _as_proton_script(Path(explicit))
        if proton.is_file() and os.access(proton, os.X_OK):
            return proton.resolve()
        raise RuntimeError(
            f"Configured Proton launcher does not exist or is not executable: {proton}. "
            f"Set {PROTON_ENV} to Proton's `proton` script (Proton 9 or newer is "
            "recommended)."
        )

    for candidate in _proton_candidates(environment):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise RuntimeError(
        "Proton was not found. Install Proton 9 or newer through Steam, then run "
        f"install_runtime.py --proton-path /path/to/Proton/proton, or set {PROTON_ENV}."
    )


def find_steam_root(
    config: Mapping[str, object], env: Mapping[str, str] | None = None
) -> Path | None:
    """Find the Steam client root used by Proton when one is available."""
    environment = os.environ if env is None else env
    explicit = _configured(config, environment, "steam_dir", STEAM_ENV)
    if explicit:
        root = Path(explicit).expanduser()
        if root.is_dir():
            return root.resolve()
        raise RuntimeError(f"Configured Steam directory does not exist: {root}")
    for root in _steam_roots(environment):
        if root.is_dir():
            return root.resolve()
    return None


def _dll_overrides(current: str) -> str:
    """Force Proton's D3D12/DXGI/NVAPI path and preserve other overrides."""
    required = set(REQUIRED_DLL_OVERRIDES)
    entries: list[str] = []
    for raw_entry in current.split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            if entry:
                entries.append(entry)
            continue
        names, mode = entry.split("=", 1)
        remaining = [
            name.strip()
            for name in names.split(",")
            if name.strip() and name.strip().lower() not in required
        ]
        if remaining:
            entries.append(f"{','.join(remaining)}={mode}")
    entries.append(f"{','.join(REQUIRED_DLL_OVERRIDES)}=n,b")
    return ";".join(entries)


def resolve_backend(
    config: Mapping[str, object],
    env: Mapping[str, str] | None = None,
    *,
    platform_name: str | None = None,
) -> str:
    environment = os.environ if env is None else env
    requested = _configured(config, environment, "backend", BACKEND_ENV).lower() or "auto"
    if requested not in BACKENDS:
        raise RuntimeError(
            f"Unknown DLSS5 backend {requested!r}; choose {', '.join(BACKENDS)}."
        )
    host = sys.platform if platform_name is None else platform_name
    if requested == "auto":
        if host == "win32":
            return "native"
        if host.startswith("linux"):
            return "proton"
        raise RuntimeError(f"DLSS5 does not support host platform {host!r}.")
    if requested == "native" and host != "win32":
        raise RuntimeError("The native backend can only run the D3D12 worker on Windows.")
    if requested == "proton" and not host.startswith("linux"):
        raise RuntimeError("The Proton backend is only supported on Linux.")
    return requested


def build_worker_launch(
    layout: RuntimeLayout,
    *,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    machine: str | None = None,
) -> WorkerLaunch:
    """Build a direct-Windows or Proton launch specification for ``layout``."""
    environment = dict(os.environ if env is None else env)
    host = sys.platform if platform_name is None else platform_name
    backend = resolve_backend(layout.config, environment, platform_name=host)
    if backend == "native":
        return WorkerLaunch(
            command=(str(layout.worker), "--video"),
            cwd=layout.root,
            env=None,
            backend=backend,
        )

    architecture = (machine or platform.machine()).lower()
    if architecture not in {"x86_64", "amd64"}:
        raise RuntimeError(
            f"The Proton DLSS5 backend requires x86_64 Linux, not {architecture or 'unknown'}."
        )

    missing_bridge = [
        str(path)
        for path in (LINUX_HOST, LINUX_BRIDGE, LINUX_CALLER_SHIM)
        if not path.is_file()
    ]
    if missing_bridge:
        raise RuntimeError(
            "The bundled Linux DLSS bridge is incomplete. Reinstall this custom "
            "node; missing:\n  " + "\n  ".join(missing_bridge)
        )

    proton = find_proton(layout.config, environment)
    compat_setting = _configured(
        layout.config, environment, "proton_compat_data", COMPAT_DATA_ENV
    )
    compat_data = Path(compat_setting).expanduser() if compat_setting else DEFAULT_COMPAT_DATA
    compat_data = compat_data.resolve()
    compat_data.mkdir(parents=True, exist_ok=True)

    launch_env = environment
    launch_env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    steam = find_steam_root(layout.config, launch_env)
    if steam is None:
        # Proton requires this variable even for a standalone worker with no Steam APIs.
        # Supply an isolated empty client directory; never modify a real game prefix.
        steam = compat_data / "steam-client"
        steam.mkdir(parents=True, exist_ok=True)
    launch_env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(steam))

    # Proton 9+ includes and enables DXVK-NVAPI. Do not use PROTON_FORCE_NVAPI:
    # that option reports a synthetic 999.99 driver and is meant for non-NVIDIA
    # adapters. This worker needs the real NVIDIA identity and driver version.
    launch_env.pop("PROTON_FORCE_NVAPI", None)
    launch_env.pop("PROTON_DISABLE_NVAPI", None)
    launch_env["PROTON_HIDE_NVIDIA_GPU"] = "0"
    launch_env["DXVK_ENABLE_NVAPI"] = "1"
    launch_env.setdefault("DXVK_NVAPI_DRS_NGX_DLSS_NR_OVERRIDE", "on")
    # The worker ships its matching NGX DLLs locally. Proton's optional updater
    # writes status text to stdout, which would corrupt this worker's binary
    # protocol before its setup response.
    launch_env["PROTON_ENABLE_NGX_UPDATER"] = "0"
    launch_env.setdefault("DLSS5NR_DISABLE_OTHER_SINKS", "1")
    launch_env.setdefault("DLSS5NR_GPU_INDEX", "0")
    launch_env["DLSS5NR_CALLER_SHIM"] = str(LINUX_CALLER_SHIM)
    launch_env.setdefault("WINEDEBUG", "-all")
    launch_env["WINEDLLOVERRIDES"] = _dll_overrides(
        launch_env.get("WINEDLLOVERRIDES", "")
    )

    return WorkerLaunch(
        # `runinprefix` invokes Wine directly and preserves the binary
        # stdin/stdout protocol. The project-owned host loads the direct bridge
        # beside it, while NVIDIA's runtime remains in layout.root.
        command=(str(proton), "runinprefix", str(LINUX_HOST), str(layout.root)),
        cwd=LINUX_HOST.parent,
        env=launch_env,
        backend=backend,
        # Terminating Proton alone can leave its Wine child alive with the GPU
        # and pipe handles open. A new process group makes cancellation reliable.
        start_new_session=True,
    )


def _current_prefix_version(proton: Path) -> str | None:
    """Read Proton's own compatibility-prefix version without launching it."""
    try:
        source = proton.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^CURRENT_PREFIX_VERSION=["\']([^"\']+)["\']', source, re.MULTILINE)
    return match.group(1) if match else None


def _prefix_is_ready(proton: Path, compat_data: Path) -> bool:
    """Use Proton's durable version marker instead of per-process memory alone."""
    expected = _current_prefix_version(proton)
    try:
        actual = (compat_data / "version").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    prefix = compat_data / "pfx"
    return bool(
        expected
        and actual == expected
        and (prefix / "system.reg").is_file()
        and (prefix / "drive_c").is_dir()
    )


def _wineserver_path(proton: Path) -> Path | None:
    for candidate in (
        proton.parent / "files" / "bin" / "wineserver",
        proton.parent / "dist" / "bin" / "wineserver",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def stop_proton_prefix(launch: WorkerLaunch, timeout: float = 10.0) -> bool:
    """Stop every Wine process in this node's dedicated prefix, and no other prefix."""
    if launch.backend != "proton" or launch.env is None:
        return False
    server = _wineserver_path(Path(launch.command[0]))
    if server is None:
        return False
    environment = dict(launch.env)
    environment["WINEPREFIX"] = str(
        Path(environment["STEAM_COMPAT_DATA_PATH"]) / "pfx"
    )
    deadline = time.monotonic() + timeout
    for verb in ("-k", "-w"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            result = subprocess.run(
                (str(server), verb),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                timeout=remaining,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode:
            return False
    return True


def acquire_worker_slot(launch: WorkerLaunch) -> bool:
    """Serialize Linux sessions that share one dedicated Wine prefix/GPU."""
    if launch.backend != "proton":
        return False
    _worker_slot.acquire()
    return True


def release_worker_slot(acquired: bool) -> None:
    if acquired:
        _worker_slot.release()


def _terminate_process(process, *, process_group: bool, timeout: float) -> None:
    """Terminate a process and, when requested, every descendant in its group."""
    group_id = process.pid if process_group else None
    if process.poll() is None:
        try:
            if group_id is not None:
                os.killpg(group_id, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=timeout)
        except Exception:
            pass

    if group_id is not None:
        try:
            # The Proton leader can exit before a detached Wine child. Check the
            # group itself instead of trusting only Popen.poll().
            os.killpg(group_id, 0)
        except (OSError, ProcessLookupError):
            return
        try:
            os.killpg(group_id, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return
    elif process.poll() is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            return
    try:
        process.wait(timeout=timeout)
    except Exception:
        pass


def _bootstrap_once(launch: WorkerLaunch, timeout: float) -> tuple[int, str]:
    """Run prefix maintenance without leaving pipe-owning descendants on timeout."""
    assert launch.env is not None
    compat_data = launch.env["STEAM_COMPAT_DATA_PATH"]
    with tempfile.TemporaryFile() as diagnostics:
        process = subprocess.Popen(
            (launch.command[0], "run", "cmd.exe", "/d", "/c", "exit"),
            cwd=compat_data,
            env=launch.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=diagnostics,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process(process, process_group=True, timeout=5.0)
            raise
        diagnostics.seek(0)
        detail = diagnostics.read().decode("utf-8", errors="replace").strip()
    return code, detail


def prepare_worker_launch(launch: WorkerLaunch, timeout: float = 120.0) -> None:
    """Clean the dedicated prefix and create/update it only when necessary.

    Proton deliberately skips prefix maintenance for the ``runinprefix`` verb.
    Proton's own durable version marker lets a restarted ComfyUI process avoid a
    redundant ``run`` while an orphan owns the prefix. Prefix maintenance gets
    one clean retry, with its entire process group removed on timeout.
    """
    if launch.backend != "proton":
        return
    assert launch.env is not None
    compat_data = Path(launch.env["STEAM_COMPAT_DATA_PATH"])
    proton = Path(launch.command[0])
    key = (launch.command[0], str(compat_data))
    with _bootstrap_lock:
        # The prefix is node-private. Starting from a clean Wine server prevents
        # an orphaned NGX host from poisoning every later session.
        stop_proton_prefix(launch)
        if _prefix_is_ready(proton, compat_data):
            _bootstrapped_prefixes.add(key)
            return
        # Official Proton exposes CURRENT_PREFIX_VERSION, so a missing or stale
        # on-disk marker must win over the in-memory cache (including after a
        # live Proton upgrade). Keep the cache fallback for custom wrappers that
        # do not expose a readable version variable.
        if _current_prefix_version(proton) is None and key in _bootstrapped_prefixes:
            return

        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                code, details = _bootstrap_once(launch, timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                stop_proton_prefix(launch)
                if attempt == 0:
                    continue
                raise RuntimeError(
                    f"Proton could not create or update its isolated prefix at "
                    f"{compat_data} after two clean attempts: {exc}."
                ) from exc
            if code:
                stop_proton_prefix(launch)
                raise RuntimeError(
                    f"Proton could not create or update its isolated prefix at "
                    f"{compat_data} (exit {code}):\n"
                    f"{details[-4000:] or 'Proton produced no diagnostic output.'}"
                )
            break
        else:  # pragma: no cover - loop either breaks or raises
            raise RuntimeError(str(last_error))
        _bootstrapped_prefixes.add(key)


def terminate_worker(process, launch: WorkerLaunch, timeout: float = 10.0) -> None:
    """Stop a worker and its Proton descendants without raising."""
    _terminate_process(
        process,
        process_group=launch.start_new_session,
        timeout=timeout,
    )
