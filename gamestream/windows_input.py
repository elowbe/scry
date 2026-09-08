"""Windows keyboard/mouse injection and an Xbox controller via ViGEmBus."""
import threading
from .errors import PreflightError


class WindowsInput:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.keyboard = self.mouse = self.gamepad = None
        self.keys = set()
        self.buttons = set()

    def open(self):
        if not self.config.enabled:
            return
        try:
            from pynput import keyboard, mouse
            import vgamepad
            self._keyboard_module, self._mouse_module = keyboard, mouse
            self.keyboard, self.mouse = keyboard.Controller(), mouse.Controller()
            self.gamepad = vgamepad.VX360Gamepad()
            self._pad_module = vgamepad
        except Exception as exc:
            self.close()
            raise PreflightError("Windows input could not start. Run setup to install ViGEmBus, then restart Windows: " + str(exc)) from exc

    def _key(self, code):
        # Scan codes preserve physical keyboard positions across host layouts.
        from .input import DOM_CODES
        scans = [1,59,60,61,62,63,64,65,66,67,68,87,88,
                 41,2,3,4,5,6,7,8,9,10,11,12,13,14,
                 15,16,17,18,19,20,21,22,23,24,25,26,27,43,
                 58,30,31,32,33,34,35,36,37,38,39,40,28,
                 42,44,45,46,47,48,49,50,51,52,53,54,
                 29,0xE05B,56,57,0xE038,0xE05C,0xE05D,0xE01D,
                 0xE052,0xE053,0xE047,0xE04F,0xE049,0xE051,0xE048,0xE04B,0xE050,0xE04D,
                 69,0xE035,55,74,71,72,73,78,75,76,77,79,80,81,82,83,0xE01C]
        index = DOM_CODES.index(code)
        if index < len(scans):
            import ctypes
            vk = ctypes.windll.user32.MapVirtualKeyW(scans[index], 3)
            if vk:
                return self._keyboard_module.KeyCode.from_vk(vk)
        virtual_keys = {
            "PrintScreen":0x2C, "ScrollLock":0x91, "Pause":0x13,
            "AudioVolumeMute":0xAD, "AudioVolumeDown":0xAE, "AudioVolumeUp":0xAF,
            "MediaTrackPrevious":0xB1, "MediaPlayPause":0xB3, "MediaTrackNext":0xB0,
            "IntlBackslash":0xE2, "IntlRo":0xE2, "IntlYen":0xDC,
            "Convert":0x1C, "NonConvert":0x1D, "KanaMode":0x15, "Lang1":0x15, "Lang2":0x19,
            "NumpadEqual":0x92, "NumpadComma":0x6C,
            "BrowserBack":0xA6, "BrowserForward":0xA7, "BrowserRefresh":0xA8, "BrowserHome":0xAC,
            "LaunchMail":0xB4, "LaunchApp1":0xB6, "LaunchApp2":0xB7, "MediaStop":0xB2,
        }
        vk = virtual_keys.get(code)
        return self._keyboard_module.KeyCode.from_vk(vk) if vk else None


    def handle(self, payload):
        if not self.config.enabled:
            return
        from .input import decode_packet
        event = decode_packet(payload)
        with self._lock:
            kind, *args = event
            if kind == "key":
                key = self._key(args[0])
                if key is not None:
                    (self.keyboard.press if args[1] else self.keyboard.release)(key)
                    (self.keys.add if args[1] else self.keys.discard)(key)
            elif kind == "mouse_move":
                self.mouse.move(*(round(v * self.config.mouse_sensitivity) for v in args))
            elif kind == "mouse_button":
                button = [self._mouse_module.Button.left, self._mouse_module.Button.middle,
                          self._mouse_module.Button.right, self._mouse_module.Button.x1,
                          self._mouse_module.Button.x2][args[0]]
                (self.mouse.press if args[1] else self.mouse.release)(button)
                (self.buttons.add if args[1] else self.buttons.discard)(button)
            elif kind == "wheel":
                self.mouse.scroll(*(-max(-1, min(1, v)) for v in args))
            elif kind == "gamepad":
                self._gamepad(*args)
            elif kind == "release_all":
                self._release_all()

    def _gamepad(self, index, connected, buttons, lx, ly, rx, ry, lt, rt):
        self.gamepad.reset()
        if connected:
            names = {0:"A",1:"B",2:"X",3:"Y",4:"LEFT_SHOULDER",5:"RIGHT_SHOULDER",
                     8:"BACK",9:"START",10:"LEFT_THUMB",11:"RIGHT_THUMB",12:"DPAD_UP",
                     13:"DPAD_DOWN",14:"DPAD_LEFT",15:"DPAD_RIGHT",16:"GUIDE"}
            for bit, name in names.items():
                if buttons & (1 << bit):
                    self.gamepad.press_button(getattr(self._pad_module.XUSB_BUTTON, "XUSB_GAMEPAD_" + name))
            self.gamepad.left_joystick(x_value=lx, y_value=max(-32768, min(32767, -ly)))
            self.gamepad.right_joystick(x_value=rx, y_value=max(-32768, min(32767, -ry)))
            self.gamepad.left_trigger(value=round(lt * 255 / 65535))
            self.gamepad.right_trigger(value=round(rt * 255 / 65535))
        self.gamepad.update()

    def _release_all(self):
        for key in self.keys:
            self.keyboard.release(key)
        for button in self.buttons:
            self.mouse.release(button)
        self.keys.clear()
        self.buttons.clear()
        if self.gamepad:
            self.gamepad.reset()
            self.gamepad.update()

    def release_all(self):
        with self._lock:
            self._release_all()

    def close(self):
        try:
            self.release_all()
        finally:
            self.keyboard = self.mouse = self.gamepad = None
