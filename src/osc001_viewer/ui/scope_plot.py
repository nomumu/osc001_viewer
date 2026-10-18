"""スコープの波形表示(pyqtgraph)。座標は div 単位(横 0〜10、縦 −4〜+4)で、電圧・時刻への換算は目盛りが行う。

- 縦の目盛り: 左が CH1、右が CH2(それぞれの V/div・0 V 位置・プローブで)。下が時刻(トリガ点が 0)
- 左端の ◀1 ◀2 は 0 V 位置、右端の ◀T はトリガレベル。ドラッグで動かす
- 選択中 ch の測定範囲外は斜線、各 ch の範囲外は軸側の縁に色帯。入力上限 ±24 V は破線
- ハードトリガのサンプル 0 は描かない(Frame.first_valid)。逐次表示中は書き込み済みの点まで
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPen

from ..core import trigger as trig
from ..core import timebase, vertical
from .style import C, CH, FONT_BODY, FONT_MONO

NX, NY = 10, 8
N_SCREEN = timebase.SAMPLES          # 192 点 = 10 div


def fmt_v(v):
    a = abs(v)
    if a < 1e-9:
        return "0"
    if a >= 1:
        return ("%.2f" % v).rstrip("0").rstrip(".") + "V"
    return ("%.0f" % (v * 1e3)) + "mV"


def fmt_t(s):
    a = abs(s)
    if a < 1e-12:
        return "0"
    if a >= 1:
        return ("%.3g" % s) + " s"
    if a >= 1e-3:
        return ("%.3g" % (s * 1e3)) + " ms"
    return ("%.3g" % (s * 1e6)) + " µs"


class _Axis(pg.AxisItem):
    """div 単位の位置に、外から与えた関数で文字を付ける。"""

    def __init__(self, orientation, ticks, fmt, color):
        pg.AxisItem.__init__(self, orientation)
        self._ticks, self._fmt = ticks, fmt
        self.setPen(pg.mkPen(C["axis"]))
        self.setTextPen(pg.mkPen(color))
        self.setStyle(tickLength=0, tickTextOffset=6)
        f = QFont(FONT_MONO)
        f.setPixelSize(11)
        self.setTickFont(f)

    def tickValues(self, minVal, maxVal, size):
        return [(1.0, self._ticks)]

    def tickStrings(self, values, scale, spacing):
        return [self._fmt(v) for v in values]


class ScopePlot(pg.PlotWidget):
    wheel = Signal(int, bool)                    # +1 / −1、Shift 付きか
    zero_dragged = Signal(int, float)            # ch, 新しい 0 V 位置(div)
    level_dragged = Signal(float)                # 新しいトリガレベル(表示の単位)
    cursors_moved = Signal(float, float)         # A・B の位置(div)

    def __init__(self, parent=None):
        self.st = {"axis": {1: vertical.ScreenAxis(1, -2), 2: vertical.ScreenAxis(1, -2)},
                   "on": {1: True, 2: True}, "range": {}, "active": 1,
                   "trig": {"source": "none", "level": 0.0}, "tdiv": 1e-3}
        self._frame = None
        left = _Axis("left", list(range(-4, 5)), lambda y: self._vlabel(1, y), C["ch1"])
        right = _Axis("right", list(range(-4, 5)), lambda y: self._vlabel(2, y), C["ch2"])
        bottom = _Axis("bottom", list(range(0, 11, 2)), self._tlabel, C["muted"])
        pg.PlotWidget.__init__(self, parent, background=C["scr"],
                               axisItems={"left": left, "right": right, "bottom": bottom})
        pi = self.getPlotItem()
        pi.showAxis("right")
        pi.hideButtons()
        pi.setMenuEnabled(False)
        vb = pi.getViewBox()
        vb.setMouseEnabled(False, False)
        vb.setRange(xRange=(0, NX), yRange=(-NY / 2, NY / 2), padding=0)
        vb.setLimits(xMin=0, xMax=NX, yMin=-NY / 2, yMax=NY / 2)
        for ax in ("left", "right"):
            pi.getAxis(ax).setWidth(56)
        self._grid()
        self.hatch = pg.LinearRegionItem(orientation="horizontal", movable=False,
                                         brush=QBrush(QColor(229, 120, 80, 70), Qt.BDiagPattern),
                                         pen=pg.mkPen(None))
        self.hatch2 = pg.LinearRegionItem(orientation="horizontal", movable=False,
                                          brush=QBrush(QColor(229, 120, 80, 70), Qt.BDiagPattern),
                                          pen=pg.mkPen(None))
        for h in (self.hatch, self.hatch2):
            h.setZValue(-5)
            for ln in h.lines:
                ln.setPen(pg.mkPen(None))
            self.addItem(h)
        self.edge = {}
        for ch in (1, 2):
            items = []
            for _ in range(2):
                r = pg.QtWidgets.QGraphicsRectItem()
                c = QColor(CH[ch])
                c.setAlpha(140)
                r.setBrush(QBrush(c))
                r.setPen(QPen(Qt.NoPen))
                r.setZValue(5)
                self.addItem(r)
                items.append(r)
            self.edge[ch] = items
        self.limit = {}
        for ch in (1, 2):
            pen = pg.mkPen(CH[ch], width=1, style=Qt.DotLine)
            lines = [pg.InfiniteLine(angle=0, pen=pen, movable=False,
                                     label="CH%d 入力上限 %s24 V" % (ch, s),
                                     labelOpts={"position": 0.12 if ch == 1 else 0.88, "color": CH[ch],
                                                "anchors": [(0.5, 1), (0.5, 1)] if s == "+" else [(0.5, 0), (0.5, 0)]})
                     for s in ("+", "−")]
            for ln in lines:
                self.addItem(ln)
            self.limit[ch] = lines
        self.level_line = pg.InfiniteLine(angle=0, pen=pg.mkPen(C["fg"], width=1, style=Qt.DashLine), movable=False)
        self.level_line.setOpacity(0.45)
        self.addItem(self.level_line)
        self.end_shade = pg.LinearRegionItem(orientation="vertical", movable=False,
                                             brush=QBrush(QColor(255, 255, 255, 10)), pen=pg.mkPen(None))
        self.addItem(self.end_shade)
        self.trace = {ch: pg.PlotDataItem(pen=pg.mkPen(CH[ch], width=1.5)) for ch in (1, 2)}
        for t in self.trace.values():
            self.addItem(t)
        self.prog_line = pg.InfiniteLine(angle=90, pen=pg.mkPen(C["fg"], style=Qt.DashLine), movable=False)
        self.prog_line.setOpacity(0.5)
        self.addItem(self.prog_line)
        self.trig_mark = pg.ScatterPlotItem(symbol="t", size=12, brush=pg.mkBrush(C["fg"]), pen=pg.mkPen(None))
        self.addItem(self.trig_mark)
        self.text_trig = self._text(C["muted"], anchor=(0, 0))
        self.text_prog = self._text(C["muted"], anchor=(0, 1))
        self.text_wait = self._text(C["warn"], anchor=(0.5, 0), bold=True)
        self.text_wait.setPos(NX / 2, NY / 2 - 0.1)
        self.text_end = self._text(C["muted"], anchor=(0, 1))
        self.zero = {}
        for ch in (1, 2):
            m = pg.TargetItem(pos=(0, 0), size=13, symbol="t2", pen=pg.mkPen(None), brush=pg.mkBrush(CH[ch]),
                              movable=True, label=str(ch), labelOpts={"color": C["scr"], "offset": (-3, 0)})
            m.setZValue(20)
            m.sigPositionChanged.connect(lambda item, ch=ch: self._zero_moved(ch, item))
            self.addItem(m)
            self.zero[ch] = m
        self.tmark = pg.TargetItem(pos=(NX, 0), size=13, symbol="t3", pen=pg.mkPen(None), brush=pg.mkBrush(C["fg"]),
                                   movable=True, label="T", labelOpts={"color": C["scr"], "offset": (3, 0)})
        self.tmark.setZValue(20)
        self.tmark.sigPositionChanged.connect(self._level_moved)
        self.addItem(self.tmark)
        self.cur = [pg.InfiniteLine(pos=p, angle=90, movable=True, pen=pg.mkPen(C["cur"], style=Qt.DashLine),
                                    label=n, labelOpts={"position": 0.97, "color": C["cur"]})
                    for p, n in ((3.0, "A"), (5.5, "B"))]
        for c in self.cur:
            c.setBounds((0, NX))
            c.sigPositionChanged.connect(lambda *_: self.cursors_moved.emit(self.cur[0].value(), self.cur[1].value()))
            c.setVisible(False)
            self.addItem(c)
        self._guard = False
        self.refresh()

    # ── 部品 ────────────────────────────────────────────────────────
    def _text(self, color, anchor, bold=False):
        t = pg.TextItem("", color=color, anchor=anchor)
        f = QFont(FONT_BODY)
        f.setPixelSize(11)
        f.setBold(bold)
        t.setFont(f)
        t.setZValue(30)
        self.addItem(t)
        return t

    def _grid(self):
        xs, ys = [], []
        for i in range(NX + 1):
            xs += [i, i]
            ys += [-NY / 2, NY / 2]
        for j in range(NY + 1):
            xs += [0, NX]
            ys += [j - NY / 2, j - NY / 2]
        g = pg.PlotCurveItem(np.array(xs, float), np.array(ys, float), connect="pairs", pen=pg.mkPen(C["grid"]))
        g.setZValue(-10)
        self.addItem(g)
        xs, ys = [NX / 2, NX / 2, 0, NX], [-NY / 2, NY / 2, 0, 0]
        for k in range(NX * 5 + 1):
            xs += [k / 5.0, k / 5.0]
            ys += [-0.05, 0.05]
        for k in range(NY * 5 + 1):
            xs += [NX / 2 - 0.05, NX / 2 + 0.05]
            ys += [k / 5.0 - NY / 2] * 2
        a = pg.PlotCurveItem(np.array(xs, float), np.array(ys, float), connect="pairs", pen=pg.mkPen(C["axis"]))
        a.setZValue(-9)
        self.addItem(a)

    # ── 換算 ────────────────────────────────────────────────────────
    def _ax(self, ch):
        return self.st["axis"][ch]

    def y_of(self, ch, v):
        a = self._ax(ch)
        return v / a.vdiv + a.pos_div

    def v_of(self, ch, y):
        a = self._ax(ch)
        return (y - a.pos_div) * a.vdiv

    def _vlabel(self, ch, y):
        return fmt_v(self.v_of(ch, y)) if self.st["on"][ch] else ""

    def x_origin(self):
        """x = 0 に来るサンプルの index(実数)。ソフトトリガはトリガ点がちょうど 1 div に来るようにする。"""
        f = self._frame
        if f is None:
            return 0.0
        if f.trig_kind == "sw" and f.trig_index >= 0:
            return f.trig_index - N_SCREEN / float(NX)
        return float(f.start)

    def _tlabel(self, x):
        f = self._frame
        fs = timebase.target_fs(self.st["tdiv"])      # 目盛りは公称の Time/div で付ける(実際の fs は情報行)
        t0 = f.trig_index if (f is not None and f.trig_index >= 0) else 0
        t = (self.x_origin() + x * N_SCREEN / NX - t0) / fs
        return ("+" if t > 0 and f is not None and f.trig_index >= 0 else "") + fmt_t(t)

    # ── 外から ──────────────────────────────────────────────────────
    def set_state(self, **kw):
        self.st.update(kw)
        self.refresh()

    def set_frame(self, f):
        self._frame = f
        self.refresh()

    def set_waiting(self, on):
        self.text_wait.setText("トリガ待ち(前回の波形を表示中)" if on else "")

    def set_cursors(self, on):
        for c in self.cur:
            c.setVisible(on)

    def wheelEvent(self, ev):
        d = 1 if ev.angleDelta().y() < 0 else -1
        self.wheel.emit(d, bool(ev.modifiers() & Qt.ShiftModifier))
        ev.accept()

    # ── ドラッグ ────────────────────────────────────────────────────
    def _zero_moved(self, ch, item):
        if self._guard:
            return
        y = max(-NY / 2, min(NY / 2, item.pos().y()))
        y = round(y * 10) / 10.0
        self._guard = True
        item.setPos(0, y)
        self._guard = False
        self.zero_dragged.emit(ch, y)

    def _level_moved(self, item):
        if self._guard:
            return
        src = self.st["trig"]["source"]
        if src not in ("ch1", "ch2"):
            return
        ch = int(src[-1])
        y = max(-NY / 2, min(NY / 2, item.pos().y()))
        self._guard = True
        item.setPos(NX, y)
        self._guard = False
        self.level_dragged.emit(self.v_of(ch, y))

    # ── 描画 ────────────────────────────────────────────────────────
    def _redraw_axes(self):
        pi = self.getPlotItem()
        for name in ("left", "right", "bottom"):
            ax = pi.getAxis(name)
            ax.picture = None                      # 目盛りの文字は state から作るので、毎回描き直させる
            ax.update()

    def refresh(self):
        st, f = self.st, self._frame
        self._redraw_axes()
        self._guard = True
        for ch in (1, 2):
            on = st["on"][ch]
            y0 = max(-NY / 2 + 0.15, min(NY / 2 - 0.15, self._ax(ch).pos_div))
            self.zero[ch].setVisible(on)
            self.zero[ch].setPos(0, y0)
            for i, v in enumerate((24.0, -24.0)):
                y = self.y_of(ch, v * self._ax(ch).probe)
                self.limit[ch][i].setVisible(on and -NY / 2 < y < NY / 2)
                self.limit[ch][i].setPos(y)
            r = st["range"].get(ch)
            bars = self.edge[ch]
            x = 0.0 if ch == 1 else NX - 0.07
            for b in bars:
                b.setVisible(False)
            if on and r:
                p = self._ax(ch).probe
                lo, hi = self.y_of(ch, r["window"][0] * p), self.y_of(ch, r["window"][1] * p)
                if lo > -NY / 2:
                    bars[0].setRect(x, -NY / 2, 0.07, min(lo, NY / 2) + NY / 2)
                    bars[0].setVisible(True)
                if hi < NY / 2:
                    bars[1].setRect(x, max(hi, -NY / 2), 0.07, NY / 2 - max(hi, -NY / 2))
                    bars[1].setVisible(True)
        # 選択中 ch の測定範囲外を斜線で
        a, r = st["active"], st["range"].get(st["active"])
        self.hatch.setVisible(False)
        self.hatch2.setVisible(False)
        if r and st["on"][a]:
            p = self._ax(a).probe
            lo, hi = self.y_of(a, r["window"][0] * p), self.y_of(a, r["window"][1] * p)
            if lo > -NY / 2:
                self.hatch.setRegion((-NY / 2, min(lo, NY / 2)))
                self.hatch.setVisible(True)
            if hi < NY / 2:
                self.hatch2.setRegion((max(hi, -NY / 2), NY / 2))
                self.hatch2.setVisible(True)
        # トリガレベル
        src = st["trig"]["source"]
        lvl_ch = int(src[-1]) if src in ("ch1", "ch2") else 0
        if lvl_ch and st["on"][lvl_ch]:
            y = self.y_of(lvl_ch, st["trig"]["level"])
            self.level_line.setPen(pg.mkPen(CH[lvl_ch], width=1, style=Qt.DashLine))
            self.level_line.setPos(y)
            self.level_line.setVisible(True)
            self.tmark.setPos(NX, max(-NY / 2 + 0.15, min(NY / 2 - 0.15, y)))
            self.tmark.setVisible(True)
        else:
            self.level_line.setVisible(False)
            self.tmark.setVisible(False)
        self._guard = False
        # 波形
        self.trig_mark.setData([])
        self.text_trig.setText("")
        self.prog_line.setVisible(False)
        self.text_prog.setText("")
        self.end_shade.setVisible(False)
        self.text_end.setText("")
        if f is None:
            for t in self.trace.values():
                t.setData([], [])
            return
        start, n = self.x_origin(), f.n
        end = n if f.valid is None else f.valid
        i = np.arange(max(f.first_valid, int(np.ceil(start))), end)
        xs = (i - start) * NX / float(N_SCREEN)
        for ch in (1, 2):
            t = self.trace[ch]
            t.setVisible(st["on"][ch])
            t.setPen(pg.mkPen(CH[ch], width=1.9 if ch == st["active"] else 1.4))
            t.setData(xs, self.y_of(ch, f.volts[ch][i]) if len(i) else [])
        if f.trig_index >= 0:
            x = (f.trig_index - start) * NX / float(N_SCREEN)
            self.trig_mark.setData([max(0.12, x)], [NY / 2 - 0.12])
            if f.trig_kind == "hw":
                self.text_trig.setText("トリガ点(プリトリガなし)")
                self.text_trig.setPos(max(0.12, x) + 0.15, NY / 2)
        if f.valid is not None and f.valid < n:
            xv = (f.valid - start) * NX / float(N_SCREEN)
            self.prog_line.setPos(xv)
            self.prog_line.setVisible(True)
            self.text_prog.setText("取得中 %d/%d 点" % (f.valid, n))
            self.text_prog.setPos(min(xv + 0.1, NX - 1.6), -NY / 2 + 0.1)
        xe = (n - start) * NX / float(N_SCREEN)
        if xe < NX:
            self.end_shade.setRegion((xe, NX))
            self.end_shade.setVisible(True)
            self.text_end.setText("バッファ終端")
            self.text_end.setPos(min(xe + 0.1, NX - 1.3), -NY / 2 + 0.1)
