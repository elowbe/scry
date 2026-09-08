from pathlib import Path
from dataclasses import replace
import pytest
from gamestream.config import AppConfig
from gamestream.settings import apply_settings, public_settings
from gamestream.media import build_raw_encoder_command


def test_unlimited_has_no_encoder_cap_and_roundtrips():
    base = AppConfig(source=Path('unused'))
    config = apply_settings(base, {'stream': {'bitrate_mbps': 0, 'max_bitrate_mbps': 0}})
    command = build_raw_encoder_command(config.stream, 2560, 1440)
    assert command[command.index('-maxrate') + 1] == '0'
    assert command[command.index('-b:v') + 1] == '0M'
    assert command[command.index('-cq') + 1] == '18'
    assert apply_settings(base, public_settings(config)) == config


@pytest.mark.parametrize('changes', [
    {'stream': {'fps': True}}, {'stream': {'cq': float('nan')}},
    {'stream': {'width': 1001}}, {'stream': {'bitrate_mbps': 100, 'max_bitrate_mbps': 50}},
    {'dlss': {'nr_style': 'invalid'}}, {'dlss': {'node_path': '/tmp'}},
    {'stream': {'fps': 30}}, {'stream': {'encoder_preset': '-evil'}},
    {'dlss': {'workers': 1.5}}, {'stream': {'rate_control': 'cbr', 'bitrate_mbps': 0}},
])
def test_invalid_settings_leave_original_unchanged(changes):
    config = AppConfig(source=Path('unused'))
    before = public_settings(config)
    with pytest.raises(ValueError):
        apply_settings(config, changes)
    assert public_settings(config) == before


def test_keyframes_have_bitrate_headroom_and_custom_controls_reach_encoder():
    config = apply_settings(AppConfig(source=Path('unused')), {'stream': {
        'encoder_preset': 'p6', 'max_bitrate_mbps': 120, 'vbv_ms': 150, 'keyframe_seconds': 2,
    }})
    command = build_raw_encoder_command(config.stream, 2560, 1440)
    assert command[command.index('-bufsize') + 1] == '18000000'
    assert command[command.index('-g') + 1] == '120'
    assert command[command.index('-preset') + 1] == 'p6'


@pytest.mark.asyncio
async def test_saved_custom_settings_survive_new_manager_and_preset_resets(tmp_path):
    from gamestream.server import SessionManager, ActiveSession
    from types import SimpleNamespace
    config = AppConfig(source=tmp_path / 'config.toml')
    manager = SessionManager(config)
    manager.active = ActiveSession(SimpleNamespace(public=lambda: {}), False)
    changes = {'stream': {'bitrate_mbps': 0, 'max_bitrate_mbps': 0},
               'dlss': {'nr_style': 'Cinematic'}}
    result = await manager.update_settings(settings=changes)
    assert result['settings']['stream']['max_bitrate_mbps'] == 0
    restored = SessionManager(config)
    assert restored.status()['settings'] == result['settings']
    await manager.update_settings(quality=4)
    assert manager.status()['settings']['stream']['max_bitrate_mbps'] == config.stream.max_bitrate_mbps
