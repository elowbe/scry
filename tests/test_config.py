from pathlib import Path
import tempfile
import unittest

from gamestream.config import load_config


class ConfigTests(unittest.TestCase):
    def test_stream_defaults_are_2k_60(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Path(directory) / "missing.toml")
            self.assertEqual((config.stream.width, config.stream.height, config.stream.fps), (2560, 1440, 60))

    def test_rejects_odd_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[stream]\nwidth = 2559\n")
            with self.assertRaisesRegex(ValueError, "must both be even"):
                load_config(path)

    def test_rejects_bitrate_ceiling_below_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[stream]\nbitrate_mbps = 50\nmax_bitrate_mbps = 40\n")
            with self.assertRaisesRegex(ValueError, "cannot be lower"):
                load_config(path)

    def test_dlss_target_fps_cannot_exceed_standard_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[stream]\nfps = 30\n[dlss]\ntarget_fps = 31\n")
            with self.assertRaisesRegex(ValueError, "cannot exceed"):
                load_config(path)
