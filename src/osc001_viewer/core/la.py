"""ロジックアナライザ: レートの選択肢と、記録の先頭の遅れ。

記録はトリガ点から少し遅れて始まる(プリトリガなし)。時刻はトリガ点を 0 とし、サンプル i の時刻は
lat + i/fs。遅れは 979.4 k / 490.9 k / 200 kHz で 7.3 / 6.8 / 6.3 µs、108.7 kHz 以下で約 1.8 サンプル
(2026-10-18、水晶の UART のスタートビットでトリガし、ビット境界から逆算した実測。ばらつき ±0.4 µs)。
979.4 kHz で取れるのはパルス幅 1.5 µs 以上(矩形で 333 kHz まで)。
"""

import numpy as np

from .._api import osc001

N = osc001.LA_SAMPLES                                          # 844
RATES = [979400.0, 490900.0, 200000.0, 100000.0, 20000.0, 2000.0, 100.0, 10.0]
FIXED_LATENCY = {979400.0: 7.3e-6, 490900.0: 6.8e-6, 200000.0: 6.3e-6}
VARIABLE_LATENCY_SAMPLES = 1.8
MIN_PULSE_S = 1.5e-6                                           # 979.4 kHz で取れる最短のパルス
CHANNELS = (1, 2, 3, 4)


def latency(fs):
    """トリガ点から記録の先頭(サンプル 0)までの時間。"""
    for f, lat in FIXED_LATENCY.items():
        if abs(fs - f) < 1.0:
            return lat
    return VARIABLE_LATENCY_SAMPLES / fs


class Capture(object):
    """LA 1 回分。bits[ch] は 0/1 の numpy 配列(ch 1〜4)。t0 = 先頭の時刻(= 遅れ)。"""

    def __init__(self, bits, fs, lat=None, acquired_at=None):
        self.bits = {ch: np.asarray(bits[ch], dtype=np.uint8) for ch in CHANNELS}
        self.fs = float(fs)
        self.lat = latency(fs) if lat is None else float(lat)
        self.acquired_at = acquired_at

    @classmethod
    def from_trace(cls, t, fs, acquired_at=None):
        return cls({ch: t.bits(ch) for ch in CHANNELS}, fs, acquired_at=acquired_at)

    @property
    def n(self):
        return len(self.bits[1])

    def time(self, i):
        return self.lat + i / self.fs

    def index(self, t):
        """時刻 t(トリガ点基準)に最も近いサンプル。範囲外は None。"""
        i = int(round((t - self.lat) * self.fs))
        return i if 0 <= i < self.n else None

    def transitions(self, ch):
        b = self.bits[ch]
        return int(np.count_nonzero(b[1:] != b[:-1]))

    def window(self):
        return self.n / self.fs
