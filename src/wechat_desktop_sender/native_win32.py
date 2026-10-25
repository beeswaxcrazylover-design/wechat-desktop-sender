"""Opt-in native port for ONE foreground/click/focus experiment.

Importing this module loads no DLL, reads no desktop and performs no input.
No process handles, keyboard/text injection, hooks, privilege changes or retries.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import re
import time
from datetime import datetime, timezone

from .live_capture_once import Probe, Stop, application_title_metadata, bind_selected_application, save


RUN_WINDOW_MS = 60_000
EXPLICIT_APPROVAL_WINDOW_MS = 120_000
MIN_IDLE_MS = 2_000


def make_run_clock(start_ns, started_utc, authorized_at_utc=None):
    """Local run age and optional explicit approval age, never keyboard idle age."""
    approval = None
    if authorized_at_utc is not None:
        try:
            approval = datetime.fromisoformat(authorized_at_utc.replace("Z", "+00:00"))
            if approval.tzinfo is None:
                raise ValueError("timezone missing")
            approval = approval.astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError) as error:
            raise Stop("STOP_EXPLICIT_APPROVAL_TIME_INVALID") from error
    age_ms = (started_utc - approval).total_seconds() * 1000 if approval is not None else None
    if age_ms is not None and age_ms < 0:
        raise Stop("STOP_EXPLICIT_APPROVAL_TIME_IN_FUTURE")
    return {"started_monotonic_ns": start_ns, "started_utc": started_utc.isoformat(),
            "local_run_window_ms": RUN_WINDOW_MS,
            "explicit_approval_utc": approval.isoformat() if approval else None,
            "explicit_approval_age_at_start_ms": age_ms,
            "explicit_approval_window_ms": EXPLICIT_APPROVAL_WINDOW_MS if approval else None,
            "authorization_age_known": approval is not None,
            "expiry_uses_last_keyboard_or_mouse_time": False}


def require_run_window(report, phase, *, now_ns=None, reserve_ms=0):
    now_ns = time.perf_counter_ns() if now_ns is None else now_ns
    clock = report.get("run_clock")
    decision = {"phase": phase, "checked_monotonic_ns": now_ns, "reserve_ms": reserve_ms}
    report.setdefault("run_window_checks", []).append(decision)
    if not clock:
        decision["branch"] = "MISSING_RUN_CLOCK"
        raise Stop("STOP_RUN_CLOCK_MISSING")
    elapsed = (now_ns - clock["started_monotonic_ns"]) / 1_000_000
    approval_age = clock["explicit_approval_age_at_start_ms"]
    approval_age = None if approval_age is None else approval_age + elapsed
    remaining = clock["local_run_window_ms"] - elapsed
    if approval_age is not None:
        remaining = min(remaining, clock["explicit_approval_window_ms"] - approval_age)
    decision.update(local_elapsed_ms=elapsed, explicit_approval_age_ms=approval_age,
                    local_run_window_ms=clock["local_run_window_ms"],
                    explicit_approval_window_ms=clock["explicit_approval_window_ms"], remaining_ms=remaining)
    if elapsed < 0:
        decision["branch"] = "MONOTONIC_CLOCK_WENT_BACKWARDS"
        raise Stop("STOP_RUN_CLOCK_INVALID")
    if remaining <= 0:
        decision["branch"] = "EXPLICIT_APPROVAL_EXPIRED" if approval_age is not None and approval_age >= clock["explicit_approval_window_ms"] else "LOCAL_RUN_EXPIRED"
        raise Stop("STOP_" + decision["branch"])
    if remaining <= reserve_ms:
        decision["branch"] = "INSUFFICIENT_TIME_FOR_PLANNED_WAIT"
        raise Stop("STOP_RUN_WINDOW_INSUFFICIENT_FOR_COUNTDOWN")
    decision["branch"] = "WITHIN_RUN_WINDOW"
    return decision


def require_initial_idle(report, baseline):
    idle = baseline.get("idle_ms")
    decision = {"observed_monotonic_ns": time.perf_counter_ns(), "baseline": dict(baseline),
                "idle_ms": idle, "min_idle_ms": MIN_IDLE_MS, "max_idle_ms": None,
                "comparison": "idle_ms >= min_idle_ms; expiry checked independently"}
    report["initial_activity_validation"] = decision
    if type(idle) is not int or idle < 0:
        decision["branch"] = "INVALID_ACTIVITY_OBSERVATION"
        raise Stop("STOP_ACTIVITY_OBSERVATION_INVALID")
    if idle < MIN_IDLE_MS:
        decision["branch"] = "RECENT_USER_INPUT"
        raise Stop("STOP_RECENT_USER_INPUT")
    decision["branch"] = "IDLE_MINIMUM_MET"
    return decision


def click_dispatch_state(before, current, injected_tick, point):
    """Bounded observation only; never resend a click to make evidence appear."""
    point = list(point)
    if current["tick"] == injected_tick and current["cursor"] == point:
        return "OWN_INPUT_OBSERVED"
    if current["tick"] in (before["tick"], injected_tick) and current["cursor"] in (before["cursor"], point):
        return "WAITING_FOR_OWN_INPUT_OBSERVATION"
    return "UNEXPECTED_INPUT_OR_CURSOR_CHANGE"


def related_window_class(name):
    """Discovery only, not authorization of an unreviewed Qt/mmui layout."""
    return bool(re.match(r"^Qt\w*QWindow", name) or "mmui" in name.lower() or
                name.startswith(("WeChat", "Weixin")))


def candidate_filter(record):
    reasons = []
    if record.get("metadata_complete") is not True:
        reasons.append("METADATA_INCOMPLETE")
    if record.get("class_name") != "Qt51514QWindowIcon":
        reasons.append("CLASS_NOT_REVIEWED_FOR_CLICK_LAYOUT")
    if record.get("application_title_match") is not True:
        reasons.append("APPLICATION_CAPTION_NOT_EXACT_WHITELIST")
    if record.get("visible") is not True:
        reasons.append("WINDOW_NOT_VISIBLE")
    if record.get("minimized") is not False:
        reasons.append("WINDOW_MINIMIZED_OR_UNKNOWN")
    if any(type(record.get(key)) is not int or record[key] <= 0 for key in ("hwnd", "pid", "thread_id")):
        reasons.append("WINDOW_OWNER_UNKNOWN")
    return reasons


def choose_candidate(records, *, selected_hwnd=None):
    eligible = [row for row in records if not candidate_filter(row)]
    if selected_hwnd is not None:
        eligible = [row for row in eligible if row["hwnd"] == selected_hwnd]
    if not eligible:
        raise Stop("STOP_NO_ELIGIBLE_TARGET_WINDOW")
    if len(eligible) != 1:
        raise Stop("STOP_MULTIPLE_ELIGIBLE_TARGET_WINDOWS")
    row = eligible[0]
    return bind_selected_application({key: row[key] for key in (
        "hwnd", "pid", "thread_id", "class_name", "title_length", "application_title_match",
        "title", "title_state")}, human_confirmed=True)


def key_state_interpretation(value, raw_last_error):
    """Interpret current-down bit; zero alone is neither failure nor proof.

    Normally zero means no key-down is reported. Some unreadable environments
    also return zero, so environment/binding guards and the human hands-off
    condition are checked separately. Ignore the unreliable low/history bit and
    reserved bits. GetLastError has no contract here and cannot decide access.
    """
    bits = value & 0xFFFF
    state = "DOWN" if bits & 0x8000 else "NOT_DOWN_REPORTED"
    return {"raw_result": value, "last_error_raw": raw_last_error,
            "last_error_is_authoritative": False, "access_denied_confirmed": False,
            "state": state, "zero_is_up_proof": False, "zero_is_failure_proof": False,
            "observability_proven_by_return": False, "low_bit_used_as_history": False}


class MOUSEINPUT(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]


class KEYBDINPUT(c.Structure):  # ABI union padding only; never constructed for input.
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", c.c_size_t)]


class HARDWAREINPUT(c.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class INPUTUNION(c.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(c.Structure):
    _fields_ = [("type", w.DWORD), ("data", INPUTUNION)]


class GUITHREADINFO(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD)] + [
        (name, w.HWND) for name in ("hwndActive", "hwndFocus", "hwndCapture",
                                   "hwndMenuOwner", "hwndMoveSize", "hwndCaret")] + [("rcCaret", w.RECT)]


class LASTINPUTINFO(c.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


class MONITORINFO(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


def contains(outer, inner):
    return (outer[0] <= inner[0] < inner[2] <= outer[2] and
            outer[1] <= inner[1] < inner[3] <= outer[3])


def intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def absolute_mouse_point(point, virtual):
    """Physical pixel -> SendInput normalized virtual-desktop coordinates."""
    x, y = point
    left, top, width, height = virtual
    if width <= 1 or height <= 1 or not (left <= x < left + width and top <= y < top + height):
        raise Stop("STOP_CLICK_OUTSIDE_VIRTUAL_DESKTOP")
    return (round((x - left) * 65535 / (width - 1)), round((y - top) * 65535 / (height - 1)))


def require_fresh_frame(captured_ns, now_ns, before, after, editor, point):
    if not (0 <= now_ns - captured_ns <= 2_000_000_000) or before != after:
        raise Stop("STOP_FRAME_STALE_OR_WINDOW_CHANGED")
    if not contains(after["geometry"]["client_screen"], editor):
        raise Stop("STOP_EDITOR_OUTSIDE_CURRENT_CLIENT")
    if not (editor[0] < point[0] < editor[2] and editor[1] < point[1] < editor[3]):
        raise Stop("STOP_POINT_OUTSIDE_EDITOR_INTERIOR")


def caret_evidence(info, target, editor):
    """This is native caret evidence, NOT independently verified editor semantics."""
    okay = (info.get("query_ok") is True and info.get("flags", 0) & 1 and
            not info.get("flags", 0) & 0x1E and info.get("focus_owner_matches") is True and
            info.get("caret_owner_matches") is True and info.get("active_hwnd") == target and
            info.get("focus_hwnd") == info.get("caret_hwnd") and
            not info.get("capture_hwnd") and contains(editor, info.get("caret_screen", [0, 0, 0, 0])))
    return {"status": "NATIVE_CARET_IN_EDITOR_CANDIDATE" if okay else "FOCUS_UNKNOWN",
            "native_caret_evidence": bool(okay), "editor_role_independently_verified": False,
            "send_eligible": False, "retry_allowed": False}


class NativePort(Probe):
    """Real implementation. The constructor itself requires explicit authorization."""
    def __init__(self, folder, report, *, authorized=False):
        if authorized is not True:
            raise Stop("DESKTOP_IO_DISABLED")
        super().__init__(folder, report)
        self.dwm = c.WinDLL("dwmapi", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        specs = [
            (self.user, "IsWindow", [w.HWND], w.BOOL),
            (self.user, "IsWindowVisible", [w.HWND], w.BOOL),
            (self.user, "IsIconic", [w.HWND], w.BOOL),
            (self.user, "GetAncestor", [w.HWND, w.UINT], w.HWND),
            (self.user, "GetWindowRect", [w.HWND, c.POINTER(w.RECT)], w.BOOL),
            (self.user, "EnumWindows", [self.callback_type, w.LPARAM], w.BOOL),
            (self.user, "GetSystemMetrics", [c.c_int], c.c_int),
            (self.user, "MonitorFromWindow", [w.HWND, w.DWORD], w.HANDLE),
            (self.user, "GetMonitorInfoW", [w.HANDLE, c.POINTER(MONITORINFO)], w.BOOL),
            (self.user, "SetForegroundWindow", [w.HWND], w.BOOL),
            (self.user, "SendInput", [w.UINT, c.POINTER(INPUT), c.c_int], w.UINT),
            (self.user, "GetGUIThreadInfo", [w.DWORD, c.POINTER(GUITHREADINFO)], w.BOOL),
            (self.user, "GetLastInputInfo", [c.POINTER(LASTINPUTINFO)], w.BOOL),
            (self.user, "GetAsyncKeyState", [c.c_int], c.c_short),
            (self.user, "GetCursorPos", [c.POINTER(w.POINT)], w.BOOL),
            (self.user, "OpenInputDesktop", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.user, "GetUserObjectInformationW", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
            (self.user, "CloseDesktop", [w.HANDLE], w.BOOL),
            (self.user, "SetThreadDpiAwarenessContext", [c.c_void_p], c.c_void_p),
            (self.kernel, "GetTickCount", [], w.DWORD),
            (self.dwm, "DwmGetWindowAttribute", [w.HWND, w.DWORD, c.c_void_p, w.DWORD], c.c_long),
        ]
        for dll, name, args, result in specs:
            getattr(dll, name).argtypes, getattr(dll, name).restype = args, result
        self.clicked = self.activation_attempted = self.restore_attempted = False

    def call(self, module, name, args, *, arguments=None, error_semantics="failure_only"):
        start = time.perf_counter_ns()
        c.set_last_error(0)
        result = getattr(module, name)(*args)
        error = c.get_last_error()  # Immediate; no intervening Win32 call.
        self.report["api_calls"].append({"api": name, "result": int(result or 0),
            "last_error_raw": error, "error_semantics": error_semantics,
            "arguments": arguments or {}, "started_monotonic_ns": start,
            "phase": self.report.get("phase"), "elapsed_ns": time.perf_counter_ns() - start})
        return result, error

    def u(self, name, *args, require=False, semantics="failure_only", arguments=None):
        result, error = self.call(self.user, name, args, error_semantics=semantics, arguments=arguments)
        if require:
            if not result and ("no_" in semantics or "boolean_predicate" in semantics):
                self.report["failure"] = {"api": name, "last_error_raw": error,
                    "last_error_is_authoritative": False, "access_denied_confirmed": False,
                    "meaning": semantics}
                raise Stop("STOP_" + name)
            self.require(result, name, error)
        return result

    def foreground(self):
        return int(self.u("GetForegroundWindow", semantics="no_last_error_contract") or 0)

    def owner(self, hwnd):
        pid = w.DWORD()
        tid = self.u("GetWindowThreadProcessId", hwnd, c.byref(pid), require=True, arguments={"hwnd": hwnd})
        if not pid.value:
            raise Stop("STOP_PID_UNKNOWN")
        return {"hwnd": int(hwnd), "pid": int(pid.value), "thread_id": int(tid)}

    def inspect_candidate(self, hwnd, class_name=None):
        record = {"hwnd": int(hwnd), "class_name": class_name, "title": None,
                  "title_state": "NOT_READ", "title_length": None,
                  "application_title_match": False, "visible": None, "minimized": None,
                  "pid": None, "thread_id": None, "metadata_complete": False,
                  "eligible": False, "filter_reasons": []}
        try:
            self.u("IsWindow", hwnd, require=True, semantics="boolean_predicate_not_error_code", arguments={"hwnd": hwnd})
            if class_name is None:
                cls = c.create_unicode_buffer(256)
                self.u("GetClassNameW", hwnd, cls, len(cls), require=True, arguments={"hwnd": hwnd})
                record["class_name"] = cls.value
            record["visible"] = bool(self.u("IsWindowVisible", hwnd, arguments={"hwnd": hwnd}, semantics="boolean_predicate_not_error_code"))
            record["minimized"] = bool(self.u("IsIconic", hwnd, arguments={"hwnd": hwnd}, semantics="boolean_predicate_not_error_code"))
            record.update(self.owner(hwnd))
            if related_window_class(record["class_name"]):
                title = c.create_unicode_buffer(4097)
                count, error = self.call(self.user, "GetWindowTextW", [hwnd, title, len(title)],
                                         arguments={"hwnd": hwnd}, error_semantics="zero_empty_or_failure_GetLastError_defined")
                if not count and error:
                    self.require(False, "GetWindowTextW", error)
                record.update(application_title_metadata(title.value))
            else:
                record["title_state"] = "NOT_READ_UNRELATED_CLASS"
            record["metadata_complete"] = True
        except Stop as error:
            record["metadata_stop"] = str(error)
            record["failure"] = dict(self.report.get("failure", {}))
        record["filter_reasons"] = candidate_filter(record)
        record["eligible"] = not record["filter_reasons"]
        return record

    def app(self, hwnd):
        record = self.inspect_candidate(hwnd)
        self.report["latest_recheck_candidate"] = record
        if not record["metadata_complete"]:
            raise Stop(record["metadata_stop"])
        return choose_candidate([record], selected_hwnd=hwnd)

    def windows(self):
        found = []
        @self.callback_type
        def callback(hwnd, _):
            found.append(int(hwnd))
            return True
        self.u("EnumWindows", callback, 0, require=True)
        return found

    def select_target(self, hwnd=None):
        selection = {"mode": "explicit_current_foreground" if hwnd is not None else "unique_related_window",
                     "enumerated_window_count": 0, "class_scan_complete": False, "class_histogram": {},
                     "candidate_count": 0, "eligible_candidate_count": 0, "candidate_records": [],
                     "selected_hwnd": None, "outcome": "READING_METADATA"}
        self.report["target_selection"] = selection
        try:
            if hwnd is not None:
                if self.foreground() != hwnd:
                    raise Stop("STOP_EXPLICIT_HANDLE_NOT_CURRENT_USER_SELECTION")
                handles = [hwnd]
            else:
                handles = self.windows()
            selection["enumerated_window_count"] = len(handles)
            for handle in handles:
                cls = c.create_unicode_buffer(256)
                try:
                    self.u("GetClassNameW", handle, cls, len(cls), require=True, arguments={"hwnd": handle})
                except Stop as error:
                    selection["class_scan_failure"] = {"hwnd": handle, "reason": str(error),
                        "failure": dict(self.report.get("failure", {}))}
                    raise
                selection["class_histogram"][cls.value] = selection["class_histogram"].get(cls.value, 0) + 1
                if related_window_class(cls.value) or hwnd is not None:
                    record = self.inspect_candidate(handle, cls.value)
                    selection["candidate_records"].append(record)
                    if not record["metadata_complete"]:
                        raise Stop(record["metadata_stop"])  # No query retry/fallback.
            selection["class_scan_complete"] = True
            if hwnd is not None and self.foreground() != hwnd:
                raise Stop("STOP_FOREGROUND_CHANGED_DURING_EXPLICIT_BINDING")
            result = choose_candidate(selection["candidate_records"], selected_hwnd=hwnd)
            selection.update(outcome="BOUND_APPLICATION_WINDOW", selected_hwnd=result["hwnd"])
            return result
        except Stop as error:
            selection["outcome"] = str(error)
            raise
        finally:
            records = selection["candidate_records"]
            selection["candidate_count"] = len(records)
            selection["eligible_candidate_count"] = sum(row["eligible"] for row in records)
            selection["counts_complete"] = selection["class_scan_complete"]
            save(self.folder / "result.json", self.report)  # Saved even on zero/multiple/failure.

    def bind_foreground_selection(self):
        hwnd = self.foreground()
        if not hwnd:
            raise Stop("STOP_USER_SELECTED_FOREGROUND_UNKNOWN")
        target = self.select_target(hwnd)
        self.report["foreground_binding"] = {"target": target, "bound_at_ns": time.perf_counter_ns(),
            "account_basis": "human_selected_work_account_only", "valid_for_this_process_only": True,
            "expires_after_seconds": 60, "conversation_verified": False}
        save(self.folder / "result.json", self.report)
        return target

    def revalidate_foreground_binding(self, target):
        binding = self.report.get("foreground_binding", {})
        age = time.perf_counter_ns() - binding.get("bound_at_ns", 0)
        if binding.get("target") != target or not 0 <= age <= 60_000_000_000:
            raise Stop("STOP_FOREGROUND_BINDING_EXPIRED_OR_MISSING")
        if self.app(target["hwnd"]) != target:
            raise Stop("STOP_SELECTED_WINDOW_INSTANCE_CHANGED")
        return self.guard(target, foreground=False)

    def unlocked(self):
        desktop = self.u("OpenInputDesktop", 0, False, 1, require=True)  # DESKTOP_READOBJECTS only.
        try:
            name, needed = c.create_unicode_buffer(128), w.DWORD()
            self.u("GetUserObjectInformationW", desktop, 2, name, c.sizeof(name), c.byref(needed), require=True)
            if name.value != "Default":
                raise Stop("STOP_NOT_DEFAULT_INPUT_DESKTOP")
            return name.value
        finally:
            self.u("CloseDesktop", desktop, require=True)

    def activity(self):
        sample = {"started_monotonic_ns": time.perf_counter_ns(), "phase": self.report.get("phase"),
                  "status": "READING", "get_last_input_info": None, "idle_ms": None}
        self.report.setdefault("activity_observations", []).append(sample)
        try:
            return self._read_activity(sample)
        except Stop as error:
            sample.update(status="STOP", reason=str(error), failure=dict(self.report.get("failure", {})))
            raise
        finally:
            sample["finished_monotonic_ns"] = time.perf_counter_ns()

    def _read_activity(self, sample):
        if self.report.get("human_hands_off_confirmed") is not True:
            raise Stop("STOP_BOUNDED_HANDS_OFF_CONDITION_NOT_CONFIRMED")
        desktop = self.unlocked()  # Existing permitted read; failure/non-Default stops.
        target, previous = self.report.get("bound_window"), self.report.get("previous_window")
        if desktop != "Default" or not target or not previous:
            raise Stop("STOP_ACTIVITY_ENVIRONMENT_OR_BINDING_UNKNOWN")
        current = self.foreground()
        expected = target if current == target["hwnd"] else previous if current == previous["hwnd"] else None
        if expected is None:
            raise Stop("STOP_FOREGROUND_OUTSIDE_BOUND_TEST_WINDOWS")
        owner = self.owner(current)
        if owner != {key: expected[key] for key in ("hwnd", "pid", "thread_id")}:
            raise Stop("STOP_FOREGROUND_INSTANCE_CHANGED")
        self.report["activity_guard_basis"] = {"scope": "bounded_human_hands_off_test",
            "input_desktop_name_observed": desktop, "foreground_instance_observed": owner,
            "target_binding_present": True, "human_hands_off_confirmed": True,
            "complete_user_activity_detection": False, "key_api_access_proven": False,
            "limitation": "Zero can also occur in unreadable environments. These guards reduce uncertainty but do not establish complete input visibility or user-versus-injected attribution."}
        # No keyboard text is read or recorded: only mouse buttons/modifiers.
        for key in (1, 2, 4, 5, 6, 16, 17, 18, 91, 92):
            value, error = self.call(self.user, "GetAsyncKeyState", [key], arguments={"virtual_key": key},
                                     error_semantics="no_GetLastError_contract_zero_not_failure_proof")
            interpreted = {"virtual_key": key, **key_state_interpretation(value, error)}
            self.report.setdefault("key_state_observations", []).append(interpreted)
            if interpreted["state"] == "DOWN":
                raise Stop("STOP_MOUSE_BUTTON_OR_MODIFIER_HELD")
        data = LASTINPUTINFO(c.sizeof(LASTINPUTINFO), 0)
        result, error = self.call(self.user, "GetLastInputInfo", [c.byref(data)],
                                  error_semantics="zero_failure_no_GetLastError_contract")
        sample["get_last_input_info"] = {"result": int(result or 0), "last_error_raw": error,
                                          "last_input_tick_raw": int(data.dwTime), "read_ok": bool(result)}
        if not result:
            self.report["failure"] = {"api": "GetLastInputInfo", "result": int(result or 0),
                "last_error_raw": error, "last_error_is_authoritative": False,
                "access_denied_confirmed": False, "meaning": "activity_read_failed"}
            raise Stop("STOP_GetLastInputInfo")
        tick, _ = self.call(self.kernel, "GetTickCount", [], error_semantics="no_error_contract")
        sample.update(system_tick_raw=int(tick), last_input_tick_raw=int(data.dwTime),
                      idle_ms=(int(tick) - data.dwTime) & 0xFFFFFFFF,
                      tick_arithmetic="unsigned_32_bit_modulo; independent of authorization age")
        cursor = w.POINT()
        self.u("GetCursorPos", c.byref(cursor), require=True)
        sample.update(status="OBSERVED", cursor=[cursor.x, cursor.y])
        return {"tick": int(data.dwTime), "idle_ms": sample["idle_ms"], "cursor": sample["cursor"]}

    def require_activity(self, baseline):
        current = self.activity()
        self.report.setdefault("activity_comparisons", []).append({"phase": self.report.get("phase"),
            "checked_monotonic_ns": time.perf_counter_ns(), "baseline": dict(baseline), "current": dict(current),
            "branch": "UNCHANGED" if current["tick"] == baseline["tick"] and current["cursor"] == baseline["cursor"] else "INPUT_OR_CURSOR_CHANGED"})
        if current["tick"] != baseline["tick"] or current["cursor"] != baseline["cursor"]:
            raise Stop("STOP_OBSERVED_USER_ACTIVITY")
        return current

    def bounds(self, hwnd, *, frame=False):
        rect = w.RECT()
        if frame:
            hr, _ = self.call(self.dwm, "DwmGetWindowAttribute", [hwnd, 9, c.byref(rect), c.sizeof(rect)],
                              error_semantics="HRESULT_not_GetLastError")
            if hr:
                raise Stop("STOP_DWM_FRAME_HRESULT_" + str(int(hr)))
        else:
            self.u("GetWindowRect", hwnd, c.byref(rect), require=True)
        return [rect.left, rect.top, rect.right, rect.bottom]

    def guard(self, target, *, foreground=True):
        if self.app(target["hwnd"]) != target:
            raise Stop("STOP_WINDOW_BINDING_CHANGED")
        hwnd = target["hwnd"]
        if not self.u("IsWindowVisible", hwnd) or self.u("IsIconic", hwnd):
            raise Stop("STOP_WINDOW_INVISIBLE_OR_MINIMIZED")
        bounds, geometry = self.bounds(hwnd, frame=True), self.geometry(hwnd)
        virtual = [int(self.u("GetSystemMetrics", number)) for number in (76, 77, 78, 79)]
        vx, vy, vw, vh = virtual
        monitor = self.u("MonitorFromWindow", hwnd, 0, require=True,
                         semantics="null_means_no_intersecting_monitor_no_GetLastError_contract")
        monitor_info = MONITORINFO()
        monitor_info.cbSize = c.sizeof(monitor_info)
        self.u("GetMonitorInfoW", monitor, c.byref(monitor_info), require=True,
               semantics="zero_failure_no_GetLastError_contract")
        def rect_values(rect):
            return [rect.left, rect.top, rect.right, rect.bottom]
        display = {"monitor_handle": int(monitor), "monitor_bounds": rect_values(monitor_info.rcMonitor),
                   "monitor_work_area": rect_values(monitor_info.rcWork), "monitor_primary": bool(monitor_info.dwFlags & 1),
                   "virtual_desktop_bounds": [vx, vy, vx + vw, vy + vh], "window_dpi": geometry["dpi"],
                   "coordinate_space": "physical_screen_pixels_with_thread_PER_MONITOR_AWARE_V2",
                   "capture_scale": 1, "extra_dpi_multiplier": False}
        self.report["latest_display_geometry"] = display
        self.report["display_geometry_status"] = "READ_FROM_CURRENT_BOUND_WINDOW"
        if not contains([vx, vy, vx + vw, vy + vh], bounds) or not contains(bounds, geometry["client_screen"]):
            raise Stop("STOP_WINDOW_CLIPPED_OR_CLIENT_GEOMETRY_INVALID")
        if foreground:
            if self.foreground() != hwnd:
                raise Stop("STOP_FOREGROUND_CHANGED")
            for other in self.windows():
                if other == hwnd:
                    break
                if self.u("IsWindowVisible", other) and not self.u("IsIconic", other):
                    cloaked = w.DWORD()
                    hr, _ = self.call(self.dwm, "DwmGetWindowAttribute", [other, 14, c.byref(cloaked), c.sizeof(cloaked)],
                                      error_semantics="HRESULT_not_GetLastError")
                    if hr != 0:
                        raise Stop("STOP_OCCLUSION_QUERY_UNKNOWN")
                    if not cloaked.value:
                        other_bounds = self.bounds(other)
                        if intersects(bounds, other_bounds):
                            # Non-content evidence for the exact blocker; a
                            # popup must not be guessed from a generic stop.
                            blocker = self.report["occlusion_blocker"] = {"hwnd": int(other), "bounds": other_bounds}
                            try:
                                name = c.create_unicode_buffer(256)
                                self.u("GetClassNameW", other, name, len(name), require=True)
                                blocker.update(class_name=name.value, **self.owner(other),
                                    root_owner_hwnd=int(self.u("GetAncestor", other, 3) or 0))
                            except Stop as error:
                                blocker["diagnostic_failure"] = str(error)
                            raise Stop("STOP_WINDOW_POTENTIALLY_OCCLUDED")
            else:
                raise Stop("STOP_TARGET_NOT_IN_Z_ORDER")
        return {"target": target, "bounds": bounds, "geometry": geometry, "virtual": virtual, "display": display}

    def observe_foreground_after_request(self, requested, original, activity, observation):
        """At most 1 second of reads; never reissue a foreground request.

        NULL is a documented transient value, not an access-denied result.
        No further desktop action is allowed until identity/activity are known.
        """
        start = time.perf_counter_ns()
        observation.update(readback_started_ns=start, readback_limit_ms=1000,
                           readback_read_only=True, samples=[])
        try:
            for _ in range(41):
                now = time.perf_counter_ns()
                if observation["samples"] and now - start >= 1_000_000_000:
                    break
                current = self.foreground()
                sample = {"observed_monotonic_ns": time.perf_counter_ns(), "hwnd": current}
                observation["samples"].append(sample)
                if not current:
                    sample["state"] = "NULL_FOREGROUND_REPORTED"
                elif current not in (requested["hwnd"], original["hwnd"]):
                    sample["state"] = "OTHER_FOREGROUND_OBSERVED"
                    observation["outcome"] = "OTHER_FOREGROUND_OBSERVED"
                    return observation["outcome"]
                else:
                    expected = requested if current == requested["hwnd"] else original
                    sample["owner"] = self.owner(current)
                    if sample["owner"] != {key: expected[key] for key in ("hwnd", "pid", "thread_id")}:
                        raise Stop("STOP_FOREGROUND_INSTANCE_CHANGED")
                    self.require_activity(activity)
                    # Recheck after the activity observation to avoid confirming
                    # a foreground value already superseded during these reads.
                    sample["rechecked_hwnd"] = self.foreground()
                    if sample["rechecked_hwnd"] not in (0, requested["hwnd"], original["hwnd"]):
                        sample["state"] = "OTHER_FOREGROUND_OBSERVED"
                        observation["outcome"] = "OTHER_FOREGROUND_OBSERVED"
                        return observation["outcome"]
                    if sample["rechecked_hwnd"] == current:
                        sample["state"] = "REQUESTED_FOREGROUND_OBSERVED" if current == requested["hwnd"] else "ORIGINAL_FOREGROUND_OBSERVED"
                        if current == requested["hwnd"]:
                            observation["outcome"] = sample["state"]
                            return observation["outcome"]
                    else:
                        sample["state"] = "CHANGED_DURING_READBACK"
                remaining = 1 - (time.perf_counter_ns() - start) / 1_000_000_000
                if remaining <= 0:
                    break
                time.sleep(min(.025, remaining))
            observation["outcome"] = "REQUESTED_NOT_OBSERVED_WITHIN_1_SECOND"
            return observation["outcome"]
        except Exception as error:
            observation.update(outcome="READBACK_STOPPED", stop_reason=str(error))
            raise
        finally:
            observation["readback_finished_ns"] = time.perf_counter_ns()

    def activate_once(self, target, previous, activity):
        if self.activation_attempted:
            raise Stop("STOP_ACTIVATION_ALREADY_ATTEMPTED")
        self.activation_attempted = True
        self.require_activity(activity)
        before = self.foreground()
        observation = self.report["activation_observation"] = {
            "requested_window": {key: target[key] for key in ("hwnd", "pid", "thread_id")},
            "before_hwnd": before, "before_observed_ns": time.perf_counter_ns(),
            "set_foreground_called": False, "last_error_is_authoritative": False,
            "access_denied_confirmed": False, "retry_allowed": False}
        if before != previous["hwnd"]:
            raise Stop("STOP_FOREGROUND_CHANGED_BEFORE_ACTIVATION")
        if previous["hwnd"] == target["hwnd"]:
            observation["outcome"] = "ALREADY_FOREGROUND_NO_ACTIVATION"
            return False
        require_run_window(self.report, "immediately_before_SetForegroundWindow")
        self.report["activation_calls"] += 1
        observation["set_foreground_called"] = True
        result, error = self.call(self.user, "SetForegroundWindow", [target["hwnd"]],
                                  arguments={"hwnd": target["hwnd"]},
                                  error_semantics="no_GetLastError_contract_zero_reports_not_brought_to_foreground")
        observation.update(result=int(result or 0), last_error_raw=error,
                           returned_monotonic_ns=time.perf_counter_ns())
        outcome = self.observe_foreground_after_request(target, previous, activity, observation)
        if not result:
            # Readback establishes observed state, not why it changed. A zero
            # return never authorizes capture/click/restore as a recovery action.
            self.report["restore_result"] = "SKIPPED_ACTIVATION_RESULT_OR_CAUSALITY_UNKNOWN"
            raise Stop("STOP_SET_FOREGROUND_RETURNED_ZERO_NO_RETRY")
        if outcome == "REQUESTED_FOREGROUND_OBSERVED":
            return True
        if outcome == "OTHER_FOREGROUND_OBSERVED":
            raise Stop("STOP_USER_OR_EXTERNAL_FOREGROUND_TAKEOVER")
        raise Stop("STOP_ACTIVATION_CONFIRMATION_TIMEOUT")

    def click_once(self, target, state, captured_ns, editor, point, activity):
        if self.clicked:
            raise Stop("STOP_CLICK_ALREADY_ATTEMPTED")
        self.require_activity(activity)
        after = self.guard(target)
        require_fresh_frame(captured_ns, time.perf_counter_ns(), state, after, editor, point)
        dx, dy = absolute_mouse_point(point, after["virtual"])
        self.require_activity(activity)
        if self.foreground() != target["hwnd"]:
            raise Stop("STOP_FOREGROUND_CHANGED_IMMEDIATELY_BEFORE_CLICK")
        require_fresh_frame(captured_ns, time.perf_counter_ns(), state, after, editor, point)
        require_run_window(self.report, "immediately_before_SendInput")
        return self.dispatch_left_click_once(point, after["virtual"])

    def dispatch_left_click_once(self, point, virtual):
        """Shared low-level input; caller must first validate its fresh UI target."""
        if self.clicked or getattr(self, "move_attempted", False):
            raise Stop("STOP_CLICK_ALREADY_ATTEMPTED")
        dx, dy = absolute_mouse_point(point, virtual)
        tick, _ = self.call(self.kernel, "GetTickCount", [], error_semantics="no_error_contract")
        down = INPUT(0, INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, 0xC003, tick, 0x4F4352)))
        up = INPUT(0, INPUTUNION(mi=MOUSEINPUT(0, 0, 0, 0x0004, tick, 0x4F4352)))
        # Snapshot the exact structures passed below; no inference from return
        # values, and no extra native observations or input calls.
        def arguments(frame):
            mouse = frame.data.mi
            return {"cInputs": 1, "cbSize": c.sizeof(INPUT), "pInputs_type": "POINTER(INPUT)",
                    "input_type": int(frame.type),
                    "mouse": {key: int(getattr(mouse, key)) for key in
                              ("dx", "dy", "mouseData", "dwFlags", "time", "dwExtraInfo")},
                    "INPUT_bytes_hex": bytes(frame).hex()}
        down_args, up_args = arguments(down), arguments(up)
        self.report["mouse_input_plan"] = {
            "point_screen": list(point), "virtual_xywh": list(virtual), "normalized_dxdy": [dx, dy],
            "normalization": "round((physical_pixel - virtual_origin) * 65535 / (extent - 1))",
            "down_flags": ["MOUSEEVENTF_MOVE", "MOUSEEVENTF_LEFTDOWN", "MOUSEEVENTF_ABSOLUTE", "MOUSEEVENTF_VIRTUALDESK"],
            "up_flags": ["MOUSEEVENTF_LEFTUP"], "pointer_bytes": c.sizeof(c.c_void_p),
            "INPUT_size": c.sizeof(INPUT), "INPUT_union_offset": INPUT.data.offset,
            "MOUSEINPUT_size": c.sizeof(MOUSEINPUT),
            "MOUSEINPUT_offsets": {key: getattr(MOUSEINPUT, key).offset for key in
                                   ("dx", "dy", "mouseData", "dwFlags", "time", "dwExtraInfo")},
            "planned_inputs": [down_args, up_args]}
        self.clicked = True  # Reserve before call; uncertain attempts are never retried.
        self.report["click_attempts"] = 1
        # One record per call avoids an ambiguous partial down/up array. Once a
        # down was accepted, send exactly one matching up even if Python unwinds.
        self.report["sendinput_calls"] += 1
        inserted = self.u("SendInput", 1, c.byref(down), c.sizeof(INPUT), arguments=down_args,
                          semantics="inserted_count_UIPI_reason_not_exposed")
        if inserted != 1:
            raise Stop("STOP_SENDINPUT_DOWN_NOT_INSERTED_NO_RETRY")
        try:
            self.report["left_down_inserted"] = True
        finally:
            self.report["sendinput_calls"] += 1
            released = self.u("SendInput", 1, c.byref(up), c.sizeof(INPUT), arguments=up_args,
                              semantics="inserted_count_UIPI_reason_not_exposed")
            self.report["left_up_inserted"] = released == 1
            if released != 1:
                self.report["manual_release_may_be_needed"] = True
                raise Stop("STOP_SENDINPUT_RELEASE_UNKNOWN_NO_RETRY")
        return int(tick)

    def dispatch_mouse_move_once(self, point, virtual):
        """One MOVE only through the same SendInput path; never press/release."""
        if self.clicked or getattr(self, "move_attempted", False):
            raise Stop("STOP_MOVE_ALREADY_ATTEMPTED")
        dx, dy = absolute_mouse_point(point, virtual)
        tick, _ = self.call(self.kernel, "GetTickCount", [], error_semantics="no_error_contract")
        frame = INPUT(0, INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, 0xC001, tick, 0x4F4352)))
        args = {"cInputs": 1, "cbSize": c.sizeof(INPUT), "pInputs_type": "POINTER(INPUT)",
                "input_type": int(frame.type), "mouse": {key: int(getattr(frame.data.mi, key)) for key in
                ("dx", "dy", "mouseData", "dwFlags", "time", "dwExtraInfo")},
                "INPUT_bytes_hex": bytes(frame).hex()}
        self.report["mouse_move_plan"] = {
            "point_screen": list(point), "virtual_xywh": list(virtual), "normalized_dxdy": [dx, dy],
            "flags": ["MOUSEEVENTF_MOVE", "MOUSEEVENTF_ABSOLUTE", "MOUSEEVENTF_VIRTUALDESK"],
            "button_flags": 0, "input_arguments": args}
        self.move_attempted = True
        self.report["move_attempts"] = 1
        self.report["sendinput_calls"] += 1
        inserted = self.u("SendInput", 1, c.byref(frame), c.sizeof(INPUT), arguments=args,
                          semantics="inserted_count_UIPI_reason_not_exposed")
        self.report.update(move_sendinput_return=int(inserted), move_inserted=inserted == 1)
        # Permit only readback after any return, including zero; no fallback,
        # button cleanup or retry is needed for a movement-only input record.
        return int(tick)

    def observe_focus(self, target, editor):
        info = GUITHREADINFO()
        info.cbSize = c.sizeof(info)
        ok = self.u("GetGUIThreadInfo", target["thread_id"], c.byref(info))
        data = {"query_ok": bool(ok), "flags": int(info.flags), "active_hwnd": int(info.hwndActive or 0),
                "focus_hwnd": int(info.hwndFocus or 0), "caret_hwnd": int(info.hwndCaret or 0),
                "capture_hwnd": int(info.hwndCapture or 0)}
        if not ok:
            return data
        for key, handle in (("focus", info.hwndFocus), ("caret", info.hwndCaret)):
            data[key + "_owner_matches"] = bool(handle and
                self.owner(handle)["pid"] == target["pid"] and
                self.owner(handle)["thread_id"] == target["thread_id"] and
                self.u("GetAncestor", handle, 2) == target["hwnd"])
        if data["caret_owner_matches"]:
            points = [w.POINT(info.rcCaret.left, info.rcCaret.top), w.POINT(info.rcCaret.right, info.rcCaret.bottom)]
            for point in points:
                self.u("ClientToScreen", info.hwndCaret, c.byref(point), require=True)
            data["caret_screen"] = [points[0].x, points[0].y, points[1].x, points[1].y]
        return data

    def restore_once(self, previous, target, expected_activity):
        """No restore on ownership/activity uncertainty; never move the cursor back."""
        if self.restore_attempted:
            raise Stop("STOP_RESTORE_ALREADY_ATTEMPTED")
        self.restore_attempted = True
        if previous["hwnd"] == target["hwnd"]:
            return "NO_FOREGROUND_BORROWED"
        if expected_activity is None:
            return "SKIPPED_ACTIVITY_ATTRIBUTION_UNKNOWN"
        try:
            self.require_activity(expected_activity)
            if self.foreground() != target["hwnd"] or self.owner(previous["hwnd"]) != previous:
                return "SKIPPED_FOREGROUND_OR_PREVIOUS_INSTANCE_CHANGED"
            if not self.u("IsWindowVisible", previous["hwnd"]) or self.u("IsIconic", previous["hwnd"]):
                return "SKIPPED_PREVIOUS_WINDOW_UNAVAILABLE"
        except Stop:
            return "SKIPPED_USER_ACTIVITY_OR_WINDOW_UNKNOWN"
        self.report["restore_calls"] += 1
        if not self.u("SetForegroundWindow", previous["hwnd"], semantics="no_GetLastError_contract_zero_reports_not_brought_to_foreground"):
            return "RESTORE_RETURNED_ZERO_NO_RETRY"
        deadline = time.perf_counter() + 1
        while time.perf_counter() < deadline:
            self.require_activity(expected_activity)
            current = self.foreground()
            if current == previous["hwnd"]:
                return "PREVIOUS_FOREGROUND_OBSERVED"
            if current not in (0, target["hwnd"]):
                return "RESTORE_INTERRUPTED_BY_OTHER_FOREGROUND"
            time.sleep(.025)
        return "RESTORE_CONFIRMATION_UNKNOWN_NO_RETRY"
