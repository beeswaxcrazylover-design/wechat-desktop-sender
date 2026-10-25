"""Ephemeral inter-stage reports; no screenshot or OCR text log persistence."""
import json
import threading

_reports = {}
_lock = threading.Lock()


def store_report(path, report):
    # JSON round trip preserves the original file-handoff representation.
    encoded = json.dumps(report, ensure_ascii=False)
    with _lock:
        _reports[str(path.resolve())] = encoded


def load_report(path):
    with _lock:
        return json.loads(_reports.pop(str(path.resolve())))


def discard_image(image, path):
    """Do not persist private diagnostic pixels; callers retain the live image."""
    return False


def clear_reports():
    with _lock:
        _reports.clear()

