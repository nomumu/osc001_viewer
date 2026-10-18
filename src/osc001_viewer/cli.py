"""osc001ctl — bash から osc001 を操作するコマンド。GUI か osc001d のソケットにつなぐ。

出力は既定で JSON 1 個(jq で扱える)。capture / la-capture は --csv で CSV、--out FILE でファイルに書いて
パスと要約だけを返す。終了コード: 0 成功 / 1 想定外 / 2 装置のエラー / 3 外部操作の禁止中 /
4 引数の誤り / 5 サーバにつながらない。

    osc001ctl read-voltage --avg 10
    osc001ctl set-trigger ch1 --edge rising --level 2.5
    osc001ctl capture --csv > wave.csv
    osc001ctl call read_voltage '{"avg": 10}'       # LLM のツール呼び出しをそのまま渡す
    osc001ctl schema --format openai                # ツール定義
    osc001ctl daemon start                          # GUI を使わないとき
"""

import argparse
import json
import os
import subprocess
import sys
import time

from .client import Client, ClientError
from .core import ops
from .core.control import default_socket_path

EXIT = {"device": 2, "locked": 3, "bad_args": 4, "unknown_op": 4, "no_server": 5, "unavailable": 4}


def _add_op_parser(sub, o):
    p = sub.add_parser(o.cli_name, help=o.description.split("。")[0], description=o.description)
    for prm in o.params:
        kw = {"help": prm.description}
        if prm.type == "boolean":
            p.add_argument("--" + prm.name.replace("_", "-"), dest=prm.name, action=argparse.BooleanOptionalAction,
                           default=None, required=prm.required, **kw)
            continue
        if prm.enum is not None and prm.type == "string":
            kw["choices"] = prm.enum
        if prm.positional:
            p.add_argument(prm.name, nargs=None if prm.required else "?", default=None, **kw)
        else:
            p.add_argument("--" + prm.name.replace("_", "-"), dest=prm.name, default=None, required=prm.required,
                           **kw)
    p.set_defaults(_op=o)
    return p


def build_parser():
    ap = argparse.ArgumentParser(prog="osc001ctl", description="osc001 を bash から操作する(GUI か osc001d が必要)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--socket", default=None, help="ソケットのパス(既定 %s)" % default_socket_path())
    ap.add_argument("--client", default=None, help="操作ログに出す名前(既定 osc001ctl、環境変数 OSC001_CLIENT)")
    ap.add_argument("--pretty", action="store_true", help="JSON を字下げして出す")
    ap.add_argument("--csv", action="store_true", help="capture / la-capture / log-get を CSV で出す")
    ap.add_argument("--out", default=None, help="結果をファイルに書き、パスと要約だけを出す")
    sub = ap.add_subparsers(dest="cmd", metavar="コマンド")
    for o in ops.OPS.values():
        _add_op_parser(sub, o)
    p = sub.add_parser("call", help="操作名と JSON の引数で呼ぶ(LLM のツール呼び出しの橋渡し)")
    p.add_argument("op")
    p.add_argument("params", nargs="?", default="{}", help="JSON オブジェクト(省略時 {})")
    p = sub.add_parser("schema", help="全操作のツール定義(JSON Schema)を出す。サーバは不要")
    p.add_argument("--format", default="json", choices=["json", "openai", "mcp"])
    p.add_argument("--prefix", default="", help="ツール名の前に付ける文字列(例 osc001_)")
    p = sub.add_parser("daemon", help="画面なしのサーバ osc001d を起動 / 停止する")
    p.add_argument("action", choices=["start", "stop"])
    p.add_argument("--device", default=None)
    return ap


def _emit(obj, a, stream=None):
    stream = stream or sys.stdout
    stream.write(json.dumps(obj, ensure_ascii=False, indent=2 if a.pretty else None) + "\n")


def capture_csv(res):
    lines = ["# osc001 capture",
             "# acquired_at: %s" % res.get("acquired_at"),
             "# fs: %s" % res.get("fs"),
             "# trigger: %s" % json.dumps(res.get("trigger"), ensure_ascii=False),
             "# first_valid: %s(これより前の行は無効。ハードトリガのサンプル 0 は 2〜3 コード低く出る)"
             % res.get("first_valid", 0)]
    for ch in (1, 2):
        r = res["ch%d" % ch].get("range") or {}
        lines.append("# ch%d: range %s, rail %s, clipped %s" % (ch, r.get("label"), res["ch%d" % ch].get("rail"),
                                                               res["ch%d" % ch].get("clipped")))
    lines.append("t,ch1,ch2")
    for t, a, b in zip(res["t"], res["ch1"]["volts"], res["ch2"]["volts"]):
        lines.append("%.9g,%.5f,%.5f" % (t, a, b))
    return "\n".join(lines) + "\n"


def la_csv(res):
    fs = res["fs"]
    lines = ["# osc001 la-capture", "# fs: %s" % fs, "t,d1,d2,d3,d4"]
    bits = [res["bits"]["d%d" % c] for c in (1, 2, 3, 4)]
    for i in range(res["n"]):
        lines.append("%.9g,%s" % (i / fs, ",".join(b[i] for b in bits)))
    return "\n".join(lines) + "\n"


def log_csv(res):
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write("# osc001 log-get(%s、%d 点中 %d 点)\n" % (res.get("file"), res["points_total"], res["points"]))
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(res["columns"])
    for r in res["rows"]:
        w.writerow([r.get(c, "") for c in res["columns"]])
    return buf.getvalue()


def _summary(cmd, res):
    if cmd == "capture" and "ch1" in res:
        return {"fs": res["fs"], "n": res["n"], "trigger": res["trigger"],
                "ch1": res["ch1"]["stats"], "ch2": res["ch2"]["stats"]}
    if cmd == "la-capture":
        return {"fs": res["fs"], "n": res["n"], "transitions": res["transitions"]}
    if cmd == "log-get":
        return {"file": res["file"], "points": res["points"], "points_total": res["points_total"]}
    return None


def _daemon(a):
    path = a.socket or default_socket_path()
    if a.action == "stop":
        with Client(path, client="osc001ctl") as c:
            return c.call("shutdown")
    try:
        with Client(path, client="osc001ctl") as c:
            return {"already_running": True, "server": c.call("status")["server"]}
    except ClientError:
        pass
    logdir = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "osc001_viewer")
    os.makedirs(logdir, exist_ok=True)
    log = os.path.join(logdir, "osc001d.log")
    cmd = [sys.executable, "-m", "osc001_viewer.daemon", "--socket", path]
    if a.device:
        cmd += ["--device", a.device]
    env = dict(os.environ)
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with open(log, "ab") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    for _ in range(50):
        time.sleep(0.1)
        if proc.poll() is not None:
            raise ClientError("device", "osc001d が起動直後に終了した", "ログを見る: %s" % log)
        try:
            with Client(path, client="osc001ctl") as c:
                return {"started": True, "pid": proc.pid, "log": log, "server": c.call("status")["server"]}
        except ClientError:
            continue
    raise ClientError("no_server", "osc001d の起動を待ちきれなかった", "ログを見る: %s" % log)


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 4
    try:
        if a.cmd == "schema":
            _emit(ops.schemas(a.format, a.prefix), a)
            return 0
        if a.cmd == "daemon":
            _emit(_daemon(a), a)
            return 0
        client = Client(a.socket, client=a.client or os.environ.get("OSC001_CLIENT") or "osc001ctl")
        if a.cmd == "call":
            try:
                params = json.loads(a.params)
            except ValueError as e:
                raise ClientError("bad_args", "引数の JSON が読めない: %s" % e, "例: '{\"avg\": 10}'")
            if not isinstance(params, dict):
                raise ClientError("bad_args", "引数は JSON のオブジェクト")
            name, cmd = a.op, a.op.replace("_", "-")
        else:
            o = a._op
            params = {p.name: getattr(a, p.name) for p in o.params if getattr(a, p.name, None) is not None}
            name, cmd = o.name, o.cli_name
        if cmd == "capture" and (a.csv or a.out):
            params.setdefault("samples", True)
        with client:
            res = client.call(name, **params)
        text = None
        if a.csv and cmd == "capture" and "t" in res:
            text = capture_csv(res)
        elif a.csv and cmd == "la-capture":
            text = la_csv(res)
        elif a.csv and cmd == "log-get":
            text = log_csv(res)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(text if text is not None else json.dumps(res, ensure_ascii=False) + "\n")
            out = {"file": os.path.abspath(a.out)}
            s = _summary(cmd, res)
            if s:
                out["summary"] = s
            _emit(out, a)
        elif text is not None:
            sys.stdout.write(text)
        else:
            _emit(res, a)
        return 0
    except ClientError as e:
        _emit({"error": e.as_dict()}, a)
        sys.stderr.write("osc001ctl: %s%s\n" % (e, "(%s)" % e.hint if e.hint else ""))
        return EXIT.get(e.code, 1)


if __name__ == "__main__":
    sys.exit(main())
