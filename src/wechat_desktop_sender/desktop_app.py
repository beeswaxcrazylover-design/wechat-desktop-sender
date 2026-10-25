"""Small manual-batch UI adapter. The sending/notice engines remain extracted."""
import queue
import threading
from uuid import uuid4

from .api import ManualBatch
from .configuration import validate_unchanged
from .simple_core import Engine, ForwardError, Ledger, safe_code


class Application:
    def __init__(self, root, configuration, messages):
        from tkinter import ttk, StringVar
        from .batch_notice import BatchNotice
        self.root, self.configuration, self.messages = root, configuration, messages
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.engine = None
        self.batch_id = None
        self.closing = False
        self.send_notice = BatchNotice(root)
        root.title("WeChat Desktop Sender")
        self.status = StringVar(value="准备发送；每批先显示 5 秒预告。")
        ttk.Label(root, textvariable=self.status, padding=14, wraplength=480).pack()
        ttk.Button(root, text="停止", command=self.stop_event.set).pack(side="left", padx=10, pady=10)
        ttk.Button(root, text="恢复已暂停队列", command=self.resume).pack(side="right", padx=10, pady=10)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.worker = threading.Thread(target=self.work, daemon=False)
        root.after(100, self.worker.start)
        root.after(50, self.pump)

    def emit(self, **values):
        # No message bodies, aliases, OCR, window handles or configuration paths.
        self.events.put({k:v for k,v in values.items() if k in
            ("code","job","reason","stats","counts","protected_unknown","requeued")})

    @staticmethod
    def notice_call(callback, *args, **kwargs):
        try:return callback(*args, **kwargs)
        except RuntimeError as error:
            code=str(error)
            if code in ('BATCH_NOTICE_UI_FAILED','BATCH_NOTICE_UI_TIMEOUT','BATCH_NOTICE_RELEASE_DISPLAY_FAILED'):
                raise ForwardError(code) from None
            raise

    def notice(self, stop, *, count=1):
        from .notice_runtime import observe_geometry
        self.batch_id=uuid4().hex
        geometry=observe_geometry()
        return self.notice_call(self.send_notice.begin,batch_id=self.batch_id,count=count,seconds=5,
                                stop_event=stop,geometry=geometry)

    def batch_progress(self, index, total):
        from .notice_runtime import observe_geometry
        return self.notice_call(self.send_notice.progress,self.batch_id,index=index,geometry=observe_geometry())

    def finish_batch(self, outcome):
        from .sender_worker import executor_idle
        batch=self.send_notice.batch
        if batch is None or batch.id!=self.batch_id:return
        if not self.notice_call(self.send_notice.end,self.batch_id,outcome=outcome,executor_idle=executor_idle):
            raise ForwardError('EXECUTOR_NOT_IDLE_OR_NOTICE_RELEASE_UNCONFIRMED')
        self.emit(code='BATCH_FINISHED')

    def work(self):
        from .sender_worker import call_child, verify_safe_retry
        config = self.configuration
        ledger = None
        try:
            validate_unchanged(config)
            ledger = Ledger(config.runtime / "queue.sqlite3")
            self.engine = Engine(config.route, {"poll_seconds":5,"send_seconds":config.send_seconds},
                ManualBatch(config.route,self.messages), ledger, call_child, self.notice,
                notify=self.emit, validate_route=lambda:validate_unchanged(config),
                safe_retry_check=verify_safe_retry, before_send=self.batch_progress,
                after_batch=self.finish_batch, stop=self.stop_event)
            self.engine.run()
            ledger = None  # Engine.run closed its own ledger.
        except Exception as error:
            self.emit(code="ERROR_STOPPED", reason=safe_code(error))
        finally:
            if ledger is not None:ledger.close()
            from .diagnostics import clear_reports
            clear_reports()
            self.emit(code="STOPPED")

    def resume(self):
        if self.engine is not None:self.engine.request_resume()

    def pump(self):
        while True:
            try:event = self.events.get_nowait()
            except queue.Empty:break
            if event.get("code") == "STATS":
                self.status.set(str(event.get("stats", {})))
            else:
                self.status.set(event.get("code", "")+" "+event.get("reason", ""))
        if self.closing and not self.worker.is_alive() and self.send_notice.close():
            self.root.destroy()
            return
        self.root.after(50, self.pump)

    def close(self):
        self.closing = True
        self.stop_event.set()
        if not self.worker.is_alive() and self.send_notice.close():self.root.destroy()


def launch(configuration, messages):
    from .app_lock import AppLock
    from .dpi_bootstrap import initialize_process_dpi
    lock = AppLock(configuration.runtime / "sender.lock")
    lock.acquire()
    try:
        initialize_process_dpi()  # Before Tk or PyAutoGUI import, matching the source.
        import tkinter as tk
        root = tk.Tk()
        Application(root, configuration, messages)
        root.mainloop()
    finally:
        lock.close()

