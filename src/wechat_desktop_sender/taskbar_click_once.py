"""One taskbar calibration capture, one calibrated click, OR one MOVE only.

Default is no I/O. No SetForegroundWindow, editor click, typing, send or restore.
Only the primary taskbar may be captured. All unknown outcomes stop, no retry.
"""
from __future__ import annotations
import argparse
import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
import sys
import time

from .app_paths import SCRATCH as ROOT
from .diagnostics import discard_image
from .live_capture_once import Stop, save
from .native_focus_once import capture_class, countdown, timing
from .native_win32 import (NativePort, MONITORINFO, contains, intersects,
                          make_run_clock, require_run_window, require_initial_idle,
                          click_dispatch_state)


def load_binding(path):
    path = Path(path).resolve()
    from .app_paths import CALIBRATION
    if path != CALIBRATION.resolve():
        raise Stop("STOP_BINDING_OUTSIDE_APPROVED_CALIBRATION")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("live_ready") is not True or
        not (data.get("human_icon_binding_confirmed") is True or data.get("assistant_visual_icon_identified") is True) or
        data.get("coordinate_space") != "physical_pixels_1_to_1"):
        raise Stop("STOP_TASKBAR_CALIBRATION_NOT_READY")
    # Unknown grouping is an allowed outcome of the one authorized button click,
    # not a precondition. This entry never clicks a preview or advances to typing.
    template_path = (path.parent / data["template_file"]).resolve()
    if not template_path.is_relative_to(path.parent) or sha256(template_path.read_bytes()).hexdigest() != data["template_sha256"]:
        raise Stop("STOP_TASKBAR_TEMPLATE_IDENTITY_CHANGED")
    from PIL import Image
    with Image.open(template_path) as source:
        template = source.convert("RGB")
    if not (16 <= template.width <= 96 and 16 <= template.height <= 96):
        raise Stop("STOP_TASKBAR_TEMPLATE_SIZE_UNREVIEWED")
    return data, template


def locate_icon(image, template, binding, state):
    """Only match at the reviewed physical scale; no old absolute coordinates."""
    import cv2
    import numpy as np
    bounds = state["bounds"]
    if (state["dpi"] != binding["dpi"] or image.size != (bounds[2]-bounds[0], bounds[3]-bounds[1]) or
        template.width > image.width or template.height > image.height):
        raise Stop("STOP_TASKBAR_SCALE_OR_GEOMETRY_CHANGED")
    source, needle = np.asarray(image.convert("RGB")), np.asarray(template.convert("RGB"))
    if float(needle.std()) < 4:
        raise Stop("STOP_TASKBAR_TEMPLATE_HAS_NO_DISCRIMINATING_PIXELS")
    scores = cv2.matchTemplate(source, needle, cv2.TM_CCOEFF_NORMED)
    if not np.isfinite(scores).all():
        raise Stop("STOP_TASKBAR_MATCH_UNKNOWN")
    _, best, _, position = cv2.minMaxLoc(scores)
    x, y = position
    if best < .97:
        raise Stop("STOP_TASKBAR_ICON_NOT_CONFIDENTLY_FOUND")
    # Suppress only the same icon's nearby correlation peaks; a second icon or
    # similar button remains an ambiguity even if its score is slightly lower.
    remaining = scores.copy()
    tw, th = template.size
    remaining[max(0,y-th//2):y+th//2+1, max(0,x-tw//2):x+tw//2+1] = -1
    runner_up = float(remaining.max())
    if runner_up >= .90:
        raise Stop("STOP_TASKBAR_DUPLICATE_OR_SIMILAR_ICON")
    box = [x, y, x+tw, y+th]
    point = [bounds[0] + x + tw//2, bounds[1] + y + th//2]
    return {"icon_box_image": box, "point_screen": point, "score": float(best),
            "runner_up_score": runner_up, "scale": 1,
            "grouping_proven_by_pixels": False, "send_eligible": False}


class TaskbarPort(NativePort):
    def __init__(self, folder, report, *, authorized=False):
        super().__init__(folder, report, authorized=authorized)
        self.user.WindowFromPoint.argtypes = [w.POINT]
        self.user.WindowFromPoint.restype = w.HWND

    def call(self, module, name, args, **kwargs):
        if name in {"SetForegroundWindow", "FlashWindow", "FlashWindowEx", "AttachThreadInput", "SetWindowPos"}:
            raise Stop("STOP_PROGRAMMATIC_ACTIVATION_FORBIDDEN_IN_TASKBAR_ROUTE")
        return super().call(module, name, args, **kwargs)

    def taskbar_state(self):
        ordered = self.windows()
        bars = []
        for hwnd in ordered:
            name = c.create_unicode_buffer(256)
            self.u("GetClassNameW", hwnd, name, len(name), require=True)
            if name.value == "Shell_TrayWnd" and self.u("IsWindowVisible", hwnd):
                bars.append(hwnd)
        if len(bars) != 1:
            raise Stop("STOP_PRIMARY_TASKBAR_NOT_UNIQUE")
        hwnd = bars[0]
        bounds = self.bounds(hwnd)
        monitor = self.u("MonitorFromWindow", hwnd, 0, require=True,
                         semantics="no_GetLastError_contract_null_unknown")
        info = MONITORINFO()
        info.cbSize = c.sizeof(info)
        self.u("GetMonitorInfoW", monitor, c.byref(info), require=True,
               semantics="no_GetLastError_contract_zero_failure")
        screen = [info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom]
        # This narrow trial supports only the fully visible bottom primary bar.
        # Auto-hide, another edge, changed scale and obscuration stop, no reveal.
        if not info.dwFlags & 1 or not contains(screen, bounds) or bounds[3] != screen[3] or not 24 <= bounds[3]-bounds[1] <= 180:
            raise Stop("STOP_PRIMARY_BOTTOM_TASKBAR_LAYOUT_UNKNOWN")
        for other in ordered:
            if other == hwnd:
                break
            if self.u("IsWindowVisible", other) and not self.u("IsIconic", other):
                cloaked = w.DWORD()
                hr, _ = self.call(self.dwm, "DwmGetWindowAttribute", [other, 14, c.byref(cloaked), c.sizeof(cloaked)],
                                  error_semantics="HRESULT_not_GetLastError")
                if hr or (not cloaked.value and intersects(bounds, self.bounds(other))):
                    raise Stop("STOP_TASKBAR_OCCLUDED_OR_VISIBILITY_UNKNOWN")
        return {"owner": self.owner(hwnd), "bounds": bounds,
                "dpi": int(self.u("GetDpiForWindow", hwnd, require=True)),
                "monitor_bounds": screen, "monitor_primary": True,
                "virtual": [int(self.u("GetSystemMetrics", n)) for n in (76,77,78,79)],
                "coordinate_space": "physical_pixels_1_to_1"}

    def taskbar_click(self, target, original, baseline, state, captured_ns, match, *, move_only=False):
        self.require_activity(baseline)
        if self.app(target["hwnd"]) != target or self.foreground() != original["hwnd"]:
            raise Stop("STOP_TASKBAR_TARGET_OR_FOREGROUND_CHANGED")
        after = self.taskbar_state()
        if state != after or not 0 <= time.perf_counter_ns()-captured_ns <= 2_000_000_000:
            raise Stop("STOP_TASKBAR_FRAME_STALE_OR_MOVED")
        point = match["point_screen"]
        if not contains(after["bounds"], [point[0],point[1],point[0]+1,point[1]+1]):
            raise Stop("STOP_POINT_OUTSIDE_TASKBAR")
        hit = self.u("WindowFromPoint", w.POINT(*point), require=True,
                     semantics="no_GetLastError_contract_null_unknown")
        root = self.u("GetAncestor", hit, 2, require=True,
                      semantics="no_GetLastError_contract_null_unknown")
        self.report["taskbar_point_hit"] = {"hwnd": int(hit), "root": int(root)}
        if root != state["owner"]["hwnd"]:
            raise Stop("STOP_TASKBAR_POINT_COVERED_BY_OTHER_WINDOW")
        before_input = self.require_activity(baseline)
        if move_only:
            self.report["move_before"] = dict(before_input)
        require_run_window(self.report, "immediately_before_taskbar_move" if move_only else "immediately_before_taskbar_click")
        if not 0 <= time.perf_counter_ns()-captured_ns <= 2_000_000_000:
            raise Stop("STOP_TASKBAR_FRAME_STALE_OR_MOVED")
        if move_only:
            return self.dispatch_mouse_move_once(point, state["virtual"])
        return self.dispatch_left_click_once(point, state["virtual"])

    def confirm_taskbar_move(self, original, baseline, injected_tick, point):
        samples = self.report["move_observations"] = []
        self.report.update(move_after=None, move_observation_limit_ms=500,
                           move_position_tolerance_physical_px=1)
        deadline = time.perf_counter() + .5
        while time.perf_counter() < deadline:
            row = {"observed_ns": time.perf_counter_ns(), "foreground_hwnd": self.foreground()}
            samples.append(row)
            if row["foreground_hwnd"] != original["hwnd"]:
                raise Stop("STOP_MOVE_FOREGROUND_CHANGED")
            observed = self.activity()
            row.update(activity=dict(observed), input_tick_matches_requested=observed["tick"] == injected_tick)
            self.report["move_after"] = dict(observed)
            # Read actual position independently of SendInput's return or event
            # timestamp. Allow one physical pixel of absolute-input quantization.
            if max(abs(observed["cursor"][i]-point[i]) for i in (0,1)) <= 1:
                changed = observed["cursor"] != baseline["cursor"]
                self.report["motion_observed"] = changed
                return "MOVE_TARGET_POSITION_OBSERVED" if changed else "MOVE_TARGET_ALREADY_PRESENT_NO_CHANGE_EVIDENCE"
            if observed["cursor"] != baseline["cursor"] or observed["tick"] not in (baseline["tick"], injected_tick):
                raise Stop("STOP_MOVE_OTHER_POSITION_OR_USER_ACTIVITY")
            time.sleep(.025)
        self.report["motion_observed"] = False
        return "MOVE_TARGET_NOT_OBSERVED"

    def confirm_taskbar_result(self, target, original, baseline, injected_tick, point, taskbar_hwnd):
        samples = self.report["taskbar_foreground_observations"] = []
        deadline = time.perf_counter() + 1
        while time.perf_counter() < deadline:
            current = self.foreground()
            row = {"hwnd": current, "observed_ns": time.perf_counter_ns()}
            samples.append(row)
            if current in (0, taskbar_hwnd):
                row["state"] = "TRANSITION_NOT_VERIFIED"
            elif current not in (target["hwnd"], original["hwnd"]):
                row["state"] = "OTHER_FOREGROUND_OR_PREVIEW_POSSIBLE"
                raise Stop("STOP_TASKBAR_OTHER_FOREGROUND_OR_GROUP_PREVIEW")
            else:
                activity = self.activity()
                row["dispatch"] = click_dispatch_state(baseline, activity, injected_tick, point)
                if row["dispatch"] == "UNEXPECTED_INPUT_OR_CURSOR_CHANGE":
                    raise Stop("STOP_TASKBAR_USER_INPUT_OR_ATTRIBUTION_UNKNOWN")
                if row["dispatch"] == "OWN_INPUT_OBSERVED" and current == target["hwnd"]:
                    self.guard(target)  # Current HWND/PID/TID, visibility and occlusion.
                    row["owner"] = self.owner(current)
                    if row["owner"] != {key: target[key] for key in ("hwnd", "pid", "thread_id")}:
                        raise Stop("STOP_TASKBAR_FOREGROUND_INSTANCE_CHANGED")
                    return "TASKBAR_TARGET_FOREGROUND_OBSERVED"
            time.sleep(.025)
        raise Stop("STOP_TASKBAR_FOREGROUND_UNKNOWN_NO_RETRY")


def run(args):
    if not (args.authorize_desktop_io and args.human_work_account_confirmed and args.human_hands_off_confirmed):
        raise Stop("DESKTOP_IO_DISABLED")
    started_ns, started_utc = time.perf_counter_ns(), datetime.now(timezone.utc)
    folder = ROOT / "evidence/taskbar_once" / args.attempt_id
    folder.mkdir(parents=True, exist_ok=False)
    report = {"status":"STARTING", "started_utc":started_utc.isoformat(), "api_calls":[], "timings_ns":{},
              "capture_calls":0, "click_attempts":0, "move_attempts":0, "sendinput_calls":0, "text_input_calls":0,
              "activation_calls":0, "restore_calls":0, "sends":0, "retry_allowed":False,
              "human_hands_off_confirmed":True, "work_account_basis":"current_explicit_human_confirmation",
              "send_eligible":False, "editor_focus_verified":False, "pending_desktop_actions":False,
              "grouping_state":"not_applicable_move_only" if args.move_only else "unknown_allowed_for_one_button_click",
              "allow_existing_foreground":getattr(args, "allow_existing_foreground", False) is True,
              "preview_clicks":0, "mode":"move_only" if args.move_only else "click_once" if args.click_once else "capture_calibration"}
    port = capture = image = template = None
    try:
        report["run_clock"] = make_run_clock(started_ns, started_utc, args.authorized_at_utc)
        require_run_window(report, "before_initialization")
        capture_type = capture_class()
        if args.click_once or args.move_only:
            if not args.binding:
                raise Stop("STOP_TASKBAR_BINDING_REQUIRED")
            binding, template = load_binding(args.binding)
            import cv2, numpy  # Existing dependencies only; no download/install.
        require_run_window(report, "before_countdown", reserve_ms=args.delay_seconds*1000)
        countdown(args.delay_seconds, report, "handoff_countdown")
        require_run_window(report, "after_countdown")
        port = TaskbarPort(folder, report, authorized=True)
        capture = capture_type()  # Same reviewed capture interface, thread-local PMv2.
        with timing(report, "target_binding"):
            target = port.select_target(None)
            port.guard(target, foreground=False)
            original_hwnd = port.foreground()
            if not original_hwnd:
                raise Stop("STOP_PREVIOUS_FOREGROUND_UNKNOWN")
            original = port.owner(original_hwnd)
            report.update(bound_window=target, previous_window=original)
            if args.click_once and original_hwnd == target["hwnd"]:
                if not report["allow_existing_foreground"]:
                    raise Stop("STOP_ALREADY_FOREGROUND_TASKBAR_CLICK_COULD_MINIMIZE")
                baseline = port.activity()
                require_initial_idle(report, baseline)
                fresh = port.guard(target)  # Current foreground, identity and geometry.
                current = port.foreground()
                owner = port.owner(current)
                if current != target["hwnd"] or any(owner.get(key) != target.get(key) for key in ("hwnd", "pid", "thread_id")):
                    raise Stop("STOP_TASKBAR_FOREGROUND_INSTANCE_CHANGED")
                port.require_activity(baseline)
                require_run_window(report, "existing_foreground_handoff")
                report["existing_foreground_observation"] = {
                    "observed_ns":time.perf_counter_ns(), "foreground_hwnd":current,
                    "owner":owner, "guard":fresh, "idle_snapshot":baseline,
                    "fresh_recheck":True}
                report["status"] = "TASKBAR_TARGET_ALREADY_FOREGROUND_OBSERVED"
                report["grouping_state"] = "not_applicable_existing_foreground_no_taskbar_action"
                return 0
            baseline = port.activity()
            require_initial_idle(report, baseline)
            state = port.taskbar_state()
            report["taskbar_geometry"] = state
        with timing(report, "taskbar_capture"):
            port.require_activity(baseline)
            require_run_window(report, "before_taskbar_capture")
            captured_ns = time.perf_counter_ns()
            report["capture_calls"] = 1
            save(folder / "result.json", report)
            image, details = capture.copy_rectangle_once(state["bounds"])
            if port.taskbar_state() != state:
                raise Stop("STOP_TASKBAR_CHANGED_DURING_CAPTURE")
            port.require_activity(baseline)
            report["capture_details"] = details
            report["taskbar_capture_validated"] = True
        if args.capture_calibration:
            report["status"] = "TASKBAR_CALIBRATION_CAPTURED_NO_CLICK"
            return 0
        with timing(report, "icon_localization"):
            match = locate_icon(image, template, binding, state)
            report["icon_match"] = match
        if args.move_only:
            with timing(report, "taskbar_move"):
                tick = port.taskbar_click(target, original, baseline, state, captured_ns, match, move_only=True)
            with timing(report, "move_observation"):
                report["position_outcome"] = port.confirm_taskbar_move(original, baseline, tick, match["point_screen"])
                report["status"] = report["position_outcome"] if report["move_inserted"] else "STOP_SENDINPUT_MOVE_NOT_INSERTED_NO_RETRY"
            return 0 if report["status"] == "MOVE_TARGET_POSITION_OBSERVED" else 2
        with timing(report, "taskbar_click"):
            tick = port.taskbar_click(target, original, baseline, state, captured_ns, match)
        with timing(report, "foreground_verification"):
            report["status"] = port.confirm_taskbar_result(target, original, baseline, tick,
                                                           match["point_screen"], state["owner"]["hwnd"])
    except Stop as error:
        report["status"] = str(error)
    except Exception as error:
        report.update(status="STOP_UNEXPECTED_ERROR", error={"type":type(error).__name__, "message":str(error)})
    finally:
        if image is not None:
            try:
                if report.get("taskbar_capture_validated"):
                    discard_image(image, folder / "primary-taskbar-local.png")
            except Exception as error:
                report["image_save_error"] = str(error)
            finally:
                image.close()
        if template is not None:
            template.close()
        if capture is not None and port is not None:
            try:
                port.u("SetThreadDpiAwarenessContext", capture.old_dpi, require=True)
            except Exception as error:
                report["dpi_cleanup_error"] = str(error)
        report["timings_ns"]["process_elapsed"] = time.perf_counter_ns()-started_ns
        report["restore_result"] = "NOT_REQUESTED_TASKBAR_ONLY_TRIAL_NO_PROGRAMMATIC_RESTORE"
        save(folder / "result.json", report)
        print(json.dumps({k:report[k] for k in ("status","capture_calls","click_attempts","move_attempts","sends","pending_desktop_actions")}))
    return 0 if report["status"] == "TASKBAR_TARGET_FOREGROUND_OBSERVED" else 2

