"""Fresh window geometry only. No captures, keyboard, mouse or foreground calls."""
import ctypes as c
from ctypes import wintypes as w
from uuid import uuid4
from .app_paths import SCRATCH as RUNTIME
from .notice_geometry import PlacementGeometry
from .simple_core import ForwardError


def observe_geometry():
    from .native_win32 import NativePort,MONITORINFO
    directory=RUNTIME/'notice-geometry'/uuid4().hex
    directory.mkdir(parents=True,exist_ok=False)
    report={'api_calls':[]}
    port=NativePort(directory,report,authorized=True)
    old=port.u('SetThreadDpiAwarenessContext',c.c_void_p(-4),require=True)
    try:
        target=port.select_target()
        guarded=port.guard(target,foreground=False)
        protected=[tuple(guarded['bounds']),tuple(port.bounds(target['hwnd']))]
        for hwnd in port.windows():
            if not port.u('IsWindowVisible',hwnd) or port.u('IsIconic',hwnd):continue
            name=c.create_unicode_buffer(256)
            port.u('GetClassNameW',hwnd,name,len(name),require=True)
            if name.value in ('Shell_TrayWnd','Shell_SecondaryTrayWnd') or port.owner(hwnd)['pid']==target['pid']:
                box=tuple(port.bounds(hwnd))
                if box[0]<box[2] and box[1]<box[3]:protected.append(box)
        callback_type=c.WINFUNCTYPE(w.BOOL,w.HANDLE,w.HDC,c.POINTER(w.RECT),w.LPARAM)
        port.user.EnumDisplayMonitors.argtypes=[w.HDC,c.POINTER(w.RECT),callback_type,w.LPARAM]
        port.user.EnumDisplayMonitors.restype=w.BOOL
        areas=[];errors=[]
        @callback_type
        def monitor(handle,dc,rect,param):
            try:
                info=MONITORINFO();info.cbSize=c.sizeof(info)
                if not port.user.GetMonitorInfoW(handle,c.byref(info)):raise RuntimeError()
                areas.append((info.rcWork.left,info.rcWork.top,info.rcWork.right,info.rcWork.bottom))
                return True
            except Exception:errors.append(True);return False
        if not port.user.EnumDisplayMonitors(None,None,monitor,0) or errors or not areas:
            raise ForwardError('NOTICE_DISPLAY_GEOMETRY_UNAVAILABLE')
        return PlacementGeometry(tuple(areas),tuple(set(protected)))
    finally:
        port.u('SetThreadDpiAwarenessContext',old,require=True)
