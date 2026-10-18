"""トリガ: CH1・EXT はハードウェア、CH2 はソフトウェア(取得した波形から交差点を探す)。

ハードトリガではトリガ点 = サンプル 0(プリトリガなし)。サンプル 0 は続く点より 2〜3 コード低く出るので
描画・測定から外す(FIRST_VALID_HW)。CH2 のソフトトリガは deep(211 点)で取り、交差点の 1 div 前から表示する。
"""

SOURCES = ("none", "ch1", "ch2", "ext")
EDGES = ("rising", "falling")
MODES = ("auto", "normal")
HW_SOURCES = ("ch1", "ext")
FIRST_VALID_HW = 1
SW_PRE = 19                  # ソフトトリガで交差点より前に見せる点数(≒1 div = 19.2 点)
MARGIN_V = 0.7               # CH1 のレベルが波形の端からこれ以内だと成立しにくい(実測: 上端の 0.65 V 手前で 1/5)
EXT_RISE_V = 1.45            # EXT のしきい値(実測)
EXT_FALL_V = 1.42


def kind(source):
    """"hw" / "sw" / None(フリーラン)。"""
    if source in HW_SOURCES:
        return "hw"
    if source == "ch2":
        return "sw"
    return None


def find_edge(v, level, edge, start=1):
    """v[i−1] と v[i] の間でレベルを指定の向きに横切る最初の i(無ければ −1)。"""
    rising = edge == "rising"
    for i in range(max(1, start), len(v)):
        p, q = v[i - 1], v[i]
        if (rising and p < level <= q) or (not rising and p > level >= q):
            return i
    return -1


def sw_trigger(v, level, edge, pre=SW_PRE):
    """CH2 のソフトトリガ。返り値 (交差点の index, 表示の開始 index)。見つからなければ (−1, 0)。"""
    i = find_edge(v, level, edge, start=pre)
    return (i, i - pre) if i >= 0 else (-1, 0)


def level_warnings(source, level, vmin, vmax, mode):
    """直前の波形の範囲(vmin〜vmax、レベルと同じ単位)に対するレベルの注意文。"""
    w = []
    if source not in ("ch1", "ch2") or vmin is None:
        return w
    if level <= vmin or level >= vmax:
        w.append("レベルが波形の範囲外。" + ("Normal では次の取得が来ない" if mode == "normal"
                                        else "Auto のためフリーランで表示している"))
    elif source == "ch1" and (level - vmin < MARGIN_V or vmax - level < MARGIN_V):
        w.append("レベルが波形の%sから %.1f V 以内。ハードウェアトリガが成立しにくい"
                 % ("上端" if vmax - level < level - vmin else "下端", MARGIN_V))
    return w


def ext_note():
    return ("EXT のしきい値は立上り ≒%.2f V・立下り ≒%.2f V で固定。ヒステリシスが小さいので、"
            "ゆっくり変わる信号では反対のエッジでも掛かることがある。T を浮かせない" % (EXT_RISE_V, EXT_FALL_V))
