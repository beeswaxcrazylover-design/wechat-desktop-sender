"""Bind a user-provided icon crop locally. No capture, click or application access."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icon", required=True, help="locally reviewed 16..96 pixel icon crop")
    parser.add_argument("--dpi", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()
    if not args.confirmed or args.dpi <= 0:parser.error("local icon review and positive DPI required")
    from PIL import Image
    source = Path(args.icon)
    with Image.open(source) as image:
        if not (16 <= image.width <= 96 and 16 <= image.height <= 96):
            parser.error("icon dimensions must be 16..96 pixels")
    output = Path(args.output)
    target = output.parent / "taskbar-icon.png"
    if output.exists() or target.exists():parser.error("calibration output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    data = {"live_ready":True,"human_icon_binding_confirmed":True,
            "coordinate_space":"physical_pixels_1_to_1","dpi":args.dpi,
            "template_file":target.name,"template_sha256":sha256(target.read_bytes()).hexdigest()}
    with output.open("x", encoding="utf-8") as stream:json.dump(data, stream, indent=2)
    print("Local taskbar template binding created; no desktop actions.")


if __name__ == "__main__":main()

