"""LA のデコーダ(UART・I2C・SPI・CAN)。どれも core.la.Capture と設定の dict から復号する。

返り値は Result: 帯の注釈(ann)・一覧(items)・注意(notes)。時刻はトリガ点を 0 とした秒。
opts["trigger"] = {"channels": [...], "edge": "rising" / "falling" / "auto"} に LA トリガの設定を渡すと、
記録の前に埋もれた通信の始まりを補う手がかりに使う(読むだけ)。
LA トリガは利用者が手動で決めるので、デコーダはトリガを書き換えない。通信の始まり(スタートビット・
START・SOF・CS)が記録の先頭より前にあるときは、トリガ点(t = 0)で始まったものとして補い、notes に書く。
"""

from dataclasses import dataclass, field


@dataclass
class Result:
    protocol: str
    rows: list                                   # 帯の行の名前
    ann: list = field(default_factory=list)      # {"row", "t0", "t1", "label", "kind"[, "tip"]}。kind: data/addr/ack/nack/ctl/stuff/err
                                                 # tip は帯のツールチップ用の説明(無ければ label を出す)
    items: list = field(default_factory=list)    # {"t", "text"}(一覧)
    notes: list = field(default_factory=list)
    errors: int = 0
    bitrate: float = None                        # 1 秒あたりのビット数(推定値のこともある)
    spb: float = None                            # 1 ビットあたりのサンプル数

    def as_dict(self):
        return {"protocol": self.protocol, "rows": self.rows, "items": self.items, "annotations": self.ann,
                "notes": self.notes, "errors": self.errors, "bitrate": self.bitrate,
                "samples_per_bit": round(self.spb, 2) if self.spb else None}


PROTOCOLS = ("uart", "i2c", "spi", "can")
MIN_SPB = 2.0                                    # これ未満はデコードしない
GOOD_SPB = 4.0                                   # これ未満は取りこぼすことがある


def decode(protocol, cap, opts):
    from . import can, i2c, spi, uart
    fn = {"uart": uart.decode, "i2c": i2c.decode, "spi": spi.decode, "can": can.decode}.get(protocol)
    if fn is None:
        raise ValueError("protocol は %s のどれか" % (PROTOCOLS,))
    r = fn(cap, dict(opts or {}))
    if r.spb is not None and r.spb < GOOD_SPB and r.spb >= MIN_SPB:
        r.notes.append("1 ビットあたり %.1f サンプルで、取りこぼすことがある(4 以上を推奨)" % r.spb)
    return r


def hex2(b):
    return "%02X" % b


CTRL = ("NUL SOH STX ETX EOT ENQ ACK BEL BS HT LF VT FF CR SO SI "
        "DLE DC1 DC2 DC3 DC4 NAK SYN ETB CAN EM SUB ESC FS GS RS US").split()


def num(b):
    """ツールチップ用: 2 進(4 ビットずつ)・10 進・ASCII。"""
    s = "%08d" % int(bin(b)[2:])
    return "2進:%s %s、10進:%d、ASCII:%s" % (s[:4], s[4:], b, "範囲外" if b >= 128 else asc(b))


def asc(b):
    """ASCII としての読み。印字できない文字も記号 1 つにせず、名前で書く。"""
    if 32 <= b < 127:
        return "'%s'" % chr(b)
    if b in (9, 10, 13):
        return {9: "\\t(TAB)", 10: "\\n(LF)", 13: "\\r(CR)"}[b]
    if b < 32 or b == 127:
        return "制御文字 %s" % (CTRL[b] if b < 32 else "DEL")
    return "ASCII 外"
