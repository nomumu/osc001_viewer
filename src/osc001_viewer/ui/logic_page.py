"""ロジック画面: LA の 4 ch(D1〜D4)と、UART・I2C・SPI・CAN のデコード結果(帯と一覧)。

LA トリガ(エッジと監視 ch)は利用者が手動で決める(反転した信号もあり得るので、デコーダから自動では決めない)。
デコーダはその設定での「通信の始まり」を参考に出し、食い違えば確かめるよう促すだけ。
取得は LaWorker スレッドで行い、待機は中止できる。ロジック画面にいる間はスコープの取得を止める
(スコープの arm は LA の待機を解くため)。ロガーの op9 は待機中に読んでも安全(実測)。
"""

import threading
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import la
from ..core.decode import GOOD_SPB, MIN_SPB, decode
from ..core.device import DeviceError
from .scope_plot import fmt_t
from .style import C, FONT_BODY, FONT_MONO
from .widgets import KV, Card, Seg, note, row

LANE_COL = {1: C["ch1"], 2: C["ch2"], 3: C["la3"], 4: C["la4"]}
KIND_COL = {"data": "#6aa8e6", "addr": "#c7a8ff", "ack": "#5fd394", "nack": "#ff8d84", "ctl": "#f2b06a",
            "stuff": "#7f8fa0", "err": "#ff5f56"}
ROW_H = 0.7
RATE_LABEL = {979400.0: "979.4 kHz", 490900.0: "490.9 kHz", 200000.0: "200 kHz", 100000.0: "100 kHz",
              20000.0: "20 kHz", 2000.0: "2 kHz", 100.0: "100 Hz", 10.0: "10 Hz"}
DEFAULTS = {
    "uart": {"rx": 2, "tx": 3, "baud": 115200, "format": "8N1", "invert": False},
    "i2c": {"scl": 1, "sda": 2, "clock": 100000},
    "spi": {"sck": 1, "mosi": 2, "miso": 3, "cs": 4, "cs_active_high": False, "mode": 0, "clock": 100000},
    "can": {"rx": 2, "bitrate": 125000, "invert": False},
}


def _font(px, mono=False, bold=False):
    f = QFont(FONT_MONO if mono else FONT_BODY)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def bitrate_of(proto, o):
    return {"uart": o.get("baud"), "i2c": o.get("clock"), "spi": o.get("clock"), "can": o.get("bitrate")}.get(proto)


def start_of(proto, o):
    """この設定での通信の始まり: (ch の list, エッジ, 説明)。"""
    if proto == "uart":
        chs = [c for c in (o.get("rx"), o.get("tx")) if c]
        return chs, "rising" if o.get("invert") else "falling", "スタートビット"
    if proto == "i2c":
        return [o.get("sda")], "falling", "START"
    if proto == "spi":
        return ([o["cs"]] if o.get("cs") else []), "rising" if o.get("cs_active_high") else "falling", "CS"
    if proto == "can":
        return [o.get("rx")], "rising" if o.get("invert") else "falling", "SOF"
    return [], None, ""


class LaWorker(QThread):
    """arm → 待機(中止できる)→ 読み出し。"""
    captured = Signal(object)
    failed = Signal(str, str)
    state = Signal(str)                 # "armed" / "idle"

    def __init__(self, device):
        QThread.__init__(self)
        self.device = device
        self._abort = threading.Event()
        self.cfg = None

    def arm(self, rate, channels, edge):
        if self.isRunning():
            return
        self.cfg = (rate, list(channels), edge)
        self._abort.clear()
        self.start()

    def abort(self):
        self._abort.set()

    def run(self):
        rate, chs, edge = self.cfg
        try:
            self.device.la_arm(rate, chs, edge)
            self.state.emit("armed")
            while not self._abort.is_set():
                if self.device.la_poll(0.1):
                    self.captured.emit(self.device.la_read())
                    break
            else:
                self.device.la_abort()
        except DeviceError as e:
            self.failed.emit(str(e), e.hint or "")
        except Exception as e:                                    # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, e), "")
        self.state.emit("idle")


class _TimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [("+" if v > 0 else "") + fmt_t(v) for v in values]


class _LaneAxis(pg.AxisItem):
    def __init__(self):
        pg.AxisItem.__init__(self, "left")
        self.lanes = []                                           # [(y, text, color)]
        self.setStyle(tickLength=0)

    def tickValues(self, minVal, maxVal, size):
        return [(1.0, [y for y, _, _ in self.lanes])]

    def tickStrings(self, values, scale, spacing):
        m = {round(y, 3): t for y, t, _ in self.lanes}
        return [m.get(round(v, 3), "") for v in values]


class LogicPage(QWidget):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(6)
        self.head = QLabel("")
        self.head.setObjectName("head")
        self.head.setTextFormat(Qt.RichText)
        v.addWidget(self.head)
        self.lane_axis = _LaneAxis()
        self.lane_axis.setTickFont(_font(11, mono=True, bold=True))
        self.lane_axis.setWidth(64)
        self.lane_axis.setTextPen(pg.mkPen(C["fg"]))
        tax = _TimeAxis("bottom")
        tax.setTickFont(_font(11, mono=True))
        tax.setTextPen(pg.mkPen(C["muted"]))
        self.plot = pg.PlotWidget(background=C["scr"], axisItems={"left": self.lane_axis, "bottom": tax})
        pi = self.plot.getPlotItem()
        pi.hideButtons()
        pi.setMenuEnabled(False)
        pi.showGrid(x=True, y=False, alpha=0.15)
        pi.getViewBox().setMouseEnabled(x=True, y=False)
        v.addWidget(self.plot, 3)
        self.msg = pg.TextItem("", color=C["muted"], anchor=(0.5, 0.5))
        self.msg.setFont(_font(13))
        self.plot.addItem(self.msg)
        self.trig_text = pg.TextItem("", color=C["muted"], anchor=(0, 0))
        self.trig_text.setFont(_font(11))
        self.plot.addItem(self.trig_text)
        self._items = []
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["時刻(トリガ点から)", "内容"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setColumnWidth(0, 150)
        v.addWidget(self.table, 1)
        self.show_capture(None, None, None, "idle")

    def _clear(self):
        for it in self._items:
            self.plot.removeItem(it)
        self._items = []

    def show_capture(self, cap, result, roles, state):
        """cap: la.Capture か None、result: decode.Result か None、roles: {ch: "RX" ...}。"""
        self._clear()
        roles = roles or {}
        nrows = len(result.rows) if result is not None else 0
        base = nrows * ROW_H
        lanes = []
        for k, ch in enumerate((1, 2, 3, 4)):
            y = base + (3 - k) + 0.5
            lanes.append((y, "D%d%s" % (ch, (" " + roles[ch]) if ch in roles else ""), LANE_COL[ch]))
        if result is not None:
            for r, name in enumerate(result.rows):                 # 帯の行の名前も左の目盛りに出す
                lanes.append((base - (r + 0.5) * ROW_H, "[%s]" % name, C["muted"]))
        self.lane_axis.lanes = lanes
        self.lane_axis.picture = None
        self.lane_axis.update()
        vb = self.plot.getPlotItem().getViewBox()
        top = base + 4
        if cap is None:
            self.msg.setText("LA の待機中です(トリガを待っています)…" if state == "armed" else
                             "右の「Arm」を押すと取得します")
            self.msg.setPos(0.5, top / 2)
            self.trig_text.setText("")
            vb.setRange(xRange=(0, 1), yRange=(0, top), padding=0)
            self.table.setRowCount(0)
            return
        self.msg.setText("")
        t = cap.lat + np.arange(cap.n) / cap.fs
        tt = np.repeat(t, 2)[1:]
        for k, ch in enumerate((1, 2, 3, 4)):
            b = cap.bits[ch].astype(float)
            y0 = base + (3 - k) + 0.2
            yy = np.repeat(y0 + 0.6 * b, 2)[:-1]
            it = pg.PlotCurveItem(tt, yy, pen=pg.mkPen(LANE_COL[ch], width=1.4))
            self.plot.addItem(it)
            self._items.append(it)
            sep = pg.InfiniteLine(pos=base + (3 - k), angle=0, pen=pg.mkPen(C["grid"]))
            self.plot.addItem(sep)
            self._items.append(sep)
        x0, x1 = cap.lat, cap.lat + cap.n / cap.fs
        vb.setRange(xRange=(x0, x1), yRange=(0, top + 0.35), padding=0)
        self.trig_text.setText("記録開始 = トリガ点 +%s(トリガのエッジ自体は記録の前)" % fmt_t(cap.lat))
        self.trig_text.setPos(x0, top + 0.35)
        if result is not None:
            self._draw_ann(result, base, x0, x1)
        self._fill_table(result)

    def _draw_ann(self, result, base, x0, x1):
        width_px = max(200, self.plot.width() - 80)
        px_per_s = width_px / max(x1 - x0, 1e-12)
        for a in result.ann:
            t0, t1 = max(a["t0"], x0), min(a["t1"], x1)
            if t1 <= x0 or t0 >= x1:
                continue
            y = base - (a["row"] + 1) * ROW_H + 0.08
            col = QColor(KIND_COL.get(a["kind"], C["fg"]))
            rect = pg.QtWidgets.QGraphicsRectItem(QRectF(t0, y, t1 - t0, ROW_H - 0.16))
            fill = QColor(col)
            fill.setAlpha(55)
            rect.setBrush(QBrush(fill))
            pen = QPen(col)
            pen.setCosmetic(True)
            rect.setPen(pen)
            self.plot.addItem(rect)
            self._items.append(rect)
            if (t1 - t0) * px_per_s >= 6 * len(a["label"]) + 4:
                tx = pg.TextItem(a["label"], color=col.name(), anchor=(0.5, 0.5))
                tx.setFont(_font(10, mono=True, bold=a["kind"] != "stuff"))
                tx.setPos((t0 + t1) / 2, y + (ROW_H - 0.16) / 2)
                self.plot.addItem(tx)
                self._items.append(tx)

    def _fill_table(self, result):
        rows = []
        if result is not None:
            rows = [("+" + fmt_t(x["t"]) if x["t"] > 0 else "0", x["text"], None) for x in result.items]
            rows += [("", n, C["muted"]) for n in result.notes]
            if not result.items and not result.notes:
                rows.append(("", "通信が見つからない(ch・速度・極性と LA トリガを確かめる)", C["warn"]))
        self.table.setRowCount(len(rows))
        for i, (a, b, col) in enumerate(rows):
            for j, txt in enumerate((a, b)):
                it = QTableWidgetItem(txt)
                if col:
                    it.setForeground(QColor(col))
                if j == 0:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, j, it)


class LogicSide(QWidget):
    arm_req = Signal(float, list, str)
    abort_req = Signal()
    changed = Signal()                  # デコーダ・レートの設定が変わった

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.armed = False
        self._edge = "falling"
        self._proto = "uart"
        self.opts = {k: dict(d) for k, d in DEFAULTS.items()}
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        c = Card("サンプリング")
        self.rate = QComboBox()
        for r in la.RATES:
            self.rate.addItem(RATE_LABEL[r], r)
        self.rate.currentIndexChanged.connect(lambda *_: (self._render(), self.changed.emit()))
        c.add(row("サンプルレート", self.rate))
        self.kv_r = KV(["窓の長さ", "時間分解能", "記録の遅れ"])
        c.add(self.kv_r)
        c.add(note("上の 3 つは固定の段、108.7 kHz 以下は可変域。979.4 kHz で取れるのはパルス幅 1.5 µs 以上"
                   "(矩形で 333 kHz まで)。記録はトリガ点の少し後から始まります(プリトリガなし)。"))
        v.addWidget(c)
        c = Card("LA トリガ")
        self.edge = Seg([("falling", "↓ 立下り"), ("rising", "↑ 立上り"), ("auto", "自動")])
        self.edge.set("falling")
        self.edge.changed.connect(lambda e: self._set_edge(e))
        c.add(row("エッジ", self.edge))
        h = QHBoxLayout()
        self.mask = {}
        for ch in (1, 2, 3, 4):
            cb = QCheckBox("D%d" % ch)
            cb.setChecked(ch == 2)
            cb.toggled.connect(lambda *_: self._render())
            h.addWidget(cb)
            self.mask[ch] = cb
        c.add(row("監視 ch", self._wrap(h)))
        self.guard = note("「自動」では監視 ch を空にしてください。この組み合わせは装置がハングするため拒否します。",
                          warn=True)
        c.add(self.guard)
        h = QHBoxLayout()
        self.btn_arm = QPushButton("Arm")
        self.btn_arm.setObjectName("run")
        self.btn_arm.clicked.connect(lambda: self.arm_req.emit(self.rate.currentData(), self.channels(),
                                                               self._edge))
        self.btn_abort = QPushButton("中止")
        self.btn_abort.setEnabled(False)
        self.btn_abort.clicked.connect(self.abort_req.emit)
        h.addWidget(self.btn_arm)
        h.addWidget(self.btn_abort)
        h.addStretch(1)
        c.add(h)
        c.add(note("待機中にタブを切り替えると自動で中止します。未接続の ch を監視に入れると、"
                   "隣の ch からの漏れで誤発火することがあります。"))
        v.addWidget(c)
        c = Card("デコーダ")
        self.proto = Seg([("none", "なし"), ("uart", "UART"), ("i2c", "I2C"), ("spi", "SPI"), ("can", "CAN")])
        self.proto.set("uart")
        self.proto.changed.connect(lambda p: self._set_proto(p))
        c.add(self.proto)
        self.opts_box = QGridLayout()
        self.opts_box.setHorizontalSpacing(8)
        self.opts_box.setVerticalSpacing(4)
        c.add(self.opts_box)
        self.kv_d = KV(["通信の始まり", "1 ビット"])
        c.add(self.kv_d)
        self.dec_warn = c.add(note(warn=True))
        self.dec_note = c.add(note())
        v.addWidget(c)
        c = Card("遷移数")
        self.kv_t = KV(["D1", "D2", "D3", "D4"])
        c.add(self.kv_t)
        v.addWidget(c)
        v.addStretch(1)
        self._build_opts()
        self.set_armed(False)

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def channels(self):
        return [ch for ch, cb in self.mask.items() if cb.isChecked()]

    def _set_edge(self, e):
        self._edge = e
        self._render()

    def _set_proto(self, p):
        self._proto = p
        self._build_opts()
        self.changed.emit()

    def protocol(self):
        return None if self._proto == "none" else self._proto

    def decoder_opts(self):
        p = self.protocol()
        if not p:
            return None
        o = {k: v for k, v in self.opts[p].items() if k != "clock"}
        o["trigger"] = {"channels": self.channels(), "edge": self._edge}
        return o

    def roles(self):
        p, o = self.protocol(), self.opts.get(self._proto, {})
        names = {"uart": [("rx", "RX"), ("tx", "TX")], "i2c": [("scl", "SCL"), ("sda", "SDA")],
                 "spi": [("sck", "SCLK"), ("mosi", "MOSI"), ("miso", "MISO"), ("cs", "CS")],
                 "can": [("rx", "RX")]}.get(p, [])
        return {o[k]: n for k, n in names if o.get(k)}

    def _build_opts(self):
        while self.opts_box.count():
            it = self.opts_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        p = self._proto
        if p == "none":
            self._render()
            return
        o = self.opts[p]
        chs = [(c, "D%d" % c) for c in (1, 2, 3, 4)]
        opt_chs = [(None, "なし")] + chs
        spec = {
            "uart": [("RX", "rx", opt_chs), ("TX", "tx", opt_chs),
                     ("極性", "invert", [(False, "通常(アイドル High)"), (True, "反転(アイドル Low)")]),
                     ("ボーレート", "baud", [(b, str(b)) for b in (9600, 19200, 38400, 57600, 115200)] +
                      [(230400, "230400(ぎりぎり)")]),
                     ("形式", "format", [("8N1", "8N1"), ("8E1", "8E1"), ("8O1", "8O1")])],
            "i2c": [("SCL", "scl", chs), ("SDA", "sda", chs),
                    ("クロック", "clock", [(10000, "10 kHz"), (100000, "100 kHz")])],
            "spi": [("SCLK", "sck", chs), ("MOSI", "mosi", opt_chs), ("MISO", "miso", opt_chs), ("CS", "cs", opt_chs),
                    ("CS の極性", "cs_active_high", [(False, "アクティブ Low"), (True, "アクティブ High")]),
                    ("モード", "mode", [(0, "0(CPOL0 CPHA0)"), (1, "1"), (2, "2"), (3, "3")]),
                    ("クロック", "clock", [(50000, "50 kHz"), (100000, "100 kHz"), (250000, "250 kHz(ぎりぎり)")])],
            "can": [("RX", "rx", chs), ("極性", "invert", [(False, "通常(ドミナント Low)"), (True, "反転")]),
                    ("ビットレート", "bitrate", [(125000, "125 kbps"), (250000, "250 kbps(ぎりぎり)")])],
        }[p]
        for r, (label, key, items) in enumerate(spec):
            lb = QLabel(label)
            lb.setObjectName("note")
            cb = QComboBox()
            for val, text in items:
                cb.addItem(text, val)
            vals = [val for val, _ in items]
            cb.setCurrentIndex(vals.index(o.get(key)) if o.get(key) in vals else 0)
            cb.currentIndexChanged.connect(lambda _i, key=key, cb=cb: self._opt(key, cb.currentData()))
            self.opts_box.addWidget(lb, r, 0)
            self.opts_box.addWidget(cb, r, 1)
        self._render()

    def _opt(self, key, val):
        self.opts[self._proto][key] = val
        self._render()
        self.changed.emit()

    def set_armed(self, armed):
        self.armed = armed
        self.btn_abort.setEnabled(armed)
        self.btn_arm.setText("発火待ち…" if armed else "Arm")
        self._render()

    def show_transitions(self, cap):
        for ch in (1, 2, 3, 4):
            self.kv_t.set("D%d" % ch, "—" if cap is None else str(cap.transitions(ch)))

    def _render(self):
        fs = self.rate.currentData()
        self.kv_r.set("窓の長さ", fmt_t(la.N / fs))
        self.kv_r.set("時間分解能", fmt_t(1 / fs))
        self.kv_r.set("記録の遅れ", "+" + fmt_t(la.latency(fs)))
        bad = self._edge == "auto" and bool(self.channels())
        self.guard.setVisible(bad)
        self.btn_arm.setEnabled(not bad and not self.armed)
        p = self.protocol()
        warn, nt = [], []
        if not p:
            self.kv_d.set("通信の始まり", "—")
            self.kv_d.set("1 ビット", "—")
        else:
            o = self.opts[p]
            chs, edge, what = start_of(p, o)
            ename = {"falling": "立下り", "rising": "立上り"}.get(edge, "")
            self.kv_d.set("通信の始まり", "%s の%s(%s)" % ("・".join("D%d" % c for c in chs if c) or "—", ename, what))
            br = bitrate_of(p, o)
            spb = fs / br
            self.kv_d.set("1 ビット", "%.1f サンプル" % spb, C["crit"] if spb < MIN_SPB else
                          (C["warn"] if spb < GOOD_SPB else None))
            ok = [r for r in sorted(la.RATES) if r / br >= GOOD_SPB]
            faster = [r for r in ok if r > fs]
            if spb < MIN_SPB:
                usable = [r for r in sorted(la.RATES) if r / br >= MIN_SPB and r > fs]
                warn.append("1 ビットあたり %.1f サンプルで、デコードできません。" % spb +
                            ("%s 以上にしてください。" % RATE_LABEL[(faster or usable)[0]] if (faster or usable)
                             else "この速度は観測できません(979.4 kHz でも 1 ビット 2 サンプル未満)。"))
            elif spb < GOOD_SPB:
                warn.append("1 ビットあたり %.1f サンプルで、取りこぼすことがあります(4 以上を推奨)。" % spb +
                            ("%s にすると 4 以上になります。" % RATE_LABEL[faster[0]] if faster
                             else "これより速いレートはありません(ぎりぎりの速度)。"))
            elif ok and ok[0] < fs:
                nt.append("窓を長くするなら %s まで下げられます(1 ビット 4 サンプル以上)。" % RATE_LABEL[ok[0]])
            if p == "spi" and br and o.get("cs") and la.latency(fs) > 0.5 / br:
                warn.append("最初のクロックエッジ(CS の約 %s 後)が記録の開始(+%s)より前なら、先頭ビットを取れません。"
                            % (fmt_t(0.5 / br), fmt_t(la.latency(fs))))
            m = self.channels()
            if chs and edge and (self._edge != edge or not any(c in m for c in chs)):
                nt.append("LA トリガ(%s の%s)は、この設定の通信の始まり(%s の%s)と違います。意図した設定か確かめてください。"
                          % ("・".join("D%d" % c for c in m) or "監視なし",
                             {"falling": "立下り", "rising": "立上り", "auto": "自動"}[self._edge],
                             "・".join("D%d" % c for c in chs if c), ename))
            if p == "can":
                nt.append("CANH/CANL を直接つながず、トランシーバの RXD(ロジックレベル)を入れてください。")
        self.dec_warn.setText("\n".join(warn))
        self.dec_warn.setVisible(bool(warn))
        self.dec_note.setText("\n".join(nt))
        self.dec_note.setVisible(bool(nt))
