"""装置なしで core・画面を試すための偽の Scope(テストと osc001-viewer --fake で使う)。

osc001.Scope を継承し、HID の送受信(_txn)だけを差し替える。op8 は「そのときの設定で signal(ch, t) を
取ったら返るはずの生値」を作って返すので、API の縦軸の計算(zero_code・V/LSB)もそのまま通る。
"""

import math

import time

from .._api import osc001


def default_la_signal(ch, t):
    """LA の偽の信号。D1 = 10 kHz の矩形、D2 = UART 115200 8N1 で "OK\\r\\n"(t = 0 から)、D3・D4 = High。"""
    if ch == 1:
        return 1 if (t * 1e4) % 1.0 < 0.5 else 0
    if ch == 2:
        tb, k = 1 / 115200.0, int(t * 115200.0)
        frame, pos = divmod(k, 11)                       # 1 バイト = スタート + 8 + ストップ + 間 1
        data = b"OK\r\n"
        if t < 0 or frame >= len(data):
            return 1
        if pos == 0:
            return 0
        if 1 <= pos <= 8:
            return (data[frame] >> (pos - 1)) & 1
        return 1
    return 1


def default_signal(ch, t):
    if ch == 1:
        return 5.0 if (t * 1000.0) % 1.0 < 0.5 else 0.0          # 1 kHz、0/5 V の矩形
    return 1.0 + 0.8 * math.sin(2 * math.pi * 250.0 * t)        # 250 Hz の正弦


class FakeScope(osc001.Scope):
    def __init__(self, path="/dev/null", signal=None, mon=None, op6=1, live=False, la_signal=None):
        osc001.Scope.__init__(self, path)
        self.la_signal = la_signal or default_la_signal
        self.live = live              # True なら取得ごとに位相が進む(画面の確認用)
        self._t0 = 0.0
        self.signal = signal or default_signal
        self.mon = dict(mon or {5: 600, 8: 497, 6: 1019, 9: 386})
        self.op6 = op6
        self.ops = []

    def open(self):
        self._fd = -1
        return self

    def close(self):
        self._fd = None

    def _code(self, ch, v):
        c = int(round(v / self.v_per_lsb(ch) + self.zero_code(ch)))
        return max(-127, min(128, c)) + 0x7F

    def _txn(self, op, args=(), timeout=None):
        self.ops.append((op, tuple(args)))
        if op == 9:
            a, b = self.mon.get(args[0], 0), self.mon.get(args[1], 0)
            return bytes([a >> 8, a & 0xFF, b >> 8, b & 0xFF]) + bytes(60)
        if op == 6:
            return bytes([self.op6]) + bytes(63)
        if op == 8 and self._la_armed_at is not None:  # LA: 1 バイトに 2 サンプル(下位ニブル先)、ch1 = bit3
            from .la import latency
            fs = self._la_rate[0]
            lat = latency(fs)
            raw = bytearray()
            for k in range(448):
                nib = []
                for i in (2 * k, 2 * k + 1):
                    t = lat + i / fs
                    nib.append(sum(self.la_signal(ch, t) << (4 - ch) for ch in (1, 2, 3, 4)))
                raw.append(nib[0] | (nib[1] << 4))
            c = args[0]
            return bytes(raw[c * 64:(c + 1) * 64])
        if op == 8:
            fs = self.sample_rate()
            if args[0] == 0:
                self._t0 = time.time() % 1.0 if self.live else 0.0
            raw = bytearray()
            for i in range(224):
                for ch in (1, 2):
                    raw.append(self._code(ch, self.signal(ch, self._t0 + i / fs)))
            c = args[0]
            return bytes(raw[c * 64:(c + 1) * 64])
        return bytes([op]) + bytes(63)
