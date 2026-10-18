"""外部操作の一覧。サーバの受け口・osc001ctl の引数・Python クライアント・ツール定義(JSON Schema)は
すべてここから作る。操作を足すときはここに 1 つ足すだけでよい。

各操作の名前は CLI では kebab-case(read-voltage)、Python とツール定義では snake_case(read_voltage)。
引数の型は JSON Schema の一部(integer / number / boolean / string、enum、array of integer)だけを使う。
"""

from dataclasses import dataclass, field

from . import autoset as _autoset
from . import timebase, trigger, vertical


class OpError(Exception):
    """操作の失敗。code は bad_args / device / locked / unknown_op / unavailable。"""

    def __init__(self, code, message, hint=None):
        Exception.__init__(self, message)
        self.code = code
        self.hint = hint

    def as_dict(self):
        return {"code": self.code, "message": str(self), "hint": self.hint}


@dataclass
class Param:
    name: str
    type: str                       # integer / number / boolean / string / array
    description: str
    default: object = None
    required: bool = False
    enum: list = None
    items: str = None               # array の要素の型
    positional: bool = False        # osc001ctl で位置引数にする(例: set-channel 1、set-trigger ch1)

    def schema(self):
        s = {"type": self.type, "description": self.description}
        if self.type == "object":
            s["additionalProperties"] = True
        if self.enum is not None:
            s["enum"] = list(self.enum)
        if self.items:
            s["items"] = {"type": self.items}
        if self.default is not None:
            s["default"] = self.default
        return s


@dataclass
class Op:
    name: str                       # snake_case
    description: str
    params: list = field(default_factory=list)
    mutating: bool = False          # 設定を変える(「外部からの設定変更を止める」で拒否)
    handler: object = None

    @property
    def cli_name(self):
        return self.name.replace("_", "-")

    def schema(self):
        return {"type": "object",
                "properties": {p.name: p.schema() for p in self.params},
                "required": [p.name for p in self.params if p.required]}


OPS = {}


def op(name, description, params=(), mutating=False):
    def deco(fn):
        OPS[name] = Op(name, description, list(params), mutating, fn)
        return fn
    return deco


# ── 引数の検査 ─────────────────────────────────────────────────────
def _coerce(p, v):
    try:
        if p.type == "integer":
            if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                raise ValueError
            v = int(v)
        elif p.type == "number":
            if isinstance(v, bool):
                raise ValueError
            v = float(v)
        elif p.type == "boolean":
            if isinstance(v, str):
                if v.lower() not in ("true", "false", "1", "0", "yes", "no"):
                    raise ValueError
                v = v.lower() in ("true", "1", "yes")
            elif not isinstance(v, bool):
                raise ValueError
        elif p.type == "string":
            v = str(v)
        elif p.type == "object":
            if isinstance(v, str):
                import json
                v = json.loads(v)
            if not isinstance(v, dict):
                raise ValueError
        elif p.type == "array":
            if isinstance(v, str) and v.strip().startswith("["):     # JSON の配列(例 ["ch1<3.6"])
                import json
                v = json.loads(v)
            elif isinstance(v, str):                                 # カンマ区切り(例 1,2)
                v = [x.strip() for x in v.split(",") if x.strip()]
            v = [str(x) for x in v] if p.items == "string" else [int(x) for x in v]
    except (TypeError, ValueError):
        raise OpError("bad_args", "%s の型が違う(%s が要る): %r" % (p.name, p.type, v))
    if p.enum is not None and v not in p.enum:
        raise OpError("bad_args", "%s=%r は選べない" % (p.name, v), "%s のどれか" % (p.enum,))
    return v


def validate(o, params):
    params = dict(params or {})
    unknown = sorted(set(params) - {p.name for p in o.params})
    if unknown:
        raise OpError("bad_args", "%s に無い引数: %s" % (o.name, ", ".join(unknown)),
                      "使える引数: %s" % (", ".join(p.name for p in o.params) or "なし"))
    out = {}
    for p in o.params:
        if p.name in params and params[p.name] is not None:
            out[p.name] = _coerce(p, params[p.name])
        elif p.required:
            raise OpError("bad_args", "%s には %s が要る" % (o.name, p.name))
        elif p.default is not None:
            out[p.name] = p.default
    return out


def schemas(fmt="json", prefix=""):
    """ツール定義。fmt: json(素の一覧)/ openai(関数呼び出し形式)/ mcp。"""
    out = []
    for o in OPS.values():
        name, desc, sch = prefix + o.name, o.description, o.schema()
        if fmt == "openai":
            out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": sch}})
        elif fmt == "mcp":
            out.append({"name": name, "description": desc, "inputSchema": sch})
        else:
            out.append({"name": name, "cli": o.cli_name, "description": desc, "mutating": o.mutating,
                        "parameters": sch})
    return out


# ── 操作 ───────────────────────────────────────────────────────────
# handler(ctx, **params)。ctx は control.Context(device・サーバの状態)。

@op("status", "接続・設定(縦軸・時間軸・トリガ)・0 V 合わせ・注意をまとめて返す。")
def _status(ctx):
    d = ctx.device.status()
    d["server"] = ctx.info()
    return d


@op("set_channel", "チャンネルの表示を変える。V/div は表示の単位(プローブ ×10 なら 10 倍)。"
    "装置のレンジは画面の範囲を覆う最も細かいものを自動で選ぶ(fixed で固定もできる)。",
    [Param("ch", "integer", "チャンネル", required=True, enum=[1, 2], positional=True),
     Param("vdiv", "number", "表示の V/div(%s × プローブ倍率)" % vertical.VDIV_STEPS),
     Param("pos_div", "number", "0 V の位置。画面中央から上へ何 div(−4 で下端)"),
     Param("probe", "integer", "プローブ倍率", enum=[1, 10]),
     Param("fixed", "string", "レンジ固定。auto = 自動、x1:0..2 / div8:0..2 = ×1・÷8 タップの段",
           enum=["auto", "x1:0", "x1:1", "x1:2", "div8:0", "div8:1", "div8:2"])],
    mutating=True)
def _set_channel(ctx, ch, vdiv=None, pos_div=None, probe=None, fixed=None):
    fx = "keep"
    if fixed is not None:
        fx = None if fixed == "auto" else ((1 if fixed.startswith("x1") else 8), int(fixed[-1]))
    r = ctx.device.set_channel(ch, vdiv=vdiv, pos_div=pos_div, probe=probe, fixed=fx)
    return {"ch": ch, "range": r.as_dict(), "axis": vars(ctx.device.axis[ch]).copy()}


@op("set_timebase", "Time/div を変える。fs は 192 点が 10 div に収まるように決まる。",
    [Param("tdiv", "number", "Time/div(秒)", required=True, enum=timebase.TDIV_STEPS, positional=True)],
    mutating=True)
def _set_timebase(ctx, tdiv):
    fs = ctx.device.set_timebase(tdiv)
    return {"tdiv": tdiv, "fs": fs, "acq_time": timebase.acq_time(fs, ctx.device.deep)}


@op("set_record", "記録長。deep = 211 点(後方 19 点の書き込み時刻が未確認なので取得は 422/fs 待つ)。",
    [Param("deep", "boolean", "true で 211 点、false で 192 点", required=True)],
    mutating=True)
def _set_record(ctx, deep):
    ctx.device.set_record(deep)
    return {"deep": deep}


@op("set_trigger", "トリガ。ch1 と ext はハードウェア、ch2 はソフトウェア(取得した波形から探す)、none はフリーラン。"
    "level は表示の単位。ext のしきい値は固定(立上り ≒1.45 V)で level は使わない。",
    [Param("source", "string", "トリガ源", default="none", enum=list(trigger.SOURCES), positional=True),
     Param("edge", "string", "エッジ", default="rising", enum=list(trigger.EDGES)),
     Param("level", "number", "レベル(V、表示の単位)"),
     Param("mode", "string", "auto = 未成立ならフリーランで取り直す / normal = 成立まで待つ",
           default="auto", enum=list(trigger.MODES))],
    mutating=True)
def _set_trigger(ctx, source, edge, mode, level=None):
    t = ctx.device.set_trigger(source, edge, level, mode)
    t["kind"] = trigger.kind(source)
    if source == "ext":
        t["note"] = trigger.ext_note()
    return t


@op("autoset", "Auto Set。入力を見て V/div・0 V 位置・Time/div・トリガ(振幅のある ch、レベルは波形の中央)を決める。",
    mutating=True)
def _autoset_op(ctx):
    return _autoset.autoset(ctx.device)


@op("capture", "スコープで 1 回取得し、各 ch の統計値(Vpp・最大・最小・平均・RMS・周波数)を返す。"
    "samples=true で時刻と電圧の列も返す(192 点、deep なら 211 点)。Normal で未成立なら triggered=false。",
    [Param("samples", "boolean", "電圧列も返す", default=False),
     Param("timeout", "number", "トリガを待つ秒数", default=2.0)])
def _capture(ctx, samples, timeout):
    f = ctx.device.capture(timeout=timeout)
    if f is None:
        return {"triggered": False, "message": "Normal トリガが %.3g 秒で成立しなかった" % timeout}
    d = f.as_dict(samples=samples)
    d["triggered"] = f.trig_status != "auto_free" if f.trig_kind else None
    return d


@op("read_voltage", "op9 で CH1・CH2 の直流電圧をすぐ読む(装置の設定に依らない、1 回 ≒1 ms)。"
    "tap=8 は ±24 V を 47.9 mV 刻み、tap=1 は −1.9〜+3.1 V を ≒5 mV 刻み。値は表示の単位。",
    [Param("avg", "integer", "平均回数(1〜1000)", default=10),
     Param("tap", "integer", "8 = ±24 V、1 = 細かい方", default=8, enum=[8, 1])])
def _read_voltage(ctx, avg, tap):
    return ctx.device.read_voltage(avg=avg, tap=tap)


@op("calibrate_zero", "0 V 合わせ。**合わせる ch の入力を GND につないだことを人が確かめてから**、"
    "yes=true で呼ぶ。op9 と 6 レンジのスコープで 0 V の値を測り直して保存する。",
    [Param("channels", "array", "合わせる ch", default=[1, 2], items="integer"),
     Param("yes", "boolean", "入力を GND につないだ確認", default=False)],
    mutating=True)
def _calibrate_zero(ctx, channels, yes):
    if not yes:
        raise OpError("bad_args", "0 V 合わせは入力を GND につないでから行う",
                      "入力を GND(G)につないだことを確かめて yes=true で呼ぶ(osc001ctl では --yes)")
    if not channels or any(c not in (1, 2) for c in channels):
        raise OpError("bad_args", "channels は 1・2 の組み合わせ")
    return ctx.device.calibrate_zero(channels=channels)


@op("la_capture", "ロジックアナライザで 1 回取得する(4 ch × 844 点)。edge=auto は即時取得で、channels は空にする"
    "(装置がハングする組み合わせなので拒否する)。記録はトリガ点の少し後(979.4 kHz で 7.3 µs)から始まる。"
    "bits は ch ごとの 0/1 の文字列。",
    [Param("rate", "number", "サンプルレート(Hz)。979400 / 490900 / 200000 か 108700 以下", default=979400.0),
     Param("channels", "array", "トリガを見る ch(1〜4)", default=[], items="integer"),
     Param("edge", "string", "トリガのエッジ", default="auto", enum=["rising", "falling", "auto"]),
     Param("timeout", "number", "トリガを待つ秒数(過ぎたら待機を中止して返す)", default=2.0),
     Param("decode", "object", "デコードの指定。protocol と各設定(ch は 1〜4)。例: "
           "{\"protocol\": \"uart\", \"rx\": 2, \"tx\": 3, \"baud\": 115200, \"format\": \"8N1\", \"invert\": false} / "
           "{\"protocol\": \"i2c\", \"scl\": 1, \"sda\": 2} / "
           "{\"protocol\": \"spi\", \"sck\": 1, \"mosi\": 2, \"miso\": 3, \"cs\": 4, \"mode\": 0, \"cs_active_high\": false} / "
           "{\"protocol\": \"can\", \"rx\": 2, \"bitrate\": 125000, \"invert\": false}"),
     Param("bits", "boolean", "ch ごとの 0/1 の文字列も返す(844 文字 × 4)", default=True)])
def _la_capture(ctx, rate, channels, edge, timeout, bits, decode=None):
    from .decode import PROTOCOLS
    from .decode import decode as run_decode
    if any(c not in (1, 2, 3, 4) for c in channels):
        raise OpError("bad_args", "channels は 1〜4")
    if edge == "auto" and channels:
        raise OpError("bad_args", "edge=auto のときは channels を空にする",
                      "この組み合わせは装置がハングする。エッジを rising / falling にするか channels を空にする")
    if decode is not None and decode.get("protocol") not in PROTOCOLS:
        raise OpError("bad_args", "decode.protocol は %s のどれか" % (PROTOCOLS,))
    cap = ctx.device.la_capture(rate=rate, channels=channels, edge=edge, timeout=timeout)
    out = {"fs": cap.fs, "n": cap.n, "latency_s": cap.lat, "window_s": cap.window(),
           "transitions": {"d%d" % c: cap.transitions(c) for c in (1, 2, 3, 4)},
           "note": "時刻はトリガ点が 0。サンプル i の時刻は latency_s + i/fs(記録はトリガ点の少し後から始まる)"}
    if bits:
        out["bits"] = {"d%d" % c: "".join(str(int(b)) for b in cap.bits[c]) for c in (1, 2, 3, 4)}
    if decode is not None:
        opts = {k: v for k, v in decode.items() if k != "protocol"}
        opts["trigger"] = {"channels": list(channels), "edge": edge}
        try:
            out["decode"] = run_decode(decode["protocol"], cap, opts).as_dict()
        except (TypeError, ValueError, KeyError) as e:
            raise OpError("bad_args", "decode の設定が読めない: %s" % e)
    return out


@op("log_start", "ロガーを始める。op9 で CH1・CH2 を interval 秒ごとに avg 回読み、平均・最小・最大を 1 回の記録 = 1 つの CSV"
    "(~/osc001_logs/log_日時.csv)に書く。スコープ・ロジックと同時に使える。終了は手動・duration・until・max_points。",
    [Param("interval", "number", "記録間隔(秒、0.1 以上)", default=10.0),
     Param("avg", "integer", "1 点あたりの読み取り回数(1 回 ≒1 ms)", default=10, enum=[1, 10, 100]),
     Param("tap", "integer", "8 = ±24 V(47.9 mV 刻み)、1 = −1.9〜+3.1 V(≒5 mV 刻み)", default=8, enum=[8, 1]),
     Param("duration", "string", "この時間で終える(例 600、10m、2h)"),
     Param("until", "string", "この時刻で終える(ISO 8601、例 2026-10-18T18:00)"),
     Param("max_points", "integer", "この点数で終える"),
     Param("alerts", "array", "アラート条件(例 [\"ch1<3.6\", \"ch2>1.4\"])。条件に入った点の event に書く",
           items="string")],
    mutating=True)
def _log_start(ctx, interval, avg, tap, duration=None, until=None, max_points=None, alerts=None):
    return ctx.need_logger().start(interval=interval, avg=avg, tap=tap, duration=duration, until=until,
                                   max_points=max_points, alerts=alerts or [], by=ctx.client)


@op("log_stop", "ロガーを止める(ファイルの最後に # end 行を書く)。", mutating=True)
def _log_stop(ctx):
    return ctx.need_logger().stop()


@op("log_status", "ロガーの状態(記録中か、ファイル、点数、最新の点、アラート回数)。")
def _log_status(ctx):
    return ctx.need_logger().status()


@op("log_get", "記録中・直前の記録の点を返す。last で期間を絞り、max_points を超える分は区間ごとにまとめる"
    "(平均は平均、最小は最小、最大は最大)。",
    [Param("last", "string", "直近の期間(例 10m、1h)。省略で全部"),
     Param("max_points", "integer", "返す最大の点数", default=200)])
def _log_get(ctx, max_points, last=None):
    return ctx.need_logger().get(last=last, max_points=max_points)


@op("log_files", "ログファイルの一覧(新しい順)。CSV は直接読んでもよい。",
    [Param("limit", "integer", "最大件数", default=20)])
def _log_files(ctx, limit):
    from .logger import Logger
    return Logger.files(limit=limit)


@op("marker", "記録中のログにマーカー(注記)を付ける。その場で 1 点取り、その行の event に書く。",
    [Param("text", "string", "注記(例 負荷 ON)", required=True, positional=True)])
def _marker(ctx, text):
    return ctx.need_logger().marker(text, by=ctx.client)


@op("shutdown", "画面なしのサーバ(osc001d)を止める。GUI では使えない。")
def _shutdown(ctx):
    return ctx.request_shutdown()
