"""Explicit live opt-in; ordinary invocation validates a batch offline."""
import argparse
import json
import os
from pathlib import Path
import sys

from .api import dry_run, load_messages
from .configuration import load
from .simple_core import safe_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--live", action="store_true", help="Windows GUI send; requires local first calibration")
    args = parser.parse_args(argv)
    try:
        config = load(args.config, live=args.live)
        messages = load_messages(args.messages)
        preview = dry_run(config, messages)
        if not args.live:
            print(json.dumps(preview, ensure_ascii=True))
            return 0
        if sys.platform != "win32":raise ValueError("WINDOWS_REQUIRED")
        os.environ["WECHAT_SENDER_CONFIG"] = str(config.file)
        from .desktop_app import launch
        launch(config, messages)
        return 0
    except Exception as error:
        print(json.dumps({"status":"BLOCKED", "reason":safe_code(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

