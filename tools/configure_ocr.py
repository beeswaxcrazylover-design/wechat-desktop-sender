"""Generate local paths for the original CPU OCR configuration; never download."""
import argparse
from hashlib import sha256
from importlib import metadata, resources
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if metadata.version("rapidocr-onnxruntime") != "1.4.4":
        raise SystemExit("rapidocr-onnxruntime==1.4.4 required")
    import yaml
    import rapidocr_onnxruntime
    model_root = Path(rapidocr_onnxruntime.__file__).parent / "models"
    settings = json.loads(resources.files("wechat_desktop_sender").joinpath("ocr-settings.json").read_text(encoding="utf-8"))
    hashes = {
        "ch_PP-OCRv4_det_infer.onnx":"d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
        "ch_PP-OCRv4_rec_infer.onnx":"48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
        "ch_ppocr_mobile_v2.0_cls_infer.onnx":"e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"
    }
    for name in ("Det", "Cls", "Rec"):
        basename = settings[name]["model_path"]
        path = model_root / basename
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != hashes[basename]:
            raise SystemExit("matching locally installed model required: "+basename)
        settings[name]["model_path"] = str(path.resolve())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(settings, stream, allow_unicode=True)
    print("Local CPU configuration created; no desktop actions.")


if __name__ == "__main__":main()

