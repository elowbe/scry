from dataclasses import replace
import json

import pytest

from gamestream.config import AppConfig
from gamestream.input import DOM_CODES
from gamestream.server import create_app


def get_handler(app, path, method="GET"):
    resource = next(item for item in app.router.resources() if item.canonical == path)
    return next(route.handler for route in resource if route.method == method)


@pytest.mark.asyncio
async def test_info_describes_complete_client_protocol(tmp_path):
    config = AppConfig(source=tmp_path / "config.toml")
    config = replace(
        config,
        server=replace(
            config.server,
            token_file=tmp_path / "access.token",
            ice_urls=["stun:stun.example.test:3478"],
        ),
    )
    app = create_app(config)
    response = await get_handler(app, "/info")(None)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    info = json.loads(response.body)

    assert info["protocol_version"] == 2
    assert info["authentication"]["required_for"] == "Every route except GET /healthz"
    assert info["client_config"]["ice_servers"] == [
        {"urls": ["stun:stun.example.test:3478"]}
    ]
    assert info["input"]["keyboard_codes"] == list(DOM_CODES)
    assert {packet["type"] for packet in info["input"]["packets"]} == {
        "0x01", "0x02", "0x03", "0x04", "0x05", "0x10", "0x7f"
    }
    assert info["cursor"]["channel"]["ordered"] is True
    assert info["cursor"]["channel"]["reliable"] is True
    assert set(info["cursor"]["hosts"]) == {"windows", "x11", "wayland"}
    documented = {(item["method"], item["path"]) for item in info["endpoints"]}
    assert {
        ("GET", "/info"),
        ("GET", "/healthz"),
        ("GET", "/api/health"),
        ("GET", "/api/client-config"),
        ("GET", "/api/games"),
        ("GET", "/api/games/{provider}/{game_id}/art"),
        ("GET", "/api/session"),
        ("POST", "/api/session/start"),
        ("POST", "/api/session/stop"),
        ("POST", "/api/session/settings"),
        ("POST", "/api/webrtc/offer"),
        ("POST", "/api/webrtc/diagnostics"),
        ("POST", "/api/input/escape"),
    } <= documented


@pytest.mark.asyncio
async def test_client_config_matches_embedded_info(tmp_path):
    config = AppConfig(source=tmp_path / "config.toml")
    config = replace(config, server=replace(config.server, token_file=tmp_path / "access.token"))
    app = create_app(config)
    info_response = await get_handler(app, "/info")(None)
    config_response = await get_handler(app, "/api/client-config")(None)
    assert json.loads(config_response.body) == json.loads(info_response.body)["client_config"]
