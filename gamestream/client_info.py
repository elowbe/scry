"""Machine-readable client protocol description served by ``/info``."""

from __future__ import annotations

from . import __version__
from .config import AppConfig
from .input import DOM_CODES
from .quality import quality_options
from .settings import public_settings, settings_schema


def _client_config(config: AppConfig, manager) -> dict:
    """Return the runtime values a client needs to establish a stream."""
    return {
        "quality_options": quality_options(config),
        "settings_schema": settings_schema(),
        "settings": public_settings(manager.effective_config()),
        "quality": manager.quality_level,
        "width": config.stream.width,
        "height": config.stream.height,
        "fps": config.stream.fps,
        "dlss_target_fps": config.dlss.target_fps,
        "bitrate_mbps": config.stream.bitrate_mbps,
        "ice_servers": (
            [{
                "urls": config.server.ice_urls,
                **({"username": config.server.ice_username} if config.server.ice_username else {}),
                **({"credential": config.server.ice_credential} if config.server.ice_credential else {}),
            }]
            if config.server.ice_urls
            else []
        ),
    }


def build_client_info(config: AppConfig, manager) -> dict:
    """Describe the complete public HTTP, WebRTC, and input contract."""
    client_config = _client_config(config, manager)
    return {
        "name": "Scry - Server",
        "version": __version__,
        "protocol_version": 2,
        "description": "Authenticated HTTPS signalling for one WebRTC game or desktop streaming session.",
        "desktop_streaming": {
            "start": {"provider": "desktop", "game_id": "desktop", "dlss_enabled": False},
            "capture": "Streams the configured host monitor using the existing capture backend and system audio source.",
            "input": "Uses the same keyboard, relative mouse and controller data channel as game streaming.",
            "lifecycle": "Does not launch an application or watch a game process. Stop disconnects capture/input without closing desktop applications.",
            "requirements": "Steam and a game Proton installation are not required for standard desktop streaming. DLSS requires an NVIDIA RTX GPU and runtime; Linux also uses Proton.",
        },
        "temporal_stability": {
            "setting": "dlss.temporal_stability",
            "default": False,
            "method_setting": "dlss.temporal_method",
            "default_method": "static_regions",
            "methods": {
                "static_regions": "Optional bounded output smoothing only where source RGB is exactly unchanged for two comparisons. Changed areas plus an 8-input-pixel margin use the current independent render. No optical flow, reprojection, or NGX history accumulation.",
                "optical_flow": "Previous experimental motion-guided history. May smear, ghost, or locally warp during motion; explicitly select this method to use it.",
            },
            "original_renderer": {"settings": {"dlss": {"temporal_stability": False, "neural_before_upscale": False}}},
            "original_behavior": "Zero motion and reset on every frame, configured parallel workers, original DLSS-then-neural order. Keeps current neural style/intensity and stream quality. Player settings include an Original flickery renderer button.",
            "resets": "Stabilization state clears on long delivery gaps, renderer rebuilds and warm-cache reuse. Static-region history is rejected locally on any RGB change; optical-flow mode uses scene-cut detection.",
            "workers": "One render worker while stabilization is enabled; configured dlss.workers otherwise.",
            "tradeoff": "Static-only smoothing deliberately leaves motion flicker rather than reusing moving content. Optical flow can distort motion. Either option adds cost and may lower FPS.",
            "migration": "Saved player settings from the earlier boolean-only version do not automatically opt into a new stabilization method; stabilization is disabled until selected again.",
            "request": {"settings": {"dlss": {"temporal_stability": True, "temporal_method": "static_regions"}}},
            "endpoint": "POST /api/session/settings",
        },
        "neural_rendering": {
            "setting": "dlss.neural_before_upscale",
            "default": False,
            "false": "Low-resolution capture -> DLSS Super Resolution to target -> neural rendering",
            "true": "Low-resolution capture -> neural rendering at input resolution -> DLSS Super Resolution to target",
            "note": "Neural rendering is a same-resolution pass. At factor 1 there is no spatial upscale. Applies only when DLSS is enabled, for both games and desktop.",
            "configure": "Set in [dlss] in config.toml or use player Advanced settings. Live changes rebuild the renderer and briefly interrupt video.",
            "request": {"settings": {"dlss": {"neural_before_upscale": True}}},
            "endpoint": "POST /api/session/settings",
        },
        "content_type": "application/json",
        "max_request_bytes": 2 * 1024 * 1024,
        "authentication": {
            "required_for": "Every route except GET /healthz",
            "methods": [
                {
                    "name": "bearer",
                    "header": "Authorization: Bearer <pairing-token>",
                    "recommended_for": "native and programmatic clients",
                },
                {
                    "name": "query",
                    "parameter": "token",
                    "effect": (
                        "A valid token authenticates the request and sets the Secure, HttpOnly, "
                        "SameSite=Strict game_stream_session cookie. GET / redirects to / without "
                        "the token after pairing."
                    ),
                },
                {
                    "name": "cookie",
                    "cookie": "game_stream_session",
                    "note": "Browser clients receive this cookie through query-token pairing.",
                },
            ],
            "failure": {"status": 401, "content_type": "text/plain"},
            "tls": {"required": True, "minimum_version": "TLS 1.2"},
        },
        "client_config": client_config,
        "workflow": [
            "Authenticate and GET /api/client-config (or use client_config in this document).",
            "GET /api/health and inspect required checks; desktop mode ignores steam/proton game checks.",
            "GET /api/games and select a provider/id pair.",
            "POST /api/session/start.",
            "Create an RTCPeerConnection and the input data channel exactly as documented below.",
            "Add recvonly video and audio transceivers, create an offer, and wait for ICE gathering to complete.",
            "POST the complete SDP offer to /api/webrtc/offer, then apply the returned remote description.",
            "Send binary input only while the data channel is open; send release_all on blur or loss of focus.",
            "Close the peer connection and POST /api/session/stop when finished.",
        ],
        "endpoints": [
            {
                "method": "GET", "path": "/info", "auth": True,
                "response": "This protocol manifest. Cache-Control is no-store.",
            },
            {
                "method": "GET", "path": "/healthz", "auth": False,
                "response": {"ok": "boolean", "service": "string"},
                "note": "Process liveness only; use /api/health for stream readiness.",
            },
            {
                "method": "GET", "path": "/api/health", "auth": True,
                "response": {
                    "ready": "boolean",
                    "checks": [{
                        "id": "string", "label": "string", "status": "ok|warning|error",
                        "detail": "string", "required": "boolean",
                    }],
                },
            },
            {
                "method": "GET", "path": "/api/client-config", "auth": True,
                "response": "Same shape and current values as the top-level client_config object.",
            },
            {
                "method": "GET", "path": "/api/games", "auth": True,
                "response": {"games": [{
                    "id": "string", "name": "string", "provider": "steam|ubisoft|desktop",
                    "install_dir": "string", "manifest": "string|null", "artwork": "string|null",
                    "artwork_url": "string", "size_bytes": "integer",
                    "proton_ready": "boolean", "uplay_id": "string",
                }]},
            },
            {
                "method": "GET", "path": "/api/games/{provider}/{game_id}/art", "auth": True,
                "response": "Artwork bytes with the file's media type.", "errors": {"404": "Unknown game or no artwork."},
            },
            {
                "method": "GET", "path": "/api/session", "auth": True,
                "response": {
                    "active": "boolean", "quality": "integer 0..4", "quality_settings": "object",
                    "settings": "object", "last_error": "string", "ice": "object",
                    "game": "game|null", "dlss_enabled": "boolean",
                    "peer_state": "new|connecting|connected|disconnected|failed|closed",
                    "pipeline": "pipeline telemetry|null",
                },
            },
            {
                "method": "POST", "path": "/api/session/start", "auth": True,
                "request": {
                    "provider": "steam|ubisoft|desktop; defaults to steam", "game_id": "string; use desktop for desktop mode",
                    "dlss_enabled": "boolean; defaults to false", "quality": "optional integer 0..4",
                },
                "response": {"ok": "true", "game": "game", "dlss_enabled": "boolean"},
                "errors": {"409": {"error": "unknown game, active-session conflict, or failed preflight"}},
            },
            {
                "method": "POST", "path": "/api/session/stop", "auth": True,
                "request": {}, "response": {"ok": "true"},
            },
            {
                "method": "POST", "path": "/api/session/settings", "auth": True,
                "request": {
                    "quality": "optional integer 0..4; selecting it resets custom overrides",
                    "dlss_enabled": "optional boolean",
                    "settings": "optional object matching client_config.settings_schema",
                },
                "note": "Only these three keys are accepted. The response has the GET /api/session shape.",
                "errors": {"400": {"error": "validation or live reconfiguration failure"}},
            },
            {
                "method": "POST", "path": "/api/webrtc/offer", "auth": True,
                "request": {"sdp": "complete SDP offer string", "type": "offer; defaults to offer"},
                "response": {"sdp": "SDP answer string", "type": "answer"},
                "errors": {"400": {"error": "invalid request/session"}, "503": {"error": "media startup failure"}},
            },
            {
                "method": "POST", "path": "/api/webrtc/diagnostics", "auth": True,
                "request": {
                    "event": "optional string", "ice_state": "optional string",
                    "connection_state": "optional string", "detail": "optional string",
                },
                "response": {"ok": "true"},
                "note": "Each accepted field is truncated to 500 characters. Do not send SDP, URLs, or credentials.",
            },
            {
                "method": "POST", "path": "/api/input/escape", "auth": True,
                "request": {}, "response": {"ok": "true"},
                "errors": {"409": {"error": "No open stream input channel."}},
            },
        ],
        "webrtc": {
            "peer_connection": {
                "configuration": {"iceServers": client_config["ice_servers"], "bundlePolicy": "max-bundle"},
                "offer": "Non-trickle ICE: wait for iceGatheringState=complete before POSTing the offer.",
                "transceivers": [
                    {"kind": "video", "direction": "recvonly", "codec": "H.264"},
                    {"kind": "audio", "direction": "recvonly", "codec": "Opus", "optional": True},
                ],
                "ownership": "A new offer replaces the existing peer; only one controlling peer is supported.",
            },
            "data_channel": {
                "label": "input", "ordered": False, "maxRetransmits": 0,
                "binary_type": "arraybuffer", "maximum_inbound_packet_bytes": 64,
                "unexpected_labels": "Labels other than input and pointer are closed by the host",
                "server_message_encoding": "UTF-8 JSON text (not binary)",
                "server_messages": [
                    {"type": "host", "fields": {"fps": "integer"}, "when": "channel open"},
                    {"type": "pipeline", "fields": {
                        "dlss_enabled": "boolean", "output_fps": "integer",
                        "dlss_processing_ms": "number", "frames_filtered": "integer", "last_error": "string",
                    }, "when": "approximately once per second"},
                ],
            },
        },
        "cursor": {
            "channel": {"label": "pointer", "ordered": True, "reliable": True, "binary_type": "arraybuffer"},
            "ownership": "Client owns the visible cursor position. Render a virtual cursor immediately at the client mouse position, within Pointer Lock. Send absolute normalized positions to the host; do not predict from host mouse deltas.",
            "capture": "The host OS cursor is excluded from captured video. The actual host cursor still exists for application hit testing and cursor-image queries. Software cursors drawn into game frames cannot be extracted as OS cursor images.",
            "hosts": {"windows": "GetCursorInfo/GetIconInfo; absolute virtual-desktop positioning",
                      "x11": "XFixes cursor image and XWarpPointer",
                      "wayland": "Absolute uinput pointer; separate read-only PipeWire metadata consumer. Requires portal metadata cursor mode (or GNOME) and C compiler/PipeWire development headers from install-host.sh."},
            "messages": {
                "cursor_image": {"id": "image revision", "offset": "character offset", "total": "total data URL characters",
                    "data": "PNG data URL fragment, at most 12000 characters; assemble in order by id"},
                "cursor": {"image_id": "image revision; 0 explicitly clears the previous image", "epoch": "position-reset generation",
                    "warp": "true only for host repositioning or cursor visibility transitions",
                    "warp_reason": "initial, visibility, external, or null; external requires confirmed host movement",
                    "x": "normalized 0..1 host hotspot x", "y": "normalized 0..1 host hotspot y",
                    "width": "host capture coordinate width", "height": "host capture coordinate height",
                    "image_width": "cursor bitmap width", "image_height": "cursor bitmap height",
                    "hotspot": "[x,y] within bitmap", "visible": "false when the game hides its cursor"},
                "cursor_error": {"message": "cursor metadata failure; show to user"},
            },
            "rendering": "Use the video content rectangle, excluding letterboxing, to map client mouse position to 0..1. Scale the host bitmap and hotspot to this rectangle. Ordinary image updates must never move the client cursor. Hide the virtual cursor on pointer-lock loss.",
            "warps": "Apply a host reset only when its epoch increases. Send that epoch on absolute packets; the host rejects obsolete epochs so queued client positions cannot undo game recentering. Wayland position resets require observing the requested position, then a fresh sample departing from it without intervening client input. Cached samples or elapsed time alone never authorize resets. Ignore legacy unverified position resets, keep the client position, and resend it with the new epoch.",
            "fps": "When the host cursor is hidden, keep Pointer Lock and send relative 0x02 camera motion, including movement past screen edges. Return to absolute positioning when it becomes visible. Raw-input FPS games require this exception to absolute mouse positioning.",
            "actions": "Send position then button transition/wheel on the same reliable ordered pointer channel. Do not synthesize button-up during movement or host warps; held buttons continue dragging. Send release_all on blur, hidden document, pointer-lock loss and disconnect.",
            "image_updates": "Focused XWayland games supply appearance directly through XFixes, independently of compositor frame delivery; native Wayland windows keep compositor metadata. Invalid Wayland cursor metadata hides the overlay. Empty/unsupported bitmap replacements clear the old image; position-only updates preserve it. X11 pixels are re-read even when the cursor serial is reused. Wayland metadata wakes the sender immediately; Windows/X11 are sampled at 60 Hz. PNG compression caches exact bitmap content, including animated frames. New shapes wait for outgoing data to drain, then use the latest host image rather than queueing stale frames.",
            "backpressure": "Coalesce unsent motion to the newest position. Flush that position before each button/wheel action. Never drop button-up or release_all.",
            "setup": "Create both input and pointer channels before creating the WebRTC offer. Reset epoch to 0 and cursor state on every new peer. Input channel remains available for legacy relative input and gamepads.",
        },
        "input": {
            "transport": "Binary keyboard/mouse/release messages on reliable pointer RTCDataChannel; gamepads and legacy relative input use input. Absolute positions require pointer.",
            "byte_order": "little-endian for every multibyte integer",
            "packets": [
                {"name": "mouse_position", "type": "0x05", "length": 9,
                 "layout": ["u8 type", "u32 epoch", "u16 x", "u16 y"],
                 "note": "Absolute position: 0=left/top, 65535=right/bottom. Use the latest host cursor epoch (0 before first update). No mouse sensitivity multiplier. Send only while host cursor is visible."},
                {
                    "name": "key", "type": "0x01", "length": 4,
                    "layout": ["u8 type", "u8 down (0|1)", "u16 code_id"],
                    "note": "code_id is the zero-based index in keyboard_codes.",
                },
                {
                    "name": "mouse_move", "type": "0x02", "length": 5,
                    "layout": ["u8 type", "i16 dx", "i16 dy"],
                },
                {
                    "name": "mouse_button", "type": "0x03", "length": 3,
                    "layout": ["u8 type", "u8 button (0..4)", "u8 down (0|1)"],
                    "buttons": {"0": "left", "1": "middle", "2": "right", "3": "side/back", "4": "extra/forward"},
                },
                {
                    "name": "wheel", "type": "0x04", "length": 5,
                    "layout": ["u8 type", "i16 dx", "i16 dy"],
                    "note": "Use browser WheelEvent signs: positive dx is right and positive dy is down.",
                },
                {
                    "name": "gamepad", "type": "0x10", "length": 19,
                    "layout": [
                        "u8 type", "u8 gamepad_index", "u8 connected (0|1)", "u32 button_mask",
                        "i16 left_x", "i16 left_y", "i16 right_x", "i16 right_y",
                        "u16 left_trigger", "u16 right_trigger",
                    ],
                    "ranges": {"sticks": "-32768..32767", "triggers": "0..65535"},
                    "gamepad_index": "Accepted for wire compatibility; the host currently exposes one virtual gamepad.",
                    "button_bits": {
                        "0": "A/South", "1": "B/East", "2": "X/West", "3": "Y/North",
                        "4": "left bumper", "5": "right bumper", "6": "left trigger button",
                        "7": "right trigger button", "8": "select/back", "9": "start",
                        "10": "left stick", "11": "right stick", "12": "D-pad up",
                        "13": "D-pad down", "14": "D-pad left", "15": "D-pad right", "16": "guide",
                    },
                    "note": (
                        "Button bits follow the browser Standard Gamepad mapping. Bits 6 and 7 are not emitted as "
                        "buttons by the host; send trigger values in the u16 fields at byte offsets 15 and 17."
                    ),
                },
                {
                    "name": "release_all", "type": "0x7f", "length": 1,
                    "layout": ["u8 type"],
                    "note": "Send on blur, visibility loss, pointer-lock loss, and before disconnecting.",
                },
            ],
            "keyboard_codes": list(DOM_CODES),
        },
        "client_requirements": [
            "HTTPS with the host certificate trusted by the client",
            "WebRTC with H.264 video support",
            "RTCDataChannel support",
            "Pointer Lock for a confined local virtual cursor and hidden-cursor FPS camera input (browser clients)",
            "Gamepad API with standard mapping for controller control (browser clients)",
        ],
    }
