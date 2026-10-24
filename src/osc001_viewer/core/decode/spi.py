"""SPI(MSB 先、8 ビット)。CS が有効な間、取り込みエッジで MOSI・MISO を読む。

opts: sck, mosi, miso, cs(LA の ch。mosi・miso・cs は省略可)、mode(0〜3)、cs_active_high。
取り込みエッジ: CPHA = 0 なら先のエッジ(CPOL 0 で立上り、1 で立下り)、CPHA = 1 なら後のエッジ。
CS が記録の先頭ですでに有効なら、その区間は記録の前に始まっている(先頭のビットが欠けていることがある)。
その区間が記録の中で終わるなら、バイトの区切りは CS の終わりから合わせる(欠けたバイトの残りは飛ばす)。
CS が無効な間の SCLK のレベル(アイドル)が設定の CPOL と違えば注意する。モード 0 と 3(1 と 2)は
読むエッジが同じなので、デコード結果からは食い違いに気づけないため。
"""

import numpy as np

from . import MIN_SPB, Result, hex2, num


def decode(cap, o):
    sck_ch, mosi_ch, miso_ch, cs_ch = o.get("sck"), o.get("mosi"), o.get("miso"), o.get("cs")
    mode = int(o.get("mode", 0))
    cpol, cpha = mode >> 1, mode & 1
    rows = [x for x, ch in (("MOSI", mosi_ch), ("MISO", miso_ch)) if ch]
    r = Result("spi", rows)
    if not sck_ch or not rows:
        r.notes.append("SCLK と、MOSI・MISO の少なくとも一方を選ぶ")
        return r
    sck = cap.bits[sck_ch].astype(int)
    rising = (sck[:-1] == 0) & (sck[1:] == 1)
    falling = (sck[:-1] == 1) & (sck[1:] == 0)
    sample_rise = (cpol == 0) == (cpha == 0)              # mode 0 / 3 は立上り、1 / 2 は立下り
    edges = np.nonzero(rising if sample_rise else falling)[0] + 1
    if len(edges) >= 2:
        period = float(np.median(np.diff(edges))) / cap.fs
        r.bitrate, r.spb = 1.0 / period, cap.fs * period
        if r.spb < MIN_SPB:
            r.notes.append("SCLK 1 周期あたり %.1f サンプルしかなく、デコードできない" % r.spb)
            return r
    skip = 0
    if cs_ch:
        act = cap.bits[cs_ch].astype(int) if o.get("cs_active_high") else 1 - cap.bits[cs_ch].astype(int)
        if act[0]:
            ends = np.nonzero(act == 0)[0]
            if len(ends):
                skip = int(np.count_nonzero(edges < ends[0])) % 8
            if skip:
                r.notes.append("CS は記録の前から有効。最初のバイトは %d ビットしか記録に無いので飛ばし、"
                               "バイトの区切りは CS の終わりから合わせた" % skip)
            elif not len(ends):
                r.notes.append("CS は記録の前から有効。最初のバイトの先頭ビットが欠けていることがある(遅れの短いレートを使う)")
            else:
                r.notes.append("CS は記録の前から有効。バイトの区切りは CS の終わりから合わせた")
        idle = sck[act == 0]
        if len(idle) >= 8:
            level = int(round(float(idle.mean())))
            if level != cpol:
                same = {0: 3, 3: 0, 1: 2, 2: 1}[mode]
                r.notes.append("CS が無効な間の SCLK は %s(CPOL = %d)。設定のモード %d は CPOL = %d。"
                               "読むエッジが同じモード %d ではないか確かめる"
                               % ("High" if level else "Low", level, mode, cpol, same))
    else:
        act = np.ones(cap.n, dtype=int)
    half = 0.5 / r.bitrate if r.bitrate else 0.0
    eset = set(int(x) for x in edges)
    data = {"MOSI": cap.bits[mosi_ch] if mosi_ch else None, "MISO": cap.bits[miso_ch] if miso_ch else None}
    acc = {k: 0 for k in data}
    nb, t0, prev_act = 0, None, act[0]
    nbyte = None if act[0] else 0                          # CS 区間の何バイト目か(区間の始まりが記録の前なら不明)
    for i in range(cap.n):
        if act[i] != prev_act:
            if not act[i] and nb:                          # CS が切れた: 端数のビットは捨てる
                r.notes.append("CS の区間が 8 ビットの倍数で終わらなかった(%d ビット余り)" % nb)
            nb, nbyte, prev_act = 0, 0, act[i]
            continue
        if not act[i] or i not in eset:
            continue
        if skip:                                           # 記録の前に始まったバイトの残り
            skip -= 1
            continue
        if nb == 0:
            t0 = cap.time(i) - half
        for k, d in data.items():
            if d is not None:
                acc[k] = ((acc[k] << 1) | int(d[i])) & 0xFF
        nb += 1
        if nb == 8:
            t1 = cap.time(i) + half
            vals = []
            if nbyte is not None:
                nbyte += 1
            where = "CS 区間の %d バイト目" % nbyte if nbyte else "CS 区間の始まりは記録の前"
            for row, k in enumerate(rows):
                r.ann.append({"row": row, "t0": t0, "t1": t1, "label": hex2(acc[k]),
                              "kind": "data" if k == "MOSI" else "addr",
                              "tip": "%s 0x%s(%s)\n%s、モード %d、MSB 先" % (k, hex2(acc[k]), num(acc[k]), where, mode)})
                vals.append("%s 0x%s" % (k, hex2(acc[k])))
            r.items.append({"t": t0, "text": "  /  ".join(vals)})
            nb = 0
    if nb:
        r.notes.append("最後のバイトの途中で窓が終わった")
    return r

