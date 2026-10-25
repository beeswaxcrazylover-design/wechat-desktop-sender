"""Original bounded GDI rectangle capture. No historical PID/HWND or probe entry point."""
import ctypes
from ctypes import wintypes as w


class Native:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = [
            (self.user, "SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p),
            (self.user, "GetThreadDpiAwarenessContext", [], ctypes.c_void_p),
            (self.user, "GetAwarenessFromDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_int),
            (self.user, "GetDpiForWindow", [w.HWND], w.UINT),
            (self.user, "GetWindowDisplayAffinity", [w.HWND, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.user, "GetDC", [w.HWND], w.HDC),
            (self.user, "ReleaseDC", [w.HWND, w.HDC], ctypes.c_int),
            (self.gdi, "CreateCompatibleDC", [w.HDC], w.HDC),
            (self.gdi, "CreateCompatibleBitmap", [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
            (self.gdi, "SelectObject", [w.HDC, w.HANDLE], w.HANDLE),
            (self.gdi, "BitBlt", [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD], w.BOOL),
            (self.gdi, "GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT], ctypes.c_int),
            (self.gdi, "DeleteObject", [w.HANDLE], w.BOOL),
            (self.gdi, "DeleteDC", [w.HDC], w.BOOL),
            (self.kernel, "ProcessIdToSessionId", [w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.kernel, "WTSGetActiveConsoleSessionId", [], w.DWORD),
        ]
        for module, name, arguments, result in signatures:
            function = getattr(module, name)
            function.argtypes, function.restype = arguments, result
        ctypes.set_last_error(0)
        self.old_dpi = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not self.old_dpi:
            raise ctypes.WinError(ctypes.get_last_error())

    def copy_rectangle_once(self, bounds):
        # Only this rectangle is allocated and copied. No full-screen image or
        # Pillow bbox fallback (which would capture then crop) is used.
        from PIL import Image
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ValueError("Invalid target rectangle")
        screen = memory = bitmap = previous = None
        try:
            ctypes.set_last_error(0)
            screen = self.user.GetDC(None)
            if not screen:
                raise ctypes.WinError(ctypes.get_last_error())
            memory = self.gdi.CreateCompatibleDC(screen)
            if not memory:
                raise ctypes.WinError(ctypes.get_last_error())
            bitmap = self.gdi.CreateCompatibleBitmap(screen, width, height)
            if not bitmap:
                raise ctypes.WinError(ctypes.get_last_error())
            previous = self.gdi.SelectObject(memory, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.set_last_error(0)
            if not self.gdi.BitBlt(memory, 0, 0, width, height, screen, left, top, 0x00CC0020):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.gdi.SelectObject(memory, previous):
                raise ctypes.WinError(ctypes.get_last_error())
            previous = None

            class Header(ctypes.Structure):
                _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG),
                            ("planes", w.WORD), ("bit_count", w.WORD), ("compression", w.DWORD),
                            ("size_image", w.DWORD), ("xppm", w.LONG), ("yppm", w.LONG),
                            ("colors_used", w.DWORD), ("colors_important", w.DWORD)]
            header = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, width*height*4, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width*height*4)
            ctypes.set_last_error(0)
            rows = self.gdi.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(header), 0)
            if rows != height:
                raise OSError(ctypes.get_last_error(), "Incomplete target rectangle scanlines")
            image = Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
            return image, {"source_rectangle": bounds, "allocated_bitmap_size": [width, height],
                           "bitblt_calls": 1, "scanlines_received": rows, "expected_scanlines": height,
                           "full_desktop_image_created": False, "raster_operation": "SRCCOPY"}
        finally:
            if previous and memory:
                self.gdi.SelectObject(memory, previous)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if memory:
                self.gdi.DeleteDC(memory)
            if screen:
                self.user.ReleaseDC(None, screen)
