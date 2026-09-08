import unittest

from gamestream.config import StreamConfig
from gamestream.media import (
    build_encoded_capture_command,
    build_raw_capture_command,
    build_raw_encoder_command,
)


class MediaCommandTests(unittest.TestCase):
    def test_nvenc_command_is_low_delay_and_color_tagged(self):
        command = build_encoded_capture_command(StreamConfig(capture_backend="kmsgrab", encoder="h264_nvenc"))
        joined = " ".join(command)
        self.assertIn("-tune ull", joined)
        self.assertIn("-bf 0", joined)
        self.assertIn("-zerolatency 1", joined)
        self.assertIn("-flush_packets 1", joined)
        self.assertIn("-colorspace bt709", joined)
        self.assertIn("-color_range tv", joined)
        self.assertEqual(command[-2:], ["h264", "pipe:1"])

    def test_dlss_capture_and_encode_are_raw_rgba(self):
        config = StreamConfig(fps=24, capture_backend="pipewire", encoder="h264_nvenc")
        capture = build_raw_capture_command(config, 1280, 720)
        encoder = build_raw_encoder_command(config, 2560, 1440)
        self.assertTrue(capture[1].endswith("portal_capture.py"))
        self.assertIn("rgba", encoder)
        self.assertEqual(capture[capture.index("--fps") + 1], "24")
        self.assertEqual(encoder[encoder.index("-framerate") + 1], "24")
        self.assertEqual(encoder[encoder.index("-preset") + 1], config.encoder_preset)
        self.assertEqual(encoder[encoder.index("-spatial-aq") + 1], "1")
        self.assertEqual(encoder[encoder.index("-video_size") + 1], "2560x1440")


class WaylandCaptureTests(unittest.TestCase):
    def test_auto_uses_portal_in_wayland_session(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'XDG_SESSION_TYPE': 'wayland'}, clear=True):
            command = build_raw_capture_command(StreamConfig(), 1280, 720)
            self.assertTrue(command[1].endswith('portal_capture.py'))
            self.assertIn('1280', command)
            encoded = build_encoded_capture_command(StreamConfig())
            self.assertIn('pipe:0', encoded)
            self.assertNotIn('x11grab', encoded)

    def test_auto_preserves_x11_on_x11_desktop(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'XDG_SESSION_TYPE': 'x11'}, clear=True):
            command = build_encoded_capture_command(StreamConfig())
            self.assertIn('x11grab', command)

    def test_explicit_x11_on_wayland_is_actionable_error(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'WAYLAND_DISPLAY': 'wayland-0'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'cannot reliably capture a Wayland'):
                build_encoded_capture_command(StreamConfig(capture_backend='x11grab'))


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_capture_reports_stderr_before_answer(self):
        import asyncio
        from unittest.mock import patch
        from gamestream.config import AppConfig
        from gamestream.media import EncodedVideoTrack
        from gamestream.errors import PreflightError
        from pathlib import Path
        with patch.object(EncodedVideoTrack, '_start'):
            track = EncodedVideoTrack(AppConfig(source=Path('unused')), False)
        track._logs.append('portal: screen sharing was cancelled (response 1)')
        track.stats.last_error = 'H.264 stream stopped'
        track._ready.set()
        try:
            with self.assertRaisesRegex(PreflightError, 'screen sharing was cancelled'):
                await track.wait_ready(.1)
        finally:
            track.stop()
            await asyncio.sleep(0)

    async def test_startup_timeout_is_bounded(self):
        import asyncio
        from unittest.mock import patch
        from gamestream.config import AppConfig
        from gamestream.media import EncodedVideoTrack
        from gamestream.errors import PreflightError
        from pathlib import Path
        with patch.object(EncodedVideoTrack, '_start'):
            track = EncodedVideoTrack(AppConfig(source=Path('unused')), False)
        try:
            with self.assertRaisesRegex(PreflightError, 'startup timed out'):
                await track.wait_ready(.01)
        finally:
            track.stop()
            await asyncio.sleep(0)


class GnomeCaptureTests(unittest.TestCase):
    def test_gnome_auto_uses_unattended_capture(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'}, clear=True):
            command = build_raw_capture_command(StreamConfig(), 1280, 720)
            self.assertEqual(command[command.index('--method') + 1], 'gnome')
            self.assertIn('pipe:0', build_encoded_capture_command(StreamConfig()))

    def test_explicit_portal_remains_portable(self):
        command = build_raw_capture_command(StreamConfig(capture_backend='pipewire'), 1280, 720)
        self.assertEqual(command[command.index('--method') + 1], 'portal')
