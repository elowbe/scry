from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from gamestream.config import AppConfig, load_config
from gamestream.dlss import WarmDlssCache
from gamestream.doctor import Check
from gamestream.errors import PreflightError, SessionConflict
from gamestream.server import SessionManager
from gamestream.settings import apply_settings, public_settings
from gamestream.client_info import build_client_info


def test_order_roundtrips_config_and_invalidates_warm_cache(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[dlss]\nneural_before_upscale = true\n')
    config = load_config(path)
    assert config.dlss.neural_before_upscale is True
    default = replace(config.dlss, neural_before_upscale=False)
    assert WarmDlssCache.signature(default, 640, 360) != WarmDlssCache.signature(config.dlss, 640, 360)
    assert public_settings(config)['dlss']['neural_before_upscale'] is True
    for invalid in (1, 'true', None):
        with pytest.raises(ValueError):
            apply_settings(config, {'dlss': {'neural_before_upscale': invalid}})


@pytest.mark.asyncio
async def test_desktop_session_has_no_game_lifecycle(tmp_path, monkeypatch):
    config = AppConfig(source=tmp_path / 'config.toml')
    config.input.enabled = False
    manager = SessionManager(config)
    manager.launcher.start = AsyncMock()
    manager.launcher.stop = AsyncMock()
    manager._close_peer = AsyncMock()
    manager.input.release_all = Mock()
    monkeypatch.setattr('gamestream.server.run_checks', lambda *a, **k: [
        Check('steam', 'Steam', 'error', 'not installed'),
        Check('proton', 'Proton', 'error', 'not installed'),
    ])
    active = await manager.start('desktop', 'desktop', False)
    assert active.game.provider == 'desktop'
    assert manager.game_watch_task is None
    assert await manager.start('desktop', 'desktop', False) is active
    manager.launcher.start.assert_not_awaited()
    game = next(iter(manager.catalog._games.values()))
    manager.catalog._games['steam', '123'] = replace(game, provider='steam', id='123')
    with pytest.raises(SessionConflict):
        await manager.start('steam', '123', False)
    await manager.stop()
    assert manager.active is None
    assert manager.launcher.running is None
    manager.input.release_all.assert_called_once()


@pytest.mark.asyncio
async def test_desktop_still_requires_capture_preflight(tmp_path, monkeypatch):
    manager = SessionManager(AppConfig(source=tmp_path / 'config.toml'))
    monkeypatch.setattr('gamestream.server.run_checks', lambda *a, **k: [
        Check('encoder', 'Encoder', 'error', 'unavailable'),
    ])
    with pytest.raises(PreflightError, match='Encoder'):
        await manager.start('desktop', 'desktop', False)
    assert manager.active is None


def test_info_documents_desktop_and_neural_order(tmp_path):
    config = AppConfig(source=tmp_path / 'config.toml')
    info = build_client_info(config, SessionManager(config))
    assert info['desktop_streaming']['start']['provider'] == 'desktop'
    assert info['neural_rendering']['default'] is False
    assert info['temporal_stability']['default'] is False
    assert info['client_config']['settings']['dlss']['temporal_stability'] is False
    assert info['client_config']['settings']['dlss']['neural_before_upscale'] is False
    assert 'neural_before_upscale' in info['client_config']['settings_schema']['dlss']
