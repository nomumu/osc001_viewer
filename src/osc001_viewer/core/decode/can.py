"""CAN(標準 11 bit / 拡張 29 bit ID)。トランシーバの RXD(ロジックレベル)を 1 本読む。

opts: rx(LA の ch)、bitrate、invert(True で ドミナント = High)。
SOF の立下りで同期し、各ビットの 75 % で読む。リセッシブ → ドミナントの立下りごとに位置を合わせ直す。
SOF〜CRC はビットスタッフィング(同じ値が 5 つ続いた後の逆の 1 ビット)を外し、CRC-15(0x4599)を検査する。
記録の先頭がドミナント(SOF の途中)なら、トリガ点(t = 0)を SOF の始まりとして補う。
"""

from . import MIN_SPB, Result, hex2, num

SAMPLE_POINT = 0.75


def crc15(bits):
    c = 0
    for b in bits:
        n = b ^ ((c >> 14) & 1)
        c = (c << 1) & 0x7FFF
        if n:
            c ^= 0x4599
    return c


class _Reader(object):
    """ビット時間ごとに読む。立下りで位置を合わせ直す。"""

    def __init__(self, cap, v, t_sof, tb):
        self.cap, self.v, self.tb = cap, v, tb
        self.t = t_sof                                     # 今のビットの始まり
        self.assumed = False

    def bit(self):
        cap, tb = self.cap, self.tb
        tp = self.t + SAMPLE_POINT * tb
        j = cap.index(tp)
        if j is None and tp < cap.lat and not self.assumed:
            val = 0                                        # SOF の読み取り位置が記録の前: SOF はドミナント
            self.assumed = True
        elif j is None:
            return None, self.t
        else:
            val = int(self.v[j])
        t0 = self.t
        nxt = self.t + tb
        a, b = cap.index(self.t + 0.5 * tb), cap.index(self.t + 1.5 * tb)
        if a is not None:
            b = min(b if b is not None else cap.n - 1, cap.n - 1)
            for k in range(max(a, 1), b + 1):              # 次のビットの始まりの近くの立下り
                if self.v[k - 1] == 1 and self.v[k] == 0:
                    nxt = cap.time(k - 0.5)
                    break
        self.t = nxt
        return val, t0


def _frame(cap, v, t_sof, tb, r, head_lost):
    rd = _Reader(cap, v, t_sof, tb)
    raw, fields, stuffs = [], [], []                       # raw: スタッフを外したビット
    run_val, run_len, destuff = None, 0, True

    def take(field_name):
        nonlocal run_val, run_len
        while True:
            val, t0 = rd.bit()
            if val is None:
                return None
            if destuff and run_len == 5:                  # この 1 ビットはスタッフ
                if val == run_val:
                    r.notes.append("スタッフ誤り(%.1f µs)" % (t0 * 1e6))
                    r.errors += 1
                stuffs.append(t0)
                run_val, run_len = val, 1
                continue
            if val == run_val:
                run_len += 1
            else:
                run_val, run_len = val, 1
            fields.append((field_name, t0, val))
            return val

    def take_n(name, n):
        x = 0
        for _ in range(n):
            b = take(name)
            if b is None:
                return None
            raw.append(b)
            x = (x << 1) | b
        return x

    if take_n("SOF", 1) is None:
        return None
    ident = take_n("ID", 11)
    rtr = take_n("RTR", 1)
    ide = take_n("IDE", 1)
    if ident is None or rtr is None or ide is None:
        return _cut(r, fields, stuffs, tb)
    ext = None
    if ide:
        ext = take_n("ID", 18)
        rtr = take_n("RTR", 1)
        if take_n("r1", 1) is None:
            return _cut(r, fields, stuffs, tb)
    if take_n("r0", 1) is None:
        return _cut(r, fields, stuffs, tb)
    dlc = take_n("DLC", 4)
    if dlc is None:
        return _cut(r, fields, stuffs, tb)
    data = []
    if not rtr:
        for k in range(min(dlc, 8)):
            b = take_n("D%d" % k, 8)
            if b is None:
                return _cut(r, fields, stuffs, tb)
            data.append(b)
    crc_calc = crc15(raw)
    crc = take_n("CRC", 15)
    if crc is None:
        return _cut(r, fields, stuffs, tb)
    destuff = False
    tail = [take("CRCdel"), take("ACK"), take("ACKdel")]
    for _ in range(7):
        tail.append(take("EOF"))
    full_id = (ident << 18 | ext) if ide else ident
    ack = tail[1] == 0
    ok = crc == crc_calc
    _emit(r, fields, stuffs, tb, full_id, ide, dlc, data, crc, rtr)
    t0 = fields[0][1]
    r.items.append({"t": t0, "text": "ID 0x%X%s  DLC %d  [%s]  CRC 0x%X %s  ACK %s%s" % (
        full_id, "(拡張)" if ide else "", dlc, " ".join(hex2(b) for b in data) if not rtr else "リモート",
        crc, "OK" if ok else "誤り(計算 0x%X)" % crc_calc, "あり" if ack else "なし",
        "  ※ SOF は記録の前。トリガ点から補った" if head_lost else "")})
    r.items.append({"t": t0, "text": "スタッフビット %d 個  フレーム長 %d ビット" % (len(stuffs), len(fields) + len(stuffs))})
    if not ok:
        r.errors += 1
    return rd.t


def _emit(r, fields, stuffs, tb, full_id, ide, dlc, data, crc, rtr):
    labels = {"SOF": "SOF", "ID": "ID %X" % full_id, "RTR": "RTR", "IDE": "IDE", "r0": "r0", "r1": "r1",
              "DLC": "DLC %d" % dlc, "CRC": "CRC %X" % crc, "ACK": "ACK", "EOF": "EOF", "CRCdel": "", "ACKdel": ""}
    for k, b in enumerate(data):
        labels["D%d" % k] = hex2(b)
    seg = {}
    order = []
    bits = {}
    for name, t0, val in fields:
        if name not in seg:
            seg[name] = [t0, t0 + tb]
            order.append(name)
        else:
            seg[name][1] = t0 + tb
        bits.setdefault(name, []).append(int(val))
    one = {k: v[0] for k, v in bits.items()}
    tips = {"SOF": "SOF(フレームの始まり、ドミナント 1 ビット)",
            "ID": "ID 0x%X(%s、%d ビット)" % (full_id, "拡張" if ide else "標準", len(bits.get("ID", []))),
            "RTR": "RTR = %d(%s)" % (one.get("RTR", 0), "リモートフレーム" if rtr else "データフレーム"),
            "IDE": "IDE = %d(%s)" % (one.get("IDE", 0), "拡張 ID" if ide else "標準 ID"),
            "r0": "予約ビット r0 = %d" % one.get("r0", 0), "r1": "予約ビット r1 = %d" % one.get("r1", 0),
            "DLC": "DLC = %d(データ長 %d バイト)" % (dlc, min(dlc, 8)),
            "CRC": "CRC 0x%X(15 ビット)" % crc,
            "ACK": "ACK スロット(ドミナント = どこかのノードが受信を応答)",
            "EOF": "EOF(フレームの終わり、レセッシブ 7 ビット)"}
    for k, b in enumerate(data):
        tips["D%d" % k] = "データ %d バイト目 0x%s(%s)" % (k + 1, hex2(b), num(b))
    for name in order:
        lab = labels.get(name, name)
        if not lab:
            continue
        kind = "addr" if name == "ID" else ("ack" if name == "ACK" else ("data" if name.startswith("D") and
                                                                        name != "DLC" else "ctl"))
        r.ann.append({"row": 0, "t0": seg[name][0], "t1": seg[name][1], "label": lab, "kind": kind,
                      "tip": tips.get(name, lab)})
    for t in stuffs:
        r.ann.append({"row": 1, "t0": t, "t1": t + tb, "label": "s", "kind": "stuff",
                      "tip": "スタッフビット(同じレベルが 5 ビット続いたので挿入された逆レベル)"})


def _cut(r, fields, stuffs, tb):
    r.notes.append("フレームの途中で窓が終わった(%d ビットまで)" % len(fields))
    return None


def decode(cap, o):
    ch = o.get("rx")
    rate = float(o.get("bitrate", 125000))
    inv = bool(o.get("invert", False))
    r = Result("can", ["CAN", "stuff"], bitrate=rate, spb=cap.fs / rate)
    if not ch:
        r.notes.append("RX の ch を選ぶ")
        return r
    if r.spb < MIN_SPB:
        r.notes.append("1 ビットあたり %.1f サンプルしかなく、デコードできない" % r.spb)
        return r
    v = cap.bits[ch] ^ (1 if inv else 0)                   # 論理値(リセッシブ = 1)
    tb = 1.0 / rate
    i, first = 0, True
    while i < cap.n - 1:
        if first and v[0] == 0:
            t_sof, head = 0.0, True
            r.notes.append("SOF は記録の前。トリガ点(t = 0)を SOF の始まりとして補った")
        else:
            while i < cap.n - 1 and not (v[i] == 1 and v[i + 1] == 0):
                i += 1
            if i >= cap.n - 1:
                break
            t_sof, head = cap.time(i + 0.5), False
        first = False
        t_end = _frame(cap, v, t_sof, tb, r, head)
        if t_end is None:
            break
        j = cap.index(t_end + 2 * tb)                      # インターミッション(3 ビット)の後から次を探す
        if j is None:
            break
        i = j
    return r
