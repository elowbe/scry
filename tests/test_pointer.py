import asyncio
import io
import json
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gamestream.cursor import PointerSession, png_cursor
from gamestream.input import VirtualInput, decode_packet
from gamestream.config import InputConfig, AppConfig


def position(epoch, x, y):
    return struct.pack('<BIHH', 5, epoch, x, y)


def sample(x=50, y=50, visible=True):
    return dict(x=x, y=y, width=101, height=101, visible=visible)


def test_absolute_decode_and_extremes():
    assert decode_packet(position(4, 0, 65535)) == ('mouse_position', 4, 0, 65535)
    with pytest.raises(ValueError):
        decode_packet(position(4, 1, 2)[:-1])


def test_linux_absolute_coordinates_ignore_relative_sensitivity():
    host = VirtualInput(InputConfig(mouse_sensitivity=9))
    host.absolute_mouse = Mock()
    host._ecodes = SimpleNamespace(EV_ABS=3, ABS_X=0, ABS_Y=1)
    host.handle(position(0, 1234, 5678))
    assert [c.args for c in host.absolute_mouse.write.call_args_list] == [(3, 0, 1234), (3, 1, 5678)]
    host.absolute_mouse.syn.assert_called_once()


def test_drag_moves_do_not_release_buttons():
    target = Mock(config=InputConfig())
    session = PointerSession(target)
    session.handle(position(0, 100, 100))
    session.handle(b'\x03\x00\x01')
    session.handle(position(0, 500, 800))
    session.handle(b'\x03\x00\x00')
    assert [c.args[0] for c in target.handle.call_args_list] == [
        position(0, 100, 100), b'\x03\x00\x01', position(0, 500, 800), b'\x03\x00\x00']


def test_game_warp_rejects_queued_position_but_keeps_button_up():
    target = Mock(config=InputConfig())
    session = PointerSession(target, provider=Mock())
    session.handle(position(0, 65535, 65535))
    update = session.update(sample())  # host game reset to center
    assert update['warp'] and update['epoch'] == 1 and update['x'] == .5
    session.handle(position(0, 65535, 0))
    session.provider.position.assert_called_once_with(65535, 65535)
    session.handle(b'\x03\x00\x00')
    target.handle.assert_called_once_with(b'\x03\x00\x00')


def test_image_updates_do_not_change_client_position_epoch():
    session = PointerSession(Mock(config=InputConfig()), provider=Mock())
    first = session.update(sample())
    second = session.update(dict(sample(), image='different'))
    assert second['epoch'] == first['epoch']
    assert second['warp'] is False


def test_hidden_game_cursor_keeps_relative_input():
    target = Mock(config=InputConfig())
    session = PointerSession(target)
    update = session.update(sample(visible=False))
    session.handle(position(update['epoch'], 500, 500))
    session.handle(struct.pack('<Bhh', 2, 80, -20))
    target.handle.assert_called_once_with(struct.pack('<Bhh', 2, 80, -20))
    shown = session.update(sample(visible=True))
    assert shown['warp'] and shown['epoch'] > update['epoch']


def test_capture_consumes_cursor_messages_without_logging_as_video_error():
    from gamestream.media import EncodedVideoTrack
    track = object.__new__(EncodedVideoTrack)
    update = dict(sample(), rgba_hex='ff000080', image_width=1, image_height=1, hotspot=[0, 0])
    track._log_loop(io.BytesIO(('SCRY_CURSOR '+json.dumps(update)+'\n').encode()), 'test')
    assert track.cursor_state['image'].startswith('data:image/png;base64,')
    assert track.cursor_state['x'] == 50


@pytest.mark.asyncio
async def test_pointer_loop_sends_image_before_state(tmp_path, monkeypatch):
    from gamestream.server import SessionManager
    manager = SessionManager(AppConfig(source=tmp_path/'config.toml'))
    manager.pc = pc = SimpleNamespace(connectionState='connected')
    manager.pipeline = SimpleNamespace(video=SimpleNamespace(cursor_state=dict(sample(),
        **png_cursor(b'\xff\x00\x00\xff',1,1))))
    monkeypatch.setattr('gamestream.server.create_cursor', lambda *_: None)
    sent = []
    def send(payload):
        sent.append(json.loads(payload))
        if sent[-1]['type'] == 'cursor':
            pc.connectionState = 'closed'
    channel = SimpleNamespace(readyState='open', bufferedAmount=0, send=send)
    await asyncio.wait_for(manager._pointer_loop(pc,channel,PointerSession(manager.input)),1)
    assert [m['type'] for m in sent] == ['cursor_image','cursor']
    assert sent[1]['image_id'] == sent[0]['id']


def test_windows_absolute_position_handles_negative_virtual_desktop(monkeypatch):
    import ctypes
    from gamestream.windows_input import WindowsInput
    metrics = {76:-1920, 77:0, 78:3840, 79:1080}
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=SimpleNamespace(GetSystemMetrics=metrics.__getitem__)), raising=False)
    host = WindowsInput(InputConfig(mouse_sensitivity=20))
    host.mouse = SimpleNamespace(position=None)
    host.handle(position(0, 0, 65535))
    assert host.mouse.position == (-1920, 1079)
    host.handle(position(0, 65535, 0))
    assert host.mouse.position == (1919, 0)


@pytest.mark.parametrize('cursor_only,method,mode', [(False,'RecordArea',0),(True,'RecordMonitor',2)])
def test_gnome_video_and_cursor_use_separate_stream_modes(cursor_only, method, mode):
    from gamestream.gnome_capture import start
    from tests.test_gnome_capture import display_state
    class Bus:
        def __init__(self): self.calls=[]
        def call_sync(self, *args):
            self.calls.append(args)
            name=args[3]
            value=display_state() if name=='GetCurrentState' else ('/session',) if name=='CreateSession' else ('/stream',)
            if name=='Start': self.callback(None,None,None,None,None,SimpleNamespace(unpack=lambda:(42,)))
            return SimpleNamespace(unpack=lambda:value)
        def signal_subscribe(self,*args): self.callback=args[-1]; return 1
        def signal_unsubscribe(self,*args): pass
    bus=Bus()
    loop=SimpleNamespace(quit=lambda:None,run=lambda:None)
    glib=SimpleNamespace(Variant=lambda signature,value:value,MainLoop=lambda:loop,timeout_add_seconds=lambda *args:1,
        MainContext=SimpleNamespace(default=lambda:SimpleNamespace(find_source_by_id=lambda _:False)))
    gio=SimpleNamespace(DBusCallFlags=SimpleNamespace(NONE=0),DBusSignalFlags=SimpleNamespace(NONE=0))
    result=start(bus,gio,glib,cursor_only=cursor_only)
    assert result[1]==42
    recorded=next(call for call in bus.calls if call[3]==method)
    assert recorded[4][-1]['cursor-mode']==mode


def test_stale_wayland_position_never_resets_cursor_after_pause():
    session = PointerSession(Mock(config=InputConfig()))
    session.update(dict(sample(25, 25), _sample_id=1))
    epoch = session.epoch
    session.handle(position(epoch, 52428, 52428))
    for ident in [1, 1, 2, 3, 3, 4]:
        update = session.update(dict(sample(25, 25), _sample_id=ident))
        assert not update['warp']
        assert update['epoch'] == epoch
    assert session.expected == (.8, .8)


def test_wayland_recenter_requires_confirmed_position_and_fresh_sample():
    session = PointerSession(Mock(config=InputConfig()))
    session.handle(position(0, 52428, 52428))
    assert not session.update(dict(sample(80, 80), _sample_id=10))['warp']
    assert not session.update(dict(sample(50, 50), _sample_id=10))['warp']
    update = session.update(dict(sample(50, 50), _sample_id=11))
    assert update['warp_reason'] == 'external'
    assert update['epoch'] == 1
    assert not session.update(dict(sample(50, 50), _sample_id=11))['warp']


def test_new_client_move_cancels_wayland_warp_confirmation():
    session = PointerSession(Mock(config=InputConfig()))
    session.handle(position(0, 52428, 52428))
    session.update(dict(sample(80, 80), _sample_id=10))
    session.handle(position(0, 65535, 65535))
    assert not session.update(dict(sample(85, 85), _sample_id=11))['warp']


def test_cursor_cache_tracks_pixels_not_handle_or_hotspot():
    from gamestream.cursor import _cursor_png
    _cursor_png.cache_clear()
    red = png_cursor(b'\xff\x00\x00\xff', 1, 1)
    again = png_cursor(b'\xff\x00\x00\xff', 1, 1, (1, 1))
    blue = png_cursor(b'\x00\x00\xff\xff', 1, 1)
    assert red['image'] == again['image']
    assert again['hotspot'] == [1, 1]
    assert blue['image'] != red['image']
    assert _cursor_png.cache_info().hits == 1
    assert _cursor_png.cache_info().misses == 2


def test_large_cursor_message_uses_buffered_pipe_reads():
    from gamestream.media import EncodedVideoTrack
    class Pipe(io.RawIOBase):
        def __init__(self, data):
            self.data = io.BytesIO(data)
            self.reads = 0
        def readable(self): return True
        def readinto(self, buffer):
            self.reads += 1
            data = self.data.read(len(buffer))
            buffer[:len(data)] = data
            return len(data)
    update = dict(sample(), rgba_hex='ff0000ff' * 64 * 64,
                  image_width=64, image_height=64, hotspot=[0, 0])
    pipe = Pipe(('SCRY_CURSOR '+json.dumps(update)+'\n').encode())
    track = object.__new__(EncodedVideoTrack)
    track._log_loop(pipe, 'test')
    assert pipe.reads <= 3
    assert track.cursor_state['image_width'] == 64


@pytest.mark.asyncio
async def test_wayland_cursor_event_sends_latest_shape_after_congestion(tmp_path, monkeypatch):
    from gamestream.server import SessionManager
    manager = SessionManager(AppConfig(source=tmp_path/'config.toml'))
    manager.pc = pc = SimpleNamespace(connectionState='connected')
    updated = asyncio.Event()
    updated.set()
    video = SimpleNamespace(cursor_updated=updated, cursor_state=dict(sample(),
        **png_cursor(b'\xff\x00\x00\xff', 1, 1)))
    manager.pipeline = SimpleNamespace(video=video)
    monkeypatch.setattr('gamestream.server.create_cursor', lambda *_: None)
    sent = []
    def send(payload):
        sent.append(json.loads(payload))
        if sent[-1]['type'] == 'cursor': pc.connectionState = 'closed'
    channel = SimpleNamespace(readyState='open', bufferedAmount=1, send=send)
    task = asyncio.create_task(manager._pointer_loop(pc, channel, PointerSession(manager.input)))
    await asyncio.sleep(.01)
    assert sent == []
    latest = png_cursor(b'\x00\x00\xff\xff', 1, 1)
    video.cursor_state = dict(sample(), **latest)
    channel.bufferedAmount = 0
    updated.set()
    await asyncio.wait_for(task, 1)
    assert sent[0]['data'] == latest['image']
    assert sent[-1]['type'] == 'cursor'
    assert not updated.is_set()
