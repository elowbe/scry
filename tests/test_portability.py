"""Portable contracts, with no real desktop, downloads, drivers or GPU required."""
import io
import json
from pathlib import Path
from unittest.mock import patch, Mock
import pytest
from gamestream.config import load_config, StreamConfig, capture_backend
from gamestream.media import build_encoded_capture_command, build_raw_encoder_command
from gamestream.platform_support import auto_encoder


def test_paths_are_relative_to_config_not_working_directory(tmp_path, monkeypatch):
    folder = tmp_path / 'config folder'
    folder.mkdir()
    file = folder / 'scry.toml'
    file.write_text('[server]\ncert_file="state/cert.pem"\n[dlss]\nruntime_dir="runtime"\n')
    monkeypatch.chdir(tmp_path)
    config = load_config(file)
    assert config.server.cert_file == folder / 'state/cert.pem'
    assert config.dlss.runtime_dir == folder / 'runtime'
    assert not config.steam.require_proton
    assert config.stream.encoder == 'auto'


def test_windows_capture_uses_full_desktop_and_cpu_encoder():
    with patch('gamestream.config.sys.platform', 'win32'):
        config = StreamConfig(encoder='libx264')
        assert capture_backend(config) == 'gdigrab'
        command = build_encoded_capture_command(config)
    assert command[command.index('-i') + 1] == 'desktop'
    assert 'gdigrab' in command
    assert command[command.index('-c:v') + 1] == 'libx264'
    assert command[command.index('-tune') + 1] == 'zerolatency'
    assert not set(command).intersection({'-rc', '-cq', '-spatial-aq', '-zerolatency'})
    assert command[-3:] == ['-f', 'h264', 'pipe:1']


def test_raw_cpu_encoder_has_crf_and_preserves_output():
    command = build_raw_encoder_command(StreamConfig(encoder='libx264'), 1280, 720)
    assert command[command.index('-crf') + 1] == '18'
    assert command[-3:] == ['-f', 'h264', 'pipe:1']


def test_nvidia_failure_falls_back_to_cpu():
    auto_encoder.cache_clear()
    with patch('gamestream.platform_support.subprocess.run', side_effect=[Mock(returncode=1), Mock(returncode=0)]) as run:
        assert auto_encoder() == 'libx264'
        assert run.call_count == 2
    auto_encoder.cache_clear()


def test_missing_encoder_is_actionable():
    auto_encoder.cache_clear()
    with patch('gamestream.platform_support.subprocess.run', side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match='installer'):
            auto_encoder()
    auto_encoder.cache_clear()


def test_import_server_without_linux_only_dependencies():
    # CI executes this on Windows too, catching import-time fcntl/evdev dependencies.
    import gamestream.server
    import gamestream.windows_audio
    import gamestream.windows_input
    import gamestream.tray


def test_native_dlss_launch_needs_no_wine(tmp_path):
    from gamestream.dlss_support.launcher import build_worker_launch
    from gamestream.dlss_support.paths import RuntimeLayout
    launch = build_worker_launch(RuntimeLayout(tmp_path), platform_name='win32', env={})
    assert launch.backend == 'native'
    assert not launch.start_new_session


def test_partial_download_keeps_existing_file_and_cleans_temp(tmp_path):
    from gamestream.onboarding import download
    target = tmp_path/'runtime.dll'
    target.write_bytes(b'previous')
    response = Mock()
    response.headers = {'Content-Length':'100'}
    response.read.side_effect = [b'short', b'']
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    with patch('urllib.request.urlopen', return_value=response):
        with pytest.raises(ValueError, match='Incomplete'):
            download('https://example.com/file', target)
    assert target.read_bytes() == b'previous'
    assert not target.with_suffix('.dll.part').exists()


def test_brand_assets_are_included():
    from gamestream.config import PROJECT_ROOT
    manifest = json.loads((PROJECT_ROOT/'web/manifest.webmanifest').read_text())
    assert manifest['name'] == 'Scry Web'
    for icon in manifest['icons']:
        assert (PROJECT_ROOT/'web'/icon['src'].lstrip('/')).is_file()


@pytest.mark.asyncio
async def test_server_health_identity_and_branded_assets(tmp_path):
    import os
    from aiohttp.test_utils import TestClient, TestServer
    from gamestream.server import create_app
    config = load_config(tmp_path / 'scry.toml')
    app = create_app(config)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/healthz')
        assert await response.json() == {'ok': True, 'service': 'scry-server', 'pid': os.getpid()}
        assert (await client.get('/icon.svg')).status == 401
        headers = {'Authorization': 'Bearer ' + app['auth'].token}
        response = await client.get('/icon.svg', headers=headers)
        assert response.status == 200
        assert 'svg' in response.content_type
        response = await client.get('/', headers=headers)
        assert 'Scry Web' in await response.text()


def test_windows_controller_axes_triggers_and_release():
    from types import SimpleNamespace
    from gamestream.windows_input import WindowsInput
    from gamestream.config import InputConfig
    controller = WindowsInput(InputConfig())
    controller.gamepad = Mock()
    controller._pad_module = SimpleNamespace(XUSB_BUTTON=SimpleNamespace(XUSB_GAMEPAD_A=1))
    controller._gamepad(0, 1, 1, 100, -32768, -200, 32767, 65535, 32768)
    controller.gamepad.left_joystick.assert_called_with(x_value=100, y_value=32767)
    controller.gamepad.right_joystick.assert_called_with(x_value=-200, y_value=-32767)
    controller.gamepad.left_trigger.assert_called_with(value=255)
    controller.gamepad.right_trigger.assert_called_with(value=128)
    controller.gamepad.press_button.assert_called_once_with(1)
    controller.gamepad.reset_mock()
    controller.release_all()
    controller.gamepad.reset.assert_called_once()
    controller.gamepad.update.assert_called_once()


def test_standalone_proton_has_client_directory_without_steam(tmp_path):
    from gamestream.dlss_support.launcher import build_worker_launch
    from gamestream.dlss_support.paths import RuntimeLayout
    with patch('gamestream.dlss_support.launcher.find_proton', return_value=tmp_path/'proton'), \
         patch('gamestream.dlss_support.launcher.find_steam_root', return_value=None):
        launch = build_worker_launch(RuntimeLayout(tmp_path), platform_name='linux', machine='x86_64',
                                    env={'DLSS5_PROTON_COMPAT_DATA':str(tmp_path/'prefix')})
    assert Path(launch.env['STEAM_COMPAT_CLIENT_INSTALL_PATH']).is_dir()
    assert launch.backend == 'proton'
