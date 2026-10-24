"""メインウィンドウ: 上部バー・タブ(スコープ / ロガー / ロジック)・右パネル・測定欄・ステータス。

この段階ではスコープ画面を作る。ロガーとロジックのタブは次の段階で中身を入れる。
画面の状態は Device.status()(ワーカーが設定を反映するたびに届く)を正とし、ドラッグ中だけ手元で先に動かす。
"""

import datetime
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QStackedWidget, QTabBar,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import autoset, timebase, trigger, vertical
from ..core.device import DeviceError
from ..core.decode import decode as run_decode
from .logger_page import REDRAW_MS, LoggerPage, LoggerSide, short_path
from .logic_page import LaWorker, LogicPage, LogicSide
from .scope_plot import ScopePlot, fmt_t, fmt_v
from .style import C, CH
from .widgets import KV, Card, Seg, Stepper, note, row

RANGE_LABELS = {(1, 0): "×1 段0", (1, 1): "×1 段1", (1, 2): "×1 段2",
                (8, 0): "÷8 段0", (8, 1): "÷8 段1", (8, 2): "÷8 段2"}


def fmt_vdiv(v):
    return ("%g V" % v) if v >= 1 else ("%g mV" % round(v * 1e3, 6))


def fmt_meas(v):
    """測定欄の電圧。有効 3 桁、単位の前に空白。"""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1:
        return "%.3g V" % v if a < 100 else "%.0f V" % v
    if a >= 1e-3:
        return "%.3g mV" % (v * 1e3)
    return "0 V" if a < 1e-9 else "%.3g mV" % (v * 1e3)


def fmt_freq(f):
    if not f:
        return "—"
    return "%.3g kHz" % (f / 1e3) if f >= 1000 else "%.3g Hz" % f


MEAS_KEYS = [("vpp", "Vpp"), ("max", "Max"), ("min", "Min"), ("mean", "平均"), ("rms", "RMS"), ("freq", "周波数")]
MEAS_PERIOD_MS = 250                 # 測定欄の数字は 4 回/s(波形は取得のたびに描く)


def fmt_hz(h):
    return ("%.2f kSPS" % (h / 1e3)) if h >= 1000 else ("%.1f SPS" % h if h >= 1 else "%.3f SPS" % h)


class ZeroDialog(QDialog):
    """0 V 合わせ。入力を GND につないでから開始する。"""

    def __init__(self, win):
        QDialog.__init__(self, win)
        self.win = win
        self.setWindowTitle("0 V 合わせ")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        msg = QLabel("合わせる ch の入力を <b>GND(G)につないでから</b>「開始」を押してください。約 1.5 秒かかります。<br>"
                     "op9 で ÷8・×1 の 0 V の値を測り、6 レンジでスコープも取得してレンジごとの 0 V を記録します。"
                     "結果はこの基板の設定として保存し、次回の起動でも使います。")
        msg.setWordWrap(True)
        v.addWidget(msg)
        h = QHBoxLayout()
        self.c1, self.c2 = QCheckBox("CH1"), QCheckBox("CH2")
        self.c1.setChecked(True)
        self.c2.setChecked(True)
        h.addWidget(self.c1)
        h.addWidget(self.c2)
        h.addStretch(1)
        v.addLayout(h)
        self.tbl = QTableWidget(2, 3)
        self.tbl.setHorizontalHeaderLabels(["÷8 の 0 V", "×1 の 0 V", "表示のずれ(合わせる前)"])
        self.tbl.setVerticalHeaderLabels(["CH1", "CH2"])
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setFixedHeight(self.tbl.horizontalHeader().sizeHint().height() + 2 * 30 + 4)
        for r in range(2):
            self.tbl.setRowHeight(r, 30)
        v.addWidget(self.tbl)
        f = QHBoxLayout()
        self.state = note("値は 10 bit ADC のカウント(1 カウント = ÷8 で 47.9 mV)")
        f.addWidget(self.state, 1)
        close = QPushButton("閉じる")
        close.clicked.connect(self.reject)
        self.run = QPushButton("開始")
        self.run.setObjectName("run")
        self.run.clicked.connect(self._start)
        f.addWidget(close)
        f.addWidget(self.run)
        v.addLayout(f)
        self._before = None
        self._fill()

    def _fill(self, before=None):
        sc = self.win.device.scope
        for r, ch in enumerate((1, 2)):
            self.tbl.setItem(r, 0, QTableWidgetItem("%.1f" % sc.m0[ch][8]))
            self.tbl.setItem(r, 1, QTableWidgetItem("%.1f" % sc.m0[ch][1]))
            if before:
                d = sc.m0[ch][8] - before[ch]
                self.tbl.setItem(r, 2, QTableWidgetItem("%+.0f mV" % (d * 47.9)))

    def _start(self):
        chs = [c for c, b in ((1, self.c1), (2, self.c2)) if b.isChecked()]
        if not chs:
            return
        sc = self.win.device.scope
        self._before = {ch: sc.m0[ch][8] for ch in (1, 2)}
        self.run.setEnabled(False)
        self.state.setText("測定中…")
        self.win.worker.post("zero", self.win.device.calibrate_zero, channels=tuple(chs))

    def done_zero(self, r):
        self._fill(self._before)
        self.run.setEnabled(True)
        self.state.setText("保存しました(%s)。入力を元の信号に戻してください" % r.get("saved", ""))


class ShotDialog(QDialog):
    """スクリーンショットの保存先。既定は ~/Pictures/Screenshots/、名前は GNOME と同じ形式。"""

    def __init__(self, win):
        QDialog.__init__(self, win)
        self.win = win
        self.setWindowTitle("スクリーンショットを保存")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        d = datetime.datetime.now()
        self.name = QLineEdit(d.strftime("Screenshot from %Y-%m-%d %H-%M-%S.png"))
        self.dir = QLineEdit(win.shot_dir)
        b = QPushButton("参照…")
        b.clicked.connect(self._browse)
        v.addWidget(QLabel("ファイル名"))
        v.addWidget(self.name)
        v.addWidget(QLabel("保存先"))
        h = QHBoxLayout()
        h.addWidget(self.dir, 1)
        h.addWidget(b)
        v.addLayout(h)
        self.info = QCheckBox("設定と測定値を含める(画面に見えているとおり)")
        self.info.setChecked(True)
        self.csv = QCheckBox("同じ名前で波形の CSV も保存")
        v.addWidget(self.info)
        v.addWidget(self.csv)
        f = QHBoxLayout()
        f.addStretch(1)
        c = QPushButton("キャンセル")
        c.clicked.connect(self.reject)
        s = QPushButton("保存")
        s.setObjectName("run")
        s.clicked.connect(self.accept)
        f.addWidget(c)
        f.addWidget(s)
        v.addLayout(f)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "保存先", self.dir.text())
        if d:
            self.dir.setText(d)


class MainWindow(QMainWindow):
    ext_op = Signal(str, str, object, bool)          # 外部操作の通知(サーバのスレッドから)
    log_point = Signal(dict)                         # ロガーの点(記録スレッドから)
    log_state = Signal(dict)

    def __init__(self, device, worker, server=None, logger=None):
        QMainWindow.__init__(self)
        self.device, self.worker, self.server, self.logger = device, worker, server, logger
        self.log_pts, self.log_marks, self.log_st, self._log_dirty = [], [], None, False
        self.setWindowTitle("osc001 Viewer")
        self.setMinimumSize(1280, 720)
        self.shot_dir = os.path.expanduser("~/Pictures/Screenshots/")
        self.st = None                                 # Device.status() の最新
        self.frame = None
        self.active = 1
        self.on = {1: True, 2: True}
        self.waiting = False
        self.count = 0
        self.last_at = None
        self.oplog = []
        self.la_cap, self.la_res, self.la_state = None, None, "idle"
        self.la_worker = LaWorker(device)
        self.la_worker.captured.connect(self._on_la_capture)
        self.la_worker.failed.connect(self._on_failed)
        self.la_worker.state.connect(self._on_la_state)
        self._scope_was_running = True
        self._build()
        worker.frame.connect(self._on_frame)
        worker.waiting.connect(self._on_waiting)
        worker.status.connect(self._on_status)
        worker.failed.connect(self._on_failed)
        worker.result.connect(self._on_result)
        worker.run_state.connect(self._on_run_state)
        self.ext_op.connect(self._on_ext_op)
        self.log_point.connect(self._on_log_point)
        self.log_state.connect(self._on_log_state)
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._log_tick)
        self._log_timer.start(REDRAW_MS)
        self._zero_dlg = None

    # ── 構築 ────────────────────────────────────────────────────────
    def _build(self):
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._topbar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._scope_page())
        self.log_page = LoggerPage()
        self.pages.addWidget(self.log_page)
        self.logic_page = LogicPage()
        self.pages.addWidget(self.logic_page)
        body.addWidget(self.pages, 1)
        self.side = QStackedWidget()
        self.side.setObjectName("side")
        self.side.setFixedWidth(300)
        self.side.addWidget(self._scope_side())
        self.side.addWidget(self._log_side())
        self.side.addWidget(self._logic_side())
        body.addWidget(self.side)
        v.addLayout(body, 1)
        v.addWidget(self._statusbar())
        self.setCentralWidget(root)

    def _topbar(self):
        w = QWidget()
        w.setObjectName("topbar")
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 7, 12, 7)
        h.setSpacing(10)
        self.dev_label = QLabel("")
        self.dev_label.setObjectName("head")
        h.addWidget(self.dev_label)
        self.tabs = QTabBar()
        for t in ("スコープ", "ロガー", "ロジック"):
            self.tabs.addTab(t)
        self.tabs.currentChanged.connect(self._on_tab)
        h.addWidget(self.tabs)
        h.addStretch(1)
        self.ext_chip = QLabel("外部接続 0")
        self.ext_chip.setObjectName("extchip")
        self.ext_chip.setToolTip("外部(LLM・スクリプト)からの接続。osc001ctl / Python クライアント")
        h.addWidget(self.ext_chip)
        self.btn_auto = QPushButton("Auto Set")
        self.btn_cur = QPushButton("カーソル")
        self.btn_cur.setCheckable(True)
        self.btn_run = QPushButton("Stop")
        self.btn_run.setObjectName("stop")
        self.btn_single = QPushButton("Single")
        self.btn_shot = QPushButton("撮影")
        self.btn_shot.setToolTip("スクリーンショット(Ctrl+Shift+S)")
        self.btn_shot.setShortcut("Ctrl+Shift+S")
        for b in (self.btn_auto, self.btn_cur, self.btn_run, self.btn_single, self.btn_shot):
            h.addWidget(b)
        self.btn_auto.clicked.connect(lambda: self.worker.post("autoset", autoset.autoset, self.device))
        self.btn_cur.toggled.connect(self._on_cursor)
        self.btn_run.clicked.connect(lambda: self.worker.set_running(not self.worker.running))
        self.btn_single.clicked.connect(self._single)
        self.btn_shot.clicked.connect(self._shot)
        return w

    def _scope_page(self):
        self.scope_main = QWidget()
        v = QVBoxLayout(self.scope_main)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(6)
        hb = QHBoxLayout()
        self.head = QLabel("")
        self.head.setObjectName("head")
        self.head.setTextFormat(Qt.RichText)
        hb.addWidget(self.head, 1)
        v.addLayout(hb)
        self.plot = ScopePlot()
        self.plot.wheel.connect(self._on_wheel)
        self.plot.zero_dragged.connect(self._on_zero_drag)
        self.plot.level_dragged.connect(self._on_level_drag)
        self.plot.cursors_moved.connect(lambda *_: self._render_meas())
        v.addWidget(self.plot, 1)
        self.meas = QHBoxLayout()
        self.meas.setSpacing(8)
        self.mcard, self.mval = {}, {}

        def lab(g, r, c, text, key=True, color=None, width=None):
            lb = QLabel(text)
            lb.setObjectName("kvkey" if key else "kvval")
            if color:
                lb.setStyleSheet("color:%s;font-weight:600;" % color)
            if width:
                lb.setMinimumWidth(width)
                lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                lb.setMinimumWidth(lb.sizeHint().width())
            g.addWidget(lb, r, c)
            return lb

        for k in ("ch1", "ch2", "cur"):
            f = QFrame()
            f.setObjectName("card")
            g = QGridLayout(f)
            g.setContentsMargins(10, 6, 10, 6)
            g.setHorizontalSpacing(6)
            g.setVerticalSpacing(1)
            vals = {}
            if k == "cur":
                lab(g, 0, 0, "カーソル A–B", color=C["cur"])
                for i, name in enumerate(("Δt", "1/Δt", "CH1 A", "CH2 A", "CH1 B", "CH2 B")):
                    lab(g, 1 + i // 2, (i % 2) * 2, name)
                    vals[name] = lab(g, 1 + i // 2, (i % 2) * 2 + 1, "—", key=False, width=62)
            else:
                ch = int(k[-1])
                lab(g, 0, 0, "CH%d" % ch, color=CH[ch])
                for i, (key, name) in enumerate(MEAS_KEYS):
                    lab(g, 1 + i // 3, (i % 3) * 2, name)
                    vals[key] = lab(g, 1 + i // 3, (i % 3) * 2 + 1, "—", key=False, width=62)
            self.mcard[k], self.mval[k] = f, vals
            self.meas.addWidget(f, 1)
        v.addLayout(self.meas)
        self._meas_dirty = False
        self._meas_timer = QTimer(self)
        self._meas_timer.timeout.connect(self._meas_tick)
        self._meas_timer.start(MEAS_PERIOD_MS)
        return self.scope_main

    def _todo_page(self, title, text):
        w = QWidget()
        v = QVBoxLayout(w)
        lb = QLabel("<h3>%s</h3><p>%s</p>" % (title, text))
        lb.setAlignment(Qt.AlignCenter)
        v.addWidget(lb)
        return w

    def _scope_side(self):
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        # 水平 / 取得
        c = Card("水平 / 取得")
        self.t_step = Stepper()
        self.t_step.step.connect(self._on_tstep)
        c.add(row("Time/div", self.t_step))
        self.kv_h = KV(["実効 fs", "1 取得の時間"])
        c.add(self.kv_h)
        self.seg_len = Seg([(False, "192"), (True, "211 deep")])
        self.seg_len.changed.connect(lambda deep: self.worker.post(None, self.device.set_record, deep))
        c.add(row("記録長", self.seg_len))
        v.addWidget(c)
        # トリガ
        c = Card("トリガ")
        self.trig_pill = QLabel("")
        self.trig_pill.setObjectName("chip")
        c.head.addWidget(self.trig_pill)
        self.seg_src = Seg([("none", "なし"), ("ch1", "CH1"), ("ch2", "CH2"), ("ext", "EXT")])
        self.seg_src.changed.connect(self._on_trig_src)
        c.add(row("ソース", self.seg_src))
        self.seg_edge = Seg([("rising", "↑ 立上り"), ("falling", "↓ 立下り")])
        self.seg_edge.changed.connect(lambda e: self._post_trig(edge=e))
        c.add(row("エッジ", self.seg_edge))
        self.seg_mode = Seg([("auto", "Auto"), ("normal", "Normal")])
        self.seg_mode.changed.connect(lambda m: self._post_trig(mode=m))
        c.add(row("モード", self.seg_mode))
        self.kv_t = KV(["レベル", "方式", "状態"])
        c.add(self.kv_t)
        self.trig_note = c.add(note())
        self.trig_warn = c.add(note(warn=True))
        v.addWidget(c)
        # CH カード
        self.chw = {}
        for ch in (1, 2):
            c = Card("CH%d" % ch, swatch=CH[ch])
            en = QCheckBox("表示")
            en.setChecked(True)
            en.toggled.connect(lambda on, ch=ch: self._on_enable(ch, on))
            c.head.addWidget(en)
            st = Stepper()
            st.step.connect(lambda d, ch=ch: self._vstep(ch, -d))
            c.add(row("V/div", st))
            pr = QComboBox()
            pr.addItems(["×1", "×10"])
            pr.currentIndexChanged.connect(lambda i, ch=ch: self.worker.post(None, self.device.set_channel, ch,
                                                                             probe=10 if i else 1))
            c.add(row("プローブ", pr))
            rg = Seg([("auto", "自動"), ("fix", "固定")])
            rg.changed.connect(lambda m, ch=ch: self._on_fix(ch, m))
            c.add(row("レンジ", rg))
            kv = KV(["選ばれたレンジ", "分解能", "測定範囲"])
            c.add(kv)
            c.mousePressEvent = lambda ev, ch=ch: self._set_active(ch)
            self.chw[ch] = {"card": c, "en": en, "step": st, "probe": pr, "rng": rg, "kv": kv}
            v.addWidget(c)
        # 0 V 合わせ
        c = Card("0 V 合わせ")
        self.kv_z = KV(["前回"])
        c.add(self.kv_z)
        b = QPushButton("0 V 合わせ…")
        b.clicked.connect(self._zero_open)
        h = QHBoxLayout()
        h.addWidget(b)
        h.addStretch(1)
        c.add(h)
        c.add(note("基板の可変抵抗 R12・R13 を回したとき、別の基板につないだとき、表示の 0 V がずれて見えるときに行います。"))
        v.addWidget(c)
        v.addStretch(1)
        sa = QScrollArea()
        sa.setWidget(inner)
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return sa

    def _log_side(self):
        self.log_side = LoggerSide()
        self.log_side.start_req.connect(self._log_start)
        self.log_side.stop_req.connect(self._log_stop)
        self.log_side.marker_req.connect(self._log_marker)
        c = Card("外部操作ログ")
        self.oplog_label = note("(まだありません)")
        c.add(self.oplog_label)
        self.lock = QCheckBox("外部からの設定変更を止める")
        self.lock.toggled.connect(self._on_lock)
        c.add(self.lock)
        self.log_side.add_card(c)
        self.log_side.finish()
        sa = QScrollArea()
        sa.setWidget(self.log_side)
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return sa

    def _logic_side(self):
        self.logic_side = LogicSide()
        self.logic_side.arm_req.connect(self._la_arm)
        self.logic_side.abort_req.connect(self.la_worker.abort)
        self.logic_side.changed.connect(self._la_redecode)
        sa = QScrollArea()
        sa.setWidget(self.logic_side)
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return sa

    # ── ロジック ────────────────────────────────────────────────────
    def _la_arm(self, rate, channels, edge):
        self.la_worker.arm(rate, channels, edge)

    def _on_la_state(self, st):
        self.la_state = st
        self.logic_side.set_armed(st == "armed")
        if self.la_cap is None or st == "armed":
            self.logic_page.show_capture(None if st == "armed" else self.la_cap, None if st == "armed" else self.la_res,
                                         self.logic_side.roles(), st)
        self._render_logic_head()
        self._render_status()

    def _on_la_capture(self, cap):
        self.la_cap = cap
        self.la_state = "idle"
        self._la_redecode()

    def _la_redecode(self):
        cap, side = self.la_cap, self.logic_side
        p = side.protocol()
        self.la_res = run_decode(p, cap, side.decoder_opts()) if (cap is not None and p) else None
        self.logic_side.show_transitions(cap)
        if self.la_state != "armed":
            self.logic_page.show_capture(cap, self.la_res, side.roles(), self.la_state)
        self._render_logic_head()
        self._render_status()

    def _render_logic_head(self):
        side, cap = self.logic_side, self.la_cap
        fs = cap.fs if cap is not None else side.rate.currentData()
        p = side.protocol()
        dec = "なし"
        if p:
            o = side.opts[p]
            dec = {"uart": lambda: "UART %s %s" % (o["baud"], o["format"]), "i2c": lambda: "I2C",
                   "spi": lambda: "SPI mode %d" % o["mode"],
                   "can": lambda: "CAN %g kbps" % (o["bitrate"] / 1e3)}[p]()
        st = {"armed": "<b style='color:%s'>発火待ち</b>" % C["warn"]}.get(
            self.la_state, "<b style='color:%s'>%s</b>" % (C["fg"], "発火済み" if cap is not None else "未取得"))
        parts = ["LA " + st, "4 ch × 844 サンプル", "fs <b style='color:%s'>%s</b>" % (C["fg"], fmt_hz(fs)),
                 "窓 <b style='color:%s'>%s</b>" % (C["fg"], fmt_t(844 / fs)),
                 "エッジ <b style='color:%s'>%s</b>" % (C["fg"], {"falling": "↓ 立下り", "rising": "↑ 立上り",
                                                                "auto": "自動"}[side._edge]),
                 "デコード <b style='color:%s'>%s</b>" % (C["fg"], dec)]
        self.logic_page.head.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    # ── ロガー ──────────────────────────────────────────────────────
    def _log_start(self, params):
        if not self.logger:
            return
        try:
            st = self.logger.start(by="user", **params)
        except DeviceError as e:
            QMessageBox.warning(self, "osc001", "%s\n\n%s" % (e, e.hint or ""))
            return
        self._on_log_state(st)

    def _log_stop(self):
        try:
            st = self.logger.stop()
        except DeviceError as e:
            QMessageBox.warning(self, "osc001", str(e))
            return
        self.statusBar_msg("保存しました: %s(%d 点)" % (short_path(st["file"]), st["points"]))

    def _log_marker(self, text):
        try:
            self.logger.marker(text, by="user")
        except DeviceError as e:
            QMessageBox.warning(self, "osc001", str(e))

    def _on_log_point(self, p):
        self._check_log_file()
        self.log_pts.append(p)
        for ev in (p.get("event") or "").split(" | "):
            if ev.startswith("marker: "):
                body = ev[len("marker: "):]
                by = body[body.rfind("(") + 1:-1] if body.endswith(")") and "(" in body else "user"
                text = body[:body.rfind("(")].strip() if body.endswith(")") and "(" in body else body
                self.log_marks.append((p["t"], text, by))
        self._log_dirty = True

    def _check_log_file(self):
        f = self.logger.state.get("file") if self.logger else None
        if f != getattr(self, "_log_file", None):        # 新しい記録(画面からでも外部操作からでも)
            self._log_file = f
            self.log_pts, self.log_marks = [], []

    def _on_log_state(self, st):
        self._check_log_file()
        self.log_st = st
        self.log_side.set_state(st)
        self._log_dirty = True

    def _log_tick(self):
        if not self._log_dirty:
            if self.log_st and self.log_st.get("running") and self.tabs.currentIndex() == 1:
                self._render_log_head()                      # 経過時間だけ進める
            return
        self._log_dirty = False
        st = self.logger.status() if self.logger else None
        self.log_st = st
        self.log_side.set_state(st)
        if self.tabs.currentIndex() == 1:
            al = (self.logger.state.get("alerts") or []) if self.logger else []
            self.log_page.show_points(self.log_pts, self.log_marks, al, st)
            self._render_log_head()
            self._render_status()

    def _render_log_head(self):
        st = self.log_st
        if not st or not st.get("file"):
            self.log_page.head.setText("<b style='color:%s'>■ 停止中</b>" % C["muted"])
            return
        if st.get("running"):
            el = int(datetime.datetime.now().timestamp() -
                     datetime.datetime.fromisoformat(st["started_at"]).timestamp())
            rec = "<b style='color:#ff6b62'>● REC</b> %02d:%02d:%02d" % (el // 3600, el // 60 % 60, el % 60)
        else:
            rec = "<b style='color:%s'>■ 停止</b> %d 点" % (C["muted"], st.get("points", 0))
        parts = [rec, "間隔 <b style='color:%s'>%g s</b>" % (C["fg"], st["interval"]),
                 "op9 <b style='color:%s'>%d 回平均 · %s</b>" % (C["fg"], st["avg"], "±24 V" if st["tap"] == 8 else "細かい方"),
                 "上段 <b style='color:%s'>CH1</b> · 下段 <b style='color:%s'>CH2</b>(軸は自動)" % (CH[1], CH[2]),
                 "<span style='color:%s'>%s</span>" % (C["muted"], os.path.basename(st["file"]))]
        self.log_page.head.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    def _statusbar(self):
        w = QWidget()
        w.setObjectName("topbar")
        self.status_lay = QHBoxLayout(w)
        self.status_lay.setContentsMargins(12, 5, 12, 5)
        self.status_lay.setSpacing(6)
        self.status_right = QLabel("")
        self.status_right.setObjectName("kvkey")
        self.hint = QLabel("ホイール: V/div · Shift+ホイール: Time/div · ◀ をドラッグ: 0 V 位置")
        self.hint.setObjectName("note")
        self._chips = []
        self.status_lay.addStretch(1)
        self.status_lay.addWidget(self.hint)
        self.status_lay.addSpacing(14)
        self.status_lay.addWidget(self.status_right)
        return w

    # ── 操作 ────────────────────────────────────────────────────────
    def _axis(self, ch):
        return self.st["channels"]["ch%d" % ch] if self.st else None

    def _vstep(self, ch, d):
        """d=+1 で V/div を大きく。"""
        a = self._axis(ch)
        if not a:
            return
        steps = [s * a["probe"] for s in vertical.VDIV_STEPS]
        i = min(range(len(steps)), key=lambda k: abs(steps[k] - a["vdiv"]))
        j = max(0, min(len(steps) - 1, i + d))
        if j != i:
            self.worker.post(None, self.device.set_channel, ch, vdiv=steps[j])

    def _on_tstep(self, d):
        """◀(−1)で Time/div を大きく。"""
        self._tstep(-d)

    def _tstep(self, d):
        if not self.st:
            return
        steps = timebase.TDIV_STEPS
        i = min(range(len(steps)), key=lambda k: abs(steps[k] - self.st["timebase"]["tdiv"]))
        j = max(0, min(len(steps) - 1, i + d))
        if j != i:
            self.worker.post(None, self.device.set_timebase, steps[j])

    def _on_wheel(self, d, shift):
        if shift:
            self._tstep(d)
        else:
            self._vstep(self.active, d)

    def _on_zero_drag(self, ch, y):
        self.plot.st["axis"][ch].pos_div = y
        self.plot.refresh()
        self.worker.post(None, self.device.set_channel, ch, pos_div=y)

    def _on_level_drag(self, v):
        self.plot.st["trig"]["level"] = v
        self.plot.refresh()
        self._post_trig(level=v)

    def _post_trig(self, **kw):
        t = dict(self.st["trigger"]) if self.st else {}
        t.pop("kind", None)
        t.update(kw)
        self.worker.post(None, self.device.set_trigger, **t)

    def _on_trig_src(self, src):
        lvl = None
        if src in ("ch1", "ch2") and self.frame is not None:
            v = self.frame.measured(int(src[-1]))
            if len(v):
                cur = self.st["trigger"]["level"]
                if not (v.min() < cur < v.max()):
                    lvl = float((v.min() + v.max()) / 2)     # 波形の外なら中央へ
        self._post_trig(source=src, **({"level": lvl} if lvl is not None else {}))

    def _on_fix(self, ch, mode):
        if mode == "auto":
            self.worker.post(None, self.device.set_channel, ch, fixed=None)
        else:
            r = self._axis(ch)["range"]
            self.worker.post(None, self.device.set_channel, ch, fixed=(r["tap"], r["gain"]))

    def _on_enable(self, ch, on):
        self.on[ch] = on
        self.plot.set_state(on=dict(self.on))
        self._render_meas()

    def _set_active(self, ch):
        self.active = ch
        self.plot.set_state(active=ch)
        self._render_panel()

    def _on_cursor(self, on):
        self.plot.set_cursors(on)
        self._render_meas()

    def _single(self):
        self.worker.single()

    def _on_tab(self, i):
        prev = self.pages.currentIndex()
        if i == 2 and prev != 2:                         # ロジック: スコープの arm は LA の待機を解くので止める
            self._scope_was_running = self.worker.running
            self.worker.set_running(False)
        elif prev == 2 and i != 2:
            if self.la_worker.isRunning():
                self.la_worker.abort()
                self.statusBar_msg("LA の待機を自動で中止しました(タブを切り替えたため)")
            self.worker.set_running(self._scope_was_running)
        self.pages.setCurrentIndex(i)
        self.side.setCurrentIndex(i)
        if i == 2:
            self._render_logic_head()
        if i == 1:
            self._log_dirty = True
        self._chip_spec = None
        self._render_status()
        for b in (self.btn_auto, self.btn_cur, self.btn_run, self.btn_single, self.hint):
            b.setVisible(i == 0)

    def _on_lock(self, on):
        if self.server:
            self.server.readonly = on

    def _zero_open(self):
        self._zero_dlg = ZeroDialog(self)
        self._zero_dlg.show()

    def _shot(self):
        d = ShotDialog(self)
        if d.exec() != QDialog.Accepted:
            return
        folder = os.path.expanduser(d.dir.text())
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, d.name.text())
        page = self.pages.currentWidget()
        if self.tabs.currentIndex() == 0:
            img = (self.scope_main if d.info.isChecked() else self.plot).grab()
        else:
            img = (page if d.info.isChecked() else (self.log_page.glw if page is self.log_page else
                                                    self.logic_page.plot)).grab()
        img.save(path, "PNG")
        self.shot_dir = folder
        if d.csv.isChecked() and self.frame is not None and self.tabs.currentIndex() == 0:
            from ..cli import capture_csv
            with open(os.path.splitext(path)[0] + ".csv", "w", encoding="utf-8") as f:
                f.write(capture_csv(self.frame.as_dict(samples=True)))
        self.statusBar_msg("保存しました: %s" % path)

    def statusBar_msg(self, text, seconds=8):
        """ステータスバーの右に、しばらく(既定 8 秒)知らせを出す。"""
        self._msg = (text, datetime.datetime.now().timestamp() + seconds)
        self.status_right.setText(text)
        QTimer.singleShot(int(seconds * 1000) + 50, self._render_status)

    # ── ワーカーから ────────────────────────────────────────────────
    def _on_frame(self, f):
        self.frame = f
        if f.valid is None:
            self.count += 1
            self.last_at = datetime.datetime.now()
        self.plot.set_frame(f)
        self._meas_dirty = True
        self._render_status()
        self._render_trig_warn()
        self._render_trig_state()

    def _render_trig_state(self):
        st, f = self.st, self.frame
        if not st:
            return
        kind = st["trigger"]["kind"]
        self.kv_t.set("状態", "フリーラン" if not kind else ("待機中(Normal)" if self.waiting else
                     ("成立" if f is not None and f.trig_status == "triggered" else "未成立 → フリーラン")))

    def _on_waiting(self, on):
        if on == self.waiting:
            return
        self.waiting = on
        self.plot.set_waiting(on)
        self._render_trig_state()
        self._render_status()

    def _on_status(self, st):
        self.st = st
        axes = {ch: vertical.ScreenAxis(st["channels"]["ch%d" % ch]["vdiv"], st["channels"]["ch%d" % ch]["pos_div"],
                                        st["channels"]["ch%d" % ch]["probe"]) for ch in (1, 2)}
        self.plot.set_state(axis=axes, range={ch: st["channels"]["ch%d" % ch]["range"] for ch in (1, 2)},
                            trig=dict(st["trigger"]), tdiv=st["timebase"]["tdiv"], active=self.active,
                            on=dict(self.on))
        self._render_panel()
        self._render_status()

    def _on_failed(self, msg, hint):
        QMessageBox.warning(self, "osc001", msg + ("\n\n" + hint if hint else ""))

    def _on_result(self, name, r):
        if name == "zero" and self._zero_dlg is not None:
            self._zero_dlg.done_zero(r)

    def _on_run_state(self, running):
        self.btn_run.setText("Stop" if running else "Run")
        self.btn_run.setObjectName("stop" if running else "run")
        self.btn_run.style().unpolish(self.btn_run)
        self.btn_run.style().polish(self.btn_run)
        self._render_panel()

    def _on_ext_op(self, client, op, params, mutating):
        t = datetime.datetime.now().strftime("%H:%M:%S")
        args = " ".join("--%s %s" % (k.replace("_", "-"), v) for k, v in params.items())
        self.oplog.append("%s  %s: %s %s" % (t, client, op.replace("_", "-"), args))
        self.oplog = self.oplog[-30:]
        self.oplog_label.setText("\n".join(reversed(self.oplog[-12:])))
        if mutating:
            self.worker.refresh()
        self._render_ext()

    # ── 表示 ────────────────────────────────────────────────────────
    def _render_ext(self):
        if not self.server:
            self.ext_chip.setText("外部操作なし")
            return
        n = self.server.client_count()
        last = self.server.last_client
        self.ext_chip.setText("外部接続 %d%s%s" % (n, " · " + last if last else "",
                                                  " · 読み取り専用" if self.server.readonly else ""))

    def _render_panel(self):
        st = self.st
        if not st:
            return
        tb = st["timebase"]
        self.dev_label.setText("%s — osc001" % (st["path"] or "未接続"))
        self.t_step.set_text(fmt_t(tb["tdiv"]))
        self.kv_h.set("実効 fs", fmt_hz(tb["fs"]))
        acq = tb["acq_time"]
        self.kv_h.set("1 取得の時間", "%s · %.2g 回/s" % (fmt_t(acq), min(40, 1 / acq)))
        self.seg_len.set(tb["deep"])
        t = st["trigger"]
        self.seg_src.set(t["source"])
        self.seg_edge.set(t["edge"])
        self.seg_mode.set(t["mode"])
        kind = t["kind"]
        self.trig_pill.setText({"hw": "HW", "sw": "SW"}.get(kind, "—"))
        lvl_ch = int(t["source"][-1]) if t["source"] in ("ch1", "ch2") else 0
        self.kv_t.set("レベル", "≒1.45 V 固定" if t["source"] == "ext" else (fmt_v(t["level"]).replace("V", " V")
                                                                             if lvl_ch else "—"))
        self.kv_t.set("方式", {"hw": "ハードウェア", "sw": "ソフトウェア"}.get(kind, "—"))
        f = self.frame
        state = "フリーラン" if not kind else ("待機中(Normal)" if self.waiting else
                                         ("成立" if f is not None and f.trig_status == "triggered" else "未成立 → フリーラン"))
        self.kv_t.set("状態", state)
        self.trig_note.setText({
            None: "フリーランです。取得ごとに波形の位置が揃いません。",
            "hw": "ハードウェアで処理します。トリガ点が記録の先頭になり、トリガより前は見えません。先頭の 1 点は描きません。"
                  "レベルは右端の ◀ T をドラッグして変えます。",
            "sw": "CH2 のハードウェアトリガは無いため、取得した波形(211 点)の中から交差点を探し、1 div 前から表示します。",
        }[kind] if t["source"] != "ext" else trigger.ext_note())
        for ch in (1, 2):
            w, a = self.chw[ch], st["channels"]["ch%d" % ch]
            r = a["range"]
            w["step"].set_text(fmt_vdiv(a["vdiv"]))
            w["probe"].blockSignals(True)
            w["probe"].setCurrentIndex(1 if a["probe"] == 10 else 0)
            w["probe"].blockSignals(False)
            w["rng"].set("fix" if a["fixed"] else "auto")
            w["kv"].set("選ばれたレンジ", "%s · pos %d%s" % (r["label"], r["position"], "(固定)" if a["fixed"] else ""))
            w["kv"].set("分解能", "%.1f コード/div" % (a["vdiv"] / a["probe"] / r["v_per_lsb"]))
            p = a["probe"]
            w["kv"].set("測定範囲", "%s 〜 %s" % (fmt_v(r["window"][0] * p).replace("V", " V"),
                                              fmt_v(r["window"][1] * p).replace("V", " V")),
                        None if r["covers"] else C["warn"])
            border = CH[ch] if ch == self.active else C["line"]
            if w.get("border") != border:                   # スタイルシートの設定は重いので、変わったときだけ
                w["border"] = border
                w["card"].setStyleSheet("QFrame#card{border-color:%s;}" % border)
        z = st["zero"]
        self.kv_z.set("前回", (z["calibrated_at"] or "未実施(出荷時の値)").replace("T", " ")[:19])
        # 情報行
        run = "<b style='color:%s'>%s</b>" % ((C["ok"], "RUN") if self.worker.running else (C["crit"], "STOP"))
        parts = [run]
        for ch in (1, 2):
            a = st["channels"]["ch%d" % ch]
            if self.on[ch]:
                parts.append("<span style='color:%s'>CH%d <b>%s/div</b> %s</span>"
                             % (CH[ch], ch, fmt_vdiv(a["vdiv"]), a["range"]["label"]))
        parts.append("H <b style='color:%s'>%s/div</b>" % (C["fg"], fmt_t(tb["tdiv"])))
        parts.append("fs <b style='color:%s'>%s</b>" % (C["fg"], fmt_hz(tb["fs"])))
        if t["source"] == "none":
            parts.append("Trig <b style='color:%s'>なし</b>" % C["fg"])
        else:
            parts.append("Trig <b style='color:%s'>%s %s%s</b> %s · %s" % (
                C["fg"], t["source"].upper(), "↑" if t["edge"] == "rising" else "↓",
                "" if t["source"] == "ext" else " " + fmt_v(t["level"]), {"hw": "HW", "sw": "SW"}[kind],
                "Auto" if t["mode"] == "auto" else "Normal"))
        self.head.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        self._render_ext()

    def _render_trig_warn(self):
        st, f = self.st, self.frame
        w = []
        if st and f is not None:
            src = st["trigger"]["source"]
            if src in ("ch1", "ch2"):
                v = f.measured(int(src[-1]))
                if len(v):
                    w = trigger.level_warnings(src, st["trigger"]["level"], float(v.min()), float(v.max()),
                                               st["trigger"]["mode"])
        self.trig_warn.setText("\n".join(w))
        self.trig_warn.setVisible(bool(w))

    def _meas_tick(self):
        if self._meas_dirty:
            self._meas_dirty = False
            self._render_meas()

    def _render_meas(self):
        f = self.frame
        self.mcard["cur"].setVisible(self.btn_cur.isChecked())
        for ch in (1, 2):
            self.mcard["ch%d" % ch].setVisible(self.on[ch])
        if f is None:
            return
        for ch in (1, 2):
            s = f.stats(ch)
            vals = self.mval["ch%d" % ch]
            for key, _name in MEAS_KEYS:
                vals[key].setText(fmt_freq(s[key]) if key == "freq" else fmt_meas(s[key]))
        if self.btn_cur.isChecked():
            vals = self.mval["cur"]
            a, b = self.plot.cur[0].value(), self.plot.cur[1].value()
            dt = abs(b - a) * self.st["timebase"]["tdiv"]
            vals["Δt"].setText(fmt_t(dt))
            vals["1/Δt"].setText(fmt_freq(1 / dt) if dt > 0 else "—")
            for name, x in (("A", a), ("B", b)):
                i = int(round(self.plot.x_origin() + x * timebase.SAMPLES / 10.0))
                for ch in (1, 2):
                    ok = f.first_valid <= i < (f.n if f.valid is None else f.valid)
                    vals["CH%d %s" % (ch, name)].setText(fmt_meas(float(f.volts[ch][i])) if ok else "—")

    def _render_status(self):
        chips = []
        f, st = self.frame, self.st
        if self.tabs.currentIndex() == 2:
            cap, res = self.la_cap, self.la_res
            chips.append(("LA %s" % {"armed": "発火待ち"}.get(self.la_state, "発火済み" if cap is not None else "未取得"),
                          "chipwarn" if self.la_state == "armed" else "chip"))
            if res is not None and res.spb:
                chips.append(("%s · %.1f サンプル/ビット" % (res.protocol.upper(), res.spb),
                              "chipcrit" if res.spb < 2 else ("chipwarn" if res.spb < 4 else "chip")))
            if res is not None and res.errors:
                chips.append(("デコード誤り %d" % res.errors, "chipcrit"))
            f = None
        elif self.tabs.currentIndex() == 1:
            ls = self.log_st or {}
            if ls.get("running"):
                chips.append(("記録中 · %g s 間隔" % ls["interval"], "chip"))
            else:
                chips.append(("記録停止中", "chip"))
            if ls.get("errors"):
                chips.append(("読み取りエラー %d" % ls["errors"], "chipcrit"))
            if ls.get("saturated"):
                chips.append(("張り付き %d 点" % ls["saturated"], "chipwarn"))
            if ls.get("alert_count"):
                chips.append(("アラート %d 回" % ls["alert_count"], "chipwarn"))
            chips.append(("0 V 合わせ %s" % ("済" if st and st["zero"]["calibrated_at"] else "未実施"), "chip"))
            f = None
        if f is not None:
            for ch in (1, 2):
                if not self.on[ch]:
                    continue
                if f.clipped.get(ch):
                    chips.append(("CH%d クリップ(3.35 V)" % ch, "chipwarn"))
                if f.rail.get(ch):
                    chips.append(("CH%d レール到達" % ch, "chipcrit"))
                r = f.ranges.get(ch)
                if r and not r["covers"]:
                    chips.append(("CH%d 画面の一部が測定範囲外" % ch, "chipwarn"))
            if not chips:
                chips.append(("レール・クリップなし", "chip"))
            if f.trig_kind:
                if self.waiting:
                    chips.append(("Normal: トリガ待ち", "chipwarn"))
                elif f.trig_status == "auto_free":
                    chips.append(("Auto: 未成立 → フリーラン", "chipwarn"))
                else:
                    chips.append(("トリガ %s 成立" % ("HW" if f.trig_kind == "hw" else "SW"), "chip"))
        if st:
            for n in st["notes"]:
                chips.append((n, "chipwarn"))
        if chips != getattr(self, "_chip_spec", None):      # 中身が変わったときだけ作り直す(ちらつき防止)
            self._chip_spec = chips
            for c in self._chips:
                c.deleteLater()
            self._chips = []
            for text, obj in chips:
                lb = QLabel(text)
                lb.setObjectName(obj)
                self.status_lay.insertWidget(len(self._chips), lb)
                self._chips.append(lb)
        msg = getattr(self, "_msg", None)
        if msg and datetime.datetime.now().timestamp() < msg[1]:
            self.status_right.setText(msg[0])
        elif self.tabs.currentIndex() == 1:
            fl = (self.log_st or {}).get("file")
            self.status_right.setText(short_path(fl) or "ファイル未作成")
        elif self.tabs.currentIndex() == 2:
            cap = self.la_cap
            self.status_right.setText(("取得 " + cap.acquired_at.replace("T", " ")[:19]) if cap is not None and
                                      cap.acquired_at else "")
        else:
            self.status_right.setText("取得 %d 回%s" % (self.count, " · " + self.last_at.strftime("%H:%M:%S")
                                                         if self.last_at else ""))
