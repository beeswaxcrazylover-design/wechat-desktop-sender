"""Own-window-only non-activating Windows status banner. Import has no I/O.

No screenshots, target enumeration, foreground/focus APIs, mouse or keyboard
APIs. A native STATIC popup avoids Tk deiconify/activation behavior mid-send.
Every native operation restores the GUI thread's original DPI context.
"""
from contextlib import contextmanager
import ctypes as c
from ctypes import wintypes as w

WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
WS_BORDER = 0x00800000
SS_CENTER = 0x00000001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_SETFONT = 0x0030


class NativeBanner:
    def __init__(self, bounds, *, font_pixels=16, user32=None, gdi32=None):
        self.user = user32 or c.WinDLL('user32', use_last_error=True)
        self.gdi = gdi32 or c.WinDLL('gdi32', use_last_error=True)
        self.hwnd = self.font = None
        self.bounds = tuple(bounds)
        if user32 is None:
            signatures = (
                (self.user, 'SetThreadDpiAwarenessContext', [c.c_void_p], c.c_void_p),
                (self.user, 'CreateWindowExW', [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                    c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p], w.HWND),
                (self.user, 'SetWindowPos', [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT], w.BOOL),
                (self.user, 'SetWindowTextW', [w.HWND, w.LPCWSTR], w.BOOL),
                (self.user, 'SendMessageW', [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.LPARAM),
                (self.user, 'DestroyWindow', [w.HWND], w.BOOL),
                (self.gdi, 'CreateFontW', [c.c_int, c.c_int, c.c_int, c.c_int, c.c_int,
                    w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR], w.HANDLE),
                (self.gdi, 'DeleteObject', [w.HANDLE], w.BOOL),
            )
            for dll, name, args, result in signatures:
                function = getattr(dll, name)
                function.argtypes, function.restype = args, result
        try:
            with self._dpi():
                x, y, r, b = self.bounds
                self.hwnd = self.user.CreateWindowExW(
                    WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
                    'STATIC', '', WS_POPUP | WS_BORDER | SS_CENTER,
                    x, y, r-x, b-y, None, None, None, None)
                if not self.hwnd:
                    raise RuntimeError('NOTICE_WINDOW_CREATE_FAILED')
                self.font = self.gdi.CreateFontW(-font_pixels, 0, 0, 0, 600, 0, 0, 0,
                                               1, 0, 0, 5, 0, 'Microsoft YaHei UI')
                if not self.font:
                    raise RuntimeError('NOTICE_FONT_CREATE_FAILED')
                self.user.SendMessageW(self.hwnd, WM_SETFONT, self.font, 1)
        except BaseException:
            self.destroy()
            raise

    @contextmanager
    def _dpi(self):
        old = self.user.SetThreadDpiAwarenessContext(c.c_void_p(-4))
        if not old:
            raise RuntimeError('NOTICE_THREAD_DPI_FAILED')
        try:
            yield
        finally:
            if not self.user.SetThreadDpiAwarenessContext(old):
                raise RuntimeError('NOTICE_THREAD_DPI_RESTORE_FAILED')

    def show(self, text):
        self.set_text(text)
        self.move(self.bounds)

    def move(self, bounds):
        x, y, r, b = bounds
        with self._dpi():
            if not self.user.SetWindowPos(self.hwnd, c.c_void_p(-1), x, y, r-x, b-y,
                                           SWP_NOACTIVATE | SWP_SHOWWINDOW):
                raise RuntimeError('NOTICE_WINDOW_POSITION_FAILED')
        self.bounds = tuple(bounds)

    def set_text(self, text):
        with self._dpi():
            if not self.user.SetWindowTextW(self.hwnd, text):
                raise RuntimeError('NOTICE_WINDOW_TEXT_FAILED')

    def destroy(self):
        # Never delete a font while a surviving window still refers to it.
        with self._dpi():
            if self.hwnd:
                if not self.user.DestroyWindow(self.hwnd):
                    raise RuntimeError('NOTICE_WINDOW_DESTROY_FAILED')
                self.hwnd = None
            if self.font:
                if not self.gdi.DeleteObject(self.font):
                    raise RuntimeError('NOTICE_FONT_DELETE_FAILED')
                self.font = None
