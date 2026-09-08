import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from gamestream.pacing import pace_video
from gamestream.config import StreamConfig


@pytest.mark.asyncio
async def test_pacer_preserves_fragments_and_audio():
    send = AsyncMock()
    result = SimpleNamespace(payloads=[b'fragment'] * 20)
    sender = SimpleNamespace(_ssrc=42, transport=SimpleNamespace(_send_rtp=send),
                             _next_encoded_frame=AsyncMock(return_value=result))
    pace_video(sender, lambda: StreamConfig(pacing_ms=1))
    assert await sender._next_encoded_frame(None) is result
    packets = [b'\x80\x63' + bytes(6) + struct.pack('!I', 42) + bytes([i]) for i in range(20)]
    audio = b'\x80\x6f' + bytes(6) + struct.pack('!I', 43)
    for packet in packets:
        await sender.transport._send_rtp(packet)
    await sender.transport._send_rtp(audio)
    assert [call.args[0] for call in send.await_args_list] == packets + [audio]
