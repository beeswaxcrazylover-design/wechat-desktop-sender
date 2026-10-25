"""Original local OCR setup, timing and reviewed capture loader."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import runpy
import socket
import time
from .app_paths import LEGACY_OCR as LEGACY
from .live_capture_once import Stop

@contextmanager
def timing(report, stage):
    start = time.perf_counter_ns()
    previous_phase = report.get("phase")
    report["phase"] = stage
    event = {"stage": stage, "started_monotonic_ns": start}
    report.setdefault("stage_timeline", []).append(event)
    try:
        yield
    finally:
        finished = time.perf_counter_ns()
        event.update(finished_monotonic_ns=finished, elapsed_ns=finished - start)
        report["timings_ns"][stage] = report["timings_ns"].get(stage, 0) + finished - start
        report["phase"] = previous_phase


def local_engine():
    # All models/dependencies must already exist. No downloads/install fallback.
    def no_network(*args, **kwargs):
        raise Stop("STOP_NETWORK_DISABLED_FOR_LOCAL_OCR")
    socket.create_connection = socket.socket.connect = socket.socket.connect_ex = no_network
    import yaml
    config = LEGACY / "cpu-only-config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    for name in ("Det", "Cls", "Rec"):
        section = data[name]
        if section.get("use_cuda") or section.get("use_dml") or not Path(section["model_path"]).is_file():
            raise Stop("STOP_EXISTING_CPU_MODELS_UNAVAILABLE")
    from rapidocr_onnxruntime import RapidOCR
    # Existing config enlarges short UI strips to a 736px MINIMUM side. Bound
    # the LONG side instead so a 48px control strip is not enlarged needlessly.
    # Constructor overrides are process-local; the legacy YAML is never edited.
    engine = RapidOCR(config_path=str(config), det_limit_type="max", det_limit_side_len=1024,
                      intra_op_num_threads=2, inter_op_num_threads=1)
    # Warm detection/classification/recognition on synthetic non-private text
    # BEFORE the countdown or foreground borrow; this is not a screenshot.
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    warmup = Image.new("RGB", (480, 96), (240, 240, 240))
    ImageDraw.Draw(warmup).text((12, 22), "LOCAL WARMUP 2468", fill=(20, 20, 20),
                              font=ImageFont.load_default(size=30))
    engine(np.asarray(warmup))
    warmup.close()
    return engine


def capture_class():
    path = Path(__file__).with_name("capture.py")
    if sha256(path.read_bytes()).hexdigest() != '24dbd012304607eab7c903ff970d5d91156cee0bc8e29a1400f1318039185978':
        raise Stop("STOP_REVIEWED_CAPTURE_SOURCE_CHANGED")
    # Definitions only. Never run old main, inventory, polling or whole-frame OCR.
    return runpy.run_path(str(path))["Native"]


def countdown(seconds, report, label):
    with timing(report, label):
        for remaining in range(seconds, 0, -1):
            print(str(remaining), flush=True)
            time.sleep(1)
