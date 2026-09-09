"""Authenticated HTTPS API, WebRTC signalling, and session ownership."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from pathlib import Path

from .auth import AuthManager
from .catalog import Game, GameCatalog
from .client_info import build_client_info
from .config import AppConfig, capture_backend
from .cursor import create_cursor, PointerSession
from .doctor import ready, run_checks
from .dlss import FAST_HOST, ORDERED_BRIDGE, WARM_DLSS
from .settings import apply_settings, public_settings
from .pacing import pace_video
from .errors import GameStreamError, PreflightError, SessionConflict
from .input import VirtualInput
from .launcher import GameLauncher
from .media import StreamPipeline, EncodedVideoTrack, SwitchableVideoTrack
from .quality import DEFAULT_QUALITY, quality_config, quality_options
from .network import add_lan_candidates, candidate_summary

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveSession:
    game: Game
    dlss_enabled: bool


class SessionManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.catalog = GameCatalog(config)
        self.launcher = GameLauncher(config, self.catalog)
        self.input = VirtualInput(config.input)
        self.active: ActiveSession | None = None
        self.pc = None
        self.pipeline: StreamPipeline | None = None
        self.video_track: SwitchableVideoTrack | None = None
        self.quality_level = DEFAULT_QUALITY
        self.settings_override = {}
        self.settings_file = config.source.parent / ".state/player-settings.json"
        try:
            saved = json.loads(self.settings_file.read_text())
            # Do not silently opt an old boolean-only preference into a new
            # stabilization algorithm after the motion-artifact regression.
            saved_dlss = saved["settings"].get("dlss", {}) if isinstance(saved["settings"], dict) else {}
            if isinstance(saved_dlss, dict) and saved_dlss.get("temporal_stability") is True and "temporal_method" not in saved_dlss:
                saved_dlss["temporal_stability"] = False
            apply_settings(config, saved["settings"])
            quality_config(config, saved["quality"])
            self.settings_override, self.quality_level = saved["settings"], saved["quality"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.channel = None
        self.pointer_channel = None
        self.pointer_task = None
        self.telemetry_task: asyncio.Task | None = None
        self.warmup_task: asyncio.Task | None = None
        self.game_watch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error = ""
        self.ice_diagnostics = {}

    async def start(self, provider: str, game_id: str, dlss_enabled: bool) -> ActiveSession:
        async with self._lock:
            game = self.catalog.get(provider, game_id)
            if self.active and self.active.game != game:
                raise SessionConflict(f"{self.active.game.name} is already active")
            if self.active:
                return self.active
            checks = await asyncio.to_thread(run_checks, self.config, probe_capture=False)
            failures = [
                check for check in checks
                if check.required and check.status == "error"
                and not (provider == "desktop" and check.id in {"steam", "proton"})
            ]
            if failures:
                detail = "; ".join(f"{check.label}: {check.detail}" for check in failures)
                raise PreflightError(f"Host preflight failed — {detail}")
            if self.config.input.enabled and self.input.keyboard is None:
                self.input.open()
            if dlss_enabled:
                dlss = self.effective_config().dlss
                runtime = dlss.runtime_dir or dlss.node_path / "runtime"
                required = [
                    runtime / "nvngx_dlss.dll",
                    runtime / "nvngx_dlssnr.dll",
                    ORDERED_BRIDGE,
                    FAST_HOST,
                ]
                missing = [str(path) for path in required if not path.is_file()]
                if missing:
                    raise PreflightError(
                        "DLSS5 was selected, but its bridge/runtime is incomplete: "
                        + ", ".join(missing)
                    )
            running = await self.launcher.start(game) if provider != "desktop" else None
            self.active = ActiveSession(game, dlss_enabled)
            self.last_error = ""
            if running is not None:
                self.game_watch_task = asyncio.create_task(self._watch_game(running))
            return self.active

    async def _watch_game(self, running) -> None:
        try:
            returncode = await self.launcher.wait_for_exit(running)
            async with self._lock:
                if self.launcher.running is not running:
                    return
                await self.launcher.forget(running)
                try:
                    await self._close_peer()
                    await asyncio.to_thread(WARM_DLSS.clear)
                except Exception:
                    logger.exception("Cleanup failed after the game exited")
                finally:
                    self.input.release_all()
                    self.active = None
                self.last_error = (
                    f"{running.game.name} crashed (exit code {returncode}); the stream ended."
                    if returncode not in (None, 0)
                    else f"{running.game.name} closed; the stream ended."
                )
                logger.info(self.last_error)
        except asyncio.CancelledError:
            pass
        finally:
            if self.game_watch_task is asyncio.current_task():
                self.game_watch_task = None

    async def begin_game_monitoring(self) -> None:
        if self.active and self.launcher.running and not self.game_watch_task:
            self.game_watch_task = asyncio.create_task(
                self._watch_game(self.launcher.running)
            )

    async def attach(self, offer_sdp: str, offer_type: str, peer_address: str | None = None):
        if not self.active:
            raise PreflightError("Start a game before creating a WebRTC connection")
        try:
            from aiortc import (
                RTCConfiguration,
                RTCIceServer,
                RTCPeerConnection,
                RTCSessionDescription,
                RTCRtpSender,
            )
        except ImportError as exc:
            raise PreflightError("aiortc is not installed") from exc

        async with self._lock:
            await self._close_peer()
            self.last_error = ""
            offer_sdp, lan_count = add_lan_candidates(offer_sdp, peer_address)
            self.ice_diagnostics = {"remote_candidates": candidate_summary(offer_sdp), "lan_fallbacks": lan_count}
            logger.info("WebRTC offer from %s: %s", peer_address, self.ice_diagnostics)
            ice_servers = []
            if self.config.server.ice_urls:
                ice_servers.append(
                    RTCIceServer(
                        urls=self.config.server.ice_urls,
                        username=self.config.server.ice_username or None,
                        credential=self.config.server.ice_credential or None,
                    )
                )
            pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
            self.pc = pc

            @pc.on("datachannel")
            def on_datachannel(channel):
                if channel.label == "pointer":
                    if self.pointer_channel is not None:
                        channel.close()
                        return
                    self.pointer_channel = channel
                    pointer = PointerSession(self.input)
                    self.pointer_task = asyncio.create_task(self._pointer_loop(pc, channel, pointer))
                    @channel.on("message")
                    def on_pointer_message(message):
                        if self.pc is not pc or not isinstance(message, bytes) or len(message) > 64:
                            return
                        try:
                            pointer.handle(message)
                        except ValueError:
                            logger.debug("Ignored malformed pointer packet")
                    @channel.on("close")
                    def on_pointer_close():
                        self.input.release_all()
                    return
                if channel.label != "input":
                    channel.close()
                    return
                self.channel = channel

                @channel.on("open")
                def on_open():
                    fps = self.pipeline.stats["output_fps"] if self.pipeline else self.config.stream.fps
                    channel.send(json.dumps({"type": "host", "fps": fps}))

                @channel.on("message")
                def on_message(message):
                    if self.pc is pc and isinstance(message, bytes) and len(message) <= 64 and message[:1] != b"\x05":
                        try:
                            self.input.handle(message)
                        except ValueError:
                            logger.debug("Ignored malformed input packet", exc_info=True)

                @channel.on("close")
                def on_close():
                    self.input.release_all()

            @pc.on("iceconnectionstatechange")
            async def on_iceconnectionstatechange():
                logger.info("WebRTC ICE state: %s; %s", pc.iceConnectionState, self.ice_diagnostics)
                if pc.iceConnectionState == "failed":
                    self.last_error = "Media network connection failed (ICE). HTTPS is reachable, but no WebRTC UDP candidate pair connected."
                    logger.error("%s %s", self.last_error, self.ice_diagnostics)

            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                if pc.connectionState in {"failed", "closed"}:
                    self.input.release_all()
                    if self.pc is pc:
                        await self._close_peer()

            try:
                # aiortc selects the sender codec while applying the offer.
                # Our packets are already H.264, so preferences must be set
                # before that selection; changing them afterward only changes SDP.
                capabilities = RTCRtpSender.getCapabilities("video")
                h264 = [codec for codec in capabilities.codecs if codec.mimeType.casefold() == "video/h264"]
                if not h264:
                    raise PreflightError("The WebRTC runtime has no H.264 support")
                video_transceiver = pc.addTransceiver("video", direction="sendonly")
                video_transceiver.setCodecPreferences(h264)
                await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
                self.pipeline = await StreamPipeline.create(self.effective_config(), self.active.dlss_enabled)
                self.video_track = SwitchableVideoTrack(self.pipeline.video, self._recover_video)
                sender = pc.addTrack(self.video_track)
                pace_video(sender, lambda: self.pipeline.video.config)
                if self.pipeline.audio:
                    pc.addTrack(self.pipeline.audio)
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                self.ice_diagnostics["local_candidates"] = candidate_summary(pc.localDescription.sdp)
                logger.info("WebRTC answer: %s", self.ice_diagnostics)
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("WebRTC startup failed")
                await self._close_peer()
                raise
            self.telemetry_task = asyncio.create_task(self._telemetry_loop(pc))
            if not self.active.dlss_enabled and self.effective_config().dlss.keep_warm:
                self.warmup_task = asyncio.create_task(self._warm_dlss())
            return pc.localDescription

    async def _warm_dlss(self):
        try:
            await asyncio.to_thread(WARM_DLSS.prewarm, self.effective_config())
        except Exception:
            logger.exception("Background DLSS warmup unavailable; standard streaming continues")

    def effective_config(self):
        return apply_settings(quality_config(self.config, self.quality_level), self.settings_override)

    def save_settings(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"quality": self.quality_level, "settings": self.settings_override}))
        temporary.replace(self.settings_file)

    async def update_settings(self, quality=None, dlss_enabled=None, settings=None):
        if quality is not None:
            quality_config(self.config, quality)  # Strict validation before mutation.
        if dlss_enabled is not None and type(dlss_enabled) is not bool:
            raise ValueError("dlss_enabled must be a boolean")
        async with self._lock:
            if not self.active:
                raise PreflightError("Start a game before changing stream settings")
            level = self.quality_level if quality is None else quality
            enabled = self.active.dlss_enabled if dlss_enabled is None else dlss_enabled
            overrides = {} if quality is not None else self.settings_override
            base = apply_settings(quality_config(self.config, level), overrides)
            candidate = apply_settings(base, {} if settings is None else settings)
            if settings is not None:
                overrides = public_settings(candidate)
            if level == self.quality_level and enabled == self.active.dlss_enabled and overrides == self.settings_override:
                return self.status()
            if not self.pipeline or not self.video_track:
                self.quality_level, self.active.dlss_enabled = level, enabled
                self.settings_override = overrides
                self.save_settings()
                return self.status()
            pc, pipeline, proxy = self.pc, self.pipeline, self.video_track
            old_level, old_enabled = self.quality_level, self.active.dlss_enabled
            old_config = self.effective_config()
            # A DLSS worker owns an exclusive runtime slot. Reconfiguring an
            # already-enabled filter must release it before opening another.
            suspended = enabled and old_enabled
            if suspended:
                previous = proxy.suspend()
                await asyncio.to_thread(previous.stop)
            try:
                video = await EncodedVideoTrack.create(candidate, enabled)
            except Exception as exc:
                self.last_error = f"Stream setting change failed: {exc}"
                if suspended and self.pc is pc:
                    try:
                        restored = await EncodedVideoTrack.create(old_config, old_enabled)
                        pipeline.video = restored
                        proxy.replace(restored)
                    except Exception:
                        logger.exception("Could not restore previous video settings")
                        await self._close_peer()
                logger.exception("Could not apply live settings")
                raise PreflightError(self.last_error) from exc
            if self.pc is not pc or self.pipeline is not pipeline:
                await asyncio.to_thread(video.stop)
                raise PreflightError("Stream disconnected while applying settings")
            previous = proxy.replace(video)
            pipeline.video = video
            self.quality_level, self.active.dlss_enabled = level, enabled
            self.settings_override = overrides
            self.last_error = ""
            if previous:
                await asyncio.to_thread(previous.stop)
            if not candidate.dlss.keep_warm:
                await asyncio.to_thread(WARM_DLSS.clear)
            self.save_settings()
            logger.info("Live stream settings: quality=%s DLSS=%s", level, enabled)
            return self.status()

    async def _recover_video(self, failed) -> None:
        """Repair capture without ending the RTP sender or the running game."""
        pc, pipeline, proxy = self.pc, self.pipeline, self.video_track
        attempt = 0
        logger.warning("Video recovery requested: %s", failed.stats.last_error)
        while self.pc is pc and self.pipeline is pipeline and proxy and not proxy.closed:
            async with self._lock:
                if (self.pc is not pc or self.pipeline is not pipeline
                        or proxy.closed or proxy.source is not failed):
                    return
                attempt += 1
                self.last_error = f"Video capture interrupted; restarting (attempt {attempt})."
                failed.stats.last_error = self.last_error
                logger.warning("%s", self.last_error)
                await asyncio.to_thread(failed.stop)
                try:
                    video = await EncodedVideoTrack.create(
                        self.effective_config(), self.active.dlss_enabled
                    )
                except Exception as exc:
                    self.last_error = f"Video recovery attempt {attempt} failed: {exc}"
                    failed.stats.last_error = self.last_error
                    logger.warning("%s", self.last_error)
                else:
                    if self.pc is not pc or self.pipeline is not pipeline or proxy.closed:
                        await asyncio.to_thread(video.stop)
                        return
                    pipeline.video = video
                    proxy.replace(video)
                    self.last_error = ""
                    logger.info("Video capture recovered after %s attempt(s)", attempt)
                    return
            # Release the settings/session lock between attempts so stopping,
            # reconnecting, or changing capture settings can interrupt recovery.
            await asyncio.sleep(min(2 ** min(attempt - 1, 4), 15))

    async def _telemetry_loop(self, pc) -> None:
        try:
            while self.pc is pc and pc.connectionState not in {"failed", "closed"}:
                await asyncio.sleep(1)
                channel = self.channel
                if channel and channel.readyState == "open" and self.pipeline:
                    channel.send(json.dumps({"type": "pipeline", **self.pipeline.stats}))
        except asyncio.CancelledError:
            pass

    async def _pointer_loop(self, pc, channel, pointer):
        provider = None
        last_shape = None
        shape_id = 0
        missing_samples = 0
        try:
            provider = create_cursor(capture_backend(self.config.stream), self.config.stream.display)
            pointer.provider = provider
            while self.pc is pc and pc.connectionState not in {"failed", "closed"} and channel.readyState != "closed":
                video = getattr(self.pipeline, "video", None)
                updated = getattr(video, "cursor_updated", None) if provider is None else None
                if updated is not None:
                    # Wake on compositor updates; Event coalesces bursts to the
                    # latest state instead of accumulating animation frames.
                    try:
                        await asyncio.wait_for(updated.wait(), timeout=1 / 60)
                    except asyncio.TimeoutError:
                        pass
                    updated.clear()
                else:
                    await asyncio.sleep(1 / 60)
                # Do not enqueue more stale shapes while a previous one is
                # still waiting in SCTP. The next iteration takes newest state.
                if channel.readyState != "open" or channel.bufferedAmount > 0:
                    continue
                sample = provider.sample() if provider else getattr(getattr(self.pipeline, "video", None), "cursor_state", None)
                message = pointer.update(sample)
                if not message:
                    missing_samples += 1
                    if missing_samples == 300:
                        channel.send(json.dumps(dict(type='cursor_error', message='The host did not supply cursor metadata. Absolute mouse input remains available.')))
                    continue
                missing_samples = 0
                shape = message.pop('image', None)
                if shape and shape != last_shape:
                    shape_id += 1
                    for offset in range(0, len(shape), 12000):
                        channel.send(json.dumps(dict(type='cursor_image', id=shape_id, offset=offset,
                            total=len(shape), data=shape[offset:offset+12000])))
                    last_shape = shape
                channel.send(json.dumps(dict(message, image_id=shape_id)))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Cursor metadata stopped")
            if channel.readyState == 'open':
                channel.send(json.dumps(dict(type='cursor_error', message=str(exc))))
        finally:
            pointer.provider = None
            if provider:
                provider.close()

    async def _close_peer(self) -> None:
        task, self.pointer_task = self.pointer_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.pointer_channel = None
        self.input.release_all()
        if self.warmup_task:
            await self.warmup_task
            self.warmup_task = None
        task, self.telemetry_task = self.telemetry_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        proxy, self.video_track = self.video_track, None
        if proxy:
            proxy.stop()
        pipeline, self.pipeline = self.pipeline, None
        if pipeline:
            pipeline.close()
        pc, self.pc = self.pc, None
        self.channel = None
        if pc and pc.connectionState != "closed":
            await pc.close()

    async def stop(self) -> None:
        # A recovery attempt can hold the session lock while capture starts.
        # Cancel it before waiting for that lock (e.g. an open portal dialog).
        if self.video_track:
            self.video_track.stop()
        async with self._lock:
            task, self.game_watch_task = self.game_watch_task, None
            if task and task is not asyncio.current_task():
                task.cancel()
            await self._close_peer()
            await asyncio.to_thread(WARM_DLSS.clear)
            await self.launcher.stop()
            self.input.release_all()
            self.active = None

    async def close(self) -> None:
        await self.stop()
        self.input.close()

    def status(self) -> dict:
        effective = self.effective_config()
        return {
            "active": bool(self.active),
            "quality": self.quality_level,
            "quality_settings": quality_options(self.config)[self.quality_level],
            "settings": public_settings(effective),
            "last_error": self.last_error,
            "ice": self.ice_diagnostics,
            "game": self.active.game.public() if self.active else None,
            "dlss_enabled": self.active.dlss_enabled if self.active else False,
            "peer_state": self.pc.connectionState if self.pc else "closed",
            "pipeline": self.pipeline.stats if self.pipeline else None,
        }


def create_app(config: AppConfig):
    try:
        from aiohttp import web
    except ImportError as exc:
        raise RuntimeError("aiohttp is not installed; run scripts/bootstrap.sh") from exc

    auth = AuthManager(config.server.token_file)
    manager = SessionManager(config)

    @web.middleware
    async def security_headers(request, handler):
        response = await handler(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": (
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'self'; connect-src 'self'; media-src 'self' blob:"
                ),
            }
        )
        return response

    @web.middleware
    async def authenticate(request, handler):
        if request.path == "/healthz":
            return await handler(request)
        query_token = request.query.get("token")
        cookie_token = request.cookies.get(auth.cookie_name)
        header = request.headers.get("Authorization", "")
        bearer = header[7:] if header.startswith("Bearer ") else None
        if not any(auth.valid(candidate) for candidate in (query_token, cookie_token, bearer)):
            raise web.HTTPUnauthorized(text="A valid Scry - Server pairing token is required")
        response = await handler(request)
        if auth.valid(query_token):
            response.set_cookie(
                auth.cookie_name,
                auth.token,
                httponly=True,
                secure=True,
                samesite="Strict",
                max_age=60 * 60 * 24 * 90,
            )
        return response

    app = web.Application(
        middlewares=[security_headers, authenticate],
        client_max_size=2 * 1024 * 1024,
    )
    app["config"] = config
    app["auth"] = auth
    app["manager"] = manager

    async def index(request):
        if request.query.get("token"):
            response = web.HTTPFound("/")
            response.set_cookie(
                auth.cookie_name, auth.token, httponly=True, secure=True,
                samesite="Strict", max_age=60 * 60 * 24 * 90,
            )
            return response
        return web.FileResponse(config.server.static_dir / "index.html")

    async def static_file(request):
        name = request.match_info["name"]
        if name not in {"app.js", "styles.css", "manifest.webmanifest", "icon.svg", "icon.png"}:
            raise web.HTTPNotFound()
        return web.FileResponse(config.server.static_dir / name)

    async def healthz(request):
        import os
        return web.json_response({"ok": True, "service": "scry-server", "pid": os.getpid()})

    async def health(request):
        checks = await asyncio.to_thread(run_checks, config, probe_capture=False)
        return web.json_response({"ready": ready(checks), "checks": [item.public() for item in checks]})

    async def client_config(request):
        return web.json_response(build_client_info(config, manager)["client_config"])

    async def info(request):
        response = web.json_response(build_client_info(config, manager))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def games(request):
        return web.json_response({"games": manager.catalog.public()})

    async def artwork(request):
        try:
            game = manager.catalog.get(request.match_info["provider"], request.match_info["game_id"])
        except KeyError:
            raise web.HTTPNotFound()
        if not game.artwork or not game.artwork.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(game.artwork)

    async def status(request):
        return web.json_response(manager.status())

    async def start(request):
        body = await request.json()
        try:
            if "quality" in body:
                quality_config(config, body["quality"])
            active = await manager.start(
                str(body.get("provider", "steam")),
                str(body.get("game_id", "")),
                bool(body.get("dlss_enabled", False)),
            )
            if "quality" in body and body["quality"] != manager.quality_level:
                await manager.update_settings(quality=body["quality"])
        except (KeyError, ValueError, GameStreamError) as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response(
            {"ok": True, "game": active.game.public(), "dlss_enabled": active.dlss_enabled}
        )

    async def stop(request):
        await manager.stop()
        return web.json_response({"ok": True})

    async def offer(request):
        body = await request.json()
        try:
            answer = await manager.attach(str(body["sdp"]), str(body.get("type", "offer")), request.remote)
        except (KeyError, ValueError, GameStreamError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("WebRTC offer failed")
            return web.json_response({"error": f"Stream could not start: {exc}"}, status=503)
        return web.json_response({"sdp": answer.sdp, "type": answer.type})

    async def stream_settings(request):
        body = await request.json()
        try:
            if not isinstance(body, dict) or body.keys() - {"quality", "dlss_enabled", "settings"}:
                raise ValueError("Unknown settings request fields")
            result = await manager.update_settings(body.get("quality"), body.get("dlss_enabled"), body.get("settings"))
            return web.json_response(result)
        except (ValueError, GameStreamError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def send_escape(request):
        if not manager.active or not manager.channel or manager.channel.readyState != "open":
            return web.json_response({"error": "Connect the stream before sending Escape"}, status=409)
        # Reliable HTTP command with host-side release prevents an unordered
        # key-up packet being lost and leaving Escape or shortcut modifiers held.
        manager.input.release_all()
        manager.input.handle(bytes((1, 1, 0, 0)))
        try:
            await asyncio.sleep(.04)
        finally:
            manager.input.handle(bytes((1, 0, 0, 0)))
        return web.json_response({"ok": True})

    app.router.add_post("/api/session/settings", stream_settings)
    app.router.add_post("/api/input/escape", send_escape)

    app.router.add_get("/", index)
    app.router.add_get("/info", info)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/{name:app.js|styles.css|manifest.webmanifest|icon.svg|icon.png}", static_file)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/client-config", client_config)
    app.router.add_get("/api/games", games)
    app.router.add_get("/api/games/{provider}/{game_id}/art", artwork)
    app.router.add_get("/api/session", status)
    app.router.add_post("/api/session/start", start)
    app.router.add_post("/api/session/stop", stop)
    app.router.add_post("/api/webrtc/offer", offer)

    async def client_diagnostics(request):
        body = await request.json()
        # Limit fields and lengths; do not accept raw SDP, URLs, or credentials.
        event = {key: str(body[key])[:500] for key in ("event", "ice_state", "connection_state", "detail") if key in body}
        logger.warning("Client WebRTC %s: %s", request.remote, event)
        return web.json_response({"ok": True})
    app.router.add_post("/api/webrtc/diagnostics", client_diagnostics)

    async def cleanup(_app):
        await manager.close()

    app.on_startup.append(lambda _app: manager.begin_game_monitoring())
    app.on_cleanup.append(cleanup)
    return app


def run_server(config: AppConfig, *, resume_steam: str | None = None, resume_dlss: bool = False) -> None:
    from aiohttp import web
    import sys
    import signal
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    if not config.server.cert_file.is_file() or not config.server.key_file.is_file():
        from .certificates import ensure_certificate
        ensure_certificate(config)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(config.server.cert_file, config.server.key_file)
    from logging.handlers import RotatingFileHandler
    log_path = config.source.parent / ".state" / "game-stream.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    app = create_app(config)
    if resume_steam:
        from .launcher import RunningGame
        manager = app["manager"]
        game = manager.catalog.get("steam", resume_steam)
        manager.input.open()
        manager.active = ActiveSession(game, resume_dlss)
        manager.launcher.running = RunningGame(game, None, [])
        logger.info("Resuming existing Steam game %s without relaunch", resume_steam)
    auth: AuthManager = app["auth"]
    host = config.server.public_host or "localhost"
    print(f"Scry - Server: https://{host}:{config.server.port}/?token={auth.token}", flush=True)
    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        ssl_context=ssl_context,
        access_log_format='%a %s %Tf',
    )
