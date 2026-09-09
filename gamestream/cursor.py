"""Read cursor appearance independently of client-authoritative mouse position."""
from __future__ import annotations

import base64
import ctypes as C
import io
from functools import lru_cache
import os
import sys


@lru_cache(maxsize=32)
def _cursor_png(rgba: bytes, width: int, height: int) -> str:
    from PIL import Image
    out = io.BytesIO()
    Image.frombytes('RGBA', (width, height), rgba).save(out, format='PNG', compress_level=1)
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


def png_cursor(rgba, width, height, hotspot=(0, 0)):
    if not (0 < width <= 384 and 0 < height <= 384) or len(rgba) != width * height * 4:
        raise ValueError('Invalid cursor bitmap')
    # Cache by pixels, never handle/id: games can change a bitmap in-place.
    # Animation frames are reused without repeating PNG compression.
    return dict(image=_cursor_png(bytes(rgba), width, height),
                image_width=width, image_height=height, hotspot=list(hotspot))


class X11Cursor:
    def __init__(self, display):
        class Image(C.Structure):
            _fields_ = [('x', C.c_short), ('y', C.c_short), ('width', C.c_ushort),
                        ('height', C.c_ushort), ('xhot', C.c_ushort), ('yhot', C.c_ushort),
                        ('serial', C.c_ulong), ('pixels', C.POINTER(C.c_ulong)),
                        ('atom', C.c_ulong), ('name', C.c_char_p)]
        self.x = C.CDLL('libX11.so.6')
        self.f = C.CDLL('libXfixes.so.3')
        self.x.XOpenDisplay.argtypes = [C.c_char_p]
        self.x.XOpenDisplay.restype = C.c_void_p
        self.x.XDefaultScreen.argtypes = [C.c_void_p]
        self.x.XDisplayWidth.argtypes = self.x.XDisplayHeight.argtypes = [C.c_void_p, C.c_int]
        self.x.XDefaultRootWindow.argtypes = [C.c_void_p]
        self.x.XDefaultRootWindow.restype = C.c_ulong
        self.x.XWarpPointer.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_int, C.c_int]
        self.x.XSync.argtypes = [C.c_void_p, C.c_int]
        self.x.XFree.argtypes = [C.c_void_p]
        self.x.XCloseDisplay.argtypes = [C.c_void_p]
        self.f.XFixesGetCursorImage.argtypes = [C.c_void_p]
        self.f.XFixesGetCursorImage.restype = C.POINTER(Image)
        self.display = self.x.XOpenDisplay(display.encode())
        if not self.display:
            raise RuntimeError('Cannot open X11 cursor display')
        self.serial = None
        self.shape = {}

    def sample(self):
        pointer = self.f.XFixesGetCursorImage(self.display)
        if not pointer:
            return None
        try:
            item = pointer.contents
            if not (0 < item.width <= 384 and 0 < item.height <= 384):
                return None
            if item.serial != self.serial:
                rgba = bytearray()
                for i in range(item.width * item.height):
                    pixel = item.pixels[i]
                    a = (pixel >> 24) & 255
                    # XFixes pixels are premultiplied ARGB.
                    rgba.extend([min(255, ((pixel >> shift) & 255) * 255 // a) if a else 0 for shift in (16, 8, 0)] + [a])
                self.shape = png_cursor(bytes(rgba), item.width, item.height, (item.xhot, item.yhot))
                self.shape['visible'] = any(rgba[3::4])
                self.serial = item.serial
            screen = self.x.XDefaultScreen(self.display)
            return dict(x=item.x, y=item.y, width=self.x.XDisplayWidth(self.display, screen),
                        height=self.x.XDisplayHeight(self.display, screen), **self.shape)
        finally:
            self.x.XFree(pointer)

    def position(self, x, y):
        screen = self.x.XDefaultScreen(self.display)
        width, height = self.x.XDisplayWidth(self.display, screen), self.x.XDisplayHeight(self.display, screen)
        self.x.XWarpPointer(self.display, 0, self.x.XDefaultRootWindow(self.display), 0, 0, 0, 0,
                            round(x * (width-1) / 65535), round(y * (height-1) / 65535))
        self.x.XSync(self.display, False)

    def close(self):
        if self.display:
            self.x.XCloseDisplay(self.display)
            self.display = None


def create_cursor(backend, display):
    if backend in {'gnome', 'pipewire'}:
        return None  # The capture process owns the compositor-authorized metadata.
    if sys.platform == 'win32':
        from .windows_cursor import WindowsCursor
        return WindowsCursor()
    if backend == 'x11grab' or (backend == 'kmsgrab' and os.environ.get('XDG_SESSION_TYPE') != 'wayland'):
        return X11Cursor(display)
    raise RuntimeError('This capture backend cannot provide cursor metadata; use GNOME or portal capture on Wayland')


class PointerSession:
    """Positions are client-owned; only external warps change their epoch."""
    def __init__(self, target, provider=None):
        self.target, self.provider = target, provider
        self.epoch = 0
        self.expected = None
        self.visible = True
        self.position_confirmed = False
        self.last_sample_id = None

    def handle(self, payload):
        from .input import decode_packet
        event = decode_packet(payload)
        if event[0] == 'mouse_position':
            _, epoch, x, y = event
            if epoch != self.epoch or not self.visible:
                return
            if self.provider and hasattr(self.provider, 'position') and self.target.config.enabled:
                self.provider.position(x, y)
            else:
                self.target.handle(payload)
            self.expected = (x / 65535, y / 65535)
            # X11/Windows setters complete synchronously; uinput does not.
            self.position_confirmed = self.provider is not None
        elif event[0] in ('mouse_move', 'mouse_button', 'wheel', 'release_all', 'key'):
            # Button-up/release are never discarded when the cursor mode changes.
            self.target.handle(payload)

    def update(self, sample):
        if not sample or sample.get('width', 0) <= 1 or sample.get('height', 0) <= 1:
            return None
        sample = dict(sample)
        x = max(0, min(1, sample['x'] / (sample['width'] - 1)))
        y = max(0, min(1, sample['y'] / (sample['height'] - 1)))
        visible = sample.get('visible', self.visible)
        changed = visible != self.visible
        # A cached or delayed position is not evidence of an application warp.
        # For asynchronous input, first observe the requested position, then a
        # *new* sample departing from it with no intervening client move.
        sample_id = sample.pop('_sample_id', None)
        fresh = self.provider is not None or (sample_id is not None and sample_id != self.last_sample_id)
        if sample_id is not None:
            self.last_sample_id = sample_id
        moved = self.expected is not None and max(abs(x-self.expected[0]), abs(y-self.expected[1])) > .002
        external = bool(fresh and self.position_confirmed and moved)
        reason = 'initial' if self.expected is None else 'visibility' if changed else 'external' if external else None
        warp = reason is not None
        if warp:
            self.epoch += 1
            self.expected = (x, y)
            self.position_confirmed = False
        elif fresh and self.expected is not None and not moved:
            self.position_confirmed = True
        self.visible = visible
        sample.update(type='cursor', epoch=self.epoch, warp=bool(warp), warp_reason=reason, x=x, y=y, visible=visible)
        return sample
