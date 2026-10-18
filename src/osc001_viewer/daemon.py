"""osc001d — 画面なしで装置を持ち、外部操作のソケットを開くサーバ。

GUI を起動していないときに、LLM やスクリプトから装置を使うためのもの。GUI と同時には動かせない
(装置を開けるのは 1 プロセスだけ)。止めるのは Ctrl+C か `osc001ctl shutdown`。
"""

import argparse
import signal
import sys

from .core.control import Server, default_socket_path
from .core.device import Device, DeviceError
from .core.logger import Logger


def main(argv=None):
    ap = argparse.ArgumentParser(prog="osc001d", description=__doc__.strip().splitlines()[0])
    ap.add_argument("--device", help="/dev/hidrawN(省略時は自動で探す)")
    ap.add_argument("--socket", default=None, help="ソケットのパス(既定 %s)" % default_socket_path())
    ap.add_argument("--readonly", action="store_true", help="設定を変える操作を拒否する")
    a = ap.parse_args(argv)
    try:
        dev = Device(path=a.device).open()
    except DeviceError as e:
        print("osc001d: %s(%s)" % (e, e.hint), file=sys.stderr)
        return 2
    try:
        logger = Logger(dev)
        srv = Server(dev, path=a.socket, kind="daemon", readonly=a.readonly, logger=logger).start()
    except DeviceError as e:
        dev.close()
        print("osc001d: %s(%s)" % (e, e.hint), file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: srv.shutdown())
    print("osc001d: %s を開き、%s で待ち受け中%s" % (dev.path, srv.path, "(読み取り専用)" if a.readonly else ""),
          file=sys.stderr, flush=True)
    for n in dev.notes:
        print("osc001d: 注意: %s" % n, file=sys.stderr)
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        if logger.state["running"]:
            logger.stop("osc001d の終了で停止")
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
