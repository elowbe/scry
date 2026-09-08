"""Opt-in real desktop -> WebRTC -> decoded video integration probe."""
import argparse
import asyncio
from unittest.mock import patch
import logging
import os
from pathlib import Path
import time

from aiortc import RTCPeerConnection, RTCConfiguration
from aiortc.jitterbuffer import JitterBuffer
from gamestream.config import load_config
from gamestream.server import SessionManager, ActiveSession
from gamestream.dlss import WARM_DLSS


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unresolved-mdns", action="store_true")
    parser.add_argument("--live-settings", action="store_true")
    args = parser.parse_args()
    if args.live_settings:
        os.environ["GAMESTREAM_DLSS_PREFIX"] = str(Path(".state/dlss-test-prefix").resolve())
    logging.basicConfig(level=logging.WARNING)
    manager = SessionManager(load_config())
    manager.settings_file = Path('.state/probe-player-settings.json')
    manager.settings_override = {}
    manager.quality_level = 4
    manager.active = ActiveSession(manager.catalog.all()[0], False)
    client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    received = asyncio.Queue()
    reader = None
    client.on("track", lambda track: received.put_nowait(track))
    receiver = client.addTransceiver("video", direction="recvonly").receiver
    # aiortc's default 128 packets cannot hold a detailed 1440p IDR.
    receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(capacity=2048, is_video=True)
    client.addTransceiver("audio", direction="recvonly")
    client.createDataChannel("input", ordered=False, maxRetransmits=0)
    try:
        await client.setLocalDescription(await client.createOffer())
        if args.unresolved_mdns:
            # Model a remote browser whose multicast DNS names cannot resolve.
            # These sockets are local solely for the integration test.
            lines = client.localDescription.sdp.splitlines()
            first_address = next(line.split()[4] for line in lines if line.startswith("a=candidate:"))
            for i, line in enumerate(lines):
                if line.startswith("a=candidate:"):
                    parts = line.split()
                    parts[4] = "unresolvable-gamestream-test.local"
                    lines[i] = " ".join(parts)
            with patch("gamestream.network.directly_connected_peer", return_value=first_address):
                answer = await manager.attach("\r\n".join(lines) + "\r\n", "offer", first_address)
            assert manager.ice_diagnostics["lan_fallbacks"] > 0
            print("UNRESOLVED MDNS FALLBACK", manager.ice_diagnostics, flush=True)
        else:
            answer = await manager.attach(client.localDescription.sdp, "offer")
        print("ANSWER VIDEO", [line for line in answer.sdp.splitlines() if "fmtp" in line], flush=True)
        await client.setRemoteDescription(answer)
        tracks = [await received.get(), await received.get()]
        video = next(track for track in tracks if track.kind == "video")
        decoded = asyncio.Queue(maxsize=2)
        async def drain_video():
            while True:
                frame = await video.recv()
                if decoded.full():
                    decoded.get_nowait()
                decoded.put_nowait(frame)
        reader = asyncio.create_task(drain_video())
        start = time.monotonic()
        for i in range(120):
            frame = await asyncio.wait_for(decoded.get(), 12)
            assert (frame.width, frame.height) == (2560, 1440)
            if i in (0, 119):
                print("DECODED", i, frame.width, frame.height, time.monotonic() - start, flush=True)
        if args.live_settings:
            peer = manager.pc
            for quality, dlss in [(4, True), (4, False), (4, True), (0, True), (2, True), (2, False)]:
                print("APPLY", quality, dlss, flush=True)
                change_started = time.monotonic()
                result = await manager.update_settings(quality=quality, dlss_enabled=dlss)
                print('SETTINGS SECONDS', round(time.monotonic() - change_started, 3), flush=True)
                assert manager.pc is peer and peer.connectionState == "connected"
                expected = (result["quality_settings"]["width"], result["quality_settings"]["height"])
                good = 0
                deadline = time.monotonic() + 20
                while good < 15 and time.monotonic() < deadline:
                    frame = await asyncio.wait_for(decoded.get(), 10)
                    if (frame.width, frame.height) == expected:
                        good += 1
                assert good == 15, f"No decoded frames at {expected} after settings change"
                started = time.monotonic()
                for _ in range(120):
                    await asyncio.wait_for(decoded.get(), 10)
                print('DECODED FPS', round(120 / (time.monotonic() - started), 2), flush=True)
                print("LIVE SETTINGS DECODED", expected, "DLSS", result["dlss_enabled"], "filtered", result["pipeline"]["frames_filtered"], flush=True)
            result = await manager.update_settings(dlss_enabled=True, settings={
                'stream': {'width': 1280, 'height': 720, 'fps': 30,
                           'bitrate_mbps': 0, 'max_bitrate_mbps': 0, 'cq': 16},
                'dlss': {'upscaling_factor': 1.5, 'target_fps': 30,
                         'nr_style': 'Cinematic', 'nr_preset': 'Preset #1',
                         'nr_intensity': .8, 'automatic_mask': False},
            })
            assert result['settings']['stream']['max_bitrate_mbps'] == 0
            for _ in range(60):
                frame = await asyncio.wait_for(decoded.get(), 10)
            assert (frame.width, frame.height) == (1280, 720)
            print('CUSTOM SETTINGS DECODED: unlimited VBR, 30 fps, Quality, Cinematic, Preset #1', flush=True)
    finally:
        if reader:
            reader.cancel()
        await manager._close_peer()
        await asyncio.to_thread(WARM_DLSS.clear)
        await client.close()
        await asyncio.sleep(.2)


if __name__ == "__main__":
    asyncio.run(main())
