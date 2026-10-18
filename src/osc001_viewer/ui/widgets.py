"""右パネルの部品: カード・セグメントボタン・ステッパー・キーと値の表。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)


class Card(QFrame):
    def __init__(self, title, parent=None, swatch=None):
        QFrame.__init__(self, parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(10, 8, 10, 9)
        self.lay.setSpacing(6)
        self.head = QHBoxLayout()
        self.head.setSpacing(6)
        if swatch:
            sw = QLabel()
            sw.setFixedSize(9, 9)
            sw.setStyleSheet("background:%s;border-radius:2px;" % swatch)
            self.head.addWidget(sw)
        t = QLabel(title)
        t.setObjectName("cardtitle")
        self.head.addWidget(t)
        self.head.addStretch(1)
        self.lay.addLayout(self.head)

    def add(self, w):
        if isinstance(w, QWidget):
            self.lay.addWidget(w)
        else:
            self.lay.addLayout(w)
        return w


def row(label, widget):
    h = QHBoxLayout()
    h.setSpacing(8)
    lb = QLabel(label)
    lb.setObjectName("note")
    h.addWidget(lb)
    h.addStretch(1)
    h.addWidget(widget)
    return h


def note(text="", warn=False):
    lb = QLabel(text)
    lb.setObjectName("warn" if warn else "note")
    lb.setWordWrap(True)
    return lb


class Seg(QWidget):
    """排他のボタン列。changed(value) を出す。"""
    changed = Signal(object)

    def __init__(self, items, parent=None):
        QWidget.__init__(self, parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.btn = {}
        for value, text in items:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=value: self.changed.emit(v))
            self.group.addButton(b)
            h.addWidget(b)
            self.btn[value] = b

    def set(self, value):
        b = self.btn.get(value)
        if b is not None and not b.isChecked():
            b.setChecked(True)


class Stepper(QWidget):
    """◀ 値 ▶。step(+1 / −1)を出す(+1 = 値を小さくする向き = 右ボタン)。"""
    step = Signal(int)

    def __init__(self, parent=None, width=74):
        QWidget.__init__(self, parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.dn = QPushButton("◀")
        self.up = QPushButton("▶")
        for b in (self.dn, self.up):
            b.setObjectName("step")
            b.setCursor(Qt.PointingHandCursor)
        self.out = QLabel("")
        self.out.setAlignment(Qt.AlignCenter)
        self.out.setMinimumWidth(width)
        self.out.setStyleSheet("font-weight:600;font-size:14px;")
        h.addWidget(self.dn)
        h.addWidget(self.out)
        h.addWidget(self.up)
        self.dn.clicked.connect(lambda: self.step.emit(-1))
        self.up.clicked.connect(lambda: self.step.emit(+1))

    def set_text(self, t):
        self.out.setText(t)


class KV(QWidget):
    """キーと値の 2 列。set(key, value) で値を書き換える。"""

    def __init__(self, keys, parent=None):
        QWidget.__init__(self, parent)
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(1)
        self.val = {}
        for r, k in enumerate(keys):
            a = QLabel(k)
            a.setObjectName("kvkey")
            b = QLabel("—")
            b.setObjectName("kvval")
            b.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            g.addWidget(a, r, 0)
            g.addWidget(b, r, 1)
            self.val[k] = b

    def set(self, key, text, color=None):
        lb = self.val[key]
        lb.setText(text)
        lb.setStyleSheet("color:%s;" % color if color else "")
