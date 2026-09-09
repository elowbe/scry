"""Unreliable WebRTC data-channel protocol and Linux virtual input devices."""

from __future__ import annotations

import struct
import sys
import threading
from dataclasses import dataclass

from .config import InputConfig
from .errors import PreflightError

MSG_KEY = 0x01
MSG_MOUSE_MOVE = 0x02
MSG_MOUSE_BUTTON = 0x03
MSG_MOUSE_POSITION = 0x05
MSG_WHEEL = 0x04
MSG_GAMEPAD = 0x10
MSG_RELEASE_ALL = 0x7F

GAMEPAD_PACKET = struct.Struct("<BBBIhhhhHH")

# This order is part of the browser/host wire protocol. Append; never reorder.
DOM_CODES = (
    "Escape", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    "Backquote", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9", "Digit0", "Minus", "Equal", "Backspace",
    "Tab", "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI", "KeyO", "KeyP", "BracketLeft", "BracketRight", "Backslash",
    "CapsLock", "KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK", "KeyL", "Semicolon", "Quote", "Enter",
    "ShiftLeft", "KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM", "Comma", "Period", "Slash", "ShiftRight",
    "ControlLeft", "MetaLeft", "AltLeft", "Space", "AltRight", "MetaRight", "ContextMenu", "ControlRight",
    "Insert", "Delete", "Home", "End", "PageUp", "PageDown", "ArrowUp", "ArrowLeft", "ArrowDown", "ArrowRight",
    "NumLock", "NumpadDivide", "NumpadMultiply", "NumpadSubtract", "Numpad7", "Numpad8", "Numpad9", "NumpadAdd", "Numpad4", "Numpad5", "Numpad6", "Numpad1", "Numpad2", "Numpad3", "Numpad0", "NumpadDecimal", "NumpadEnter",
    "PrintScreen", "ScrollLock", "Pause", "AudioVolumeMute", "AudioVolumeDown", "AudioVolumeUp", "MediaTrackPrevious", "MediaPlayPause", "MediaTrackNext",
    "IntlBackslash", "IntlRo", "IntlYen", "Convert", "NonConvert", "KanaMode", "Lang1", "Lang2",
    "NumpadEqual", "NumpadComma", "BrowserBack", "BrowserForward", "BrowserRefresh", "BrowserHome",
    "LaunchMail", "LaunchApp1", "LaunchApp2", "MediaStop",
)


def decode_packet(payload: bytes) -> tuple:
    if not payload:
        raise ValueError("empty input packet")
    kind = payload[0]
    if kind == MSG_KEY and len(payload) == 4:
        down, code_id = struct.unpack_from("<BH", payload, 1)
        if code_id >= len(DOM_CODES):
            raise ValueError("unknown keyboard code id")
        return ("key", DOM_CODES[code_id], bool(down))
    if kind == MSG_MOUSE_MOVE and len(payload) == 5:
        return ("mouse_move", *struct.unpack_from("<hh", payload, 1))
    if kind == MSG_MOUSE_POSITION and len(payload) == 9:
        return ("mouse_position", *struct.unpack_from("<IHH", payload, 1))
    if kind == MSG_MOUSE_BUTTON and len(payload) == 3:
        button, down = struct.unpack_from("<BB", payload, 1)
        if button > 4:
            raise ValueError("unknown mouse button")
        return ("mouse_button", button, bool(down))
    if kind == MSG_WHEEL and len(payload) == 5:
        return ("wheel", *struct.unpack_from("<hh", payload, 1))
    if kind == MSG_GAMEPAD and len(payload) == GAMEPAD_PACKET.size:
        return ("gamepad", *GAMEPAD_PACKET.unpack(payload)[1:])
    if kind == MSG_RELEASE_ALL and len(payload) == 1:
        return ("release_all",)
    raise ValueError(f"invalid input packet type={kind:#x} length={len(payload)}")


@dataclass(slots=True)
class InputState:
    keys: set[int]
    mouse_buttons: set[int]
    gamepad_buttons: int = 0


class VirtualInput:
    """Three uinput devices: keyboard, relative mouse, and Xbox-like gamepad."""

    def __init__(self, config: InputConfig):
        self.config = config
        self._lock = threading.Lock()
        self.state = InputState(set(), set())
        self.keyboard = self.mouse = self.gamepad = None
        self._ecodes = None
        self.absolute_mouse = None

    def open(self) -> None:
        if not self.config.enabled:
            return
        try:
            from evdev import AbsInfo, UInput, ecodes
        except ImportError as exc:
            raise PreflightError("python-evdev is required for remote input") from exc
        try:
            keys = sorted({getattr(ecodes, self._evdev_name(code)) for code in DOM_CODES})
            self.keyboard = UInput(
                {ecodes.EV_KEY: keys},
                name="Scry - Server Virtual Keyboard",
                bustype=ecodes.BUS_USB,
                vendor=0x045E,
                product=0x028E,
                version=1,
            )
            self.mouse = UInput(
                {
                    ecodes.EV_KEY: [
                        ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE,
                        ecodes.BTN_SIDE, ecodes.BTN_EXTRA,
                    ],
                    ecodes.EV_REL: [
                        ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL,
                    ],
                },
                name="Scry - Server Virtual Mouse",
                bustype=ecodes.BUS_USB,
                vendor=0x045E,
                product=0x028E,
                version=1,
            )
            # An absolute-only mouse is classified separately by libinput;
            # adding ABS axes to the relative mouse makes some hosts ignore them.
            self.absolute_mouse = UInput(
                {ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
                 ecodes.EV_ABS: [(ecodes.ABS_X, AbsInfo(0, 0, 65535, 0, 0, 0)),
                                 (ecodes.ABS_Y, AbsInfo(0, 0, 65535, 0, 0, 0))]},
                name="Scry - Server Absolute Mouse", bustype=ecodes.BUS_USB,
                input_props=[ecodes.INPUT_PROP_POINTER],
            )
            pad_buttons = [
                ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_WEST, ecodes.BTN_NORTH,
                ecodes.BTN_TL, ecodes.BTN_TR, ecodes.BTN_SELECT, ecodes.BTN_START,
                ecodes.BTN_THUMBL, ecodes.BTN_THUMBR, ecodes.BTN_MODE,
                ecodes.BTN_DPAD_UP, ecodes.BTN_DPAD_DOWN,
                ecodes.BTN_DPAD_LEFT, ecodes.BTN_DPAD_RIGHT,
            ]
            stick = AbsInfo(0, -32768, 32767, 256, 1024, 0)
            trigger = AbsInfo(0, 0, 65535, 64, 0, 0)
            self.gamepad = UInput(
                {
                    ecodes.EV_KEY: pad_buttons,
                    ecodes.EV_ABS: [
                        (ecodes.ABS_X, stick), (ecodes.ABS_Y, stick),
                        (ecodes.ABS_RX, stick), (ecodes.ABS_RY, stick),
                        (ecodes.ABS_Z, trigger), (ecodes.ABS_RZ, trigger),
                    ],
                },
                name="Scry - Server Virtual Xbox 360 Controller",
                bustype=ecodes.BUS_USB,
                vendor=0x045E,
                product=0x028E,
                version=0x0114,
            )
        except (OSError, PermissionError) as exc:
            self.close()
            raise PreflightError(
                "Cannot create /dev/uinput devices. Install the supplied udev rule, "
                "load the uinput module, then sign out and in."
            ) from exc
        self._ecodes = ecodes

    @staticmethod
    def _evdev_name(code: str) -> str:
        special = {
            "Escape": "KEY_ESC", "Backquote": "KEY_GRAVE", "Minus": "KEY_MINUS",
            "Equal": "KEY_EQUAL", "Backspace": "KEY_BACKSPACE", "Tab": "KEY_TAB",
            "BracketLeft": "KEY_LEFTBRACE", "BracketRight": "KEY_RIGHTBRACE",
            "Backslash": "KEY_BACKSLASH", "CapsLock": "KEY_CAPSLOCK",
            "Semicolon": "KEY_SEMICOLON", "Quote": "KEY_APOSTROPHE", "Enter": "KEY_ENTER",
            "ShiftLeft": "KEY_LEFTSHIFT", "ShiftRight": "KEY_RIGHTSHIFT",
            "ControlLeft": "KEY_LEFTCTRL", "ControlRight": "KEY_RIGHTCTRL",
            "MetaLeft": "KEY_LEFTMETA", "MetaRight": "KEY_RIGHTMETA",
            "AltLeft": "KEY_LEFTALT", "AltRight": "KEY_RIGHTALT", "Space": "KEY_SPACE",
            "ContextMenu": "KEY_COMPOSE", "Insert": "KEY_INSERT", "Delete": "KEY_DELETE",
            "Home": "KEY_HOME", "End": "KEY_END", "PageUp": "KEY_PAGEUP",
            "PageDown": "KEY_PAGEDOWN", "ArrowUp": "KEY_UP", "ArrowLeft": "KEY_LEFT",
            "ArrowDown": "KEY_DOWN", "ArrowRight": "KEY_RIGHT", "NumLock": "KEY_NUMLOCK",
            "NumpadDivide": "KEY_KPSLASH", "NumpadMultiply": "KEY_KPASTERISK",
            "NumpadSubtract": "KEY_KPMINUS", "NumpadAdd": "KEY_KPPLUS",
            "NumpadDecimal": "KEY_KPDOT", "NumpadEnter": "KEY_KPENTER",
            "PrintScreen": "KEY_SYSRQ", "ScrollLock": "KEY_SCROLLLOCK", "Pause": "KEY_PAUSE",
            "AudioVolumeMute": "KEY_MUTE", "AudioVolumeDown": "KEY_VOLUMEDOWN",
            "AudioVolumeUp": "KEY_VOLUMEUP", "MediaTrackPrevious": "KEY_PREVIOUSSONG",
            "MediaPlayPause": "KEY_PLAYPAUSE", "MediaTrackNext": "KEY_NEXTSONG",
            "Comma": "KEY_COMMA", "Period": "KEY_DOT", "Slash": "KEY_SLASH",
            "IntlBackslash": "KEY_102ND", "IntlRo": "KEY_RO", "IntlYen": "KEY_YEN",
            "Convert": "KEY_HENKAN", "NonConvert": "KEY_MUHENKAN",
            "KanaMode": "KEY_KATAKANAHIRAGANA", "Lang1": "KEY_HANGEUL", "Lang2": "KEY_HANJA",
            "NumpadEqual": "KEY_KPEQUAL", "NumpadComma": "KEY_KPCOMMA",
            "BrowserBack": "KEY_BACK", "BrowserForward": "KEY_FORWARD",
            "BrowserRefresh": "KEY_REFRESH", "BrowserHome": "KEY_HOMEPAGE",
            "LaunchMail": "KEY_MAIL", "LaunchApp1": "KEY_PROG1", "LaunchApp2": "KEY_PROG2",
            "MediaStop": "KEY_STOPCD",
        }
        if code in special:
            return special[code]
        if code.startswith("Key"):
            return "KEY_" + code[3:].upper()
        if code.startswith("Digit"):
            return "KEY_" + code[5:]
        if code.startswith("Numpad") and code[6:].isdigit():
            return "KEY_KP" + code[6:]
        return "KEY_" + code.upper()

    def handle(self, payload: bytes) -> None:
        if not self.config.enabled:
            return
        event = decode_packet(payload)
        with self._lock:
            getattr(self, "_handle_" + event[0])(*event[1:])

    def _handle_key(self, code: str, down: bool) -> None:
        ecodes = self._ecodes
        key = getattr(ecodes, self._evdev_name(code))
        if down and key not in self.state.keys:
            self.keyboard.write(ecodes.EV_KEY, key, 1)
            self.state.keys.add(key)
        elif not down and key in self.state.keys:
            self.keyboard.write(ecodes.EV_KEY, key, 0)
            self.state.keys.remove(key)
        self.keyboard.syn()

    def _handle_mouse_move(self, dx: int, dy: int) -> None:
        ecodes = self._ecodes
        sensitivity = self.config.mouse_sensitivity
        self.mouse.write(ecodes.EV_REL, ecodes.REL_X, round(dx * sensitivity))
        self.mouse.write(ecodes.EV_REL, ecodes.REL_Y, round(dy * sensitivity))
        self.mouse.syn()

    def _handle_mouse_position(self, epoch: int, x: int, y: int) -> None:
        self.absolute_mouse.write(self._ecodes.EV_ABS, self._ecodes.ABS_X, x)
        self.absolute_mouse.write(self._ecodes.EV_ABS, self._ecodes.ABS_Y, y)
        self.absolute_mouse.syn()

    def _handle_mouse_button(self, button: int, down: bool) -> None:
        ecodes = self._ecodes
        buttons = [ecodes.BTN_LEFT, ecodes.BTN_MIDDLE, ecodes.BTN_RIGHT, ecodes.BTN_SIDE, ecodes.BTN_EXTRA]
        key = buttons[button]
        self.mouse.write(ecodes.EV_KEY, key, int(down))
        (self.state.mouse_buttons.add if down else self.state.mouse_buttons.discard)(key)
        self.mouse.syn()

    def _handle_wheel(self, dx: int, dy: int) -> None:
        ecodes = self._ecodes
        if dx:
            self.mouse.write(ecodes.EV_REL, ecodes.REL_HWHEEL, -max(-1, min(1, dx)))
        if dy:
            self.mouse.write(ecodes.EV_REL, ecodes.REL_WHEEL, -max(-1, min(1, dy)))
        self.mouse.syn()

    def _handle_gamepad(
        self, index: int, connected: int, buttons: int,
        lx: int, ly: int, rx: int, ry: int, lt: int, rt: int,
    ) -> None:
        del index
        ecodes = self._ecodes
        mapping = [
            ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_WEST, ecodes.BTN_NORTH,
            ecodes.BTN_TL, ecodes.BTN_TR, ecodes.BTN_SELECT, ecodes.BTN_START,
            ecodes.BTN_THUMBL, ecodes.BTN_THUMBR, ecodes.BTN_MODE,
            ecodes.BTN_DPAD_UP, ecodes.BTN_DPAD_DOWN, ecodes.BTN_DPAD_LEFT, ecodes.BTN_DPAD_RIGHT,
        ]
        # Standard Gamepad API bits 12-15 are D-pad; bit 16 is Guide.
        source_bits = [0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 16, 12, 13, 14, 15]
        next_mask = buttons if connected else 0
        for target, source in zip(mapping, source_bits):
            old, new = (self.state.gamepad_buttons >> source) & 1, (next_mask >> source) & 1
            if old != new:
                self.gamepad.write(ecodes.EV_KEY, target, new)
        for axis, value in (
            (ecodes.ABS_X, lx), (ecodes.ABS_Y, ly),
            (ecodes.ABS_RX, rx), (ecodes.ABS_RY, ry),
            (ecodes.ABS_Z, lt), (ecodes.ABS_RZ, rt),
        ):
            self.gamepad.write(ecodes.EV_ABS, axis, value if connected else 0)
        self.state.gamepad_buttons = next_mask
        self.gamepad.syn()

    def _handle_release_all(self) -> None:
        if self.keyboard:
            for key in tuple(self.state.keys):
                self.keyboard.write(self._ecodes.EV_KEY, key, 0)
            self.keyboard.syn()
        if self.mouse:
            for key in tuple(self.state.mouse_buttons):
                self.mouse.write(self._ecodes.EV_KEY, key, 0)
            self.mouse.syn()
        if self.gamepad:
            self._handle_gamepad(0, 0, 0, 0, 0, 0, 0, 0, 0)
        self.state.keys.clear()
        self.state.mouse_buttons.clear()

    def release_all(self) -> None:
        with self._lock:
            self._handle_release_all()

    def close(self) -> None:
        try:
            self.release_all()
        except Exception:
            pass
        for device in (self.gamepad, self.mouse, self.keyboard, self.absolute_mouse):
            if device:
                device.close()
        self.gamepad = self.mouse = self.keyboard = self.absolute_mouse = None

if sys.platform == "win32":
    from .windows_input import WindowsInput as VirtualInput
