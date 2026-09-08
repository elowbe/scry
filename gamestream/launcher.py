"""Safe Proton/Steam launch orchestration for one active game."""

from __future__ import annotations

import asyncio
import os
import sys
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

from .catalog import Game, GameCatalog
from .config import AppConfig, UbisoftGameConfig
from .errors import PreflightError, SessionConflict


def steam_command(config):
    command = config.steam.command
    if command == "steam" and not shutil.which(command):
        root = config.steam.root or GameCatalog(config).steam_root
        if root and "com.valvesoftware.Steam" in str(root) and shutil.which("flatpak"):
            return ["flatpak", "run", "com.valvesoftware.Steam"]
    resolved = shutil.which(command) or command
    if not Path(resolved).is_file() and shutil.which(resolved) is None:
        raise PreflightError(f"Steam command {command!r} was not found")
    return [resolved]


def discover_proton(config: AppConfig) -> Path | None:
    if config.steam.proton_path and config.steam.proton_path.is_file():
        return config.steam.proton_path
    root = config.steam.root or GameCatalog(config).steam_root
    if not root:
        return None
    candidates = [
        root / "steamapps/common/Proton - Experimental/proton",
        root / "steamapps/common/Proton Hotfix/proton",
    ]
    candidates.extend(sorted((root / "steamapps/common").glob("Proton */proton"), reverse=True))
    return next((path.resolve() for path in candidates if path.is_file()), None)


@dataclass(slots=True)
class RunningGame:
    game: Game
    process: asyncio.subprocess.Process | None
    command: list[str]


LAUNCH_TIMEOUT_SECONDS = 90
PROCESS_POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = 2


class GameLauncher:
    def __init__(self, config: AppConfig, catalog: GameCatalog):
        self.config = config
        self.catalog = catalog
        self.running: RunningGame | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _steam_game_pids(app_id: str) -> set[int]:
        """Find this user's Steam game processes without matching command text."""
        if sys.platform == "win32":
            import psutil
            result = set()
            for process in psutil.process_iter():
                try:
                    env = process.environ()
                    if env.get("SteamAppId") == app_id or env.get("SteamGameId") == app_id:
                        result.add(process.pid)
                except (psutil.Error, OSError):
                    pass
            return result
        markers = {f"SteamAppId={app_id}".encode(), f"SteamGameId={app_id}".encode()}
        result: set[int] = set()
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if markers.intersection(environment):
                result.add(int(entry.name))
        return result

    async def _wait_for_launch(self, running: RunningGame) -> None:
        process = running.process
        if running.game.provider != "steam":
            # A direct launcher which survives a short settling period has
            # successfully exec'd. Its process lifetime remains authoritative.
            try:
                returncode = await asyncio.wait_for(asyncio.shield(process.wait()), timeout=1)
            except asyncio.TimeoutError:
                return
            raise PreflightError(
                f"{running.game.name} closed during launch (exit code {returncode})"
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + LAUNCH_TIMEOUT_SECONDS
        while loop.time() < deadline:
            if self._steam_game_pids(running.game.id):
                return
            if process.returncode not in (None, 0):
                raise PreflightError(
                    f"Steam could not launch {running.game.name} (exit code {process.returncode})"
                )
            await asyncio.sleep(PROCESS_POLL_SECONDS)
        raise PreflightError(
            f"Timed out waiting for {running.game.name} to launch. The stream was not started."
        )

    def _steam_command(self, game: Game) -> tuple[list[str], dict[str, str]]:
        command = steam_command(self.config)
        if sys.platform != "win32" and self.config.steam.require_proton and not discover_proton(self.config):
            raise PreflightError(
                "Proton is required but was not found. Install Proton Experimental in Steam "
                "or set steam.proton_path."
            )
        env = os.environ.copy()
        env.setdefault("PROTON_ENABLE_NVAPI", "1")
        env.setdefault("DXVK_ENABLE_NVAPI", "1")
        env.setdefault("PROTON_HIDE_NVIDIA_GPU", "0")
        args = [*command, "-silent", "-applaunch", game.id, *self.config.steam.launch_args]
        return args, env

    def _ubisoft_command(self, game: Game) -> tuple[list[str], dict[str, str]]:
        item = next((entry for entry in self.config.ubisoft_games if entry.id == game.id), None)
        if item is None:
            raise PreflightError(f"Ubisoft configuration for {game.id} disappeared")
        self._validate_ubisoft(item)
        if sys.platform == "win32":
            args = [str(item.executable), *item.launch_args]
            if item.uplay_id:
                args.append(f"uplay://launch/{item.uplay_id}/0")
            return args, os.environ.copy()
        proton = discover_proton(self.config)
        if not proton:
            raise PreflightError("Ubisoft Connect requires Proton, but no Proton launcher was found")
        steam_root = self.config.steam.root or self.catalog.steam_root
        if not steam_root:
            raise PreflightError("A Steam root is required to provide Proton's runtime")
        env = os.environ.copy()
        env.update(
            {
                "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam_root),
                "STEAM_COMPAT_DATA_PATH": str(item.compat_data),
                "PROTON_ENABLE_NVAPI": "1",
                "DXVK_ENABLE_NVAPI": "1",
                "PROTON_HIDE_NVIDIA_GPU": "0",
            }
        )
        args = [str(proton), "run", str(item.executable), *item.launch_args]
        if item.uplay_id:
            args.extend([f"uplay://launch/{item.uplay_id}/0"])
        return args, env

    @staticmethod
    def _validate_ubisoft(item: UbisoftGameConfig) -> None:
        if not item.executable.is_file():
            raise PreflightError(f"Ubisoft executable does not exist: {item.executable}")
        item.compat_data.mkdir(parents=True, exist_ok=True)

    async def start(self, game: Game) -> RunningGame:
        async with self._lock:
            if self.running:
                if self.running.game == game:
                    return self.running
                raise SessionConflict(f"{self.running.game.name} is already active")
            if game.provider == "steam":
                args, env = self._steam_command(game)
            elif game.provider == "ubisoft":
                args, env = self._ubisoft_command(game)
            else:
                raise PreflightError(f"Unsupported provider {game.provider!r}")
            process = await asyncio.create_subprocess_exec(
                *args,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
            )
            self.running = RunningGame(game=game, process=process, command=args)
            try:
                await self._wait_for_launch(self.running)
            except Exception:
                self.running = None
                raise
            return self.running

    async def wait_for_exit(self, running: RunningGame) -> int | None:
        """Wait until the exact game represented by *running* is no longer alive."""
        if running.game.provider != "steam":
            return await running.process.wait() if running.process else None
        missing_polls = 0
        while self.running is running:
            if self._steam_game_pids(running.game.id):
                missing_polls = 0
            else:
                missing_polls += 1
                if missing_polls >= 3:
                    return None
            await asyncio.sleep(PROCESS_POLL_SECONDS)
        return None

    async def forget(self, running: RunningGame) -> None:
        """Clear a game which has already exited, without issuing a stop command."""
        async with self._lock:
            if self.running is running:
                self.running = None

    @staticmethod
    async def _signal_processes(pids: Iterable[int], sig: signal.Signals) -> None:
        for pid in pids:
            try:
                _terminate_pid(pid, force=sig != signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        async with self._lock:
            running, self.running = self.running, None
            if not running:
                return
            if running.game.provider == "steam":
                try:
                    steam = steam_command(self.config)
                    stop_process = await asyncio.create_subprocess_exec(
                        *steam,
                        f"steam://stop/{running.game.id}",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(stop_process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        stop_process.kill()
                        await stop_process.wait()
                except (OSError, PreflightError):
                    # The targeted process fallback below still guarantees that
                    # an unavailable Steam URI handler cannot orphan the game.
                    pass
                # Steam normally handles the URI promptly. If it does not,
                # terminate only processes carrying this game's Steam app ID.
                for _ in range(16):
                    pids = self._steam_game_pids(running.game.id)
                    if not pids:
                        break
                    await asyncio.sleep(PROCESS_POLL_SECONDS)
                else:
                    await self._signal_processes(pids, signal.SIGTERM)
                    await asyncio.sleep(STOP_GRACE_SECONDS)
                    await self._signal_processes(
                        self._steam_game_pids(running.game.id), getattr(signal, "SIGKILL", 9)
                    )
            elif running.process and running.process.returncode is None:
                try:
                    _terminate_pid(running.process.pid, group=True)
                    await asyncio.wait_for(running.process.wait(), timeout=8)
                except (ProcessLookupError, asyncio.TimeoutError):
                    if running.process.returncode is None:
                        try:
                            _terminate_pid(running.process.pid, force=True, group=True)
                        except ProcessLookupError:
                            pass
                        else:
                            await running.process.wait()


def _terminate_pid(pid, *, force=False, group=False):
    if sys.platform == "win32":
        import psutil
        try:
            parent = psutil.Process(pid)
            for process in [*parent.children(recursive=True), parent] if group else [parent]:
                try:
                    process.kill() if force else process.terminate()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass
    else:
        (os.killpg if group else os.kill)(pid, signal.SIGKILL if force else signal.SIGTERM)
