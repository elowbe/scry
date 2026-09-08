from unittest.mock import AsyncMock
import signal

import pytest

from gamestream.catalog import Game
from gamestream.config import AppConfig
from gamestream.errors import PreflightError
from gamestream.launcher import GameLauncher, RunningGame
from gamestream.server import ActiveSession, SessionManager


class FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_steam_exit_waits_for_sustained_process_disappearance(tmp_path, monkeypatch):
    game = Game("123", "Test Game", "steam", tmp_path)
    launcher = GameLauncher(AppConfig(source=tmp_path / "config.toml"), object())
    running = RunningGame(game, FakeProcess(), ["steam"])
    launcher.running = running
    observations = iter(({101}, set(), set(), set()))
    monkeypatch.setattr(launcher, "_steam_game_pids", lambda _app_id: next(observations))

    monkeypatch.setattr("gamestream.launcher.PROCESS_POLL_SECONDS", 0)
    assert await launcher.wait_for_exit(running) is None


@pytest.mark.asyncio
async def test_game_exit_ends_active_stream_session(tmp_path, monkeypatch):
    game = Game("123", "Test Game", "steam", tmp_path)
    manager = SessionManager(AppConfig(source=tmp_path / "config.toml"))
    running = RunningGame(game, FakeProcess(9), ["game"])
    manager.active = ActiveSession(game, False)
    manager.launcher.running = running
    manager.launcher.wait_for_exit = AsyncMock(return_value=9)
    manager._close_peer = AsyncMock()
    monkeypatch.setattr("gamestream.server.asyncio.to_thread", AsyncMock())

    await manager._watch_game(running)

    assert manager.active is None
    assert manager.launcher.running is None
    assert manager._close_peer.await_count == 1
    assert manager.last_error == "Test Game crashed (exit code 9); the stream ended."


@pytest.mark.asyncio
async def test_direct_game_must_survive_launch_settling_period(tmp_path, monkeypatch):
    game = Game("uplay", "Ubisoft Game", "ubisoft", tmp_path)
    launcher = GameLauncher(AppConfig(source=tmp_path / "config.toml"), object())
    running = RunningGame(game, FakeProcess(4), ["proton"])

    with pytest.raises(PreflightError, match="closed during launch.*exit code 4"):
        await launcher._wait_for_launch(running)


@pytest.mark.asyncio
async def test_steam_stop_falls_back_to_targeted_game_processes(tmp_path, monkeypatch):
    game = Game("123", "Test Game", "steam", tmp_path)
    launcher = GameLauncher(AppConfig(source=tmp_path / "config.toml"), object())
    launcher.running = RunningGame(game, FakeProcess(), ["steam"])
    monkeypatch.setattr(
        "gamestream.launcher.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError),
    )
    monkeypatch.setattr(launcher, "_steam_game_pids", lambda _app_id: {4242})
    monkeypatch.setattr("gamestream.launcher.PROCESS_POLL_SECONDS", 0)
    monkeypatch.setattr("gamestream.launcher.STOP_GRACE_SECONDS", 0)
    signals = AsyncMock()
    monkeypatch.setattr(launcher, "_signal_processes", signals)

    await launcher.stop()

    assert signals.await_args_list[0].args == ({4242}, signal.SIGTERM)
    assert signals.await_args_list[1].args == ({4242}, signal.SIGKILL)
