"""UART(8N1 / 8E1 / 8O1、LSB 先)。RX・TX の 2 本を別々に復号する(片方だけでもよい)。

opts: rx, tx(LA の ch 1〜4 か None)、baud、format("8N1" / "8E1" / "8O1")、invert(True でアイドル Low)。
スタートビットの立下り(反転なら立上り)で同期し、各ビットの中央で読む。記録の先頭で線がアイドルでない
(スタートビットの途中)ときは、トリガ点(t = 0)をスタートビットの始まりとして補う。

送り手のボーレートは数 % ずれていることがある(16 MHz の AVR で 115200 bps は実際 117,647 bps、+2.1 %)。
1 ビットのサンプル数が少ないと、このずれがバイトの後ろで効いて読む位置が隣のビットにかかるので、
線のパルス幅からビット幅を実測し、指定値から ±6 % 以内なら実測値で読む(notes に書く)。
アイドルの無い連続送信は区切りが一意に決まらないことがあり、そのときも notes に書く。
"""

import numpy as np

from . import MIN_SPB, Result, asc, hex2

BAUD_TOL = 0.06


def measure_baud(v, fs, baud):
    """パルス幅(同じ値が続くサンプル数)から実際のボーレートを推定する。推定できなければ None。"""
    d = np.nonzero(v[1:] != v[:-1])[0]
    if len(d) < 4:
        return None
    runs = np.diff(d).astype(float)                 # 両端の不完全なパルスは除く
    spb = fs / baud
    k = np.round(runs / spb)
    ok = (k >= 1) & (k <= 9)
    if ok.sum() < 3:
        return None
    est = fs * k[ok].sum() / runs[ok].sum()
    return est if abs(est / baud - 1) <= BAUD_TOL else None


def _line(cap, ch, name, row, baud, fmt, inv, r):
    v = cap.bits[ch] ^ (1 if inv else 0)                 # 論理値(アイドル = 1)
    est = measure_baud(v, cap.fs, baud)
    if est and abs(est / baud - 1) >= 0.005:
        r.notes.append("%s: パルス幅から実測したボーレート %.0f bps(指定 %.0f から %+.1f %%)で読んだ"
                       % (name, est, baud, (est / baud - 1) * 100))
        baud = est
    _frames(cap, v, name, row, baud, fmt, r)
    _check_ambiguity(cap, v, name, baud, fmt, r)


def _check_ambiguity(cap, v, name, baud, fmt, r):
    """アイドルが無く、別の区切り方でもフレーミング誤りなく読めるなら注意を出す。"""
    tb = 1.0 / baud
    nbits = 9 if fmt in ("8E1", "8O1") else 8
    frame = (nbits + 2) * tb
    idle = int(round(frame * cap.fs))
    ones = np.convolve(v.astype(int), np.ones(min(idle, cap.n)), mode="valid")
    if len(ones) and ones.max() >= idle:                # 1 フレーム以上のアイドルがあれば区切りは決まる
        return
    alt = 0
    falls = np.nonzero((v[:-1] == 1) & (v[1:] == 0))[0][:12]
    seqs = set()
    for f in falls:
        if cap.time(f) > frame:
            break
        probe = Result("uart", ["x"])
        _frames(cap, v, name, 0, baud, fmt, probe, start=int(f))
        if probe.items and probe.errors == 0:
            seqs.add(tuple(x["text"] for x in probe.items[:4]))
    if len(seqs) > 1:
        r.notes.append("%s: アイドルの無い連続送信で、バイトの区切りが一意に決まらない(別の区切りでも誤りなく読める)。"
                       "通信の始まりの前にアイドルがある所でトリガする" % name)


def _frames(cap, v, name, row, baud, fmt, r, start=None):
    n, fs, tb = cap.n, cap.fs, 1.0 / baud
    nbits = 9 if fmt in ("8E1", "8O1") else 8
    i = 0 if start is None else start
    first = start is None
    while i < n:
        if first and v[0] == 0:
            ts = 0.0                                       # スタートビットは記録の前に始まっていた
            r.notes.append("%s: 先頭のスタートビットは記録の前。トリガ点(t = 0)から補った" % name)
        else:
            while i < n - 1 and not (v[i] == 1 and v[i + 1] == 0):
                i += 1
            if i >= n - 1:
                break
            ts = cap.time(i + 0.5)                          # 立下りは i と i+1 の間
        first = False
        vals = []
        cut = False
        for k in range(1, nbits + 2):                      # データ(+パリティ)+ ストップ
            j = cap.index(ts + (k + 0.5) * tb)
            if j is None:
                cut = True
                break
            vals.append(int(v[j]))
        if cut:
            if len(vals) >= 1:
                r.notes.append("%s: 最後のバイトは窓の外へはみ出した" % name)
            break
        b = sum(vals[k] << k for k in range(8))
        err = []
        if nbits == 9:
            ones = sum(vals[:8]) + vals[8]
            if (fmt == "8E1" and ones % 2) or (fmt == "8O1" and ones % 2 == 0):
                err.append("パリティ誤り")
        if vals[-1] != 1:
            err.append("フレーミング誤り")
        t1 = ts + (nbits + 2) * tb
        r.ann.append({"row": row, "t0": ts, "t1": t1, "label": hex2(b), "kind": "err" if err else
                      ("addr" if name == "TX" else "data")})
        r.items.append({"t": ts, "text": "%s  0x%s  %s%s" % (name, hex2(b), asc(b), ("  " + "・".join(err)) if err else
                                                          ("  パリティ OK" if nbits == 9 else ""))})
        r.errors += 1 if err else 0
        nxt = cap.index(ts + (nbits + 1.5) * tb)          # ストップビットの中央から次を探す
        if nxt is None:
            break
        i = nxt


def decode(cap, o):
    baud = float(o.get("baud", 115200))
    fmt = o.get("format", "8N1")
    inv = bool(o.get("invert", False))
    lines = [(o.get("tx"), "TX"), (o.get("rx"), "RX")]
    lines = [(ch, nm) for ch, nm in lines if ch]
    if o.get("tx") and o.get("tx") == o.get("rx"):
        lines = [(o["rx"], "RX")]
    r = Result("uart", [nm for _, nm in lines], bitrate=baud, spb=cap.fs / baud)
    if not lines:
        r.notes.append("RX か TX の ch を選ぶ")
        return r
    if r.spb < MIN_SPB:
        r.notes.append("1 ビットあたり %.1f サンプルしかなく、デコードできない" % r.spb)
        return r
    for row, (ch, nm) in enumerate(lines):
        _line(cap, ch, nm, row, baud, fmt, inv, r)
    r.items.sort(key=lambda x: x["t"])
    return r
