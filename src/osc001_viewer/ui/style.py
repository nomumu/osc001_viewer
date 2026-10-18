"""配色・フォント・Qt スタイルシート(設計案 v0.6 の機器パネルに合わせる)。"""

C = {
    "bezel": "#1b232d", "bezel2": "#242e3a", "line": "#33414f", "scr": "#0b141d", "grid": "#1c2b3a",
    "axis": "#2f4559", "fg": "#c9d4df", "muted": "#7f8fa0", "hatch": "#e5785033",
    "ch1": "#f0d04a", "ch2": "#4cc9e0", "la3": "#e57fb6", "la4": "#9bd36a", "cur": "#c7a8ff", "ext": "#a99bff",
    "ok": "#5fd394", "warn": "#f2b06a", "crit": "#ff8d84", "run": "#2f8f5b", "stop": "#b5443a",
}
CH = {1: C["ch1"], 2: C["ch2"]}
FONT_BODY = "Noto Sans CJK JP"
FONT_MONO = "Noto Sans Mono"

QSS = """
QWidget {{ background: {bezel}; color: {fg}; font-family: "{body}"; font-size: 12px; }}
QToolTip {{ background: {bezel2}; color: {fg}; border: 1px solid {line}; }}
#topbar {{ background: {bezel2}; border-bottom: 1px solid {line}; }}
#side {{ border-left: 1px solid {line}; }}
QScrollArea {{ border: 0; }}
QFrame#card {{ background: {bezel2}; border: 1px solid {line}; border-radius: 9px; }}
QLabel#cardtitle {{ color: {muted}; font-size: 11px; font-weight: 600; letter-spacing: 1px; background: transparent; }}
QLabel {{ background: transparent; }}
QLabel#kvkey {{ color: {muted}; font-family: "{mono}"; font-size: 11px; }}
QLabel#kvval {{ font-family: "{mono}"; font-size: 11px; }}
QLabel#note {{ color: {muted}; font-size: 11px; }}
QLabel#warn {{ color: {warn}; font-size: 11px; }}
QLabel#head {{ font-family: "{mono}"; font-size: 12px; color: {muted}; }}
QLabel#chip {{ font-family: "{mono}"; font-size: 11px; color: {muted}; border: 1px solid {line}; border-radius: 9px; padding: 1px 8px; }}
QLabel#chipwarn {{ font-family: "{mono}"; font-size: 11px; color: {warn}; border: 1px solid #6b4a24; border-radius: 9px; padding: 1px 8px; }}
QLabel#chipcrit {{ font-family: "{mono}"; font-size: 11px; color: {crit}; border: 1px solid #6b2a24; border-radius: 9px; padding: 1px 8px; }}
QLabel#extchip {{ font-family: "{mono}"; font-size: 11px; color: {ext}; border: 1px solid #4a4380; border-radius: 9px; padding: 2px 10px; }}
QPushButton {{ font-weight: 600; border: 1px solid {line}; background: {bezel}; padding: 5px 12px; border-radius: 7px; }}
QPushButton:hover {{ border-color: #4a5d70; }}
QPushButton:disabled {{ color: #556270; }}
QPushButton#run {{ background: {run}; border-color: {run}; color: #fff; }}
QPushButton#stop {{ background: {stop}; border-color: {stop}; color: #fff; }}
QPushButton:checked {{ background: {line}; color: #fff; }}
QPushButton#seg {{ font-weight: 400; padding: 3px 9px; border-radius: 0; background: {scr}; color: {muted}; }}
QPushButton#seg:checked {{ background: {line}; color: #fff; }}
QPushButton#step {{ padding: 2px 8px; }}
QComboBox {{ background: {scr}; border: 1px solid {line}; border-radius: 6px; padding: 3px 8px; }}
QComboBox QAbstractItemView {{ background: {bezel2}; selection-background-color: {line}; }}
QCheckBox {{ background: transparent; }}
QTabBar::tab {{ background: {scr}; color: {muted}; padding: 4px 14px; border: 0; }}
QTabBar::tab:selected {{ background: {line}; color: #fff; border-radius: 6px; }}
QLineEdit {{ background: {scr}; border: 1px solid {line}; border-radius: 6px; padding: 4px 8px; font-family: "{mono}"; }}
QDialog {{ background: {bezel2}; }}
QTableWidget {{ background: {scr}; gridline-color: {line}; font-family: "{mono}"; font-size: 11px; }}
QHeaderView::section {{ background: {bezel2}; color: {muted}; border: 0; padding: 3px; }}
""".format(body=FONT_BODY, mono=FONT_MONO, **C)
