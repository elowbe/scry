import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gamestream.config import AppConfig
from gamestream.media import EncodedVideoTrack, MediaStreamError, SwitchableVideoTrack
from gamestream.server import ActiveSession, SessionManager
from tests.test_live_settings import Source


def manager_with_source():
    manager = SessionManager(AppConfig(source=Path('unused')))
    manager.active = ActiveSession(SimpleNamespace(public=lambda: {}), False)
    source = Source()
    source.stats = SimpleNamespace(last_error='capture ended')
    manager.pipeline = SimpleNamespace(video=source)
    manager.pc = object()
    manager.video_track = SwitchableVideoTrack(source, manager._recover_video)
    return manager, source


@pytest.mark.asyncio
async def test_stall_is_detected_and_capture_stopped(monkeypatch):
    monkeypatch.setattr('gamestream.media.VIDEO_STALL_SECONDS', .01)
    track = EncodedVideoTrack(AppConfig(source=Path('unused')), False, start=False)
    with pytest.raises(MediaStreamError):
        await track.recv()
    assert track._stopped.is_set()
    assert 'Video stalled' in track.stats.last_error


@pytest.mark.asyncio
async def test_capture_exit_retries_and_preserves_sender_timestamps():
    manager, old = manager_with_source()
    proxy = manager.video_track
    old.queue.put_nowait(SimpleNamespace(pts=0, dts=0))
    first = await proxy.recv()
    old.queue.put_nowait(None)
    new = Source()
    new.queue.put_nowait(SimpleNamespace(pts=0, dts=0))
    with patch('gamestream.server.EncodedVideoTrack.create', new=AsyncMock(
        side_effect=[RuntimeError('PipeWire unavailable'), new]
    )) as create, patch('gamestream.server.asyncio.sleep', new=AsyncMock()) as delay:
        second = await asyncio.wait_for(proxy.recv(), 1)
    assert create.await_count == 2
    delay.assert_awaited_once_with(1)
    assert manager.pipeline.video is new
    assert manager.video_track is proxy
    assert old.stopped and not new.stopped
    assert second.pts > first.pts
    assert second.time_base.denominator == 90000
    assert manager.last_error == ''
    proxy.stop()


@pytest.mark.asyncio
async def test_obsolete_failure_does_not_replace_live_settings_source():
    manager, old = manager_with_source()
    new = Source()
    manager.pipeline.video = new
    manager.video_track.replace(new)
    with patch('gamestream.server.EncodedVideoTrack.create', new=AsyncMock()) as create:
        await manager._recover_video(old)
    create.assert_not_awaited()
    assert manager.pipeline.video is new
    manager.video_track.stop()


@pytest.mark.asyncio
async def test_disconnect_cancels_pending_recovery():
    manager, old = manager_with_source()
    old.queue.put_nowait(None)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def create(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch('gamestream.server.EncodedVideoTrack.create', new=create):
        pending = asyncio.create_task(manager.video_track.recv())
        await asyncio.wait_for(started.wait(), 1)
        manager.video_track.stop()
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(pending, 1)
    assert cancelled.is_set()
    assert not manager._lock.locked()


@pytest.mark.asyncio
async def test_session_stop_interrupts_recovery_holding_session_lock():
    manager, old = manager_with_source()
    old.queue.put_nowait(None)
    started = asyncio.Event()

    async def create(*args):
        started.set()
        await asyncio.Event().wait()

    manager._close_peer = AsyncMock()
    manager.launcher.stop = AsyncMock()
    with patch('gamestream.server.EncodedVideoTrack.create', new=create), patch(
        'gamestream.server.WARM_DLSS.clear'
    ):
        pending = asyncio.create_task(manager.video_track.recv())
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(manager.stop(), 1)
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(pending, 1)
    assert manager.active is None
    manager._close_peer.assert_awaited_once()
