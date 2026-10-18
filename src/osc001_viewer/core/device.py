"""デバイス: osc001.Scope を 1 つ持ち、画面の設定(目盛り・時間軸・トリガ)を装置の設定に直して取得する。

GUI と画面なしのサーバ(osc001d)の両方から使う。装置を開けるのは 1 プロセスだけ(hidraw の flock)で、
同じプロセス内でも複数のスレッド(取得ループ・外部操作)から呼ばれるので、公開メソッドは RLock で直列化する。
電圧はプローブ倍率を掛けた表示の単位で受け渡す(内部の計算は入力端子の電圧)。
"""

import datetime
import os
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .._api import osc001
from . import la, measure, timebase, trigger, vertical


class DeviceError(Exception):
    """利用者に見せる失敗。hint に直し方を書く。"""

    def __init__(self, message, hint=None):
        Exception.__init__(self, message)
        self.hint = hint


def usb_port(hidraw_path):
    """/dev/hidrawN → USB の物理ポート名(例 "1-3.2")。分からなければ None。"""
    try:
        d = os.path.realpath("/sys/class/hidraw/%s/device" % os.path.basename(hidraw_path))
        while d and d != "/":
            if os.path.isfile(os.path.join(d, "devpath")) and os.path.isfile(os.path.join(d, "idVendor")):
                return os.path.basename(d)
            d = os.path.dirname(d)
    except OSError:
        pass
    return None


@dataclass
class Frame:
    """スコープ 1 回分。ch の電圧は表示の単位(プローブ倍率込み)。"""
    fs: float
    volts: dict                      # {1: ndarray, 2: ndarray}
    trig_status: str                 # "free" / "triggered" / "auto_free"(Auto で未成立 → フリーラン)
    trig_kind: str = None            # "hw" / "sw" / None
    trig_index: int = -1             # トリガ点の index(hw は 0、sw は交差点)
    start: int = 0                   # 表示の開始 index(sw はトリガの 1 div 前)
    first_valid: int = 0             # これより前は描かない・測らない(hw のサンプル 0)
    valid: int = None                # 逐次表示中: 書き込み済みの点数(None = 全部)
    rail: dict = field(default_factory=dict)
    clipped: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)
    probe: dict = field(default_factory=lambda: {1: 1.0, 2: 1.0})
    acquired_at: str = ""

    @property
    def n(self):
        return len(self.volts[1])

    def times(self):
        """トリガ点を 0 とした時刻(秒)。トリガ無しは先頭が 0。"""
        t0 = self.trig_index if self.trig_index >= 0 else 0
        return (np.arange(self.n) - t0) / self.fs

    def measured(self, ch):
        """測定に使う範囲(先頭の無効点と、逐次表示の未取得部分を除く)。"""
        end = self.n if self.valid is None else self.valid
        return self.volts[ch][self.first_valid:max(self.first_valid, end)]

    def stats(self, ch):
        r = self.ranges.get(ch)
        lsb = r["v_per_lsb"] * self.probe.get(ch, 1.0) if r else None
        return measure.stats(self.measured(ch), self.fs, lsb)

    def as_dict(self, samples=False):
        d = {"fs": self.fs, "n": self.n, "trigger": {"status": self.trig_status, "kind": self.trig_kind,
                                                      "index": self.trig_index},
             "acquired_at": self.acquired_at, "valid": self.valid,
             "first_valid": self.first_valid, "display_start": self.start}
        for ch in (1, 2):
            c = {"stats": self.stats(ch), "rail": self.rail.get(ch), "clipped": self.clipped.get(ch),
                 "range": self.ranges.get(ch)}
            if samples:
                c["volts"] = [round(float(x), 5) for x in self.volts[ch]]
            d["ch%d" % ch] = c
        if samples:
            d["t"] = [round(float(x), 9) for x in self.times()]
        return d


class Device(object):
    def __init__(self, path=None, zero_store=None, scope_factory=None):
        self.path = path
        self.zero_store = zero_store if zero_store is not None else vertical.ZeroStore()
        self._factory = scope_factory or osc001.Scope
        self.lock = threading.RLock()
        self.scope = None
        self.port = None
        self.notes = []                  # 起動時の注意(0 V 合わせのポート違いなど)
        self.zero_info = None
        self.axis = {1: vertical.ScreenAxis(1.0, -2.0), 2: vertical.ScreenAxis(1.0, -2.0)}
        self.fixed = {1: None, 2: None}
        self.choice = {}
        self.tdiv = 1e-3
        self.fs = None
        self.deep = False
        self.trig = {"source": "none", "edge": "rising", "level": 2.5, "mode": "auto"}
        self._prog = None

    # ── セッション ──────────────────────────────────────────────────
    def open(self):
        with self.lock:
            if self.path is None:
                devs = osc001.enumerate()
                if not devs:
                    raise DeviceError("osc001(04d8:f891)が見つからない", "USB の接続と udev ルールを確認する")
                self.path = devs[0].path
            sc = self._factory(self.path)
            try:
                sc.open()
            except osc001.Osc001Error as e:
                raise DeviceError(str(e), "他のプロセス(GUI か osc001d)が開いていないか確認する")
            self.scope = sc
            self.port = usb_port(self.path)
            self.zero_info, note = self.zero_store.apply(sc, self.port)
            self.notes = [note] if note else []
            self.set_timebase(self.tdiv)
            self._apply_ranges()
            self.set_trigger(**self.trig)
            return self

    def close(self):
        with self.lock:
            if self.scope is not None:
                self.scope.close()
                self.scope = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    def _need(self):
        if self.scope is None:
            raise DeviceError("装置を開いていない")
        return self.scope

    # ── 設定 ────────────────────────────────────────────────────────
    def _apply_ranges(self):
        sc = self._need()
        for ch in (1, 2):
            lo, hi = self.axis[ch].input_range()
            r = vertical.choose(lo, hi, lambda tap, g, ch=ch: sc.zero_m0(ch, tap, g), self.fixed[ch])
            sc.set_gain(ch, r.gain, tap=r.tap, position=r.position)
            self.choice[ch] = r

    def set_channel(self, ch, vdiv=None, pos_div=None, probe=None, fixed="keep"):
        """表示の V/div・0 V 位置(div)・プローブ倍率。fixed=(tap, gain) でレンジ固定、None で自動。

        V/div は表示の単位(プローブ ×10 なら 0.2〜50 V/div)。プローブだけ変えると、入力端子での
        V/div を保ったまま表示の V/div が倍率に合わせて変わる。
        """
        if ch not in (1, 2):
            raise DeviceError("ch は 1 か 2", "ch=1 または ch=2 を指定する")
        with self.lock:
            a = self.axis[ch]
            if probe is not None:
                if probe not in (1, 10):
                    raise DeviceError("プローブ倍率は 1 か 10", "probe=1 または probe=10")
                a.vdiv = a.vdiv / a.probe * probe
                a.probe = float(probe)
            if vdiv is not None:
                steps = [v * a.probe for v in vertical.VDIV_STEPS]
                if not any(abs(vdiv - v) < 1e-9 * max(1.0, v) for v in steps):
                    raise DeviceError("V/div %r は選べない" % vdiv, "%s のどれか(プローブ ×%g)" % (steps, a.probe))
                a.vdiv = float(vdiv)
            if pos_div is not None:
                a.pos_div = float(pos_div)
            if fixed != "keep":
                if fixed is not None and tuple(fixed) not in vertical.HW_RANGES:
                    raise DeviceError("固定レンジ %r は無い" % (fixed,), "%s のどれか" % vertical.HW_RANGES)
                self.fixed[ch] = None if fixed is None else tuple(fixed)
            self._apply_ranges()
            return self.choice[ch]

    def set_timebase(self, tdiv):
        with self.lock:
            if tdiv not in timebase.TDIV_STEPS:
                raise DeviceError("Time/div %r は選べない" % tdiv, "%s のどれか" % timebase.TDIV_STEPS)
            self.tdiv = float(tdiv)
            self.fs = self._need().set_sample_rate(timebase.target_fs(tdiv))
            return self.fs

    def set_record(self, deep):
        with self.lock:
            self.deep = bool(deep)

    def set_trigger(self, source="none", edge="rising", level=None, mode="auto"):
        """source: none / ch1 / ch2 / ext。level は表示の単位(その ch のプローブ倍率込み)。"""
        if source not in trigger.SOURCES or edge not in trigger.EDGES or mode not in trigger.MODES:
            raise DeviceError("トリガ設定が不正: source=%r edge=%r mode=%r" % (source, edge, mode),
                              "source は %s、edge は %s、mode は %s" % (trigger.SOURCES, trigger.EDGES, trigger.MODES))
        with self.lock:
            sc = self._need()
            lvl = self.trig["level"] if level is None else float(level)
            try:
                if source == "ch1":
                    sc.set_trigger("ch1", edge, level_v=lvl / self.axis[1].probe, mode=mode)
                elif source == "ext":
                    sc.set_trigger("ext", edge, mode=mode)
                else:
                    sc.set_trigger(None)          # CH2 はソフトウェア。装置はフリーラン
            except ValueError as e:
                raise DeviceError(str(e), "レベルを画面の範囲に入れるか、CH1 の V/div・0 V 位置を見直す")
            self.trig = {"source": source, "edge": edge, "level": lvl, "mode": mode}
            return dict(self.trig)

    # ── 取得 ────────────────────────────────────────────────────────
    def _frame(self, t, kind, status, trig_index=-1, start=0, valid=None):
        volts = {ch: np.asarray(t.to_volts(ch), dtype=float) * self.axis[ch].probe for ch in (1, 2)}
        return Frame(fs=t.rate_hz, volts=volts, trig_status=status, trig_kind=kind, trig_index=trig_index,
                     start=start, first_valid=trigger.FIRST_VALID_HW if (kind == "hw" and status == "triggered") else 0,
                     valid=valid, rail={ch: t.rail_hit(ch) for ch in (1, 2)},
                     clipped={ch: t.clipped(ch) for ch in (1, 2)},
                     ranges={ch: self.choice[ch].as_dict() for ch in (1, 2)},
                     probe={ch: self.axis[ch].probe for ch in (1, 2)},
                     acquired_at=datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"))

    def capture(self, timeout=2.0):
        """1 回取得。Normal でトリガが成立しなければ None(前回の波形を残すのは呼び出し側)。"""
        with self.lock:
            sc = self._need()
            kind = trigger.kind(self.trig["source"])
            deep = self.deep or kind == "sw"
            try:
                t = sc.capture_deep(timeout) if deep else sc.capture(timeout)
            except osc001.TriggerTimeout:
                return None
            except ValueError as e:                  # レンジを変えてレベルが窓の外に出た
                raise DeviceError("トリガレベルを装置の値に直せない: %s" % e,
                                  "レベルを画面の範囲に入れるか、V/div・0 V 位置を見直す")
            if kind == "hw":
                return self._frame(t, kind, "triggered" if t.triggered else "auto_free",
                                   0 if t.triggered else -1)
            if kind == "sw":
                v2 = np.asarray(t.to_volts(2), dtype=float) * self.axis[2].probe
                idx, start = trigger.sw_trigger(v2, self.trig["level"], self.trig["edge"])
                if idx < 0:
                    if self.trig["mode"] == "normal":
                        return None
                    return self._frame(t, kind, "auto_free")
                return self._frame(t, kind, "triggered", idx, start)
            return self._frame(t, None, "free")

    def begin_progressive(self):
        """遅い時間軸の逐次表示を始める(フリーランだけ)。以後 poll_progressive() を繰り返す。"""
        with self.lock:
            if trigger.kind(self.trig["source"]) is not None:
                raise DeviceError("逐次表示はフリーランのときだけ", "トリガを none にする")
            self._need().arm()
            self._prog = time.time()

    def poll_progressive(self):
        """返り値 (Frame, done)。done なら Frame は最終データ。"""
        with self.lock:
            sc = self._need()
            if self._prog is None:
                raise DeviceError("begin_progressive() を先に呼ぶ")
            if time.time() - self._prog >= timebase.acq_time(self.fs) - timebase.XFER_S:
                sc.wait_ready(2.0)
                t = sc.read()
                self._prog = None
                return self._frame(t, None, "free"), True
            t = sc.read_partial()
            return self._frame(t, None, "free", valid=t.valid), False

    def read_voltage(self, avg=10, tap=8):
        """op9 で CH1・CH2 の電圧を avg 回読む(装置の設定に依らない)。平均・最小・最大を表示の単位で返す。"""
        if tap not in (1, 8) or not 1 <= int(avg) <= 1000:
            raise DeviceError("read_voltage の引数が不正", "tap は 8(±24 V)か 1(−1.9〜+3.1 V)、avg は 1〜1000")
        n = int(avg)
        with self.lock:
            sc = self._need()
            top = osc001.MON_X1_SAT if tap == 1 else osc001.MON_MAX
            ms = {1: [], 2: []}
            for _ in range(n):
                a, b = sc.monitor_raw(osc001.MON_SEL[1][tap], osc001.MON_SEL[2][tap])
                ms[1].append(a)
                ms[2].append(b)
            out = {"unit": "V", "tap": tap, "avg": n, "probe": [self.axis[1].probe, self.axis[2].probe],
                   "m": [], "sat": []}
            for ch in (1, 2):
                k = osc001.VCOUNT[tap] * self.axis[ch].probe
                m0 = sc.m0[ch][tap]
                mean = sum(ms[ch]) / float(n)
                out["ch%d" % ch] = (mean - m0) * k
                out["ch%d_min" % ch] = (min(ms[ch]) - m0) * k
                out["ch%d_max" % ch] = (max(ms[ch]) - m0) * k
                out["m"].append(mean)
                out["sat"].append(any(x <= 0 or x >= top for x in ms[ch]))
            return out

    def calibrate_zero(self, channels=(1, 2), ranges=True):
        """0 V 合わせ(入力を GND につないでから)。保存して、レンジの選び直しまで行う。"""
        with self.lock:
            sc = self._need()
            r = sc.calibrate_zero(channels=tuple(channels), ranges=ranges)
            self.zero_info = self.zero_store.save(sc, self.port)
            self.notes = [n for n in self.notes if "0 V 合わせ" not in n]
            self._apply_ranges()
            self.set_trigger(**self.trig)
            return {"m0": {"ch%d_%s" % (ch, "div8" if tap == 8 else "x1"): v for (ch, tap), v in r["m0"].items()},
                    "saved": self.zero_store.path, "calibrated_at": self.zero_info["calibrated_at"]}

    def la_capture(self, rate=979400.0, channels=(), edge="auto", timeout=2.0):
        """LA を 1 回取得して la.Capture を返す。timeout 秒で発火しなければ待機を中止して DeviceError。

        arm から読み出しまでロックを持つ(途中でスコープの arm が入ると待機が解けるため)。
        """
        with self.lock:
            self.la_arm(rate, channels, edge)
            t_end = time.time() + timeout + la.N / self._la_fs
            while time.time() < t_end:
                if self.la_poll(0.1):
                    return self.la_read()
            self.la_abort()
        raise DeviceError("LA のトリガが %.3g 秒で発火しなかった(待機は中止した)" % timeout,
                          "信号・監視 ch・エッジを確かめる。すぐ取るなら edge=auto(監視 ch は空)")

    # 画面から中止できるように、arm・待機・読み出し・中止を分けたもの。待機中は他の操作がロックを取れるよう、
    # la_poll は短い区切りで返す。LA の待機中にスコープを arm(op5)すると待機は解ける。op9 は読んでよい。
    def la_arm(self, rate=979400.0, channels=(), edge="auto"):
        with self.lock:
            sc = self._need()
            self._la_fs = sc.la_set_rate(rate)
            try:
                sc.la_configure(channels=tuple(channels), edge=edge)
            except osc001.Osc001Error as e:
                raise DeviceError(str(e), "監視 ch とエッジを見直す(自動のときは監視 ch を空にする)")
            sc.la_arm()
            self._la_cfg = {"channels": list(channels), "edge": edge, "rate": self._la_fs}
            return self._la_fs

    def la_poll(self, slice_s=0.1):
        with self.lock:
            return self._need().la_wait(slice_s)

    def la_read(self):
        with self.lock:
            t = self._need().la_read()
            return la.Capture.from_trace(t, self._la_fs,
                                         datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"))

    def la_abort(self):
        with self.lock:
            self._need().la_abort()

    def status(self):
        with self.lock:
            return {"connected": self.scope is not None, "path": self.path, "port": self.port,
                    "timebase": {"tdiv": self.tdiv, "fs": self.fs, "deep": self.deep,
                                 "acq_time": timebase.acq_time(self.fs, self.deep) if self.fs else None},
                    "channels": {"ch%d" % ch: {"vdiv": self.axis[ch].vdiv, "pos_div": self.axis[ch].pos_div,
                                               "probe": self.axis[ch].probe,
                                               "fixed": self.fixed[ch],
                                               "range": self.choice[ch].as_dict() if ch in self.choice else None}
                                 for ch in (1, 2)},
                    "trigger": dict(self.trig, kind=trigger.kind(self.trig["source"])),
                    "zero": {"calibrated_at": (self.zero_info or {}).get("calibrated_at"),
                             "port": (self.zero_info or {}).get("port")},
                    "notes": list(self.notes),
                    "warnings": self.scope.warnings() if self.scope else []}
