"""デコーダ(合成した波形で)。サンプル i の時刻は lat + i/fs、トリガ点が t = 0。"""

import numpy as np
import pytest

from osc001_viewer.core import la
from osc001_viewer.core.decode import can, decode

FS = 979400.0


class Wave(object):
    def __init__(self, level):
        self.l0, self.tr = level, []

    def set(self, t, level):
        last = self.tr[-1][1] if self.tr else self.l0
        if level != last:
            self.tr.append((t, level))

    def at(self, t):
        v = self.l0
        for tt, ll in self.tr:
            if tt > t:
                break
            v = ll
        return v


def cap_of(waves, fs=FS, lat=None):
    lat = la.latency(fs) if lat is None else lat
    t = lat + np.arange(la.N) / fs
    bits = {}
    for ch in (1, 2, 3, 4):
        w = waves.get(ch)
        bits[ch] = [w.at(x) if w else 1 for x in t]
    return la.Capture(bits, fs, lat)


def uart(data, baud, t0, inv=False, parity=None, gap=1.5):
    w = Wave(0 if inv else 1)
    I = (lambda v: 1 - v) if inv else (lambda v: v)          # noqa: E731
    tb, t = 1.0 / baud, t0
    for b in data:
        w.set(t, I(0))
        bits = [(b >> k) & 1 for k in range(8)]
        if parity == "E":
            bits.append(sum(bits) & 1)
        for k, v in enumerate(bits):
            w.set(t + (1 + k) * tb, I(v))
        w.set(t + (1 + len(bits)) * tb, I(1))
        t += (2 + len(bits) + gap) * tb
    return w


def test_uart_basic_and_head_loss():
    c = cap_of({2: uart(b"OK\r\n", 115200, 0.0)})            # スタートビットの立下りが t = 0(記録の前)
    r = decode("uart", c, {"rx": 2, "baud": 115200})
    assert [x["text"].split()[1] for x in r.items] == ["0x4F", "0x4B", "0x0D", "0x0A"]
    assert r.errors == 0 and any("記録の前" in n for n in r.notes)
    assert r.spb == pytest.approx(8.5, rel=0.01)
    assert r.ann[0]["tip"] == "RX 0x4F(2進:0100 1111、10進:79、ASCII:'O')\n8N1:スタート + データ 8 ビット(LSB 先)+ ストップ"


def test_ascii_names():
    from osc001_viewer.core.decode import asc, num
    assert [asc(b) for b in (0x41, 0x20, 0x0D, 0x1B, 0x7F, 0x80)] == \
        ["'A'", "' '", "\\r(CR)", "制御文字 ESC", "制御文字 DEL", "ASCII 外"]
    assert num(0x0A) == "2進:0000 1010、10進:10、ASCII:\\n(LF)"


def test_uart_tx_rx_inverted_parity():
    c = cap_of({3: uart(b"AT", 57600, 20e-6, inv=True, parity="E"),
                2: uart(b"OK", 57600, 300e-6, inv=True, parity="E")})
    r = decode("uart", c, {"rx": 2, "tx": 3, "baud": 57600, "format": "8E1", "invert": True})
    texts = [x["text"] for x in r.items]
    assert texts[0].startswith("TX  0x41") and texts[2].startswith("RX  0x4F") and r.errors == 0
    assert all("パリティ OK" in t for t in texts)
    r = decode("uart", c, {"rx": 2, "tx": 3, "baud": 57600, "format": "8O1", "invert": True})
    assert r.errors == 4                                        # 奇数パリティで読むと全部誤り


def test_uart_too_slow_rate():
    c = cap_of({2: uart(b"A", 115200, 10e-6)}, fs=100000.0)
    r = decode("uart", c, {"rx": 2, "baud": 115200})
    assert not r.items and "デコードできない" in r.notes[0]


def i2c_wave(clk, t0, ops):
    tb = 1.0 / clk
    scl, sda = Wave(1), Wave(1)
    t = t0

    def bit(v):
        nonlocal t
        sda.set(t + tb / 4, v)
        scl.set(t + tb / 2, 1)
        scl.set(t + tb, 0)
        t += tb
    for op, val in ops:
        if op == "S":
            sda.set(t, 0)
            scl.set(t + tb / 2, 0)
            t += tb / 2
        elif op == "Sr":
            sda.set(t + tb / 4, 1)
            scl.set(t + tb / 2, 1)
            sda.set(t + tb * 0.75, 0)
            scl.set(t + tb, 0)
            t += tb
        elif op == "P":
            sda.set(t + tb / 4, 0)
            scl.set(t + tb / 2, 1)
            sda.set(t + tb * 0.75, 1)
            t += tb
        else:                                                    # ("B", (byte, ack))
            b, ack = val
            for k in range(7, -1, -1):
                bit((b >> k) & 1)
            bit(0 if ack else 1)
    return scl, sda


def test_i2c_write_read_with_repeated_start():
    scl, sda = i2c_wave(100000, 20e-6, [("S", 0), ("B", (0xA0, True)), ("B", (0x10, True)), ("Sr", 0),
                                         ("B", (0xA1, True)), ("B", (0x3A, False)), ("P", 0)])
    r = decode("i2c", cap_of({1: scl, 2: sda}), {"scl": 1, "sda": 2})
    t = [x["text"] for x in r.items]
    assert t == ["START", "アドレス 0x50 W(書き込み)  ACK", "データ 0x10  ACK", "Repeated START",
                 "アドレス 0x50 R(読み出し)  ACK", "データ 0x3A  NACK", "STOP"]
    assert r.bitrate == pytest.approx(100000, rel=0.03)
    tips = [a["tip"] for a in r.ann]
    assert "アドレス 0x50(7 ビット)+ W(書き込み)\n送られたバイトは 0xA0" in tips
    assert tips[-2].startswith("NACK") and tips[-1].startswith("STOP")


def test_i2c_start_before_record():
    scl, sda = i2c_wave(100000, -2e-6, [("S", 0), ("B", (0x90, True)), ("P", 0)])
    c = cap_of({1: scl, 2: sda})
    r = decode("i2c", c, {"scl": 1, "sda": 2, "trigger": {"channels": [2], "edge": "falling"}})
    assert r.items[0]["text"].startswith("START(記録の前") and r.items[1]["text"].startswith("アドレス 0x48 W")
    r = decode("i2c", c, {"scl": 1, "sda": 2})                  # トリガの手がかりが無ければ補わない
    assert not r.items[0]["text"].startswith("START(記録の前")


def spi_wave(clk, t0, mode, mosi_b, miso_b, cs_low=True):
    tb = 1.0 / clk
    cpol, cpha = mode >> 1, mode & 1
    sck, mosi, miso, cs = Wave(cpol), Wave(1), Wave(1), Wave(1 if cs_low else 0)
    cs.set(t0, 0 if cs_low else 1)
    for k in range(len(mosi_b) * 8):
        byte, bit = k // 8, 7 - k % 8
        lead = t0 + tb / 2 + k * tb
        dt = lead - tb / 4 if cpha == 0 else lead + tb / 8
        mosi.set(dt, (mosi_b[byte] >> bit) & 1)
        miso.set(dt, (miso_b[byte] >> bit) & 1)
        sck.set(lead, 1 - cpol)
        sck.set(lead + tb / 2, cpol)
    cs.set(t0 + (len(mosi_b) * 8 + 1) * tb, 1 if cs_low else 0)
    return sck, mosi, miso, cs


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_modes(mode):
    sck, mosi, miso, cs = spi_wave(100000, 30e-6, mode, [0x9F, 0x00, 0x00], [0xFF, 0xEF, 0x40])
    r = decode("spi", cap_of({1: sck, 2: mosi, 3: miso, 4: cs}, fs=490900.0),
               {"sck": 1, "mosi": 2, "miso": 3, "cs": 4, "mode": mode})
    assert [x["text"] for x in r.items] == ["MOSI 0x9F  /  MISO 0xFF", "MOSI 0x00  /  MISO 0xEF",
                                            "MOSI 0x00  /  MISO 0x40"]
    assert r.ann[0]["tip"] == "MOSI 0x9F(2進:1001 1111、10進:159、ASCII:範囲外)\nCS 区間の 1 バイト目、モード %d、MSB 先" % mode


def test_spi_cs_before_record_warns():
    sck, mosi, miso, cs = spi_wave(100000, 0.0, 0, [0x9F], [0xFF])
    r = decode("spi", cap_of({1: sck, 2: mosi, 3: miso, 4: cs}), {"sck": 1, "mosi": 2, "miso": 3, "cs": 4})
    assert any("記録の前から有効" in n for n in r.notes)


def test_spi_head_loss_aligned_from_cs_end():
    # 実機(979.4 kHz、遅れ 7.3 µs)で、CS の直後の SCK(5 µs 後)が記録の前になった形
    sck, mosi, miso, cs = spi_wave(100000, 0.0, 0, [0x9F, 0x00, 0x00, 0x00], [0xFF, 0xEF, 0x40, 0x18])
    r = decode("spi", cap_of({1: sck, 2: mosi, 3: miso, 4: cs}), {"sck": 1, "mosi": 2, "miso": 3, "cs": 4})
    assert [x["text"] for x in r.items] == ["MOSI 0x00  /  MISO 0xEF", "MOSI 0x00  /  MISO 0x40",
                                            "MOSI 0x00  /  MISO 0x18"]
    assert any("7 ビットしか記録に無い" in n for n in r.notes)
    assert not any("8 ビットの倍数" in n for n in r.notes)


def test_spi_idle_level_mismatch():
    sck, mosi, miso, cs = spi_wave(100000, 30e-6, 3, [0x9F, 0x00], [0xFF, 0xEF])
    c = cap_of({1: sck, 2: mosi, 3: miso, 4: cs}, fs=490900.0)
    o = {"sck": 1, "mosi": 2, "miso": 3, "cs": 4}
    r = decode("spi", c, dict(o, mode=0))
    assert [x["text"] for x in r.items] == ["MOSI 0x9F  /  MISO 0xFF", "MOSI 0x00  /  MISO 0xEF"]
    assert any("モード 3 ではないか" in n for n in r.notes)
    assert not any("CPOL" in n for n in decode("spi", c, dict(o, mode=3)).notes)


def can_wave(rate, t0, ident, data, inv=False, flip=None):
    raw = []
    put = lambda v, n: raw.extend((v >> k) & 1 for k in range(n - 1, -1, -1))   # noqa: E731
    put(0, 1); put(ident, 11); put(0, 1); put(0, 1); put(0, 1); put(len(data), 4)   # noqa: E702
    for b in data:
        put(b, 8)
    put(can.crc15(raw), 15)
    if flip is not None:
        raw[flip] ^= 1                                           # CRC 誤りを作る
    st, run, last = [], 0, None
    for v in raw:
        st.append(v)
        run = run + 1 if v == last else 1
        last = v
        if run == 5:
            st.append(1 - v)
            last, run = 1 - v, 1
    st += [1, 0, 1] + [1] * 7
    w = Wave(0 if inv else 1)
    for k, v in enumerate(st):
        w.set(t0 + k / rate, (1 - v) if inv else v)
    return w


def test_can_frame_crc_ok_and_head_loss():
    c = cap_of({2: can_wave(250000, 0.0, 0x123, [0x11, 0x22])})   # SOF の始まり = トリガ点(記録の前)
    r = decode("can", c, {"rx": 2, "bitrate": 250000})
    assert r.items[0]["text"].startswith("ID 0x123  DLC 2  [11 22]") and "CRC" in r.items[0]["text"]
    assert "OK" in r.items[0]["text"] and "ACK あり" in r.items[0]["text"] and r.errors == 0
    assert any(a["kind"] == "stuff" for a in r.ann) or "スタッフビット 0" in r.items[1]["text"]
    tips = {a["label"]: a["tip"] for a in r.ann if a["row"] == 0}
    assert tips["DLC 2"] == "DLC = 2(データ長 2 バイト)" and tips["RTR"].startswith("RTR = 0(データフレーム")


def test_can_inverted_and_crc_error():
    c = cap_of({1: can_wave(125000, 40e-6, 0x7E0, [0x00, 0xFF, 0x00], inv=True, flip=20)}, fs=490900.0)
    r = decode("can", c, {"rx": 1, "bitrate": 125000, "invert": True})
    assert "誤り" in r.items[0]["text"] and r.errors >= 1


def test_can_500k_rejected():
    c = cap_of({2: can_wave(500000, 10e-6, 0x1, [1])})
    r = decode("can", c, {"rx": 2, "bitrate": 500000})
    assert not r.items and "デコードできない" in r.notes[0]


def test_latency_table():
    assert la.latency(979400.0) == 7.3e-6 and la.latency(100000.0) == pytest.approx(1.8e-5)
