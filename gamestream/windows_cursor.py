"""Read Windows cursor state without changing the host's ShowCursor counter."""
import ctypes as C
from ctypes import wintypes as W
from .cursor import png_cursor


class CursorInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('flags', W.DWORD), ('cursor', W.HANDLE), ('position', W.POINT)]


class IconInfo(C.Structure):
    _fields_ = [('icon', W.BOOL), ('xhot', W.DWORD), ('yhot', W.DWORD), ('mask', W.HBITMAP), ('color', W.HBITMAP)]


class BitmapInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('width', W.LONG), ('height', W.LONG), ('planes', W.WORD),
                ('bits', W.WORD), ('compression', W.DWORD), ('image_size', W.DWORD),
                ('xppm', W.LONG), ('yppm', W.LONG), ('used', W.DWORD), ('important', W.DWORD)]


class WindowsCursor:
    def __init__(self):
        self.u, self.g = C.windll.user32, C.windll.gdi32
        self.u.GetCursorInfo.argtypes = [C.POINTER(CursorInfo)]
        self.u.GetIconInfo.argtypes = [W.HANDLE, C.POINTER(IconInfo)]
        self.u.DrawIconEx.argtypes = [W.HDC, C.c_int, C.c_int, W.HANDLE, C.c_int, C.c_int, W.UINT, W.HBRUSH, W.UINT]
        self.g.CreateCompatibleDC.argtypes = [W.HDC]
        self.g.CreateCompatibleDC.restype = W.HDC
        self.g.CreateDIBSection.argtypes = [W.HDC, C.POINTER(BitmapInfo), W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD]
        self.g.CreateDIBSection.restype = W.HBITMAP
        self.g.SelectObject.argtypes = [W.HDC, W.HANDLE]
        self.g.SelectObject.restype = W.HANDLE
        self.g.DeleteObject.argtypes = [W.HANDLE]
        self.g.DeleteDC.argtypes = [W.HDC]
        self.g.GetObjectW.argtypes = [W.HANDLE, C.c_int, C.c_void_p]

    def sample(self):
        cursor = CursorInfo(size=C.sizeof(CursorInfo))
        if not self.u.GetCursorInfo(C.byref(cursor)):
            return None
        result = dict(x=cursor.position.x - self.u.GetSystemMetrics(76), y=cursor.position.y - self.u.GetSystemMetrics(77),
                      width=self.u.GetSystemMetrics(78), height=self.u.GetSystemMetrics(79),
                      visible=bool(cursor.flags & 1))
        if not result['visible']:
            return result
        icon = IconInfo()
        if not self.u.GetIconInfo(cursor.cursor, C.byref(icon)):
            return None
        class Bitmap(C.Structure):
            _fields_ = [('type', W.LONG), ('width', W.LONG), ('height', W.LONG), ('stride', W.LONG),
                        ('planes', W.WORD), ('bits', W.WORD), ('pixels', C.c_void_p)]
        try:
            bitmap = Bitmap()
            if not self.g.GetObjectW(icon.color or icon.mask, C.sizeof(bitmap), C.byref(bitmap)):
                return None
            width, height = bitmap.width, bitmap.height if icon.color else bitmap.height // 2
            if not (0 < width <= 384 and 0 < height <= 384):
                return None
            dc = self.g.CreateCompatibleDC(None)
            bits = C.c_void_p()
            info = BitmapInfo(size=C.sizeof(BitmapInfo), width=width, height=-height, planes=1, bits=32)
            dib = self.g.CreateDIBSection(dc, C.byref(info), 0, C.byref(bits), None, 0)
            if not dib:
                self.g.DeleteDC(dc)
                return None
            old = self.g.SelectObject(dc, dib)
            try:
                # Render on black and white to recover alpha for legacy AND/XOR cursors too.
                size = width * height * 4
                C.memset(bits, 0, size)
                self.u.DrawIconEx(dc, 0, 0, cursor.cursor, width, height, 0, None, 3)
                black = C.string_at(bits, size)
                C.memset(bits, 255, size)
                self.u.DrawIconEx(dc, 0, 0, cursor.cursor, width, height, 0, None, 3)
                white = C.string_at(bits, size)
                rgba = bytearray()
                for offset in range(0, size, 4):
                    alpha = 255 - max(0, max(white[offset+i] - black[offset+i] for i in range(3)))
                    rgba.extend([min(255, black[offset+i] * 255 // alpha) if alpha else 0 for i in (2, 1, 0)] + [alpha])
                result.update(png_cursor(bytes(rgba), width, height, (icon.xhot, icon.yhot)))
            finally:
                self.g.SelectObject(dc, old)
                self.g.DeleteObject(dib)
                self.g.DeleteDC(dc)
        finally:
            for handle in (icon.color, icon.mask):
                if handle:
                    self.g.DeleteObject(handle)
        return result

    def close(self):
        pass
