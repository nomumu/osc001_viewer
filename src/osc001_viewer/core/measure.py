"""測定: Vpp・最大・最小・平均・RMS・周波数。"""

import numpy as np

MIN_VPP_V = 0.05             # 周波数を求める最小の振幅
MIN_VPP_LSB = 8              # 同じく、量子化の何コード分以上か(ノイズで周波数を出さない)
HYST = 0.1                   # 中点の上下に振幅のこの割合の幅を取り、往復をここで数える


def stats(v, fs, lsb=None):
    """1 ch 分の電圧列(numpy 配列か list)の統計。

    周波数は、中点 ±HYST×振幅 の幅を下から上へ抜けた回数と間隔から求める(ヒステリシスで
    ノイズの往復を数えない)。抜けが 3 回未満か、振幅が MIN_VPP_V 未満(lsb を渡せば MIN_VPP_LSB コード未満)
    なら None。
    """
    a = np.asarray(v, dtype=float)
    if a.size == 0:
        return {"vpp": None, "max": None, "min": None, "mean": None, "rms": None, "freq": None}
    mx, mn = float(a.max()), float(a.min())
    vpp = mx - mn
    out = {"vpp": vpp, "max": mx, "min": mn, "mean": float(a.mean()),
           "rms": float(np.sqrt(np.mean(a * a))), "freq": None}
    need = max(MIN_VPP_V, MIN_VPP_LSB * lsb) if lsb else MIN_VPP_V
    if vpp >= need:
        mid = (mx + mn) / 2.0
        hi, lo = mid + HYST * vpp, mid - HYST * vpp
        ups, armed = [], False
        for i, x in enumerate(a):
            if x <= lo:
                armed = True
            elif x >= hi and armed:
                ups.append(i)
                armed = False
        if len(ups) >= 3:
            out["freq"] = float(fs) * (len(ups) - 1) / float(ups[-1] - ups[0])
    return out
