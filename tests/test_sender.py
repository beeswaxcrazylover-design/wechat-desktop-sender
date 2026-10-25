import _support
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from wechat_desktop_sender.simple_core import Route
from wechat_desktop_sender import sender_worker as sender
from wechat_desktop_sender.live_capture_once import Stop


class Port:
    def require_activity(self, baseline):
        assert baseline == "synthetic-baseline"


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.runtime = patch.object(sender, "RUNTIME", Path(self.directory.name))
        self.runtime.start()
        self.route = Route("fictional", "local-binding", "manual-input", "remark-731", "manual-input", "731")
        self.body = "  synthetic exact message\nsecond line  "
        self.request = {"job_id":1,"text":self.body,"route":asdict(self.route)}
        self.calls = []
        self.report = {"opened_remark_verified":"731"}

    def tearDown(self):
        self.runtime.stop()
        self.directory.cleanup()

    def search(self, binding, **kw):
        self.assertIsNone(binding)
        self.assertEqual(kw["taskbar_delay_seconds"], 0)
        self.assertTrue(kw["allow_existing_foreground"])
        self.assertEqual(kw["search_alias"], "731")
        kw["on_open"]({"report":self.report,"stable":lambda:None,
                       "port":Port(),"activity_baseline":"synthetic-baseline"})

    def backend(self):
        return (lambda text:self.calls.append(("copy",text)),
                lambda *a,**kw:self.calls.append(("hotkey",a,kw)),
                lambda *a,**kw:self.calls.append(("press",a,kw)))

    def submit(self, **kw):
        with patch.object(sender.time,"sleep"):
            return sender.submit(self.request, search_run=kw.pop("search_run",self.search),
                paste_backend=kw.pop("paste_backend",self.backend),directory_reader=lambda:self.route,**kw)

    def receipt(self):
        return json.loads(sender.attempt_paths(self.request)[0].read_text(encoding="utf-8"))

    def test_exact_text_one_paste_one_enter_without_editor_probes(self):
        result = self.submit()
        self.assertEqual(result["status"],"SUBMITTED_UNVERIFIED")
        self.assertEqual(self.calls,[("copy",self.body),("hotkey",("ctrl","v"),{"logScreenshot":False}),
            ("press",("enter",),{"presses":1,"interval":0,"logScreenshot":False})])
        self.assertEqual(self.report["editor_focus_probes"],0)
        self.assertEqual(self.report["editor_focus_clicks"],0)
        record = self.receipt()
        self.assertTrue(record["submission_possible"])
        self.assertNotIn(self.body,json.dumps(record))

    def test_wrong_remark_blocks_before_clipboard(self):
        self.report["opened_remark_verified"] = "846"
        self.assertEqual(self.submit()["status"],"BLOCKED")
        self.assertEqual(self.calls,[])
        self.assertFalse(self.receipt()["submission_possible"])

    def test_failure_after_paste_is_unknown_no_enter_retry(self):
        def failed():
            def paste(*a,**k):
                self.calls.append("paste-attempt")
                raise RuntimeError("synthetic clipboard failure")
            return lambda text:None,paste,lambda *a,**k:self.calls.append("enter")
        self.assertEqual(self.submit(paste_backend=failed)["status"],"UNKNOWN")
        self.assertEqual(self.calls,["paste-attempt"])
        self.assertTrue(self.receipt()["submission_possible"])

    def test_cancel_before_paste_can_only_be_explicitly_recovered(self):
        path,cancel = sender.attempt_paths(self.request)
        path.parent.mkdir(parents=True)
        cancel.touch()
        result = self.submit()
        self.assertEqual(result,{"status":"BLOCKED","reason":"STOP_USER_CANCELLED"})
        self.assertFalse(self.receipt()["submission_possible"])
        job={"id":1,"content_hash":sha256(self.body.encode()).hexdigest(),"state":"BLOCKED","reason":result["reason"]}
        self.assertTrue(sender.verify_safe_retry(job))
        job["state"]="UNKNOWN"
        self.assertFalse(sender.verify_safe_retry(job))

    def test_existing_attempt_is_never_spawned_again(self):
        self.submit()
        job={"id":1,"text":self.body}
        with patch.object(sender,"tracked_process",side_effect=AssertionError("must not spawn")):
            self.assertEqual(sender.call_child(job,self.route,threading.Event())["status"],"UNKNOWN")

    def test_uncertain_search_without_paste_is_blocked(self):
        def uncertain(binding,**kw):kw["result_sink"]({"status":"STOP_SEARCH_UNCERTAIN"},None)
        self.assertEqual(self.submit(search_run=uncertain),{"status":"BLOCKED","reason":"STOP_SEARCH_UNCERTAIN"})
        self.assertFalse(self.calls)

    def test_cancel_after_copy_is_unknown_and_no_paste(self):
        def backend():
            def copy(text):sender.attempt_paths(self.request)[1].touch()
            return copy,lambda *a,**k:self.calls.append("paste"),lambda *a,**k:self.calls.append("enter")
        self.assertEqual(self.submit(paste_backend=backend)["status"],"UNKNOWN")
        self.assertFalse(self.calls)

