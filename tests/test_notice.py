import _support
import threading
import time
import unittest
from wechat_desktop_sender.batch_notice import BatchNotice
from wechat_desktop_sender.notice_geometry import PlacementGeometry, PlacementError, choose_rect, intersects


class Owner:
    def after(self, ms, fn):return "synthetic-timer"
    def after_cancel(self, timer):pass


class View:
    def __init__(self, owner, total, cancel):
        self.size=(200,60);self.banner=None;self.countdown=None;self.destroyed=False;self.messages=[]
        self.label=self
    def config(self,**kwargs):pass
    def start_countdown(self, seconds):self.countdown=True
    def tick(self, seconds):pass
    def set_banner(self,text,geometry):self.banner=self;self.messages.append(text)
    def set_text(self,text):self.messages.append(text)
    def close_countdown(self):self.countdown=None
    def destroy(self):self.destroyed=True;self.banner=None;self.countdown=None


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.notice=BatchNotice(Owner(),view_factory=View,clock=lambda:self.now)
        self.stop=threading.Event()
        self.geometry=PlacementGeometry(((0,0,1000,700),),((0,150,700,700),))
        self.threads=[]
    def threaded(self, callback):
        result=[]
        def run():
            try:result.append(callback())
            except Exception as error:result.append(error)
        thread=threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread,result
    def until(self, predicate):
        for _ in range(100):
            if predicate():return
            time.sleep(.002)
        self.fail("worker did not reach expected synthetic phase")
    def start(self):
        t,out=self.threaded(lambda:self.notice.begin(batch_id="synthetic-batch",count=2,
            stop_event=self.stop,geometry=self.geometry,seconds=5))
        self.until(lambda:self.notice.batch is not None)
        self.notice._pump()
        return t,out
    def end(self,idle=True):
        t,out=self.threaded(lambda:self.notice.end("synthetic-batch",outcome="completed",executor_idle=lambda:idle))
        if idle:
            self.until(lambda:not self.notice.batch.commands.empty())
            self.notice._pump()
        t.join(1)
        return out
    def tearDown(self):
        if self.notice.batch and self.notice.batch.phase != "FINISHED":self.end()
        self.notice.close()
        for t in self.threads:t.join(1)
    def test_five_seconds_before_work_then_banner_until_idle(self):
        t,out=self.start()
        self.now=4.99;self.notice._pump()
        self.assertEqual(out,[])
        self.now=5;self.notice._pump();t.join(1)
        self.assertEqual(out,[True])
        view=self.notice.view
        self.assertIsNotNone(view.banner)
        self.assertIsNone(view.countdown)
        self.now=40;self.notice._pump()
        self.assertFalse(view.destroyed)
        self.assertEqual(self.end(),[True])
        self.assertFalse(view.destroyed)
        self.now=43;self.notice._pump()
        self.assertTrue(view.destroyed)
    def test_unknown_executor_keeps_warning_visible(self):
        t,out=self.start();self.now=5;self.notice._pump();t.join(1)
        self.assertEqual(self.end(idle=False),[False])
        self.assertTrue(self.stop.is_set())
        self.notice._pump()
        self.assertIsNotNone(self.notice.view.banner)
        self.assertFalse(self.notice.close())
    def test_stop_during_countdown_prevents_work(self):
        t,out=self.start()
        self.notice.cancel();t.join(1)
        self.assertTrue(self.stop.is_set())
        self.assertEqual(out,[False])
        self.assertFalse(self.notice.close())
    def test_owner_thread_wait_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,"WORKER"):
            self.notice.begin(batch_id="synthetic-batch",count=1,stop_event=self.stop,geometry=self.geometry)
    def test_no_overlap_with_protected_windows(self):
        box=choose_rect((200,60),self.geometry)
        self.assertFalse(any(intersects(box,p) for p in self.geometry.protected))
    def test_full_screen_target_cannot_hide_notice(self):
        geometry=PlacementGeometry(((0,0,1000,700),),((0,0,1000,700),))
        with self.assertRaisesRegex(PlacementError,"NO_SAFE_VISIBLE_SPACE"):choose_rect((200,60),geometry)

