"""取得ワーカー: Device をこのスレッドで回し、取得結果をシグナルで画面へ送る。

設定の変更は画面のスレッドから post() でキューに入れ、取得の合間に反映する(遅い取得や Normal トリガの
待ちで画面が固まらないように)。外部操作(ソケット)は Device のロックで直列化されるので、ここを通らない。
遅い時間軸のフリーランは逐次表示(begin_progressive / poll_progressive)で回す。
"""

import threading
import time

from PySide6.QtCore import QThread, Signal

from ..core import timebase, trigger
from ..core.device import DeviceError

NORMAL_SLICE_S = 0.4         # Normal トリガの待ちを区切る(この間隔で設定の変更を拾う)
PROG_POLL_S = 0.25
MIN_FRAME_S = 1 / 40.0       # 画面の更新は最大 40 回/s
RETRY_S = 0.1                # 未成立(Normal)の取り直しの最短間隔


class Worker(QThread):
    frame = Signal(object)              # core.device.Frame
    waiting = Signal(bool)              # Normal トリガ待ち
    status = Signal(dict)               # Device.status() の写し
    failed = Signal(str, str)           # message, hint
    result = Signal(str, object)        # post() に付けた名前と、その戻り値
    run_state = Signal(bool)

    def __init__(self, device):
        QThread.__init__(self)
        self.device = device
        self.running = True
        self._single = False
        self._quit = False
        self._q = []
        self._lock = threading.Lock()
        self._waiting = None

    def _set_waiting(self, on):
        if on != self._waiting:                  # 変わったときだけ知らせる(画面を描き直させすぎない)
            self._waiting = on
            self.waiting.emit(on)

    # ── 画面のスレッドから ──────────────────────────────────────────
    def post(self, name, fn, *args, **kwargs):
        with self._lock:
            self._q.append((name, fn, args, kwargs))

    def refresh(self):
        self.post(None, lambda: None)

    def set_running(self, on):
        self.running = bool(on)
        self.run_state.emit(self.running)

    def single(self):
        self._single = True

    def stop(self):
        """取得スレッドを止める。取得中なら終わるまで最大 3 秒待ち、それでも止まらなければ打ち切る。"""
        self._quit = True
        if not self.wait(3000):
            self.terminate()
            self.wait(1000)

    # ── ワーカー ────────────────────────────────────────────────────
    def _apply(self):
        with self._lock:
            q, self._q = self._q, []
        if not q:
            return False
        for name, fn, args, kwargs in q:
            try:
                r = fn(*args, **kwargs)
                if name:
                    self.result.emit(name, r)
            except DeviceError as e:
                self.failed.emit(str(e), e.hint or "")
            except Exception as e:                       # noqa: BLE001
                self.failed.emit("%s: %s" % (type(e).__name__, e), "")
        self.status.emit(self.device.status())
        return True

    def _pending(self):
        with self._lock:
            return bool(self._q)

    def run(self):
        self.status.emit(self.device.status())
        last = 0.0
        while not self._quit:
            self._apply()
            if not (self.running or self._single):
                time.sleep(0.02)
                continue
            d = self.device
            try:
                free = trigger.kind(d.trig["source"]) is None
                if timebase.progressive(d.fs, free, d.deep):
                    self._progressive()
                else:
                    normal = d.trig["mode"] == "normal" and not free
                    t0 = time.time()
                    f = d.capture(timeout=NORMAL_SLICE_S if normal else 2.0)
                    if f is None:
                        self._set_waiting(True)
                        dt = time.time() - t0
                        if dt < RETRY_S:
                            time.sleep(RETRY_S - dt)
                        continue
                    self._set_waiting(False)
                    dt = time.time() - last
                    if dt < MIN_FRAME_S:
                        time.sleep(MIN_FRAME_S - dt)
                    last = time.time()
                    self.frame.emit(f)
                if self._single:
                    self._single = False
                    self.set_running(False)
            except DeviceError as e:
                self.failed.emit(str(e), e.hint or "")
                time.sleep(0.5)
            except Exception as e:                           # noqa: BLE001
                self.failed.emit("%s: %s" % (type(e).__name__, e), "")
                time.sleep(0.5)

    def _progressive(self):
        d = self.device
        d.begin_progressive()
        while not self._quit:
            time.sleep(PROG_POLL_S)
            if self._pending() or not (self.running or self._single):
                return                                        # 設定が変わった / 止めた → 取り直し
            f, done = d.poll_progressive()
            self.frame.emit(f)
            if done:
                return
