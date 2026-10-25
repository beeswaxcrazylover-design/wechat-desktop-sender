"""Batch begin/progress/end UI, with executor-idle acknowledgement before release.

All GUI/native-window methods run on the owner thread. All wait methods run on
the serial worker. Countdown completion grants work while a persistent banner
remains visible. Only explicit end + verified executor idle releases it.
"""
from dataclasses import dataclass, field
import math
import queue
import threading
import time

from .notice_geometry import PlacementGeometry, choose_rect


@dataclass
class _Command:
    kind: str
    values: dict
    done: threading.Event = field(default_factory=threading.Event)
    result: bool = False
    error: str | None = None


@dataclass
class _Batch:
    id: str
    total: int
    seconds: int
    stop: object
    geometry: PlacementGeometry
    ready: _Command = field(default_factory=lambda: _Command('begin', {}))
    commands: queue.Queue = field(default_factory=queue.Queue)
    phase: str = 'QUEUED'
    index: int = 0
    deadline: float | None = None
    idle_confirmed: bool = False
    error: str | None = None


class _View:
    def __init__(self, owner, total, cancel):
        self.owner, self.total, self.cancel = owner, total, cancel
        self.countdown = self.banner = None
        self.label = None
        self.bounds = None
        scale = float(owner.winfo_fpixels('1i')) / 96
        self.size = (math.ceil((360 + 12*len(str(total))) * scale), math.ceil(78 * scale))
        self.font_pixels = max(12, math.ceil(16 * scale))

    def start_countdown(self, seconds):
        import tkinter as tk
        from tkinter import ttk
        window = self.countdown = tk.Toplevel(self.owner)
        window.withdraw()
        try:
            window.title('即将发送 · 请暂勿操作键鼠')
            window.resizable(False, False)
            area = ttk.Frame(window, padding=24)
            area.pack(fill='both', expand=True)
            ttk.Label(area, text='即将发送 %s 条消息' % self.total,
                      font=('Microsoft YaHei UI', 14, 'bold')).pack()
            ttk.Label(area, text='请暂时不要操作鼠标和键盘。\n倒计时后会持续显示发送进度。\n看到“可以操作”后再使用键鼠。', justify='center').pack(pady=12)
            self.label = ttk.Label(area, font=('Microsoft YaHei UI', 18, 'bold'))
            self.label.pack(pady=8)
            ttk.Button(area, text='停止本次运行', command=self.cancel).pack()
            window.protocol('WM_DELETE_WINDOW', self.cancel)
            self.tick(seconds)
            window.update_idletasks()
            width, height = window.winfo_reqwidth(), window.winfo_reqheight()
            window.geometry('+%d+%d' % (max(0, (window.winfo_screenwidth()-width)//2),
                                       max(0, (window.winfo_screenheight()-height)//2)))
            window.attributes('-topmost', True)
            window.deiconify()
            window.lift()
            window.focus_force()  # Only the preflight countdown may activate.
            window.update_idletasks()
        except BaseException:
            self.close_countdown()
            raise

    def tick(self, remaining):
        self.label.config(text='%s 秒后开始' % remaining)

    def set_banner(self, text, geometry):
        from .notice_native import NativeBanner
        bounds = choose_rect(self.size, geometry, previous=self.bounds)
        if self.banner is None:
            self.banner = NativeBanner(bounds, font_pixels=self.font_pixels)
            self.banner.show(text)
        else:
            if self.bounds != bounds:
                self.banner.move(bounds)
            self.banner.set_text(text)
        self.bounds = bounds

    def close_countdown(self):
        if self.countdown is not None:
            self.countdown.destroy()
            self.countdown = None
            self.owner.update_idletasks()

    def destroy(self):
        self.close_countdown()
        if self.banner is not None:
            self.banner.destroy()
            self.banner = None


class BatchNotice:
    def __init__(self, owner, *, view_factory=_View, clock=time.monotonic, release_seconds=2):
        if not 0 < release_seconds <= 10:
            raise ValueError('INVALID_RELEASE_SECONDS')
        self.owner, self.factory, self.clock = owner, view_factory, clock
        self.release_seconds = release_seconds
        self.ui_thread = threading.get_ident()
        self.lock = threading.Lock()
        self.batch = None
        self.view = None
        self.view_batch = None
        self.closed = self.close_requested = False
        self.timer = owner.after(50, self._pump)

    def _worker(self):
        if threading.get_ident() == self.ui_thread:
            raise RuntimeError('BATCH_NOTICE_WAIT_REQUIRES_WORKER')

    def _wait(self, batch, command, timeout=15):
        until = self.clock() + timeout
        while not command.done.wait(.05):
            if self.clock() >= until:
                batch.stop.set()
                raise RuntimeError('BATCH_NOTICE_UI_TIMEOUT')
        if command.error:
            raise RuntimeError(command.error)
        return command.result

    def begin(self, *, batch_id, count, stop_event, geometry, seconds=5):
        self._worker()
        if not isinstance(batch_id, str) or not batch_id or type(count) is not int or count < 1:
            raise ValueError('INVALID_BATCH')
        if type(seconds) is not int or not 0 <= seconds <= 30:
            raise ValueError('INVALID_NOTICE_SECONDS')
        if not isinstance(geometry, PlacementGeometry):
            raise ValueError('NOTICE_FRESH_TARGET_GEOMETRY_REQUIRED')
        with self.lock:
            if self.closed or self.close_requested or stop_event.is_set():
                return False
            if self.batch is not None and self.batch.phase != 'FINISHED':
                raise RuntimeError('BATCH_ALREADY_ACTIVE')
            if self.batch is not None and self.batch.id == batch_id:
                raise RuntimeError('BATCH_ID_REUSED')
            batch = self.batch = _Batch(batch_id, count, seconds, stop_event, geometry)
        return self._wait(batch, batch.ready, seconds+15) and not stop_event.is_set()

    def _matching(self, batch_id):
        with self.lock:
            batch = self.batch
            return batch if batch is not None and batch.id == batch_id else None

    def progress(self, batch_id, *, index, geometry):
        self._worker()
        batch = self._matching(batch_id)
        if batch is None or batch.phase == 'FINISHED' or batch.stop.is_set():
            return False
        if type(index) is not int or not 1 <= index <= batch.total:
            raise ValueError('INVALID_BATCH_INDEX')
        if not isinstance(geometry, PlacementGeometry):
            raise ValueError('NOTICE_FRESH_TARGET_GEOMETRY_REQUIRED')
        command = _Command('progress', {'index': index, 'geometry': geometry})
        batch.commands.put(command)
        return self._wait(batch, command) and not batch.stop.is_set()

    def end(self, batch_id, *, outcome, executor_idle):
        self._worker()
        batch = self._matching(batch_id)
        if batch is None:
            return False  # A stale completion must never hide the next batch.
        if outcome not in ('completed', 'cancelled', 'failed', 'unknown'):
            raise ValueError('INVALID_BATCH_OUTCOME')
        try:
            idle = callable(executor_idle) and executor_idle() is True
        except Exception:
            idle = False
        if not idle:
            batch.stop.set()
            return False  # Keep the warning until the executor has actually joined.
        if batch.phase == 'FINISHED':
            return True
        command = _Command('end', {'outcome': outcome})
        batch.commands.put(command)
        return self._wait(batch, command)

    @staticmethod
    def _answer(command, result=False, error=None):
        command.result, command.error = result, error
        command.done.set()

    def _dispose_view(self):
        if self.view is not None:
            self.view.destroy()
            self.view = self.view_batch = None

    def _warning(self, batch):
        return '\n正在发送 %s/%s\n请勿动鼠标和键盘' % (batch.index, batch.total)

    def _stopping(self, batch):
        return '\n正在停止 %s/%s\n请勿动键鼠，等待当前操作结束' % (batch.index, batch.total)

    def _show_stopping(self, batch):
        if batch.phase == 'FINISHED':
            return
        first = batch.phase != 'STOPPING'
        batch.phase = 'STOPPING'
        if not batch.ready.done.is_set():
            self._answer(batch.ready)
        if first and self.view is not None:
            # Retain the existing banner at its last verified safe rectangle.
            # If no banner was shown, keep the countdown warning until end().
            if self.view.banner is not None:
                self.view.banner.set_text(self._stopping(batch))
            elif self.view.countdown is not None:
                self.view.label.config(text='正在停止，请等待…')

    def _finish(self, batch, command):
        # This command is enqueued only after executor_idle returned exactly True.
        batch.idle_confirmed = True
        batch.phase = 'FINISHED'
        batch.deadline = self.clock() + self.release_seconds
        names = {'completed': '本批操作已结束', 'cancelled': '本批已安全取消',
                 'failed': '本批已停止，发送受阻', 'unknown': '本批已停止，结果待核'}
        error = None
        try:
            if self.view is not None:
                text = '\n%s\n可以操作鼠标和键盘' % names[command.values['outcome']]
                if self.view.banner is not None:
                    # Executor is idle; keep the existing status window even if
                    # a failed geometry refresh had no alternative safe slot.
                    self.view.banner.set_text(text)
                else:
                    self.view.set_banner(text, batch.geometry)
                self.view.close_countdown()
        except Exception:
            # Safe to clean up now, because the executor has stopped all actions.
            error = 'BATCH_NOTICE_RELEASE_DISPLAY_FAILED'
            self._dispose_view()
        finally:
            if not batch.ready.done.is_set():
                self._answer(batch.ready)
            self._answer(command, error is None, error)

    def _pump(self):
        self.timer = None
        with self.lock:
            batch = self.batch
        command = None
        try:
            if batch is not None:
                if self.view_batch is not None and self.view_batch is not batch:
                    self._dispose_view()  # Previous batch already has an idle acknowledgement.
                if batch.phase == 'QUEUED':
                    if batch.stop.is_set() or self.close_requested:
                        self._show_stopping(batch)
                    else:
                        self.view = self.factory(self.owner, batch.total, self.cancel)
                        self.view_batch = batch
                        choose_rect(self.view.size, batch.geometry)  # Reject unsafe placement before countdown/work.
                        batch.phase = 'COUNTDOWN'
                        if batch.seconds:
                            self.view.start_countdown(batch.seconds)
                        batch.deadline = self.clock() + batch.seconds
                if batch.stop.is_set() and batch.phase != 'FINISHED':
                    self._show_stopping(batch)
                if batch.phase == 'COUNTDOWN':
                    remaining = max(0, math.ceil(batch.deadline-self.clock()))
                    if remaining:
                        self.view.tick(remaining)
                    else:
                        self.view.set_banner(self._warning(batch), batch.geometry)
                        self.view.close_countdown()
                        batch.phase = 'ACTIVE'
                        self._answer(batch.ready, not batch.stop.is_set())
                for _ in range(100):
                    try:
                        command = batch.commands.get_nowait()
                    except queue.Empty:
                        break
                    if command.kind == 'end':
                        self._finish(batch, command)
                    elif command.kind == 'progress':
                        if batch.phase != 'ACTIVE' or batch.stop.is_set():
                            self._answer(command)
                        elif command.values['index'] < batch.index:
                            self._answer(command, error='BATCH_PROGRESS_REWOUND')
                        else:
                            batch.geometry = command.values['geometry']
                            batch.index = command.values['index']
                            self.view.set_banner(self._warning(batch), batch.geometry)
                            self._answer(command, True)
                    command = None
                if batch.phase == 'FINISHED' and self.clock() >= batch.deadline:
                    self._dispose_view()
        except Exception:
            if batch is not None:
                batch.stop.set()
                batch.error = 'BATCH_NOTICE_UI_FAILED'
                if not batch.ready.done.is_set():
                    self._answer(batch.ready, error=batch.error)
                if command is not None and not command.done.is_set():
                    self._answer(command, error=batch.error)
                if batch.phase != 'FINISHED':
                    batch.phase = 'STOPPING'  # Never destroy a warning while executor state is unknown.
        if not self.closed:
            try:
                self.timer = self.owner.after(50, self._pump)
            except Exception:
                self.close_requested = True
                if batch is not None:
                    batch.stop.set()

    def cancel(self):
        if threading.get_ident() != self.ui_thread:
            raise RuntimeError('BATCH_NOTICE_CANCEL_REQUIRES_UI_THREAD')
        batch = self.batch
        if batch is not None and batch.phase != 'FINISHED':
            batch.stop.set()
            self._show_stopping(batch)

    def close(self):
        """UI-thread shutdown. False means keep the owner alive until end+idle."""
        if threading.get_ident() != self.ui_thread:
            raise RuntimeError('BATCH_NOTICE_CLOSE_REQUIRES_UI_THREAD')
        self.close_requested = True
        self.cancel()
        batch = self.batch
        if batch is not None and batch.phase != 'FINISHED':
            return False
        self._dispose_view()
        self.closed = True
        if self.timer is not None:
            self.owner.after_cancel(self.timer)
            self.timer = None
        return True
