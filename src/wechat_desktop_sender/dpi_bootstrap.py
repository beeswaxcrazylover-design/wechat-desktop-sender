"""Choose this process's DPI policy before creating any Tk window.

PyAutoGUI 0.9.54 calls SetProcessDPIAware on its first Windows import. Choosing
the same system-aware policy at startup prevents that late import from changing
an already-created Tk interpreter's coordinate/font environment. This changes
only our process, never the user's display settings. Existing aware manifests
are respected. The capture worker's scoped per-monitor context stays separate.
"""
from __future__ import annotations

import sys


def initialize_process_dpi(*, user32=None, platform=None):
    if (sys.platform if platform is None else platform) != 'win32':
        return {'status': 'NOT_WINDOWS'}
    import ctypes
    if user32 is None:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        signatures = (
            ('GetThreadDpiAwarenessContext', [], ctypes.c_void_p),
            ('GetAwarenessFromDpiAwarenessContext', [ctypes.c_void_p], ctypes.c_int),
            ('SetProcessDPIAware', [], ctypes.c_int),
        )
        for name, args, result in signatures:
            function = getattr(user32, name)
            function.argtypes, function.restype = args, result

    def awareness():
        return user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext())

    before = awareness()
    if before in (1, 2):
        return {'status': 'ALREADY_AWARE', 'before': before, 'after': before}
    if before != 0:
        raise RuntimeError('DPI_CONTEXT_UNAVAILABLE')
    # Same policy/API as the installed sender dependency, called before Tk.
    returned = bool(user32.SetProcessDPIAware())
    after = awareness()
    if after not in (1, 2):
        raise RuntimeError('DPI_INITIALIZATION_FAILED')
    return {'status': 'INITIALIZED', 'before': before, 'after': after, 'api_returned': returned}
