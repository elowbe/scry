"""Small, isolated adapter for aiortc's pre-encoded video sender.

aiortc emits every RTP fragment of a video frame in a burst. Spread fragments
over a bounded fraction of one frame interval, leaving audio/RTCP untouched.
"""
import asyncio
import struct
import time


def pace_video(sender, config_supplier):
    next_frame = sender._next_encoded_frame
    transport = sender.transport
    send = transport._send_rtp
    start = 0.0
    span = 0.0
    count = 0
    sent = 0

    async def frame(codec):
        nonlocal start, span, count, sent
        result = await next_frame(codec)
        config = config_supplier()
        count = len(result.payloads) if result else 0
        sent = 0
        span = min(config.pacing_ms / 1000, .75 / config.fps)
        start = time.monotonic()
        return result

    async def packet(data):
        nonlocal sent
        # RTP SSRC is bytes 8..11. RTCP uses the reserved packet-type range.
        if (len(data) >= 12 and not 192 <= data[1] <= 223
                and struct.unpack_from('!I', data, 8)[0] == sender._ssrc):
            if count and sent < count and sent % 8 == 0:
                delay = start + span * sent / count - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            sent += 1
        await send(data)

    sender._next_encoded_frame = frame
    transport._send_rtp = packet
