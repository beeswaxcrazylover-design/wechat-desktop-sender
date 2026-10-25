import _support
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from wechat_desktop_sender.api import ManualBatch
from wechat_desktop_sender.simple_core import Engine, ForwardError, Ledger, Route


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory()
        self.ledger=Ledger(Path(self.folder.name)/"queue.sqlite3")
        self.route=Route("fictional","local-binding","manual-input","remark-731","manual-input","731")
    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()
    def accept(self, rows):
        source=ManualBatch(self.route,rows)
        return self.ledger.accept_batch(self.route,source.poll(source.initial_cursor(),0).events,not_before=0)
    def test_same_content_deduped_across_message_ids(self):
        self.assertEqual(self.accept([{"id":"a","text":"same"},{"id":"b","text":"same"}]),
                         {"PENDING":1,"DUPLICATE":1,"UNSUPPORTED":0})
    def test_exact_whitespace_is_not_rewritten(self):
        self.accept([{"id":"a","text":"  x\n "}])
        self.assertEqual(self.ledger.next_pending(self.route)["text"],"  x\n ")
    def test_stable_id_changed_content_rejected(self):
        self.accept([{"id":"a","text":"x"}])
        with self.assertRaisesRegex(ForwardError,"MESSAGE_ID_CONTENT_CONFLICT"):
            self.accept([{"id":"a","text":"y"}])
    def test_interrupted_submission_recovers_unknown_and_pauses(self):
        self.accept([{"id":"a","text":"x"}])
        job=self.ledger.next_pending(self.route)
        self.ledger.begin(job["id"])
        self.ledger.recover()
        self.assertEqual(self.ledger.stats(self.route)["UNKNOWN"],1)
        self.assertTrue(self.ledger.pause_state(self.route)[0])
        self.assertEqual(self.ledger.requeue_confirmed_blocked(self.route,lambda job:True),0)
    def test_begin_cannot_claim_same_job_twice(self):
        self.accept([{"id":"a","text":"x"}])
        job=self.ledger.next_pending(self.route)
        self.ledger.begin(job["id"])
        with self.assertRaisesRegex(ForwardError,"JOB_ALREADY_CLAIMED"):self.ledger.begin(job["id"])
    def test_resume_requires_matching_no_submit_receipt(self):
        self.accept([{"id":"a","text":"x"}])
        job=self.ledger.next_pending(self.route)
        self.ledger.begin(job["id"])
        self.ledger.finish(job["id"],{"status":"BLOCKED","reason":"STOP_USER_CANCELLED"})
        self.assertEqual(self.ledger.requeue_confirmed_blocked(self.route,lambda job:False),0)
        self.assertEqual(self.ledger.requeue_confirmed_blocked(self.route,lambda job:True),1)
    def test_invalid_text_never_enters_pending_batch(self):
        counts=self.accept([{"id":"a","text":""},{"id":"b","text":"a\x00b"},{"id":"c","text":"x"*10001}])
        self.assertEqual(counts["UNSUPPORTED"],3)
        self.assertEqual(self.ledger.pending_batch(self.route),[])
    def test_engine_one_notice_persistent_progress_and_unknown_stops_batch(self):
        source=ManualBatch(self.route,[{"id":"a","text":"x"},{"id":"b","text":"y"}])
        calls=[]
        def notice(stop,*,count):calls.append(("notice",count));return True
        def send(job,route,stop):calls.append(("send",job["id"]));return {"status":"UNKNOWN","reason":"UNCONFIRMED"}
        engine=Engine(self.route,{"poll_seconds":5,"send_seconds":1},source,self.ledger,send,notice,
            before_send=lambda index,total:calls.append(("progress",index,total)) or True,
            after_batch=lambda outcome:calls.append(("end",outcome)))
        engine.tick();engine.tick()
        self.assertEqual(calls,[("notice",2),("progress",1,2),("send",1),("end","unknown")])
        self.assertTrue(engine.send_paused)
        self.assertEqual(self.ledger.stats(self.route),{"PENDING":1,"UNKNOWN":1,"UNSUPPORTED":0})
    def test_stop_before_tick_sends_nothing(self):
        stop=threading.Event();stop.set()
        source=ManualBatch(self.route,[{"id":"a","text":"x"}])
        def never(*a,**k):raise AssertionError("no desktop work")
        Engine(self.route,{"poll_seconds":5,"send_seconds":1},source,self.ledger,never,never,stop=stop).tick()
        self.assertIsNone(self.ledger.next_pending(self.route))

