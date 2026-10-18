"""ロガー: op9 で CH1・CH2 の電圧を一定間隔で読み、1 回の記録を 1 つの CSV に書く。

1 点 = 平均回数ぶん op9 を読んだ平均・最小・最大(装置の設定に依らず、レンジ選択は要らない)。
op9 はスコープの取得を乱さないので、スコープ・ロジックの画面と同時に記録できる(Device のロックで直列化)。

ファイルの形(1 行 1 点。pandas なら comment="#" で読める):
    # osc001 log v1
    # start: 2026-10-18T10:48:31+09:00
    # ...(装置・入力・間隔・平均・0 V 合わせ・アラート)
    time,elapsed_s,ch1_avg,ch1_min,ch1_max,ch2_avg,ch2_min,ch2_max,event
    2026-10-18T10:48:31.002,0.000,3.951,3.914,3.982,1.003,0.951,1.065,"marker: 記録開始 (user)"
    # end: 2026-10-18T12:48:31+09:00 (2 時間で終了、720 点)
マーカーは付けたその場で 1 点取り、その行の event に書く。アラートは条件に入った点の event に書く。
最後の # end 行は終了時に足すので、異常終了したファイルには無い。
"""

import collections
import csv
import datetime
import os
import re
import threading
import time

from .._api import osc001
from .device import DeviceError

COLUMNS = ["time", "elapsed_s", "ch1_avg", "ch1_min", "ch1_max", "ch2_avg", "ch2_min", "ch2_max", "event"]
AVG_CHOICES = (1, 10, 100)
READ_S_PER_AVG = 0.0011             # op9 1 回 ≒ 1 ms(+ 余裕)
MIN_INTERVAL_S = 0.1
KEEP_POINTS = 200000                # 画面・log get 用にメモリに残す点数
_ALERT_RE = re.compile(r"^\s*ch([12])\s*([<>])\s*(-?[0-9.]+)\s*$")


def default_dir():
    """ログの保存先。環境変数 OSC001_LOG_DIR > 画面で選んだ保存先(settings.json)> ~/osc001_logs。"""
    from . import settings
    return (os.environ.get("OSC001_LOG_DIR") or settings.load().get("log_dir")
            or os.path.join(os.path.expanduser("~"), "osc001_logs"))


def _now():
    return datetime.datetime.now().astimezone()


def parse_alert(text):
    """"ch1<3.6" → {"ch": 1, "op": "<", "value": 3.6}。"""
    m = _ALERT_RE.match(str(text))
    if not m:
        raise DeviceError("アラート条件 %r が読めない" % text, "例: ch1<3.6、ch2>1.4(電圧は V、表示の単位)")
    return {"ch": int(m.group(1)), "op": m.group(2), "value": float(m.group(3))}


def alert_text(a):
    return "ch%d %s %g V" % (a["ch"], a["op"], a["value"])


def parse_duration(text):
    """"90" / "90s" / "10m" / "2h" → 秒。"""
    m = re.match(r"^\s*([0-9.]+)\s*([smh]?)\s*$", str(text))
    if not m:
        raise DeviceError("時間 %r が読めない" % text, "例: 600、10m、2h")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


class Logger(object):
    """on_point(point) / on_state(state) は記録スレッドから呼ばれる(GUI はシグナルに渡す)。"""

    def __init__(self, device, on_point=None, on_state=None):
        self.device = device
        self.on_point = on_point
        self.on_state = on_state
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._marks = []                       # まだ書いていない (text, by)
        self._mark_now = threading.Event()
        self.points = collections.deque(maxlen=KEEP_POINTS)
        self.state = {"running": False, "file": None}

    # ── 操作 ────────────────────────────────────────────────────────
    def start(self, interval=10.0, avg=10, tap=8, duration=None, until=None, max_points=None,
              alerts=(), directory=None, by="user"):
        interval, avg = float(interval), int(avg)
        if self.state["running"]:
            raise DeviceError("記録中", "先に log_stop で止める")
        if avg not in AVG_CHOICES:
            raise DeviceError("平均回数 %r は選べない" % avg, "%s のどれか" % (AVG_CHOICES,))
        if tap not in (1, 8):
            raise DeviceError("tap は 8(±24 V)か 1(−1.9〜+3.1 V)")
        need = max(MIN_INTERVAL_S, avg * READ_S_PER_AVG * 1.2)
        if interval < need:
            raise DeviceError("間隔 %g s は短すぎる" % interval,
                              "平均 %d 回の読み取りに約 %.2g s かかるので、%g s 以上にする" % (avg, avg * READ_S_PER_AVG, need))
        end_at = None
        if duration is not None:
            end_at = time.time() + parse_duration(duration)
        if until is not None:
            try:
                u = datetime.datetime.fromisoformat(str(until))
            except ValueError:
                raise DeviceError("終了時刻 %r が読めない" % until, "例: 2026-10-18T18:00 か 18:00:00")
            if u.tzinfo is None:
                u = u.astimezone()
            end_at = min(end_at, u.timestamp()) if end_at else u.timestamp()
        al = [parse_alert(a) if isinstance(a, str) else dict(a) for a in alerts]
        d = directory or default_dir()
        os.makedirs(d, exist_ok=True)
        t0 = _now()
        base = os.path.join(d, t0.strftime("log_%Y%m%d_%H%M%S"))
        path, k = base + ".csv", 2
        while os.path.exists(path):                       # 同じ秒に始めた記録を上書きしない
            path, k = "%s_%d.csv" % (base, k), k + 1
        st = self.device.status()
        sc = self.device.scope
        zero = st["zero"]
        head = ["# osc001 log v1",
                "# start: %s" % t0.isoformat(timespec="seconds"),
                "# device: DPScope SE (%s, USB %s)" % (st["path"], st["port"]),
                "# source: op9 %s (%.3g mV/count), interval %g s, average %d"
                % ("±24 V" if tap == 8 else "−1.9〜+3.1 V", osc001.VCOUNT[tap] * 1e3, interval, avg),
                "# probe: ch1 ×%g / ch2 ×%g(電圧はプローブ倍率を掛けた値)" % (self.device.axis[1].probe,
                                                                     self.device.axis[2].probe),
                "# zero_m0: ch1 %.1f / ch2 %.1f (%s)" % (sc.m0[1][tap], sc.m0[2][tap],
                                                         "calibrated " + zero["calibrated_at"]
                                                         if zero["calibrated_at"] else "既定値・0 V 合わせ未実施"),
                "# alerts: %s" % (", ".join(alert_text(a) for a in al) or "なし")]
        if end_at:
            head.append("# planned_end: %s" % datetime.datetime.fromtimestamp(end_at).astimezone()
                        .isoformat(timespec="seconds"))
        f = open(path, "w", encoding="utf-8", newline="")
        f.write("\n".join(head) + "\n")
        f.write(",".join(COLUMNS) + "\n")
        f.flush()
        with self._lock:
            self._f, self._w = f, csv.writer(f, lineterminator="\n")
            self._marks = [("記録開始", by)]
        self.points.clear()
        self._stop.clear()
        self.state = {"running": True, "file": path, "started_at": t0.isoformat(timespec="seconds"),
                      "interval": interval, "avg": avg, "tap": tap, "alerts": al, "alert_count": 0,
                      "end_at": end_at, "max_points": max_points, "points": 0, "errors": 0, "saturated": 0,
                      "last": None, "end_reason": None}
        self._alert_on = [False] * len(al)
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, name="osc001-logger", daemon=True)
        self._thread.start()
        self._notify()
        return self.status()

    def stop(self, reason="手動で停止"):
        if not self.state["running"]:
            raise DeviceError("記録していない")
        self._end_reason = reason
        self._stop.set()
        self._mark_now.set()
        self._thread.join(5.0)
        return self.status()

    def marker(self, text, by="user"):
        if not self.state["running"]:
            raise DeviceError("記録していないのでマーカーを付けられない", "log_start で記録を始める")
        text = str(text).replace("\n", " ").strip()[:200]
        if not text:
            raise DeviceError("マーカーの文字が空")
        with self._lock:
            self._marks.append((text, by))
        self._mark_now.set()                    # その場で 1 点取る
        return {"marker": text, "by": by}

    def status(self):
        s = dict(self.state)
        s["alerts"] = [alert_text(a) for a in s.get("alerts", [])]
        if s.get("end_at"):
            s["end_at"] = datetime.datetime.fromtimestamp(s["end_at"]).astimezone().isoformat(timespec="seconds")
        return s

    def get(self, last=None, max_points=200):
        """記録中・直前の記録の点を返す。last は "10m" などの期間、max_points を超えたら区間ごとにまとめる
        (平均は平均、最小は最小、最大は最大。event はつなげる)。"""
        pts = list(self.points)
        if last is not None and pts:
            t_end = pts[-1]["elapsed_s"]
            pts = [p for p in pts if p["elapsed_s"] >= t_end - parse_duration(last)]
        n = len(pts)
        max_points = max(1, int(max_points))
        if n > max_points:
            out, step = [], n / float(max_points)
            for k in range(max_points):
                seg = pts[int(k * step):max(int((k + 1) * step), int(k * step) + 1)]
                q = {"time": seg[0]["time"], "elapsed_s": seg[0]["elapsed_s"], "t": seg[0]["t"]}
                for ch in (1, 2):
                    q["ch%d_avg" % ch] = sum(p["ch%d_avg" % ch] for p in seg) / len(seg)
                    q["ch%d_min" % ch] = min(p["ch%d_min" % ch] for p in seg)
                    q["ch%d_max" % ch] = max(p["ch%d_max" % ch] for p in seg)
                q["event"] = " | ".join(p["event"] for p in seg if p["event"])
                out.append(q)
            pts = out
        return {"file": self.state.get("file"), "running": self.state.get("running"), "points_total": n,
                "points": len(pts), "columns": COLUMNS, "rows": pts}

    @staticmethod
    def files(directory=None, limit=20):
        d = directory or default_dir()
        try:
            names = sorted((x for x in os.listdir(d) if x.startswith("log_") and x.endswith(".csv")),
                           key=lambda x: os.path.getmtime(os.path.join(d, x)), reverse=True)
        except OSError:
            names = []
        out = []
        for nm in names[:int(limit)]:
            p = os.path.join(d, nm)
            start = end = None
            n = 0
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("# start:"):
                            start = line.split(":", 1)[1].strip()
                        elif line.startswith("# end:"):
                            end = line.split(":", 1)[1].strip()
                        elif line and line[0].isdigit():
                            n += 1
            except OSError:
                continue
            out.append({"file": p, "bytes": os.path.getsize(p), "start": start, "end": end, "points": n,
                        "complete": end is not None})
        return {"directory": d, "files": out}

    # ── 記録スレッド ────────────────────────────────────────────────
    def _notify(self):
        if self.on_state:
            self.on_state(self.status())

    def _sample(self, events):
        st = self.state
        try:
            r = self.device.read_voltage(avg=st["avg"], tap=st["tap"])
        except (DeviceError, osc001.Osc001Error) as e:
            st["errors"] += 1
            st["last_error"] = str(e)
            return None
        now = _now()
        p = {"time": now.isoformat(timespec="milliseconds")[:23], "elapsed_s": round(time.time() - self._t0, 3),
             "t": now.timestamp()}                    # t(UNIX 時刻)は CSV には書かない。グラフ・log_get 用
        for ch in (1, 2):
            p["ch%d_avg" % ch] = round(r["ch%d" % ch], 5)
            p["ch%d_min" % ch] = round(r["ch%d_min" % ch], 5)
            p["ch%d_max" % ch] = round(r["ch%d_max" % ch], 5)
        if any(r["sat"]):
            st["saturated"] += 1
            events.append("saturated: %s" % ",".join("ch%d" % (i + 1) for i, s in enumerate(r["sat"]) if s))
        for i, a in enumerate(st["alerts"]):
            v = p["ch%d_avg" % a["ch"]]
            on = v < a["value"] if a["op"] == "<" else v > a["value"]
            if on and not self._alert_on[i]:
                events.append("alert: " + alert_text(a))
                st["alert_count"] += 1
            self._alert_on[i] = on
        p["event"] = " | ".join(events)
        with self._lock:
            self._w.writerow([p[c] for c in COLUMNS])
            self._f.flush()
        self.points.append(p)
        st["points"] += 1
        st["last"] = p
        if self.on_point:
            self.on_point(p)
        return p

    def _take_marks(self):
        with self._lock:
            m, self._marks = self._marks, []
        return ["marker: %s (%s)" % (t, by) for t, by in m]

    def _run(self):
        st = self.state
        self._end_reason = None
        k = 0
        while True:
            self._sample(self._take_marks())
            k += 1
            if st["max_points"] and st["points"] >= int(st["max_points"]):
                self._end_reason = "%d 点で終了" % st["points"]
                break
            nxt = self._t0 + k * st["interval"]
            while True:
                if self._stop.is_set():
                    break
                if st["end_at"] and time.time() >= st["end_at"]:
                    self._end_reason = "予定の時刻で終了"
                    break
                wait = min(nxt - time.time(), 0.2)
                if wait <= 0:
                    break
                if self._mark_now.wait(wait):
                    self._mark_now.clear()
                    if self._stop.is_set():
                        break
                    marks = self._take_marks()
                    if marks:
                        self._sample(marks)          # マーカーはその場で 1 点取る
            if self._stop.is_set() or self._end_reason:
                break
            if time.time() > nxt + st["interval"]:  # 取り遅れたら次の枠へ飛ばす(間隔を詰めて取り戻さない)
                k = int((time.time() - self._t0) / st["interval"])
        reason = self._end_reason or "手動で停止"
        with self._lock:
            self._f.write("# end: %s (%s、%d 点)\n" % (_now().isoformat(timespec="seconds"), reason, st["points"]))
            self._f.close()
        st["running"] = False
        st["end_reason"] = reason
        self._notify()
