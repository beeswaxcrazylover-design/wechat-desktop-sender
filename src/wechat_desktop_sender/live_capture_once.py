"""Original application-caption and native observation helpers."""
from __future__ import annotations
import ctypes
from ctypes import wintypes as w
import json
import os
import time
from .diagnostics import store_report

APPLICATION_TITLES = frozenset({"微信", "WeChat", "Weixin"})


OBSERVED_WINDOW_CLASSES = frozenset({"Qt51514QWindowIcon"})


class Stop(Exception):
    pass


def application_title_metadata(text):
    """Application caption only. Never infer text from its length or Qt class."""
    known = text in APPLICATION_TITLES
    return {"title_length": len(text), "application_title_match": known,
            "title": text if known else None,
            "title_state": "EXACT_APPLICATION" if known else "EMPTY" if not text else "OTHER_REDACTED"}


def bind_selected_application(snapshot, *, human_confirmed):
    """Short-lived user-selected window binding, not account or chat identity."""
    if human_confirmed is not True:
        raise Stop("STOP_HUMAN_SELECTED_WORK_ACCOUNT_REQUIRED")
    if (snapshot.get("title_state") != "EXACT_APPLICATION" or
            snapshot.get("application_title_match") is not True or snapshot.get("title") not in APPLICATION_TITLES):
        raise Stop("STOP_APPLICATION_CAPTION_UNCONFIRMED")
    if snapshot.get("class_name") not in OBSERVED_WINDOW_CLASSES:
        raise Stop("STOP_APPLICATION_WINDOW_CLASS_UNCONFIRMED")
    if any(type(snapshot.get(key)) is not int or snapshot[key] <= 0 for key in ("hwnd", "pid", "thread_id")):
        raise Stop("STOP_WINDOW_OWNER_METADATA_INCOMPLETE")
    return dict(snapshot)


def require_same_selected_window(before, after):
    if after != before:
        raise Stop("STOP_SELECTED_WINDOW_CHANGED")


def save(path, value):
    if path.name == "result.json":
        store_report(path, value)
        return
    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


class Probe:
    """Only the selected foreground window, no process-name enumeration."""

    def __init__(self, folder, report):
        self.folder, self.report = folder, report
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        signatures = [
            (self.user, "GetForegroundWindow", [], w.HWND),
            (self.user, "GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.user, "GetWindowTextLengthW", [w.HWND], ctypes.c_int),
            (self.user, "GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "GetClassNameW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.user, "GetClientRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            (self.user, "ClientToScreen", [w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
            (self.user, "GetWindowDisplayAffinity", [w.HWND, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.user, "GetDpiForWindow", [w.HWND], w.UINT),
        ]
        for module, name, args, result in signatures:
            fn = getattr(module, name)
            fn.argtypes, fn.restype = args, result

    def call(self, module, name, args, *, arguments=None, error_semantics="failure_only"):
        # Keep these three lines adjacent: capture ctypes' saved Win32 error
        # before time/formatting/logging, another API, or CloseHandle can overwrite it.
        ctypes.set_last_error(0)
        result = getattr(module, name)(*args)
        code = ctypes.get_last_error()
        record = {"api": name, "result": int(result or 0), "last_error_raw": code,
                  "error_semantics": error_semantics, "arguments": arguments or {}}
        self.report["api_calls"].append(record)
        save(self.folder / "result.json", self.report)
        return result, code

    def require(self, result, name, code):
        if not result:
            self.report["failure"] = {"api": name, "last_error_raw": code,
                                      "access_denied_confirmed": code == 5,
                                      "meaning": "no_last_error_reported" if code == 0 else "Win32_error_returned"}
            raise Stop("STOP_" + name)

    def snapshot(self):
        hwnd, code = self.call(self.user, "GetForegroundWindow", [], error_semantics="not_defined_for_null")
        if not hwnd:
            # A null foreground window is not proof of access denial, regardless
            # of a thread's last-error slot; this API does not define that error.
            raise Stop("STOP_NO_FOREGROUND_WINDOW")
        snap = {"hwnd": int(hwnd)}
        self.report["latest_window"] = snap
        pid = w.DWORD()
        tid, code = self.call(self.user, "GetWindowThreadProcessId", [hwnd, ctypes.byref(pid)],
                              arguments={"hwnd": int(hwnd)})
        snap.update(pid=pid.value, thread_id=int(tid))
        self.require(tid, "GetWindowThreadProcessId", code)
        if not pid.value:
            raise Stop("STOP_PID_MISSING_AFTER_THREAD_QUERY")
        cls = ctypes.create_unicode_buffer(256)
        ok, code = self.call(self.user, "GetClassNameW", [hwnd, cls, len(cls)], arguments={"hwnd": int(hwnd)})
        self.require(ok, "GetClassNameW", code)
        snap["class_name"] = cls.value
        length, code = self.call(self.user, "GetWindowTextLengthW", [hwnd], arguments={"hwnd": int(hwnd)},
                                 error_semantics="zero_is_empty_or_failure")
        if length == 0 and code:
            self.require(False, "GetWindowTextLengthW", code)
        if length > 4096:
            raise Stop("STOP_TITLE_LENGTH_OUT_OF_BOUND")
        title = ctypes.create_unicode_buffer(length + 1)
        count, code = self.call(self.user, "GetWindowTextW", [hwnd, title, len(title)],
                                arguments={"hwnd": int(hwnd)}, error_semantics="zero_is_empty_or_failure")
        if not count and code:
            self.require(False, "GetWindowTextW", code)
        # Retain the exact allowed APPLICATION caption; conversation validation
        # belongs to the captured client image, never this top-level caption.
        snap.update(application_title_metadata(title.value))
        save(self.folder / "result.json", self.report)
        return snap

    def geometry(self, hwnd):
        rect = w.RECT()
        ok, code = self.call(self.user, "GetClientRect", [hwnd, ctypes.byref(rect)], arguments={"hwnd": hwnd})
        self.require(ok, "GetClientRect", code)
        points = [w.POINT(rect.left, rect.top), w.POINT(rect.right, rect.bottom)]
        for point in points:
            ok, code = self.call(self.user, "ClientToScreen", [hwnd, ctypes.byref(point)], arguments={"hwnd": hwnd})
            self.require(ok, "ClientToScreen", code)
        dpi, code = self.call(self.user, "GetDpiForWindow", [hwnd], arguments={"hwnd": hwnd},
                              error_semantics="zero_invalid_window_no_error_code_contract")
        if not dpi:
            raise Stop("STOP_DPI_UNKNOWN")
        affinity = w.DWORD()
        ok, code = self.call(self.user, "GetWindowDisplayAffinity", [hwnd, ctypes.byref(affinity)], arguments={"hwnd": hwnd})
        self.require(ok, "GetWindowDisplayAffinity", code)
        if affinity.value:
            raise Stop("STOP_CAPTURE_RESTRICTION_REPORTED")
        return {"client_screen": [points[0].x, points[0].y, points[1].x, points[1].y],
                "dpi": int(dpi), "affinity": affinity.value}
