"""Extracted original live OCR search and exact numbered-chat selection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import secrets
import sys
import time

from .app_paths import SCRATCH as ROOT, PROFILE, TARGET_ALIAS, ALLOWED_ALIASES
from .diagnostics import load_report, discard_image
from .live_capture_once import Stop, save

ALIAS = TARGET_ALIAS


class PopupBoundsChanged(Stop):
    """A read-only observation needs refreshing before the first click."""
    def __init__(self, previous, current):
        super().__init__("STOP_RESULTS_POPUP_IDENTITY_OR_BOUNDS_CHANGED")
        self.previous = dict(previous)
        self.current = dict(current)


SEARCH_STRIP = PROFILE["search_strip"]
RESULTS_PANE = PROFILE["results_pane"]
TITLE_STRIP = PROFILE["title_strip"]
from .app_paths import CALIBRATION as TASKBAR_BINDING


def search_layout(initial, report):
    """Top-left UI anchors are DPI-specific, not tied to total window size.

    Whole-window resizing does not rescale those controls. Observe field/text
    pixels at run time; only the title strip's right edge tracks current width.
    These are capture regions, never precomputed screen click coordinates.
    """
    x, y, right, bottom = initial["bounds"]
    width, height = right-x, bottom-y
    dpi = initial["geometry"]["dpi"]
    diagnostic = {"window_bounds": list(initial["bounds"]), "window_size": [width, height],
                  "window_dpi": dpi, "reviewed_control_dpi": PROFILE["dpi"],
                  "coordinate_space": "physical_screen_pixels", "capture_scale": 1,
                  "whole_window_size_locked": False,
                  "search_strip_relative": list(SEARCH_STRIP),
                  "title_strip_relative": [TITLE_STRIP[0], 0, width-2, TITLE_STRIP[3]]}
    report["observed_search_layout"] = diagnostic  # Save before any stop branch.
    if dpi != PROFILE["dpi"]:
        diagnostic["decision"] = "CONTROL_DPI_NOT_REVIEWED"
        raise Stop("STOP_UNREVIEWED_SEARCH_CONTROL_DPI")
    if width <= max(SEARCH_STRIP[2], TITLE_STRIP[0]+100)+2 or height <= max(SEARCH_STRIP[3], TITLE_STRIP[3])+2:
        diagnostic["decision"] = "CONTROL_CAPTURE_REGIONS_DO_NOT_FIT"
        raise Stop("STOP_SEARCH_LAYOUT_TOO_SMALL")
    diagnostic["decision"] = "DPI_MATCHES_CONTROL_REGIONS_FIT"
    return diagnostic


def tokens(engine, image, report):
    import numpy as np
    start = time.perf_counter_ns()
    rows, _ = engine(np.asarray(image.convert("RGB")))
    report["ocr_calls"] += 1
    report["ocr_inference_ns"] += time.perf_counter_ns() - start
    result = []
    for box, text, score in rows or []:
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        bounds = [min(xs), min(ys), max(xs), max(ys)]
        result.append({"text": text, "score": float(score), "box": bounds,
                       "complete": 0 < bounds[0] < bounds[2] < image.width - 1 and
                                   0 < bounds[1] < bounds[3] < image.height - 1})
    return result


def exact_token(rows, wanted):
    matches = [r for r in rows if r["text"] == wanted]
    if len(matches) != 1 or matches[0]["score"] < .95 or not matches[0]["complete"]:
        raise Stop("STOP_TEXT_NOT_EXACT_COMPLETE_UNIQUE")
    return matches[0]


def search_field(image, engine, report, wanted="搜索"):
    """Observe the rounded field, then its exact label; no fixed click point."""
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    background = float(np.median(gray[:8, :]))
    mask = (gray > background + 8).astype("uint8") * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if (200 <= width <= 350 and 28 <= height <= 70 and x > 1 and y > 1 and
                x + width < image.width - 1 and y + height < image.height - 1 and
                cv2.contourArea(contour) > width * height * .7):
            boxes.append([x, y, x + width, y + height])
    if len(boxes) != 1:
        raise Stop("STOP_SEARCH_FIELD_OUTLINE_NOT_UNIQUE")
    box = boxes[0]
    field = image.crop(tuple(box))
    label = enlarged = padded = None
    try:
        # The observed 44px-high field is too short for the detector, and the
        # magnifier can be read as Q/punctuation. Separate that observed icon,
        # then enlarge only the label pixels. This never changes click geometry.
        pixels = np.asarray(field.convert("RGB"))
        field_gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
        background = np.median(pixels.reshape(-1, 3), axis=0)
        ink = (field_gray > float(np.median(field_gray)) + 12).astype("uint8") * 255
        _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
        icons = []
        for x, y, width, height, area in stats[1:].tolist():
            if (1 < x and x + width < field.height and 1 < y and y + height < field.height - 1 and
                    field.height * .25 <= width <= field.height * .8 and
                    field.height * .25 <= height <= field.height * .8 and area >= 40):
                icons.append([x, y, x + width, y + height])
        if len(icons) != 1:
            raise Stop("STOP_SEARCH_MAGNIFIER_OUTLINE_NOT_UNIQUE")
        if wanted is None:
            report.setdefault("search_field_ocr", []).append({"wanted":None,"field_box_in_strip":box,
                "magnifier_box_in_field":icons[0],"observed_control_state":"SEARCH_OUTLINE_AND_ICON",
                "text_will_require_native_focus_before_replacement":True})
            return box
        left = icons[0][2] + max(3, round(field.height * .14))
        label = field.crop((left, 0, field.width, field.height))
        scale, padding = 3, 12
        enlarged = label.resize((label.width * scale, label.height * scale), Image.Resampling.LANCZOS)
        padded = ImageOps.expand(enlarged, border=padding, fill=tuple(int(v) for v in background))
        rows = tokens(engine, padded, report)
        matches = [r for r in rows if r["text"] == wanted]
        diagnostic = {"wanted": wanted, "field_box_in_strip": box, "magnifier_box_in_field": icons[0],
                      "label_left_in_field": left, "ocr_scale": scale, "ocr_padding": padding,
                      "detected_count": len(rows), "exact_match_count": len(matches),
                      "exact_matches": [{"score": r["score"], "complete": r["complete"]} for r in matches],
                      "nonmatching_text_lengths": [len(r["text"]) for r in rows if r["text"] != wanted],
                      "click_coordinates_scale": 1}
        report.setdefault("search_field_ocr", []).append(diagnostic)
        known_labels = [r["text"] for r in rows if r["text"] in ("搜索", ALIAS) and
                        r["score"] >= .95 and r["complete"]]
        diagnostic["recognized_control_labels"] = known_labels
        if wanted == ALIAS and len(rows) == 1 and known_labels == ["搜索"]:
            diagnostic["observed_control_state"] = "SEARCH_PLACEHOLDER_VISIBLE"
            raise Stop("STOP_EXPECTED_QUERY_NOT_PRESENT")
        match = exact_token(rows, wanted)
        diagnostic["observed_control_state"] = "EXPECTED_TEXT_VISIBLE"
        b = match["box"]
        if not (padding < b[0] < b[2] < padding + label.width * scale and
                padding < b[1] < b[3] < padding + label.height * scale):
            raise Stop("STOP_SEARCH_LABEL_TOUCHES_ORIGINAL_CROP_EDGE")
        diagnostic["matched_box_in_original_strip"] = [box[0] + left + (b[0]-padding)/scale,
            box[1] + (b[1]-padding)/scale, box[0] + left + (b[2]-padding)/scale, box[1] + (b[3]-padding)/scale]
    finally:
        for derived in (label, enlarged, padded):
            if derived is not None:
                derived.close()
        field.close()
    return box


GROUP_HEADERS = {"群聊", "群聊(1)", "群聊（1）"}
OTHER_HEADERS = {"联系人", "聊天记录", "公众号", "小程序", "文件", "功能", "搜索更多", "更多"}


def category_name(text):
    """Normalize category count decoration only, never target text."""
    match = re.fullmatch(r"(群聊|联系人|聊天记录|公众号|小程序|文件|功能|搜索更多|更多)(?:\s*[（(]\d+[）)])?", text)
    return match.group(1) if match else None


def category_lane_width(field, popup_width):
    """A scale-aware seed column; clipped labels are completed row by row."""
    return min(popup_width, math.ceil((field[3]-field[1])*2.5))


def category_label_probes(rows, lane_width, popup_width, popup_height):
    """Complete a clipped category using a short label-only row, not a wider
    full-height strip that would include message-preview text.
    """
    probes = []
    for row in rows:
        text, box = row["text"], row["box"]
        if row["score"] < .95 or row["complete"] or box[2] < lane_width-2 or len(text) < 2:
            continue
        bases = [name for name in {"群聊"} | OTHER_HEADERS if name.startswith(text) or text.startswith(name)]
        if len(bases) != 1:
            continue
        glyph_height = box[3]-box[1]
        right = min(popup_width-1, math.ceil(box[0] + (len(bases[0])+4)*glyph_height + 8))
        probes.append({"expected_category":bases[0], "prefix":text,
            "box_in_popup":[max(1,math.floor(box[0])-8),max(1,math.floor(box[1])-8),right,
                            min(popup_height-1,math.ceil(box[3])+8)]})
    return probes


def read_category_label(engine, image, expected, report):
    """Three-times OCR of a short category label; return original-pixel boxes."""
    from PIL import Image, ImageOps
    import numpy as np
    pixels = np.asarray(image.convert("RGB"))
    background = tuple(int(v) for v in np.median(pixels.reshape(-1,3),axis=0))
    scale, pad = 3, 12
    enlarged = image.resize((image.width*scale,image.height*scale),Image.Resampling.LANCZOS)
    padded = ImageOps.expand(enlarged,border=pad,fill=background)
    try:
        rows = tokens(engine,padded,report)
        found = [r for r in rows if category_name(r["text"]) == expected and r["score"] >= .95 and r["complete"]]
        if len(found) != 1:
            raise Stop("STOP_CATEGORY_LABEL_PATCH_NOT_COMPLETE_UNIQUE")
        row = dict(found[0]); b=row["box"]
        if not (pad < b[0] < b[2] < pad+image.width*scale and pad < b[1] < b[3] < pad+image.height*scale):
            raise Stop("STOP_CATEGORY_LABEL_PATCH_CLIPPED")
        row["box"] = [(b[0]-pad)/scale,(b[1]-pad)/scale,(b[2]-pad)/scale,(b[3]-pad)/scale]
        return row
    finally:
        enlarged.close(); padded.close()


def group_section(rows, height):
    headers = [r for r in rows if category_name(r["text"]) == "群聊" and
               r["score"] >= .95 and r["complete"]]
    if len(headers) != 1:
        raise Stop("STOP_GROUP_SECTION_NOT_VERIFIED")
    header = headers[0]
    following_rows = [r for r in rows if category_name(r["text"]) in OTHER_HEADERS and
                      r["box"][1] > header["box"][3]]
    if any(r["score"] < .95 or not r["complete"] for r in following_rows):
        raise Stop("STOP_CATEGORY_BOUNDARY_OCR_UNCERTAIN")
    following = [r["box"][1] for r in following_rows]
    end = min(following, default=height)
    return header, end


def group_capture_box(section, end, popup_size, end_observed):
    """Only the observed group section, inside the Qt panel's shadow margins."""
    width, height = popup_size
    top = max(1, int(section["box"][1])-4)
    bottom = min(height-1, int(end)-4)
    if bottom <= section["box"][3]+20:
        raise Stop("STOP_GROUP_SECTION_EMPTY_OR_CLIPPED")
    if not end_observed:
        raise Stop("STOP_GROUP_BOUNDARY_UNVERIFIED_NO_CANDIDATE_CAPTURE")
    left = max(1, math.floor(section["box"][0])-8)
    right = width-left
    if right <= left or section["box"][2] >= right:
        raise Stop("STOP_GROUP_CONTENT_COLUMN_NOT_VERIFIED")
    return [left, top, right, bottom]


def screen_box_from_crop(box, capture_bounds):
    """OCR coordinates are original capture pixels, with this frame's origin."""
    x, y = capture_bounds[:2]
    return [box[0]+x, box[1]+y, box[2]+x, box[3]+y]


def unique_group_candidate(rows, height=float("inf"), *, verified_section_top=None):
    """Exact visible result only; do not infer global identity or a real group ID."""
    if verified_section_top is None:
        header, end = group_section(rows, height)
        section_top = header["box"][3]
    else:
        # The category lane already established this crop, and its popup is
        # pinned before/after both captures. Do not demand a second successful
        # OCR of the same header merely to permit recognition of the candidate.
        section_top, end = verified_section_top, height
        following = [r for r in rows if category_name(r["text"]) in OTHER_HEADERS and r["box"][1] > section_top]
        if any(r["score"] < .95 or not r["complete"] for r in following):
            raise Stop("STOP_CATEGORY_BOUNDARY_OCR_UNCERTAIN")
        end = min((r["box"][1] for r in following), default=end)
    # Same text in a contact section is not another group candidate.
    group_rows = [r for r in rows if section_top < r["box"][1] and r["box"][1] < end]
    aliases = [r for r in group_rows if r["text"] == ALIAS]
    suspicious = [r for r in group_rows if r["text"] != ALIAS and
                  (ALIAS in r["text"] or r["text"].translate(str.maketrans("OoIl", "0011")) == ALIAS)]
    if suspicious or len(aliases) != 1:
        raise Stop("STOP_ALIAS_AMBIGUOUS_OR_NOT_EXACT")
    alias = exact_token(group_rows, ALIAS)
    if alias["box"][3] >= end:
        raise Stop("STOP_ALIAS_OUTSIDE_GROUP_SECTION")
    return {"alias": ALIAS, "alias_box": alias["box"], "kind": "group",
            "visible_exact_match_count": 1, "global_uniqueness_proven": False,
            "human_initial_binding_required": True}


def unique_chat_entry(rows, image, alias=ALIAS, diagnostic=None):
    """Find one complete numbered chat row, independent of category wording.

    A result title has an avatar immediately to its left. Message-history
    matches are outside the eligible area. Repeated OCR of the same rectangle
    is merged; separate rows remain ambiguous, even when their text is equal.
    """
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert("RGB"))
    background = np.median(pixels.reshape(-1,3),axis=0)
    mask = (np.max(np.abs(pixels.astype(float)-background),axis=2)>16).astype("uint8")*255
    mask = cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    # Native Qt popup bounds include the shadow/outer frame. RETR_EXTERNAL
    # hides avatars nested inside that contour; retain internal contours too.
    contours,_ = cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    shapes = [cv2.boundingRect(c) for c in contours]
    message_start = min((r["box"][1] for r in rows if
        (category_name(r["text"])=="聊天记录" or r["text"] in ("搜索聊天记录","消息记录"))
        and r["score"] >= .8),default=image.height)
    # These are non-chat UI actions/results, not admission categories. The
    # network lookup action has a large square add-person icon, which must not
    # be mistaken for a chat avatar. Web results may repeat the exact query.
    lookup_rows = [r for r in rows if
        r["text"].replace(" ", "").startswith("网络查找手机/QQ号")]
    lookup_start = min((r["box"][1] for r in lookup_rows),default=image.height)
    network_start = min((r["box"][1] for r in rows if
        r["text"].replace(" ", "") == "搜索网络结果" and r["score"] >= .8),
        default=image.height)
    # The lookup action starts the non-chat part even while its asynchronous
    # web-result heading is absent or has not been painted yet.
    non_chat_start = min(message_start, network_start, lookup_start)
    diagnostic = {} if diagnostic is None else diagnostic
    diagnostic.update(exact_text_count=sum(r["text"] == alias for r in rows),
                      exact_rows=[], eligible_chat_row_count=0,
                      message_history_boundary_y=message_start,
                      network_results_boundary_y=network_start,
                      network_lookup_boundary_y=lookup_start,
                      ignored_network_action_count=len(lookup_rows),
                      image_size=list(image.size), category_required=False)
    candidates=[]
    for row in rows:
        b=row["box"]; height=b[3]-b[1]; middle=(b[1]+b[3])/2
        detail = None
        if row["text"] == alias:
            detail = {"box":list(b),"score":row["score"],"complete":row["complete"]}
            diagnostic["exact_rows"].append(detail)
        if b[3] >= non_chat_start or height<=0:
            if detail is not None:
                detail["decision"] = ("EXCLUDED_NETWORK_RESULT" if b[3] >= min(network_start,lookup_start)
                                      else "EXCLUDED_MESSAGE_HISTORY_OR_INVALID_BOX")
            continue
        avatars = [[x,y,x+w,y+h] for x,y,w,h in shapes if
            .8 <= w/h <= 1.25 and 1.3*height <= h <= 3*height and
            y < middle < y+h and 0 <= b[0]-(x+w) <= 2*height]
        if not avatars:
            if detail is not None:
                detail["decision"] = "AVATAR_SHAPE_NOT_FOUND"
                detail["avatar_count"] = 0
            continue  # A bare number/message-body occurrence is not a chat row.
        if row["text"] != alias:
            if alias in row["text"] or row["text"].translate(str.maketrans("OoIl","0011")) == alias:
                raise Stop("STOP_NUMBERED_CHAT_ENTRY_NOT_EXACT")
            continue
        if not row["complete"] or row["score"] < .95:
            if detail is not None:
                detail["decision"] = "INCOMPLETE_OR_LOW_CONFIDENCE"
            raise Stop("STOP_NUMBERED_CHAT_ENTRY_OCR_UNCERTAIN")
        candidate={**row,"avatar_box":min(avatars,key=lambda a:b[0]-a[2])}
        if detail is not None:
            detail.update(avatar_count=len(avatars),avatar_box=candidate["avatar_box"])
        # Same text box emitted twice is one observation, not two identities.
        if any(max(abs(b[i]-old["box"][i]) for i in range(4)) <= 3 for old in candidates):
            if detail is not None:
                detail["decision"] = "SAME_OCR_RECTANGLE_MERGED"
            continue
        candidates.append(candidate)
        diagnostic["eligible_chat_row_count"] = len(candidates)
        if detail is not None:
            detail["decision"] = "ELIGIBLE_CHAT_ROW"
    diagnostic["eligible_chat_row_count"] = len(candidates)
    diagnostic["decision"] = ("UNIQUE" if len(candidates)==1 else
                              "NO_ELIGIBLE_CHAT_ROW" if not candidates else
                              "MULTIPLE_ELIGIBLE_CHAT_ROWS")
    if len(candidates)!=1:
        raise Stop("STOP_NUMBERED_CHAT_ENTRY_AMBIGUOUS_OR_MISSING")
    match=candidates[0]
    return {"alias":alias,"alias_box":match["box"],"avatar_box":match["avatar_box"],
            "kind":"chat_entry","visible_exact_match_count":1,"category_required":False,
            "message_history_excluded_from_y":message_start,"global_uniqueness_proven":False,
            "human_initial_binding_required":True}


def opened_remark_matches(rows, alias):
    pattern = re.compile(re.escape(alias)+r"(?:\s*[（(]\d+[）)])?")
    return len([r for r in rows if r["complete"] and r["score"]>=.95 and pattern.fullmatch(r["text"])]) == 1


def load_human_binding(path, alias=ALIAS):
    if path is None:
        return None  # Fresh exact remark verification remains mandatory in this app.
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("human_confirmed") is not True:
        return None
    if (value.get("schema_version") != 1 or value.get("search_alias") != alias or
            value.get("target_kind") != "group" or not value.get("expected_open_title") or
            not value.get("confirmed_by") or not value.get("confirmed_at_utc")):
        raise Stop("STOP_HUMAN_BINDING_INCOMPLETE")
    evidence = (ROOT / value.get("source_result", "")).resolve()
    if ROOT not in evidence.parents or not evidence.is_file():
        raise Stop("STOP_BINDING_EVIDENCE_OUTSIDE_PROJECT_OR_MISSING")
    if sha256(evidence.read_bytes()).hexdigest() != value.get("source_result_sha256"):
        raise Stop("STOP_BINDING_EVIDENCE_HASH_CHANGED")
    old = json.loads(evidence.read_text(encoding="utf-8"))
    if old.get("status") not in ("AWAITING_HUMAN_TARGET_BINDING", "AWAITING_HUMAN_OPEN_TITLE_BINDING") or old.get("candidate", {}).get("alias") != alias:
        raise Stop("STOP_BINDING_SOURCE_NOT_A_REVIEWED_CANDIDATE")
    if old.get("status") == "AWAITING_HUMAN_OPEN_TITLE_BINDING" and value["expected_open_title"] not in [
            r["text"] for r in old.get("opened_header_candidates", []) if r["complete"] and r["score"] >= .95]:
        raise Stop("STOP_CONFIRMED_TITLE_NOT_IN_CAPTURED_HEADER")
    return value


def inherit_taskbar_context(report, taskbar):
    """Carry this same run's confirmed context; never fabricate new confirmation."""
    status = taskbar.get("status")
    if status == "TASKBAR_TARGET_FOREGROUND_OBSERVED":
        disposition = "TASKBAR_CLICK_AND_FOREGROUND_OBSERVED"
    elif status == "TASKBAR_TARGET_ALREADY_FOREGROUND_OBSERVED":
        target, previous = taskbar.get("bound_window", {}), taskbar.get("previous_window", {})
        observed = taskbar.get("existing_foreground_observation", {})
        if (taskbar.get("allow_existing_foreground") is not True or observed.get("fresh_recheck") is not True or
                observed.get("foreground_hwnd") != target.get("hwnd") or
                any(taskbar.get(key, -1) != 0 for key in ("capture_calls", "click_attempts", "sendinput_calls")) or
                any(not target.get(key) or target.get(key) != previous.get(key) or
                    target.get(key) != observed.get("owner", {}).get(key) for key in ("hwnd", "pid", "thread_id"))):
            raise Stop("STOP_ALREADY_FOREGROUND_CONTEXT_INCONSISTENT")
        disposition = "ALREADY_FOREGROUND_FRESHLY_OBSERVED_NO_TASKBAR_ACTION"
    elif status == "STOP_ALREADY_FOREGROUND_TASKBAR_CLICK_COULD_MINIMIZE":
        target, previous = taskbar.get("bound_window", {}), taskbar.get("previous_window", {})
        if (any(taskbar.get(key, -1) != 0 for key in ("capture_calls", "click_attempts", "sendinput_calls")) or
                any(not target.get(key) or target.get(key) != previous.get(key) for key in ("hwnd", "pid", "thread_id"))):
            raise Stop("STOP_ALREADY_FOREGROUND_CONTEXT_INCONSISTENT")
        disposition = "ALREADY_FOREGROUND_NO_TASKBAR_ACTION_FRESH_RECHECK_REQUIRED"
    else:
        raise Stop("STOP_TASKBAR_STAGE_" + str(status))
    if taskbar.get("human_hands_off_confirmed") is not True:
        raise Stop("STOP_TASKBAR_HANDS_OFF_CONFIRMATION_NOT_PROPAGATABLE")
    for key in ("bound_window", "previous_window"):
        if not all(taskbar.get(key, {}).get(field) for field in ("hwnd", "pid", "thread_id")):
            raise Stop("STOP_TASKBAR_WINDOW_CONTEXT_MISSING")
    if not taskbar.get("run_clock"):
        raise Stop("STOP_TASKBAR_RUN_CLOCK_MISSING")
    report.update(human_hands_off_confirmed=taskbar["human_hands_off_confirmed"],
                  bound_window=dict(taskbar["bound_window"]), previous_window=dict(taskbar["previous_window"]),
                  run_clock=dict(taskbar["run_clock"]), taskbar_stage_disposition=disposition,
                  activity_baseline_basis="fresh post-taskbar read after current foreground/window revalidation; no comparison with pre-click idle tick")
    return report["bound_window"]


def popup_bounds_inside_main(bounds, main_bounds):
    """Clip only the popup bottom to the existing main-window capture limit.

    This never expands the allowed capture area or changes any identity rule.
    A clipped group section is diagnostic only until its end is observed.
    """
    from .native_win32 import contains
    if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4 and
            bounds[2] > bounds[0] and bounds[3] > bounds[1]):
        return None
    visible = [bounds[0], bounds[1], bounds[2], min(bounds[3], main_bounds[3])]
    return visible if visible[3] > visible[1] and contains(main_bounds, visible) else None


def owned_search_popup(metadata, target, envelope, field=None):
    from .native_win32 import contains
    bounds = metadata.get("bounds")
    if not (metadata.get("pid") == target["pid"] and metadata.get("thread_id") == target["thread_id"] and
            isinstance(bounds, (list, tuple)) and len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1] and
            popup_bounds_inside_main(bounds, envelope) is not None):
        return False
    if metadata.get("root_owner_hwnd") == target["hwnd"]:
        return True
    # Observed Qt behavior: Qt's results tool window is its own GA_ROOTOWNER.
    # Accept this specific class only with fresh PID/TID and search anchoring.
    if field is None or metadata.get("class_name") != "Qt51514QWindowToolSaveBits":
        return False
    height, width = field[3]-field[1], field[2]-field[0]
    return (metadata.get("hwnd") == metadata.get("root_owner_hwnd") and
            field[0]-2*height <= bounds[0] <= field[0]+height and
            field[3]-height/2 <= bounds[1] <= field[3]+height and
            width <= bounds[2]-bounds[0] <= 3*width and bounds[3] > field[3])


def region_guard(port, target, region, envelope, field=None, *, require_foreground=True):
    """After typing, check the actual search ROI, not unrelated chat-pane pixels."""
    import ctypes as c
    from ctypes import wintypes as w
    from .native_win32 import intersects
    state = port.guard(target, foreground=False)  # Same identity/geometry/visibility checks.
    if require_foreground and port.foreground() != target["hwnd"]:
        raise Stop("STOP_FOREGROUND_CHANGED")
    observation = {"region": list(region), "overlays": []}
    port.report.setdefault("search_region_visibility", []).append(observation)
    for other in port.windows():
        if other == target["hwnd"]:
            return state
        if not port.u("IsWindowVisible", other) or port.u("IsIconic", other):
            continue
        cloaked = w.DWORD()
        hr, _ = port.call(port.dwm, "DwmGetWindowAttribute", [other, 14, c.byref(cloaked), c.sizeof(cloaked)],
                          error_semantics="HRESULT_not_GetLastError")
        if hr:
            raise Stop("STOP_SEARCH_REGION_VISIBILITY_UNKNOWN")
        if cloaked.value:
            continue
        bounds = port.bounds(other)
        if not intersects(region, bounds):
            continue
        name = c.create_unicode_buffer(256)
        port.u("GetClassNameW", other, name, len(name), require=True)
        metadata = {**port.owner(other), "bounds": bounds, "class_name": name.value,
                    "root_owner_hwnd": int(port.u("GetAncestor", other, 3) or 0)}
        metadata["accepted_owned_search_popup"] = owned_search_popup(metadata, target, envelope, field)
        metadata["capture_bounds_inside_main"] = popup_bounds_inside_main(bounds, envelope)
        observation["overlays"].append(metadata)
        if not metadata["accepted_owned_search_popup"]:
            raise Stop("STOP_SEARCH_REGION_COVERED_BY_UNVERIFIED_WINDOW")
    raise Stop("STOP_TARGET_NOT_IN_Z_ORDER")


def require_same_popup(observation, expected):
    rows = [r for r in observation.get("overlays", []) if r.get("hwnd") == expected["hwnd"]]
    if len(rows) != 1:
        raise Stop("STOP_RESULTS_POPUP_DISAPPEARED_OR_REPLACED")
    fields = ("hwnd", "pid", "thread_id", "class_name", "root_owner_hwnd")
    if not rows[0].get("accepted_owned_search_popup") or any(rows[0].get(k) != expected.get(k) for k in fields):
        raise Stop("STOP_RESULTS_POPUP_IDENTITY_OR_BOUNDS_CHANGED")
    if rows[0].get("bounds") != expected.get("bounds"):
        raise PopupBoundsChanged(expected, rows[0])


def read_only_input_tick(port):
    """Read a timestamp only; no foreground requirement or typed content."""
    import ctypes as c
    from .native_win32 import LASTINPUTINFO
    info = LASTINPUTINFO()
    info.cbSize = c.sizeof(info)
    port.u("GetLastInputInfo", c.byref(info), require=True)
    return int(info.dwTime)


def run(binding_path, *, continue_results=False, inspect_open_once=False, diagnose_results_only=False,
        search_alias=ALIAS, expected_open_remark=None, on_open=None,
        taskbar_delay_seconds=15, result_sink=None, allow_existing_foreground=False):
    if search_alias not in ALLOWED_ALIASES:
        raise Stop("STOP_ONLY_CONFIGURED_TEST_ALIASES_ALLOWED")
    if type(taskbar_delay_seconds) is not int or not 0 <= taskbar_delay_seconds <= 30:
        raise ValueError("INVALID_COUNTDOWN_SECONDS")
    if type(allow_existing_foreground) is not bool:
        raise ValueError("BOOLEAN_ALLOW_EXISTING_FOREGROUND_REQUIRED")
    from types import SimpleNamespace
    from ctypes import wintypes as w
    from .native_focus_once import local_engine, capture_class
    from .native_win32 import caret_evidence, click_dispatch_state, require_run_window, make_run_clock, require_initial_idle
    from .taskbar_click_once import TaskbarPort, run as run_taskbar

    started = time.perf_counter_ns()
    attempt = "search-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
    folder = ROOT / "evidence/search_once" / attempt
    folder.mkdir(parents=True, exist_ok=False)
    report = {"status": "INITIALIZING", "started_utc": datetime.now(timezone.utc).isoformat(),
              "query": search_alias, "api_calls": [], "timings_ns": {}, "ocr_calls": 0, "ocr_inference_ns": 0,
              "capture_calls": 0, "control_clicks": [], "search_characters_requested": "", "phase_ms": {},
              "chat_editor_input_calls": 0, "enter_calls": 0, "sends": 0,
              "retry_allowed": False, "send_eligible": False, "pending_desktop_actions": False}
    report.update(continue_results=continue_results, inspect_open_once=inspect_open_once,
                  allow_existing_foreground=allow_existing_foreground,
                  diagnose_results_only=diagnose_results_only, initial_binding_confirmed=False,
                  stages={name:{"status":"NOT_RUN"} for name in
                          ("window_binding", "query_readback", "group_section", "candidate", "coordinates", "click", "opened_title")})
    capture = port = None
    try:
        binding = load_human_binding(binding_path, search_alias)
        print("Preparing existing local OCR. No desktop actions yet.", flush=True)
        phase_start = time.perf_counter_ns()
        engine = local_engine()
        report["phase_ms"]["ocr_engine_preparation"] = (time.perf_counter_ns()-phase_start)/1e6
        if continue_results:
            # User chooses the current work-account window afresh. No taskbar,
            # activation API, keyboard import/input or cached HWND is used.
            report["human_hands_off_confirmed"] = not diagnose_results_only
            report["run_clock"] = make_run_clock(time.perf_counter_ns(), datetime.now(timezone.utc))
            print(f"15 seconds: keep work WeChat {search_alias} results visible. Read-only mode does not require foreground." if diagnose_results_only else
                  f"15 seconds: select work WeChat, leave exact {search_alias} search results visible, then release input.", flush=True)
            for remaining in range(15, 0, -1):
                print(remaining, flush=True)
                time.sleep(1)
            port = TaskbarPort(folder, report, authorized=True)
            capture = capture_class()()
            if diagnose_results_only:
                port.unlocked()
                target = port.select_target()  # Unique visible app metadata; no activation.
                report["read_only_binding"] = {"target":dict(target), "source":"fresh unique visible work-account application",
                    "foreground_required":False, "conversation_verified":False}
            else:
                target = port.bind_foreground_selection()
            report.update(bound_window=dict(target), previous_window=dict(target),
                          taskbar_stage_disposition="CONTINUE_CURRENT_RESULTS_NO_TASKBAR_NO_TYPING")
            initial = port.guard(target, foreground=False)
        else:
            import pyautogui as p
            p.FAILSAFE = True
            taskbar_id = "taskbar-" + attempt
            report["taskbar_result"] = str((ROOT / "evidence/taskbar_once" / taskbar_id / "result.json").relative_to(ROOT))
            phase_start = time.perf_counter_ns()
            run_taskbar(SimpleNamespace(authorize_desktop_io=True, human_work_account_confirmed=True,
                        human_hands_off_confirmed=True, attempt_id=taskbar_id, authorized_at_utc=None,
                        capture_calibration=False, click_once=True, move_only=False,
                        binding=str(TASKBAR_BINDING), delay_seconds=taskbar_delay_seconds,
                        allow_existing_foreground=allow_existing_foreground))
            taskbar = load_report(ROOT / report["taskbar_result"])
            report["phase_ms"]["taskbar_route_including_countdown"] = (time.perf_counter_ns()-phase_start)/1e6
            report["taskbar_timings_ns"] = taskbar.get("timings_ns", {})
            report["countdown_requested_seconds"] = taskbar_delay_seconds
            target = inherit_taskbar_context(report, taskbar)
            require_run_window(report, "search_stage_handoff")
            report["phase"] = "search_window_revalidation"
            port = TaskbarPort(folder, report, authorized=True)
            capture = capture_class()()
            initial = port.guard(target, foreground=False)
        report["phase"] = "search_layout_observation"
        report["target_foreground_stage_started_ns"] = time.perf_counter_ns()
        layout = search_layout(initial, report)
        report["stages"]["window_binding"] = {"status":"OBSERVED", "source":"fresh current window metadata"}
        print("WeChat window metadata verified. Checking the visible search field.", flush=True)
        baseline = {"tick":read_only_input_tick(port)} if diagnose_results_only else port.activity()
        if continue_results and not diagnose_results_only:
            require_initial_idle(report, baseline)
        report["initial_activity_snapshot"] = dict(baseline)
        stage_deadline = time.perf_counter() + 30
        bx, by, _, _ = initial["bounds"]
        search_envelope = list(initial["bounds"])
        # Reviewed profile supplies only a geometry anchor. Every actionable
        # point still comes from this run's observed text/rectangle.
        field = [bx+PROFILE["field_anchor"][0], by+PROFILE["field_anchor"][1],
                 bx+PROFILE["field_anchor"][2], by+PROFILE["field_anchor"][3]]
        results_active = True  # An existing search popup is allowed in its verified region.

        def stable(region=None):
            require_run_window(report, "search_stage_guard")
            if time.perf_counter() > stage_deadline:
                raise Stop("STOP_SEARCH_STAGE_TIME_LIMIT")
            # The first key can open a legitimate results layer. Once the
            # complete bounded query is requested, verify only the control/ROI
            # being read. Unknown/foreign overlays covering it still stop.
            current = (region_guard(port, target, region or field, search_envelope, field,
                                    require_foreground=not diagnose_results_only)
                       if results_active or report["search_characters_requested"] else port.guard(target))
            if current != initial:
                raise Stop("STOP_SEARCH_WINDOW_GEOMETRY_CHANGED")
            return current

        def unchanged_input():
            if diagnose_results_only:
                port.unlocked()
                if read_only_input_tick(port) != baseline["tick"]:
                    raise Stop("STOP_INPUT_CHANGED_DURING_READ_ONLY_CAPTURE")
            else:
                port.require_activity(baseline)

        def snapshot(relative, name, *, persist=True, pinned_popup=None):
            report["phase"] = name
            x, y = initial["bounds"][:2]
            screen = [relative[0]+x, relative[1]+y, relative[2]+x, relative[3]+y]
            stable(screen)
            if pinned_popup is not None:
                require_same_popup(report["search_region_visibility"][-1], pinned_popup)
            unchanged_input()
            captured_ns = time.perf_counter_ns()
            report["capture_calls"] += 1
            capture_record = {"name":name, "bounds":screen, "captured_monotonic_ns":captured_ns,
                              "scope":"inside current bound main window", "saved":False}
            report.setdefault("captures", []).append(capture_record)
            image, _ = capture.copy_rectangle_once(screen)
            try:
                stable(screen)
                if pinned_popup is not None:
                    require_same_popup(report["search_region_visibility"][-1], pinned_popup)
                unchanged_input()
                if persist:
                    capture_record["saved"] = discard_image(image, folder / (name + ".png"))
            except BaseException:
                image.close()
                raise
            return image, screen, captured_ns

        def click_control(screen_box, captured_ns, name, *, pinned_popup=None):
            nonlocal baseline
            report["phase"] = "click_" + name
            stable(screen_box)
            if pinned_popup is not None:
                require_same_popup(report["search_region_visibility"][-1], pinned_popup)
            port.require_activity(baseline)
            if time.perf_counter_ns() - captured_ns > 2_000_000_000:
                raise Stop("STOP_CONTROL_OBSERVATION_STALE")
            point = [int((screen_box[0]+screen_box[2])//2), int((screen_box[1]+screen_box[3])//2)]
            action = {"name": name, "api_calls": [], "sendinput_calls": 0, "click_attempts": 0}
            report["control_clicks"].append(action)
            once = TaskbarPort(folder, action, authorized=True)
            hit = once.u("WindowFromPoint", w.POINT(*point), require=True)
            hit_root = once.u("GetAncestor", hit, 2, require=True)
            if hit_root != target["hwnd"]:
                import ctypes as c
                class_name = c.create_unicode_buffer(256)
                once.u("GetClassNameW", hit_root, class_name, len(class_name), require=True)
                metadata = {**once.owner(hit_root), "bounds": once.bounds(hit_root),
                            "class_name": class_name.value,
                            "root_owner_hwnd": int(once.u("GetAncestor", hit_root, 3) or 0)}
                action["hit_root_metadata"] = metadata
                if name not in ("human_bound_group_result", "inspect_group_result_once") or not owned_search_popup(metadata, target, search_envelope, field):
                    raise Stop("STOP_CONTROL_POINT_NOT_IN_BOUND_WINDOW")
            tick = once.dispatch_left_click_once(point, initial["virtual"])
            deadline = time.perf_counter() + .6
            while time.perf_counter() < deadline:
                stable()
                current = port.activity()
                state = click_dispatch_state(baseline, current, tick, point)
                if state == "OWN_INPUT_OBSERVED":
                    baseline = current
                    return dict(baseline)
                if state == "UNEXPECTED_INPUT_OR_CURSOR_CHANGE":
                    raise Stop("STOP_USER_ACTIVITY_OR_CLICK_ATTRIBUTION_UNKNOWN")
                time.sleep(.025)
            raise Stop("STOP_CLICK_RESULT_UNKNOWN_NO_RETRY")

        phase_start = time.perf_counter_ns()
        if not continue_results:
            # Search from the currently bound work window; the old conversation
            # title is not a prerequisite for selecting a new numbered target.
            strip, offset, observed_ns = snapshot(SEARCH_STRIP, "search-control-before")
            try:
                box = search_field(strip, engine, report, wanted=None)
            finally:
                strip.close()
            field = [box[0]+offset[0], box[1]+offset[1], box[2]+offset[0], box[3]+offset[1]]
            report["search_field_screen"] = field
            click_control(field, observed_ns, "search_field")
            report["focus_samples"] = []
            report["phase"] = "search_focus_observation"
            focus_deadline = time.perf_counter() + .8
            while True:
                stable()
                port.require_activity(baseline)
                focus = port.observe_focus(target, field)
                report["focus_samples"].append(focus)
                if caret_evidence(focus, target["hwnd"], field)["native_caret_evidence"]:
                    break
                if time.perf_counter() >= focus_deadline:
                    raise Stop("STOP_SEARCH_FOCUS_UNKNOWN_NO_TEXT_WRITTEN")
                time.sleep(.025)
            report["search_focus_observed"] = True
            report["phase"] = "search_query_input"
            stable()
            port.require_activity(baseline)
            report["query_focus_before"] = port.observe_focus(target, field)
            if not caret_evidence(report["query_focus_before"], target["hwnd"], field)["native_caret_evidence"]:
                raise Stop("STOP_SEARCH_FOCUS_CHANGED_NO_TEXT_WRITTEN")
            # One bounded string operation. Do not reclassify the results popup
            # opened by the first character between the three literal digits.
            report["search_characters_requested"] = search_alias
            report["query_write_calls"] = 1
            write_start = time.perf_counter_ns()
            p.hotkey("ctrl", "a", logScreenshot=False)  # Only after native caret proof in the search field.
            if not caret_evidence(port.observe_focus(target,field),target["hwnd"],field)["native_caret_evidence"]:
                raise Stop("STOP_SEARCH_FOCUS_CHANGED_AFTER_SELECT_ALL")
            p.write(search_alias, interval=0, logScreenshot=False)
            report["query_write_returned"] = True  # Not text/readback success.
            report["query_write_elapsed_ms"] = (time.perf_counter_ns()-write_start)/1_000_000
            current = port.activity()
            report["query_activity_after"] = dict(current)
            report["query_focus_after"] = port.observe_focus(target, field)
            if current["cursor"] != baseline["cursor"]:
                raise Stop("STOP_USER_POINTER_ACTIVITY_DURING_QUERY")
            baseline = current  # Own typing changes input time; not complete user-input attribution.
            time.sleep(.3)
        report["phase_ms"]["search_field_focus_and_query"] = (time.perf_counter_ns()-phase_start)/1e6
        phase_start = time.perf_counter_ns()
        query_problem = None
        strip, offset, _ = snapshot(SEARCH_STRIP, "search-control-after")
        try:
            box = search_field(strip, engine, report, wanted=search_alias)
            field = [box[0]+offset[0], box[1]+offset[1], box[2]+offset[0], box[3]+offset[1]]
            report["search_field_screen"] = field
            report["query_readback_verified"] = True
            report["stages"]["query_readback"] = {"status":"OBSERVED", "exact_text":search_alias}
        except Stop as error:
            query_problem = str(error)
            report["stages"]["query_readback"] = {"status":"BLOCKED", "reason":query_problem,
                                                   "image":"search-control-after.png"}
            if not diagnose_results_only:
                raise
        finally:
            strip.close()

        # One read-only refresh handles asynchronous popup expansion. No
        # click or send is retried; all coordinates are derived again.
        for observation_pass in range(2):
            report["result_observation_passes"] = observation_pass + 1
            try:
                # Locate the freshly observed Qt results window, not the former narrow
                # fixed sidebar rectangle. No process-name/session queries are needed.
                stable(search_envelope)
                popup_rows = [m for m in report["search_region_visibility"][-1]["overlays"]
                              if m["accepted_owned_search_popup"] and m["class_name"] == "Qt51514QWindowToolSaveBits"]
                report["visible_search_popup_count"] = len(popup_rows)
                if len(popup_rows) != 1:
                    if not popup_rows:
                        raise Stop(query_problem or "STOP_NO_VISIBLE_SEARCH_RESULTS_POPUP")
                    raise Stop("STOP_SEARCH_RESULTS_POPUP_AMBIGUOUS")
                popup = popup_rows[0]
                report["results_popup"] = popup
                capture_bounds = popup_bounds_inside_main(popup["bounds"], initial["bounds"])
                if capture_bounds is None:
                    raise Stop("STOP_POPUP_HAS_NO_VERIFIED_IN_WINDOW_CAPTURE_REGION")
                px, py, pr, pb = capture_bounds
                popup_bottom_clipped = pb != popup["bounds"][3]
                report["results_capture_scope"] = {"bounds":capture_bounds, "bottom_clipped":popup_bottom_clipped,
                                                   "outside_main_window_pixels":0}
                # One fresh results-panel OCR. Category labels are not entry gates.
                # The image stays in memory; only the selected avatar/title row is saved.
                panel_box = [px-bx, py-by, pr-bx, pb-by]
                panel, offset, observed_ns = snapshot(panel_box, "numbered-results-panel", persist=False, pinned_popup=popup)
                try:
                    rows = tokens(engine, panel, report)
                    report["numbered_entry_diagnostic"] = {}
                    candidate = unique_chat_entry(rows, panel, search_alias,
                                                  report["numbered_entry_diagnostic"])
                    report["stages"]["group_section"] = {"status":"NOT_REQUIRED", "category_required":False}
                    report["stages"]["candidate"] = {"status":"OBSERVED", "exact_alias":search_alias,
                                                      "visible_count":1, "global_uniqueness_proven":False}
                    a, b = candidate["avatar_box"], candidate["alias_box"]
                    row_box = [max(0,int(a[0])-2),max(0,int(min(a[1],b[1]))-2),
                               min(panel.width,int(b[2])+3),min(panel.height,int(max(a[3],b[3]))+3)]
                    selected = panel.crop(row_box)
                    try:
                        discard_image(selected, folder / "numbered-result-entry.png")
                    finally:
                        selected.close()
                    report["saved_entry_box_in_panel"] = row_box
                except Stop as error:
                    report["stages"]["candidate"] = {"status":"BLOCKED", "reason":str(error),
                                                      "category_required":False}
                    # Preserve only exact-number row context from this existing capture
                    # so a rejected avatar can be diagnosed without another desktop run.
                    # Other OCR text, message history and web-result rows are not saved.
                    samples=[]
                    for index, item in enumerate(report.get("numbered_entry_diagnostic",{}).get("exact_rows",[])):
                        if item.get("decision","").startswith("EXCLUDED_"):
                            continue
                        b=item["box"]; h=b[3]-b[1]
                        context_box=[max(0,int(b[0]-3*h)),max(0,int(b[1]-h)),
                                     min(panel.width,int(b[2])+3),min(panel.height,int(b[3]+h))]
                        sample=panel.crop(context_box)
                        try:
                            name=f"rejected-numbered-row-{index}.png"
                            discard_image(sample, folder/name)
                            samples.append({"file":name,"box_in_panel":context_box,
                                            "decision":item.get("decision")})
                        finally:
                            sample.close()
                    report["saved_numbered_diagnostic_samples"]=samples
                    raise
                finally:
                    panel.close()
                report["candidate"] = candidate
                box = candidate["alias_box"]
                result_box = screen_box_from_crop(box, offset)
                report["candidate_screen_box"] = result_box
                report["stages"]["coordinates"] = {"status":"PLANNED_ONLY", "source":"this capture's group OCR box",
                    "crop_screen_origin":offset[:2], "ocr_box_in_crop":box, "screen_box":result_box, "scale":1}
                if diagnose_results_only:
                    report["status"] = "READ_ONLY_RESULTS_DIAGNOSIS_COMPLETE" if query_problem is None else "READ_ONLY_RESULTS_DIAGNOSIS_HAS_BLOCKERS"
                    report["stages"]["click"] = {"status":"NOT_RUN", "reason":"explicit read-only diagnostic mode"}
                    report["pending_desktop_actions"] = False
                    return 0 if report["status"] == "READ_ONLY_RESULTS_DIAGNOSIS_COMPLETE" else 2
                if binding is None and not inspect_open_once:
                    report["status"] = "AWAITING_HUMAN_TARGET_BINDING"
                    return 2
                if binding is not None:
                    report["human_binding"] = {k: binding[k] for k in
                        ("schema_version", "search_alias", "target_kind", "expected_open_title", "confirmed_by", "confirmed_at_utc", "source_result", "source_result_sha256")}
                click_control(result_box, observed_ns, "human_bound_group_result" if binding else "inspect_group_result_once", pinned_popup=popup)
                break
            except PopupBoundsChanged as changed:
                report.setdefault("popup_layout_refreshes", []).append({
                    "previous":changed.previous,"current":changed.current,
                    "reason":"same popup identity changed bounds before click",
                    "desktop_input_retried":False})
                if observation_pass:
                    raise Stop("STOP_RESULTS_POPUP_CONTINUES_CHANGING")
        report["stages"]["click"] = {"status":"INPUT_OBSERVED", "opened_target_verified":False}
        report["phase_ms"]["result_observation_and_selection"] = (time.perf_counter_ns()-phase_start)/1e6
        phase_start = time.perf_counter_ns()
        time.sleep(.3)
        header, _, _ = snapshot(layout["title_strip_relative"], "opened-title-only")
        try:
            title_rows = tokens(engine, header, report)
            if binding is not None:
                exact_token(title_rows, binding["expected_open_title"])
            else:
                report["opened_header_candidates"] = [r for r in title_rows if r["complete"] and r["score"] >= .95]
        finally:
            header.close()
        # Opening can fail while the click was dispatched successfully. Require
        # the observed results popup to have disappeared before offering binding.
        if popup["hwnd"] in port.windows() and port.u("IsWindowVisible", popup["hwnd"]):
            raise Stop("STOP_GROUP_OPEN_RESULT_UNKNOWN_POPUP_STILL_VISIBLE")
        report["results_popup_closed_observed"] = True
        if expected_open_remark is not None:
            if expected_open_remark != search_alias or not opened_remark_matches(title_rows,search_alias):
                raise Stop("STOP_OPENED_NUMBERED_TARGET_NOT_MATCHED")
            report["opened_remark_verified"] = search_alias
            report["stages"]["opened_title"] = {"status":"MATCHED_OPEN_REMARK","remark":search_alias}
            report["identity_basis"] = "user-named test remark plus current unique chat row and opened remark; not independent identity or a wxid"
        report["phase_ms"]["opened_target_review"] = (time.perf_counter_ns()-phase_start)/1e6
        if on_open is not None:
            if diagnose_results_only or (binding is None and expected_open_remark is None):
                raise Stop("STOP_OPEN_CALLBACK_REQUIRES_EXPLICIT_TARGET")
            outcome = on_open({"port":port,"capture":capture,"target":target,"initial":initial,
                "layout":layout,"engine":engine,"report":report,"folder":folder,
                "snapshot":snapshot,"stable":stable,"activity_baseline":dict(baseline),
                "title_rows":title_rows,"click_control":click_control})
            report.update(outcome)
            return 0 if outcome.get("status") in ("LOCAL_SUBMISSION_OBSERVED","SUBMIT_REQUESTED_UNVERIFIED") else 2
        report["stages"]["opened_title"] = {"status":"MATCHED_BINDING" if binding else "AWAITING_HUMAN_BINDING"}
        if binding is None:
            report["status"] = "AWAITING_HUMAN_OPEN_TITLE_BINDING"
            report["identity_basis"] = "unique visible group search candidate clicked once; user must confirm actual opened group/title; no send eligibility"
            return 2
        report.update(status="HUMAN_BOUND_ALIAS_OPEN_TITLE_OBSERVED", opened_title=binding["expected_open_title"],
                      initial_binding_confirmed=True,
                      identity_basis="human-confirmed alias/title association plus this visible search result and opened header; not a WeChat group ID")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = str(error) if isinstance(error, Stop) else "STOP_UNEXPECTED_ERROR"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        return 2
    finally:
        if capture is not None and port is not None:
            try:
                port.u("SetThreadDpiAwarenessContext", capture.old_dpi, require=True)
            except Exception as error:
                report["dpi_cleanup_error"] = str(error)
        report["elapsed_ms"] = (time.perf_counter_ns() - started) / 1_000_000
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save(folder / "result.json", report)
        if result_sink is not None:
            result_sink(report,folder)
        print(json.dumps({"status": report["status"], "result": str(folder / "result.json"),
                          "search_characters_requested": report["search_characters_requested"],
                          "sends": report.get("sends",0), "pending_desktop_actions": False}, ensure_ascii=True), flush=True)

