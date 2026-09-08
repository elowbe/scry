import struct
import unittest

from gamestream.input import DOM_CODES, GAMEPAD_PACKET, decode_packet


class InputProtocolTests(unittest.TestCase):
    def test_keyboard_packet(self):
        code_id = DOM_CODES.index("KeyW")
        self.assertEqual(decode_packet(struct.pack("<BBH", 1, 1, code_id)), ("key", "KeyW", True))

    def test_mouse_and_wheel_packets(self):
        self.assertEqual(decode_packet(struct.pack("<Bhh", 2, -12, 44)), ("mouse_move", -12, 44))
        self.assertEqual(decode_packet(struct.pack("<Bhh", 4, 0, -120)), ("wheel", 0, -120))

    def test_standard_gamepad_packet_is_fixed_size(self):
        packet = GAMEPAD_PACKET.pack(0x10, 0, 1, 0b101, -32768, 32767, 0, 1, 200, 65535)
        self.assertEqual(len(packet), 19)
        self.assertEqual(
            decode_packet(packet),
            ("gamepad", 0, 1, 0b101, -32768, 32767, 0, 1, 200, 65535),
        )

    def test_malformed_packet_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid input packet"):
            decode_packet(b"\x02\x00")
