"""osc001-viewer — GUI を起動する。装置を開き、取得ワーカーと外部操作のサーバ(kind=gui)を動かす。

    python3 -m osc001_viewer.app [--device /dev/hidrawN] [--readonly] [--no-server]
"""

import argparse
import os
import sys

from .core.control import Server
from .core.device import Device, DeviceError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="osc001-viewer", description="osc001 のスコープ・ロガー・ロジック画面")
    ap.add_argument("--device", help="/dev/hidrawN(省略時は自動で探す)")
    ap.add_argument("--socket", default=None, help="外部操作のソケット(既定 $XDG_RUNTIME_DIR/osc001.sock)")
    ap.add_argument("--readonly", action="store_true", help="外部からの設定変更を止めた状態で起動する")
    ap.add_argument("--no-server", action="store_true", help="外部操作のソケットを開かない")
    ap.add_argument("--shot", default=None, help=argparse.SUPPRESS)          # 開発用: 画面を撮って終わる
    ap.add_argument("--shot-after", type=float, default=2.5, help=argparse.SUPPRESS)
    ap.add_argument("--shot-script", default=None, help=argparse.SUPPRESS)   # 開発用: 撮る前に実行する Python
    ap.add_argument("--fake", action="store_true", help="装置なしで偽の信号を表示する(画面の確認用)")
    a = ap.parse_args(argv)

    import pyqtgraph as pg
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui.main_window import MainWindow
    from .ui.style import FONT_BODY, QSS
    from .ui.worker import Worker

    pg.setConfigOptions(antialias=True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("osc001-viewer")
    app.setFont(QFont(FONT_BODY, 10))
    app.setStyleSheet(QSS)
    try:
        if a.fake:
            import tempfile
            from .core.fake import FakeScope
            from .core.vertical import ZeroStore
            dev = Device(path="/dev/fake", zero_store=ZeroStore(os.path.join(tempfile.mkdtemp(), "zero.json")),
                         scope_factory=lambda p: FakeScope(p, live=True)).open()
        else:
            dev = Device(path=a.device).open()
    except DeviceError as e:
        if a.shot:
            print("osc001-viewer: %s(%s)" % (e, e.hint), file=sys.stderr)
        else:
            QMessageBox.critical(None, "osc001", "%s\n\n%s" % (e, e.hint or ""))
        return 2
    from .core.logger import Logger
    server = None
    worker = Worker(dev)
    logger = Logger(dev)
    win = MainWindow(dev, worker, logger=logger)
    logger.on_point = lambda p: win.log_point.emit(p)
    logger.on_state = lambda st: win.log_state.emit(st)
    if not a.no_server:
        try:
            server = Server(dev, path=a.socket, kind="gui", readonly=a.readonly, logger=logger,
                            on_op=lambda c, o, p, m: win.ext_op.emit(c, o, p, m)).start()
            win.server = server
            win.lock.setChecked(a.readonly)
        except DeviceError as e:
            QMessageBox.warning(None, "osc001", "外部操作のソケットを開けなかった: %s\n\n%s" % (e, e.hint or ""))
    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())     # 端末の Ctrl+C でも閉じる
    tick = QTimer()
    tick.start(200)                                         # Qt のループ中でも Python がシグナルを受け取れるように
    tick.timeout.connect(lambda: None)
    win.show()
    worker.start()
    if a.shot:
        def shoot():
            if a.shot_script:
                exec(compile(open(a.shot_script, encoding="utf-8").read(), a.shot_script, "exec"),
                     {"win": win, "dev": dev, "worker": worker})
            QTimer.singleShot(int(a.shot_after * 1000), lambda: (win.grab().save(a.shot), app.quit()))
        QTimer.singleShot(300, shoot)
    rc = app.exec()
    if logger.state["running"]:
        logger.stop("osc001-viewer の終了で停止")
    if win.la_worker.isRunning():
        win.la_worker.abort()
        win.la_worker.wait(2000)
    worker.stop()
    if server:
        server.shutdown()
    dev.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
