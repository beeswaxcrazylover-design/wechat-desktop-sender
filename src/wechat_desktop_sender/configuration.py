"""Local configuration boundary replacing the business contact directory."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re

from .simple_core import ForwardError, Route, require


@dataclass(frozen=True)
class Configuration:
    file: Path
    runtime: Path
    ocr_directory: Path
    calibration: Path
    profile: dict
    route: Route
    send_seconds: float


def load(path=None, *, live=False):
    value = path or os.environ.get("WECHAT_SENDER_CONFIG")
    require(bool(value), "LOCAL_CONFIG_REQUIRED")
    file = Path(value).resolve()
    raw = json.loads(file.read_text(encoding="utf-8"))
    require(isinstance(raw, dict) and raw.get("schema_version") == 1, "CONFIG_SCHEMA_INVALID")
    alias = raw.get("target_alias")
    # Retain the tested three-ASCII-digit input contract of PyAutoGUI.write.
    require(isinstance(alias, str) and re.fullmatch(r"[0-9]{3}", alias), "TARGET_NUMBER_MUST_HAVE_THREE_DIGITS")
    namespace = raw.get("ledger_namespace")
    require(isinstance(namespace, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", namespace), "LOCAL_LEDGER_NAMESPACE_REQUIRED")
    interval = raw.get("send_seconds", 3)
    require(type(interval) in (int, float) and math.isfinite(interval) and 1 <= interval <= 3600, "INVALID_INTERVAL")
    def local_path(key):
        entry = raw.get(key)
        require(isinstance(entry, str) and bool(entry), "CONFIG_PATH_REQUIRED")
        return (file.parent / entry).resolve()
    profile = raw.get("layout", {})
    require(isinstance(profile, dict), "LAYOUT_REQUIRED")
    if live:
        require(raw.get("calibrated") is True, "FIRST_CALIBRATION_REQUIRED")
        require(type(profile.get("dpi")) is int and profile["dpi"] > 0, "CALIBRATED_DPI_REQUIRED")
        for name in ("search_strip", "results_pane", "title_strip", "field_anchor"):
            box = profile.get(name)
            require(isinstance(box, list) and len(box) == 4 and
                    all(type(n) is int and n >= 0 for n in box) and
                    box[0] < box[2] and box[1] < box[3], "CALIBRATED_RECTANGLE_REQUIRED")
    # These are LOCAL idempotency scope labels, not WeChat account/talker IDs.
    route = Route(namespace, "local-binding", "manual-input", "remark-"+alias, "manual-input", alias)
    return Configuration(file, local_path("runtime"), local_path("ocr_directory"),
                         local_path("taskbar_binding"), profile, route, float(interval))


def read_route():
    return load(live=True).route


def validate_unchanged(configuration):
    current = load(configuration.file, live=True)
    require(current == configuration, "LOCAL_CONFIGURATION_CHANGED")

