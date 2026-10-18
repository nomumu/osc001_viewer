"""ロガー画面: 上段 CH1・下段 CH2 のトレンド(平均の線 + 最小〜最大の帯)、アラートのしきい値、マーカー。

点は core.logger.Logger の記録スレッドから on_point で届く。描き直しは 4 回/s にまとめる。
記録はタブを切り替えても続く(スコープの取得とは独立)。
"""

import datetime
import os

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from ..core import logger as core_logger
from ..core import settings
from ..core.device import DeviceError
from .style import C, CH, FONT_BODY, FONT_MONO
from .widgets import KV, Card, note, row

INTERVALS = [(0.1, "0.1 s"), (1.0, "1 s"), (10.0, "10 s"), (60.0, "1 min"), (600.0, "10 min")]
AVGS = [(1, "1 回 · 1 ms"), (10, "10 回 · 10 ms"), (100, "100 回 · 100 ms")]
TAPS = [(8, "±24 V · 47.9 mV 刻み"), (1, "−1.9〜+3.1 V · 5 mV 刻み")]
ENDS = [(None, "手動"), ("10m", "10 分後"), ("1h", "1 時間後"), ("2h", "2 時間後"), ("8h", "8 時間後"), ("24h", "24 時間後")]
REDRAW_MS = 250


class _ClockAxis(pg.DateAxisItem):
    """時刻の目盛り。常に時:分:秒(間隔が 1 秒未満なら 1/10 秒まで、1 日を超える範囲なら日付も)。"""

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            try:
                d = datetime.datetime.fromtimestamp(v)
            except (OverflowError, OSError, ValueError):
                out.append("")
                continue
            if spacing >= 86400:
                out.append(d.strftime("%m/%d"))
            elif spacing >= 1:
                out.append(d.strftime("%H:%M:%S"))
            else:
                out.append(d.strftime("%H:%M:%S.") + str(d.microsecond // 100000))
        return out


def short_path(p):
    home = os.path.expanduser("~")
    if p and (p == home or p.startswith(home + os.sep)):
        return "~" + p[len(home):]
    return p or ""


def open_folder(path):
    """ファイルならそのフォルダ、フォルダならそれを、デスクトップのファイルマネージャで開く。"""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    QDesktopServices.openUrl(QUrl.fromLocalFile(d))


def _combo(items, default):
    c = QComboBox()
    for v, t in items:
        c.addItem(t, v)
    c.setCurrentIndex([v for v, _ in items].index(default))
    return c


def _font(px, mono=False, bold=False):
    f = QFont(FONT_MONO if mono else FONT_BODY)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class LoggerPage(QWidget):
    """主画面側。points は {"t": epoch 秒, ...} の list。"""

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(6)
        self.head = QLabel("")
        self.head.setObjectName("head")
        self.head.setTextFormat(Qt.RichText)
        v.addWidget(self.head)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(C["scr"])
        v.addWidget(self.glw, 1)
        self.plots, self.curve, self.band, self.dot = {}, {}, {}, {}
        for r, ch in enumerate((1, 2)):
            ax = _ClockAxis(orientation="bottom")
            p = self.glw.addPlot(row=r, col=0, axisItems={"bottom": ax})
            p.setMenuEnabled(False)
            p.hideButtons()
            p.showGrid(x=True, y=True, alpha=0.18)
            for name in ("left", "bottom"):
                a = p.getAxis(name)
                a.setPen(pg.mkPen(C["axis"]))
                a.setTextPen(pg.mkPen(CH[ch] if name == "left" else C["muted"]))
                a.setTickFont(_font(11, mono=True))
            p.getAxis("left").setWidth(64)
            p.setLabel("left", "CH%d" % ch, units="V", color=CH[ch])
            p.getViewBox().setMouseEnabled(x=True, y=False)
            lo = pg.PlotCurveItem(pen=pg.mkPen(None))
            hi = pg.PlotCurveItem(pen=pg.mkPen(None))
            col = pg.mkColor(CH[ch])
            col.setAlpha(45)
            self.band[ch] = pg.FillBetweenItem(lo, hi, brush=pg.mkBrush(col))
            self.band[ch].curves = (lo, hi)
            for it in (lo, hi, self.band[ch]):
                p.addItem(it)
            self.curve[ch] = p.plot(pen=pg.mkPen(CH[ch], width=1.6))
            self.dot[ch] = pg.ScatterPlotItem(size=7, brush=pg.mkBrush(CH[ch]), pen=pg.mkPen(None))
            p.addItem(self.dot[ch])
            self.plots[ch] = p
        self.plots[2].setXLink(self.plots[1])
        self.empty = pg.TextItem("", color=C["muted"], anchor=(0.5, 0.5))
        self.empty.setFont(_font(13))
        self.plots[1].addItem(self.empty)
        self._marks, self._alerts = [], []
        self.meas = QHBoxLayout()
        self.meas.setSpacing(8)
        self.mval = {}
        for k in ("ch1", "ch2", "rec"):
            f = QFrame()
            f.setObjectName("card")
            g = QGridLayout(f)
            g.setContentsMargins(10, 6, 10, 6)
            g.setHorizontalSpacing(6)
            title = {"ch1": ("CH1 · 記録全体", CH[1]), "ch2": ("CH2 · 記録全体", CH[2]), "rec": ("記録", C["ext"])}[k]
            t = QLabel(title[0])
            t.setStyleSheet("color:%s;font-weight:600;" % title[1])
            t.setObjectName("kvkey")
            g.addWidget(t, 0, 0, 1, 6)
            keys = ["最新", "最小", "最大"] if k != "rec" else ["点", "マーカー", "アラート"]
            vals = {}
            for i, name in enumerate(keys):
                a = QLabel(name)
                a.setObjectName("kvkey")
                b = QLabel("—")
                b.setObjectName("kvval")
                b.setMinimumWidth(62)
                b.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                g.addWidget(a, 1, i * 2)
                g.addWidget(b, 1, i * 2 + 1)
                vals[name] = b
            self.mval[k] = vals
            self.meas.addWidget(f, 1)
        v.addLayout(self.meas)
        self.show_points([], [], [], None)

    def show_points(self, pts, marks, alerts, state):
        """pts: Logger の点(time を epoch に直したもの)。marks: [(t, text, by)]。alerts: [{"ch","op","value"}]。"""
        has = len(pts) >= 1
        running = bool(state and state.get("running"))
        self.empty.setText("" if has else ("最初の点を取得しています…" if running else
                                           "記録していません。右の「記録を開始」で CH1・CH2 を記録します"))
        if not has:
            for ch in (1, 2):
                self.curve[ch].setData([], [])
                self.dot[ch].setData([])
                for c in self.band[ch].curves:
                    c.setData([], [])
            self.empty.setPos(0.5, 0.5)
            self.plots[1].getViewBox().setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
            self._render_meas(pts, marks, state)
            return
        t = np.array([p["t"] for p in pts])
        for ch in (1, 2):
            avg = np.array([p["ch%d_avg" % ch] for p in pts])
            lo = np.array([p["ch%d_min" % ch] for p in pts])
            hi = np.array([p["ch%d_max" % ch] for p in pts])
            self.curve[ch].setData(t, avg)
            self.band[ch].curves[0].setData(t, lo)
            self.band[ch].curves[1].setData(t, hi)
            self.dot[ch].setData([t[-1]], [avg[-1]])
            ymin, ymax = float(lo.min()), float(hi.max())
            for a in alerts:
                if a["ch"] == ch:
                    ymin, ymax = min(ymin, a["value"]), max(ymax, a["value"])
            pad = max((ymax - ymin) * 0.12, 0.05)
            self.plots[ch].getViewBox().setYRange(ymin - pad, ymax + pad, padding=0)
        span = max(t[-1] - t[0], 1.0)
        self.plots[1].getViewBox().setXRange(t[0], t[0] + span * 1.02, padding=0)
        self._draw_marks(marks)
        self._draw_alerts(alerts)
        self._render_meas(pts, marks, state)

    def _draw_marks(self, marks):
        for it, p in self._marks:
            p.removeItem(it)
        self._marks = []
        for t, text, by in marks[-50:]:
            for ch in (1, 2):
                col = C["ext"] if by not in ("user", "gui") else C["fg"]
                ln = pg.InfiniteLine(pos=t, angle=90, pen=pg.mkPen(col, style=Qt.DashLine),
                                     label=text if ch == 1 else None,
                                     labelOpts={"position": 0.95, "color": col, "anchors": [(0, 0), (0, 0)]})
                ln.setOpacity(0.7)
                self.plots[ch].addItem(ln)
                self._marks.append((ln, self.plots[ch]))

    def _draw_alerts(self, alerts):
        for it, p in self._alerts:
            p.removeItem(it)
        self._alerts = []
        for a in alerts:
            p = self.plots[a["ch"]]
            ln = pg.InfiniteLine(pos=a["value"], angle=0, pen=pg.mkPen("#e8913a", style=Qt.DashLine),
                                 label="CH%d %s %g V" % (a["ch"], a["op"], a["value"]),
                                 labelOpts={"position": 0.06, "color": C["warn"]})
            p.addItem(ln)
            self._alerts.append((ln, p))

    def _render_meas(self, pts, marks, state):
        def fv(x):
            return "—" if x is None else ("%.3g V" % x if abs(x) >= 1 else "%.3g mV" % (x * 1e3))
        for ch in (1, 2):
            vals = self.mval["ch%d" % ch]
            if pts:
                vals["最新"].setText(fv(pts[-1]["ch%d_avg" % ch]))
                vals["最小"].setText(fv(min(p["ch%d_min" % ch] for p in pts)))
                vals["最大"].setText(fv(max(p["ch%d_max" % ch] for p in pts)))
            else:
                for b in vals.values():
                    b.setText("—")
        r = self.mval["rec"]
        r["点"].setText(str(state["points"]) if state and state.get("points") is not None else "—")
        r["マーカー"].setText(str(len(marks)))
        r["アラート"].setText(str(state.get("alert_count", 0)) if state else "—")


class LoggerSide(QWidget):
    """右パネル側。start(dict) / stop() / marker(text) を要求するシグナルを出す。"""
    start_req = Signal(dict)
    stop_req = Signal()
    marker_req = Signal(str)

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(10, 10, 10, 10)
        self.lay.setSpacing(8)
        c = Card("記録")
        self.c_int = _combo(INTERVALS, 10.0)
        self.c_avg = _combo(AVGS, 10)
        self.c_tap = _combo(TAPS, 8)
        self.c_end = _combo(ENDS, None)
        c.add(row("記録間隔", self.c_int))
        c.add(row("1 点の平均", self.c_avg))
        c.add(row("入力", self.c_tap))
        c.add(row("終了", self.c_end))
        dh = QHBoxLayout()
        lb = QLabel("保存先")
        lb.setObjectName("note")
        self.dir_label = QLabel("")
        self.dir_label.setObjectName("kvval")
        self.dir_label.setWordWrap(True)
        self.dir_btn = QPushButton("変更…")
        self.dir_btn.setObjectName("step")
        self.dir_btn.clicked.connect(self._choose_dir)
        open_btn = QPushButton("開く")
        open_btn.setObjectName("step")
        open_btn.clicked.connect(lambda: open_folder(self._dir()))
        dh.addWidget(lb)
        dh.addWidget(self.dir_label, 1)
        dh.addWidget(self.dir_btn)
        dh.addWidget(open_btn)
        c.add(dh)
        self.kv = KV(["状態", "開始", "点数"])
        c.add(self.kv)
        self.file_label = QLabel("")
        self.file_label.setObjectName("kvval")
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.file_label.linkActivated.connect(open_folder)
        c.add(self.file_label)
        h = QHBoxLayout()
        self.btn = QPushButton("記録を開始")
        self.btn.setObjectName("run")
        self.btn.clicked.connect(self._toggle)
        h.addWidget(self.btn)
        h.addStretch(1)
        c.add(h)
        mh = QHBoxLayout()
        self.mark_text = QLineEdit()
        self.mark_text.setPlaceholderText("マーカーの文字(例: 負荷 ON)")
        self.mark_btn = QPushButton("マーカー")
        self.mark_btn.clicked.connect(self._mark)
        self.mark_text.returnPressed.connect(self._mark)
        mh.addWidget(self.mark_text, 1)
        mh.addWidget(self.mark_btn)
        c.add(mh)
        c.add(note("装置の op9 で CH1・CH2 の電圧を直接読みます(1 回 ≒1 ms、レンジ設定は不要)。"
                   "1 点 = 平均回数ぶん読んだ平均・最小・最大。スコープの取得を乱さないので、記録中も他のタブを使えます。"
                   "1 回の記録を 1 つの CSV に書きます(上の保存先。変えた保存先は osc001ctl にも効きます)。"))
        self.lay.addWidget(c)
        c = Card("アラート")
        self.al = []
        for ch, op, val in ((1, "<", 3.6), (2, ">", 1.4)):
            h = QHBoxLayout()
            en = QCheckBox("CH%d" % ch)
            o = QComboBox()
            o.addItems(["<", ">"])
            o.setCurrentText(op)
            sp = QDoubleSpinBox()
            sp.setRange(-240, 240)
            sp.setDecimals(2)
            sp.setSingleStep(0.1)
            sp.setValue(val)
            sp.setSuffix(" V")
            h.addWidget(en)
            h.addWidget(o)
            h.addWidget(sp, 1)
            c.add(h)
            self.al.append((ch, en, o, sp))
        c.add(note("条件に入った点の event 列に記録し、回数を数えます。設定は記録を始めるときに使います。"))
        self.lay.addWidget(c)
        self.settings = [self.c_int, self.c_avg, self.c_tap, self.c_end, self.dir_btn] + \
            [w for a in self.al for w in a[1:]]
        self.running = False
        self.set_state(None)

    def _dir(self):
        return core_logger.default_dir()

    def _choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "ログの保存先", self._dir())
        if d:
            settings.save(log_dir=d)
            self.set_state(self._st)

    def add_card(self, card):
        self.lay.addWidget(card)

    def finish(self):
        self.lay.addStretch(1)

    def alerts(self):
        return ["ch%d%s%g" % (ch, o.currentText(), sp.value()) for ch, en, o, sp in self.al if en.isChecked()]

    def _toggle(self):
        if self.running:
            self.stop_req.emit()
        else:
            self.start_req.emit({"interval": self.c_int.currentData(), "avg": self.c_avg.currentData(),
                                 "tap": self.c_tap.currentData(), "duration": self.c_end.currentData(),
                                 "alerts": self.alerts()})

    def _mark(self):
        t = self.mark_text.text().strip() or "手動マーカー"
        self.marker_req.emit(t)
        self.mark_text.clear()

    def set_state(self, st):
        self._st = st
        self.dir_label.setText(short_path(self._dir()))
        self.dir_label.setToolTip(self._dir())
        self.running = bool(st and st.get("running"))
        self.btn.setText("記録を停止" if self.running else "記録を開始")
        self.btn.setObjectName("stop" if self.running else "run")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        for w in self.settings:
            w.setEnabled(not self.running)
        self.mark_btn.setEnabled(self.running)
        self.mark_text.setEnabled(self.running)
        if not st or not st.get("file"):
            for k in ("状態", "開始", "点数"):
                self.kv.set(k, "—")
            self.kv.set("状態", "停止中")
            self.file_label.setText("")
            return
        self.kv.set("状態", "記録中" if self.running else "停止(%s)" % (st.get("end_reason") or ""))
        self.kv.set("開始", (st.get("started_at") or "—").replace("T", " ")[:19])
        self.kv.set("点数", str(st.get("points", 0)))
        f = st["file"]
        self.file_label.setText("<a href='%s' style='color:%s;'>%s</a>" % (f, C["ch2"], short_path(f)))
        self.file_label.setToolTip("クリックでフォルダを開く: %s" % f)
