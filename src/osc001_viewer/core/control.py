"""外部操作のサーバ: Unix ソケットで 1 行 1 JSON を受け、ops の操作を Device に対して行う。

要求: {"id": 1, "op": "capture", "params": {...}, "client": "local-llm"}
応答: {"id": 1, "ok": true, "result": {...}} / {"id": 1, "ok": false, "error": {"code", "message", "hint"}}

GUI と osc001d の両方がこのサーバを持つ。ソケットは本人だけが開ける権限(0600)にし、
ネットワークには口を開けない。読み取り専用(readonly)のときは mutating な操作を拒否する。
"""

import json
import os
import socket
import socketserver
import threading
import traceback

from .._api import osc001
from . import ops
from .device import DeviceError

MAX_LINE = 1 << 20


def default_socket_path():
    run = os.environ.get("XDG_RUNTIME_DIR")
    if run and os.path.isdir(run):
        return os.path.join(run, "osc001.sock")
    return "/tmp/osc001-%d.sock" % os.getuid()


class Context(object):
    """操作から見えるサーバの状態。"""

    def __init__(self, server):
        self._server = server
        self.device = server.device
        self.logger = server.logger
        self.client = "unknown"

    def need_logger(self):
        if self.logger is None:
            raise ops.OpError("unavailable", "このサーバにはロガーが無い")
        return self.logger

    def info(self):
        s = self._server
        return {"kind": s.kind, "socket": s.path, "readonly": s.readonly, "clients": s.client_count(),
                "last_client": s.last_client}

    def request_shutdown(self):
        if not self._server.allow_shutdown:
            raise ops.OpError("unavailable", "このサーバは止められない(GUI が持っている)", "GUI のウィンドウを閉じる")
        threading.Thread(target=self._server.shutdown, daemon=True).start()
        return {"stopping": True}


def dispatch(server, req):
    """要求 1 つを処理して応答 dict を返す(例外は外へ出さない)。"""
    rid = req.get("id") if isinstance(req, dict) else None
    try:
        if not isinstance(req, dict):
            raise ops.OpError("bad_args", "要求は JSON のオブジェクト")
        name = str(req.get("op", "")).replace("-", "_")
        o = ops.OPS.get(name)
        if o is None:
            raise ops.OpError("unknown_op", "操作 %r は無い" % req.get("op"),
                              "使える操作: %s" % ", ".join(sorted(x.cli_name for x in ops.OPS.values())))
        params = ops.validate(o, req.get("params"))
        if o.mutating and server.readonly:
            raise ops.OpError("locked", "外部からの設定変更は止められている",
                              "GUI の「外部からの設定変更を止める」を外す(読み取りの操作は使える)")
        client = str(req.get("client") or "unknown")[:64]
        server.last_client = client
        ctx = Context(server)
        ctx.client = client
        result = o.handler(ctx, **params)
        if server.on_op:
            server.on_op(client, o.name, params, o.mutating)
        return {"id": rid, "ok": True, "result": result}
    except ops.OpError as e:
        return {"id": rid, "ok": False, "error": e.as_dict()}
    except DeviceError as e:
        return {"id": rid, "ok": False, "error": {"code": "device", "message": str(e), "hint": e.hint}}
    except osc001.Osc001Error as e:
        return {"id": rid, "ok": False, "error": {"code": "device", "message": str(e),
                                                   "hint": "USB の接続を確かめる。続くなら抜き差しして osc001d / GUI を起動し直す"}}
    except Exception as e:                      # noqa: BLE001  想定外も JSON で返す
        return {"id": rid, "ok": False, "error": {"code": "internal", "message": "%s: %s" % (type(e).__name__, e),
                                                   "hint": traceback.format_exc(limit=3)}}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server.owner
        srv._conn(+1)
        try:
            while True:
                line = self.rfile.readline(MAX_LINE)
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except ValueError as e:
                    resp = {"id": None, "ok": False, "error": {"code": "bad_args", "message": "JSON が読めない: %s" % e,
                                                               "hint": "1 行に 1 つの JSON オブジェクトを送る"}}
                else:
                    resp = dispatch(srv, req)
                self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            srv._conn(-1)


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Server(object):
    """kind: "gui" か "daemon"。on_op(client, op, params, mutating) は操作ログ用のコールバック。"""

    def __init__(self, device, path=None, kind="daemon", readonly=False, on_op=None, logger=None):
        self.device = device
        self.logger = logger
        self.path = path or default_socket_path()
        self.kind = kind
        self.readonly = readonly
        self.on_op = on_op
        self.allow_shutdown = kind == "daemon"
        self.last_client = None
        self._clients = 0
        self._lock = threading.Lock()
        self._srv = None
        self._thread = None

    def client_count(self):
        with self._lock:
            return self._clients

    def _conn(self, d):
        with self._lock:
            self._clients += d

    def _take_path(self):
        """ソケットのパスを確保する。他のサーバが動いていれば例外、残骸なら消す。"""
        if os.path.exists(self.path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.path)
            except OSError:
                os.unlink(self.path)              # 前回の残骸
            else:
                raise DeviceError("別のサーバが %s で動いている" % self.path,
                                  "osc001ctl status で確かめ、osc001d なら osc001ctl shutdown で止める")
            finally:
                s.close()

    def start(self):
        self._take_path()
        old = os.umask(0o177)                     # ソケットは 0600
        try:
            self._srv = _UnixServer(self.path, _Handler)
        finally:
            os.umask(old)
        self._srv.owner = self
        self._thread = threading.Thread(target=self._srv.serve_forever, name="osc001-control", daemon=True)
        self._thread.start()
        return self

    def wait(self):
        while self._thread is not None and self._thread.is_alive():
            self._thread.join(0.5)

    def shutdown(self):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        try:
            os.unlink(self.path)
        except OSError:
            pass
