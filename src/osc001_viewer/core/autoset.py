"""Auto Set: 入力を見て V/div・0 V 位置・Time/div・トリガを決める。

1) 広い範囲(5 V/div、0 V 中央 = 入力 ±20 V)でフリーラン取得し、各 ch の最小・最大を知る
2) 振幅が画面の 8 割に収まる最も細かい V/div にし、波形の中央を画面中央に置く。広い範囲の取得は
   分解能が粗い(1 コード 0.19 V)ので、決めた V/div でもう一度取得して決め直す
3) 周波数が分かれば約 3 周期が見える Time/div、分からなければ遅い時間軸でもう一度探す
4) 振幅のある ch にトリガを置く(CH1 はハード、CH2 はソフト)。レベルは波形の中央
"""

from . import timebase, trigger, vertical

MIN_VPP = 0.2                  # これ未満の ch は「信号なし」とみなす(V、表示の単位)
PERIODS = 3.0


def _pick_vdiv(vpp, probe):
    for v in vertical.VDIV_STEPS:
        if 8 * v * probe >= vpp * 1.25:
            return v * probe
    return vertical.VDIV_STEPS[-1] * probe


def _pick_tdiv(freq):
    want = PERIODS / freq / timebase.DIVS
    for t in timebase.TDIV_STEPS:
        if t >= want:
            return t
    return timebase.TDIV_STEPS[-1]


def autoset(dev):
    """dev は core.device.Device。返り値は決めた設定の要約。"""
    with dev.lock:
        dev.set_trigger("none", dev.trig["edge"], dev.trig["level"], "auto")
        for ch in (1, 2):
            dev.set_channel(ch, vdiv=5.0 * dev.axis[ch].probe, pos_div=0.0, fixed=None)
        dev.set_timebase(1e-3)
        f = dev.capture()
        st = {ch: f.stats(ch) for ch in (1, 2)}
        if all((st[ch]["freq"] is None) for ch in (1, 2) if st[ch]["vpp"] >= MIN_VPP):
            dev.set_timebase(50e-3)                       # 遅い信号: 0.5 s 窓でもう一度
            f2 = dev.capture()
            st2 = {ch: f2.stats(ch) for ch in (1, 2)}
            if any(st2[ch]["freq"] for ch in (1, 2)):
                st = st2
        out = {"channels": {}}
        for _pass in range(2):
            for ch in (1, 2):
                s, a = st[ch], dev.axis[ch]
                if s["vpp"] < MIN_VPP:
                    vdiv = _pick_vdiv(max(abs(s["mean"]) * 2, 1.0), a.probe)
                else:
                    vdiv = _pick_vdiv(s["vpp"], a.probe)
                mid = (s["max"] + s["min"]) / 2.0
                pos = max(-4.0, min(4.0, round(-mid / vdiv * 2) / 2.0))     # 0.5 div 刻み
                dev.set_channel(ch, vdiv=vdiv, pos_div=pos)
                out["channels"]["ch%d" % ch] = {"vdiv": vdiv, "pos_div": pos, "vpp": s["vpp"], "freq": s["freq"]}
            if _pass == 0:
                f = dev.capture()
                st = {ch: dict(f.stats(ch), freq=f.stats(ch)["freq"] or st[ch]["freq"]) for ch in (1, 2)}
        freq = st[1]["freq"] if st[1]["vpp"] >= MIN_VPP and st[1]["freq"] else st[2]["freq"]
        tdiv = _pick_tdiv(freq) if freq else 1e-3
        dev.set_timebase(tdiv)
        out["tdiv"] = tdiv
        src = "ch1" if st[1]["vpp"] >= MIN_VPP else ("ch2" if st[2]["vpp"] >= MIN_VPP else "none")
        if src != "none":
            s = st[int(src[-1])]
            lvl = (s["max"] + s["min"]) / 2.0
            dev.set_trigger(src, "rising", lvl, "auto")
            out["trigger"] = {"source": src, "level": lvl, "kind": trigger.kind(src)}
        else:
            dev.set_trigger("none", "rising", dev.trig["level"], "auto")
            out["trigger"] = {"source": "none"}
        return out
