import _support
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sys
from unittest.mock import patch

from wechat_desktop_sender.api import dry_run
from wechat_desktop_sender.configuration import load
from wechat_desktop_sender.diagnostics import store_report, load_report, discard_image
from wechat_desktop_sender.dpi_bootstrap import initialize_process_dpi
from wechat_desktop_sender.simple_core import ForwardError


class BoundaryTests(unittest.TestCase):
    def test_example_dryrun_only_and_no_desktop_import(self):
        file=Path(__file__).resolve().parents[1]/"examples/config.example.json"
        config=load(file)
        result=dry_run(config,[{"id":"synthetic-id","text":"synthetic text"}])
        self.assertEqual(result["desktop_calls"],0)
        self.assertEqual(result["counts"]["PENDING"],1)
        with self.assertRaisesRegex(ForwardError,"FIRST_CALIBRATION"):load(file,live=True)
    def test_interstage_json_has_no_file_and_preserves_representation(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/"result.json"
            store_report(path,{"synthetic_tuple":(1,2),"query":"fictional"})
            self.assertEqual(load_report(path),{"synthetic_tuple":[1,2],"query":"fictional"})
            self.assertFalse(path.exists())
    def test_diagnostic_pixels_not_saved_or_changed(self):
        class Image:
            def save(self,*args):raise AssertionError("private pixels must not be saved")
            def close(self):raise AssertionError("live OCR image must stay open")
        self.assertFalse(discard_image(Image(),Path("synthetic.png")))
    def test_original_dpi_policy_initializes_once(self):
        class User:
            value=0;calls=0
            def GetThreadDpiAwarenessContext(self):return 1
            def GetAwarenessFromDpiAwarenessContext(self,c):return self.value
            def SetProcessDPIAware(self):self.calls+=1;self.value=1;return 1
        user=User()
        self.assertEqual(initialize_process_dpi(user32=user,platform="win32")["status"],"INITIALIZED")
        self.assertEqual(initialize_process_dpi(user32=user,platform="win32")["status"],"ALREADY_AWARE")
        self.assertEqual(user.calls,1)

    @unittest.skipUnless(sys.platform == "win32", "Windows local lock")
    def test_original_runtime_lock_rejects_second_instance(self):
        from wechat_desktop_sender.app_lock import AppLock
        with TemporaryDirectory() as folder:
            first=AppLock(Path(folder)/"sender.lock")
            second=AppLock(Path(folder)/"sender.lock")
            first.acquire()
            try:
                with self.assertRaisesRegex(ForwardError,"ALREADY_RUNNING"):second.acquire()
            finally:first.close();second.close()
            second.acquire();second.close()

