"""Python クライアント。GUI か osc001d のソケットにつなぎ、ops の操作をメソッドとして呼ぶ。

    from osc001_viewer.client import Client
    with Client() as c:
        v = c.read_voltage(avg=10)                 # {"ch1": ..., "ch2": ...}
        c.set_trigger(source="ch1", edge="rising", level=2.5)
        f = c.capture(samples=True)

メソッド名は ops の snake_case 名、引数はキーワードで渡す。失敗は ClientError(code / message / hint)。
"""

import json
import os
import socket

from .core import ops
from .core.control import default_socket_path


class ClientError(Exception):
    def __init__(self, code, message, hint=None):
        Exception.__init__(self, message)
        self.code = code
        self.hint = hint

    def as_dict(self):
        return {"code": self.code, "message": str(self), "hint": self.hint}


class Client(object):
    def __init__(self, path=None, client=None, timeout=30.0):
        self.path = path or default_socket_path()
        self.client = client or os.environ.get("OSC001_CLIENT") or "python"
        self.timeout = timeout
        self._sock = None
        self._rfile = None
        self._id = 0

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
        except OSError:
            s.close()
            raise ClientError("no_server", "osc001 のサーバにつながらない(%s)" % self.path,
                              "GUI(osc001-viewer)か osc001d を起動する(osc001ctl daemon start)")
        self._sock = s
        self._rfile = s.makefile("rb")
        return self

    def close(self):
        if self._sock is not None:
            self._rfile.close()
            self._sock.close()
            self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    def call(self, op, **params):
        """操作 op(kebab / snake どちらでも)を呼び、result を返す。"""
        if self._sock is None:
            self.connect()
        self._id += 1
        req = {"id": self._id, "op": op, "params": {k: v for k, v in params.items() if v is not None},
               "client": self.client}
        self._sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        line = self._rfile.readline()
        if not line:
            raise ClientError("no_server", "サーバが接続を閉じた", "GUI / osc001d が止まっていないか確かめる")
        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok"):
            e = resp.get("error") or {}
            raise ClientError(e.get("code", "internal"), e.get("message", "不明なエラー"), e.get("hint"))
        return resp.get("result")

    def __getattr__(self, name):
        if name.startswith("_") or name not in ops.OPS:
            raise AttributeError(name)
        return lambda **params: self.call(name, **params)

    def __dir__(self):
        return sorted(set(object.__dir__(self)) | set(ops.OPS))
