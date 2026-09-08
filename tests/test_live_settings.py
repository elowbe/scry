import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gamestream.config import AppConfig
from gamestream.errors import PreflightError
from gamestream.media import MediaStreamError, SwitchableVideoTrack
from gamestream.quality import quality_config, quality_options
from gamestream.server import ActiveSession, SessionManager


class Source:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.stopped = False

    async def recv(self):
        packet = await self.queue.get()
        if packet is None:
            raise MediaStreamError
        return packet

    def stop(self):
        self.stopped = True
        self.queue.put_nowait(None)


def test_quality_preserves_framerate_and_lowers_work():
    config = AppConfig(source=Path('unused'))
    fastest = quality_config(config, 0).stream
    highest = quality_config(config, 4).stream
    assert fastest.height == 720 and highest.height == 1440
    assert fastest.fps == highest.fps == 60
    assert fastest.bitrate_mbps < highest.bitrate_mbps
    assert fastest.encoder_preset == 'p1'
    assert config.stream.height == 1440
    assert len(quality_options(config)) == 5
    for invalid in (-1, 5, True, '2', 2.5):
        with pytest.raises(ValueError):
            quality_config(config, invalid)


@pytest.mark.asyncio
async def test_video_switch_does_not_end_pending_sender_and_pts_increase():
    old, new = Source(), Source()
    track = SwitchableVideoTrack(old)
    old.queue.put_nowait(SimpleNamespace(pts=0, dts=0, time_base=None))
    first = await track.recv()
    pending = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    previous = track.replace(new)
    previous.stop()
    new.queue.put_nowait(SimpleNamespace(pts=0, dts=0, time_base=None))
    second = await asyncio.wait_for(pending, .2)
    assert second.pts > first.pts
    assert second.time_base.denominator == 90000
    track.stop()


@pytest.mark.asyncio
async def test_exclusive_filter_replacement_can_suspend_and_resume_sender():
    old = Source()
    track = SwitchableVideoTrack(old)
    pending = asyncio.create_task(track.recv())
    await asyncio.sleep(0)
    previous = track.suspend()
    previous.stop()
    await asyncio.sleep(0)
    assert not pending.done()
    new = Source()
    track.replace(new)
    new.queue.put_nowait(SimpleNamespace(pts=0, dts=0, time_base=None))
    await asyncio.wait_for(pending, .2)
    track.stop()


@pytest.mark.asyncio
async def test_failed_filter_enable_preserves_existing_stream():
    config = AppConfig(source=Path('unused'))
    manager = SessionManager(config)
    manager.active = ActiveSession(SimpleNamespace(public=lambda: {}), False)
    old = Source()
    manager.pipeline = SimpleNamespace(video=old)
    manager.video_track = SwitchableVideoTrack(old)
    manager.pc = object()
    with patch('gamestream.server.EncodedVideoTrack.create', new=AsyncMock(side_effect=RuntimeError('NGX unavailable'))):
        with pytest.raises(PreflightError, match='NGX unavailable'):
            await manager.update_settings(dlss_enabled=True)
    assert manager.pipeline.video is old and not old.stopped
    assert not manager.active.dlss_enabled
    manager.video_track.stop()
