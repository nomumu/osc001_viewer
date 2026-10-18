"""I2C(7 bit アドレス)。SCL の立上りで SDA を読み、9 ビット(8 + ACK)ごとに 1 バイト。

opts: scl, sda(LA の ch)。START = SCL が High の間の SDA の立下り、STOP = 同じく立上り。
START の直後のバイトはアドレス + R/W。記録の先頭が START の直後とみなせるとき(SDA Low・SCL High のとき、または LA トリガが SDA の立下りで、
最初の SCL の立上りが 1.5 周期以内に来るとき)は、トリガ点を START とみなす。
"""

import numpy as np

from . import MIN_SPB, Result, hex2


def decode(cap, o):
    scl_ch, sda_ch = o.get("scl"), o.get("sda")
    r = Result("i2c", ["I2C"])
    if not scl_ch or not sda_ch or scl_ch == sda_ch:
        r.notes.append("SCL と SDA に別々の ch を選ぶ")
        return r
    scl, sda = cap.bits[scl_ch].astype(int), cap.bits[sda_ch].astype(int)
    rises = np.nonzero((scl[:-1] == 0) & (scl[1:] == 1))[0] + 1
    if len(rises) >= 2:
        period = float(np.median(np.diff(rises))) / cap.fs
        r.bitrate = 1.0 / period
        r.spb = cap.fs * period
        if r.spb < MIN_SPB:
            r.notes.append("SCL 1 周期あたり %.1f サンプルしかなく、デコードできない" % r.spb)
            return r
    half = 0.5 / r.bitrate if r.bitrate else 0.0
    in_tx, bits, byte_t0, first_byte, pending = False, [], None, True, None

    def start(i, rep):
        r.ann.append({"row": 0, "t0": cap.time(i), "t1": cap.time(i) + half, "label": "Sr" if rep else "S",
                      "kind": "ctl"})
        r.items.append({"t": cap.time(i), "text": "Repeated START" if rep else "START"})

    trig = o.get("trigger") or {}
    trig_on_sda_fall = sda_ch in (trig.get("channels") or []) and trig.get("edge") == "falling"
    just_after_start = (sda[0] == 0 and scl[0] == 1) or \
        (trig_on_sda_fall and len(rises) and r.spb and rises[0] < 1.5 * r.spb)
    if just_after_start:                                   # START の直後から記録が始まった
        r.ann.append({"row": 0, "t0": 0.0, "t1": cap.time(0), "label": "S", "kind": "ctl"})
        r.items.append({"t": 0.0, "text": "START(記録の前。トリガ点から補った)"})
        r.notes.append("START は記録の前。トリガ点(t = 0)を START とみなした")
        in_tx = True
    for i in range(1, cap.n):
        if scl[i] == 1 and scl[i - 1] == 1 and sda[i] != sda[i - 1]:
            if sda[i] == 0:                                # START / Repeated START
                start(i, in_tx)
                in_tx, bits, first_byte = True, [], True
            else:                                          # STOP
                r.ann.append({"row": 0, "t0": cap.time(i) - half, "t1": cap.time(i), "label": "P", "kind": "ctl"})
                r.items.append({"t": cap.time(i), "text": "STOP"})
                in_tx, bits = False, []
            continue
        if in_tx and scl[i] == 1 and scl[i - 1] == 0:      # SCL の立上りで読む
            if not bits:
                byte_t0 = cap.time(i) - half
            bits.append(int(sda[i]))
            if len(bits) == 8:
                b = 0
                for x in bits:
                    b = (b << 1) | x
                t_end = cap.time(i) + half
                if first_byte:
                    label, kind = "%s %s" % (hex2(b >> 1), "R" if b & 1 else "W"), "addr"
                    desc = "アドレス 0x%s %s" % (hex2(b >> 1), "R(読み出し)" if b & 1 else "W(書き込み)")
                else:
                    label, kind, desc = hex2(b), "data", "データ 0x%s" % hex2(b)
                r.ann.append({"row": 0, "t0": byte_t0, "t1": t_end, "label": label, "kind": kind})
                pending = (byte_t0, desc)
            elif len(bits) == 9:
                ack = bits[8] == 0
                r.ann.append({"row": 0, "t0": cap.time(i) - half, "t1": cap.time(i) + half, "label": "A" if ack else "N",
                              "kind": "ack" if ack else "nack"})
                t0, desc = pending
                r.items.append({"t": t0, "text": "%s  %s" % (desc, "ACK" if ack else "NACK")})
                bits, first_byte = [], False
    if in_tx and bits:
        r.notes.append("最後のバイトの途中で窓が終わった")
    return r
