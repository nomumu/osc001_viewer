"""縦軸: 画面の目盛り(V/div・0 V 位置・プローブ)→ ハードのレンジ(タップ・段・Position)の自動選択と、
0 V 合わせの値の保存。

装置の 8 bit 値は 10 bit ADC 値 m の切り出し:
    生値 ≒ m / 2^段 − Position·2^(1−段)、0 V のコード = m0 / 2^段 − 127 + a·Position(a = −2^(1−段))
1 カウントの電圧(VCOUNT)は両 ch 共通で、ch ごとに違うのは 0 V の m(m0)だけ。
電圧はすべて入力端子の値で扱い、プローブ倍率は ScreenAxis が掛け外しする。
"""

import datetime
import json
import os
from dataclasses import dataclass

from .._api import osc001

VDIV_STEPS = [0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]   # 表示の V/div(プローブ倍率を掛けた後)
DIVS = 8
# 細かい順。×1 は 5.04 mV × 2^段、÷8 は 47.9 mV × 2^段
HW_RANGES = [(1, 0), (1, 1), (1, 2), (8, 0), (8, 1), (8, 2)]
X1_CLAMP_V = osc001.X1_CLAMP_V
INPUT_LIMIT_V = 24.0                      # 回路の入力上限(公式仕様 ±24 V)


def vlsb(tap, gain):
    return osc001.VCOUNT[tap] * (1 << gain)


def slope(gain):
    return osc001.POSITION_SLOPE[gain]


def zero_code(m0, gain, position):
    return m0 / float(1 << gain) - 127.0 + slope(gain) * position


@dataclass
class ScreenAxis:
    """画面の縦軸。vdiv は表示の V/div(プローブ倍率込み)、pos_div は 0 V の位置(中央から上へ何 div)。"""
    vdiv: float = 1.0
    pos_div: float = 0.0
    probe: float = 1.0

    def input_range(self):
        """画面の下端〜上端を入力端子の電圧で。"""
        return ((-DIVS / 2 - self.pos_div) * self.vdiv / self.probe,
                (DIVS / 2 - self.pos_div) * self.vdiv / self.probe)


@dataclass
class RangeChoice:
    tap: int
    gain: int
    position: int
    vlsb: float
    win_lo: float          # 測定できる範囲(入力端子、V)
    win_hi: float
    screen_lo: float       # 画面の範囲(入力端子、V)
    screen_hi: float

    @property
    def out_low(self):
        return self.win_lo > self.screen_lo + 1e-9

    @property
    def out_high(self):
        return self.win_hi < self.screen_hi - 1e-9

    @property
    def covers(self):
        return not (self.out_low or self.out_high)

    def label(self):
        return "%s 段%d" % ("÷8" if self.tap == 8 else "×1", self.gain)

    def as_dict(self):
        return {"tap": self.tap, "gain": self.gain, "position": self.position, "v_per_lsb": self.vlsb,
                "window": [self.win_lo, self.win_hi], "screen": [self.screen_lo, self.screen_hi],
                "covers": self.covers, "label": self.label()}


def window(m0, tap, gain, position):
    """(タップ, 段, Position)で測れる入力電圧の範囲。8 bit の窓と 10 bit ADC の範囲の共通部分。

    ×1 は入力が 3.35 V でクランプされるので、上端をそこで切る。
    """
    v = vlsb(tap, gain)
    z = zero_code(m0, gain, position)
    lo, hi = (-127 - z) * v, (128 - z) * v
    vc = osc001.VCOUNT[tap]
    lo = max(lo, (0 - m0) * vc)
    hi = min(hi, (osc001.MON_MAX - m0) * vc)
    if tap == 1:
        hi = min(hi, X1_CLAMP_V)
    return lo, hi


def best_position(m0, tap, gain, v_mid):
    """窓の中央(コード 0.5)が v_mid に来る Position byte(0..255 で止める)。"""
    b = (0.5 - v_mid / vlsb(tap, gain) - (m0 / float(1 << gain) - 127.0)) / slope(gain)
    return max(0, min(255, int(round(b))))


def choose(screen_lo, screen_hi, m0_of, fixed=None):
    """画面の範囲を覆える最も細かいレンジを選ぶ。覆えなければ最も粗いレンジ(はみ出しは RangeChoice が示す)。

    m0_of(tap, gain) は 0 V の m を返す関数(Scope.zero_m0 を ch で束ねたもの)。
    fixed=(tap, gain) ならそのレンジに固定し、Position だけ合わせる。
    ×1 は画面上端が 3.35 V を超えるときは使わない(クランプ回避)。
    """
    mid = (screen_lo + screen_hi) / 2.0

    def make(tap, gain):
        m0 = m0_of(tap, gain)
        pos = best_position(m0, tap, gain, mid)
        lo, hi = window(m0, tap, gain, pos)
        return RangeChoice(tap, gain, pos, vlsb(tap, gain), lo, hi, screen_lo, screen_hi)

    if fixed is not None:
        return make(*fixed)
    for tap, gain in HW_RANGES:
        if tap == 1 and screen_hi > X1_CLAMP_V:
            continue
        r = make(tap, gain)
        if r.covers:
            return r
    return make(*HW_RANGES[-1])


# ── 0 V 合わせの値の保存 ─────────────────────────────────────────────
def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "osc001_viewer")


class ZeroStore(object):
    """calibrate_zero の結果(m0 と レンジ別 m0)を JSON に保存し、起動時に Scope へ戻す。

    osc001 には USB のシリアル番号が無いので、値は 1 組だけ持ち、測ったときの USB ポートを一緒に記録する。
    ポートが違えば別の基板の可能性があるので、apply() は注意文を返す。
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "zero.json")

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save(self, scope, port=None):
        data = {"version": 1,
                "calibrated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "port": port,
                "m0": {"%d,%d" % (ch, tap): scope.m0[ch][tap] for ch in (1, 2) for tap in (8, 1)},
                "m0_gain": {"%d,%d,%d" % k: v for k, v in sorted(scope.m0_gain.items())}}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        return data

    def apply(self, scope, port=None):
        """保存値を Scope に入れる。返り値 (data or None, 注意文 or None)。"""
        data = self.load()
        if not data:
            return None, None
        for k, v in data.get("m0", {}).items():
            ch, tap = (int(x) for x in k.split(","))
            scope.set_zero_m0(ch, tap, float(v))
        for k, v in data.get("m0_gain", {}).items():
            ch, tap, g = (int(x) for x in k.split(","))
            scope.m0_gain[(ch, tap, g)] = float(v)
        note = None
        if port and data.get("port") and data["port"] != port:
            note = ("0 V 合わせの値は別の USB ポート(%s)で測ったもの。別の基板なら 0 V 合わせをやり直す"
                    % data["port"])
        return data, note
