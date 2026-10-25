"""Paths only; no historical machine paths, account IDs or calibration data."""
import os
from pathlib import Path
import sys
import tempfile

from .configuration import load

_config = load() if os.environ.get("WECHAT_SENDER_CONFIG") else None
PYTHON = Path(sys.executable)
RUNTIME = _config.runtime if _config else Path(".wechat-sender-runtime").resolve()
LEGACY_OCR = _config.ocr_directory if _config else Path(".wechat-sender-ocr").resolve()
CALIBRATION = _config.calibration if _config else Path(".wechat-sender-calibration/missing.json").resolve()
TARGET_ALIAS = _config.route.target_alias if _config else ""
ALLOWED_ALIASES = frozenset({TARGET_ALIAS}) if TARGET_ALIAS else frozenset()
PROFILE = _config.profile if _config else {"dpi":0, "search_strip":[0,0,0,0],
    "results_pane":[0,0,0,0], "title_strip":[0,0,0,0], "field_anchor":[0,0,0,0]}
# Only empty bookkeeping folders are created here. Reports/images stay in memory.
_scratch = tempfile.TemporaryDirectory(prefix="wechat-sender-")
SCRATCH = Path(_scratch.name)

