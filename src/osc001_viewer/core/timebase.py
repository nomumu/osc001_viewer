"""時間軸: Time/div → サンプルレート、1 回の取得時間、逐次表示の可否。

画面は横 10 div で、通常の 192 点がちょうど 10 div に収まるように fs を決める。
記録は 192/fs 秒で終わり op6 が立つ。211 deep の後方 19 点の書き込み時刻は
未確認なので、deep は API と同じく 422/fs 待つ。
"""

from .._api import osc001

DIVS = 10
SAMPLES = osc001.SAMPLES_PER_CH          # 192
SAMPLES_DEEP = osc001.SAMPLES_DEEP       # 211
XFER_S = 0.02                            # 転送(op6 確認 + op8 6 チャンク ≒ 12 ms)+ 余裕
PROGRESSIVE_MIN_S = 1.5                  # これより長い取得はフリーランなら左から描いていく

# 1-2-5 刻み。1 ms/div で 19.2 kSPS(上限 33.13 kSPS)、50 s/div で 0.384 SPS(下限 0.36 SPS)
TDIV_STEPS = [1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0]


def target_fs(tdiv):
    """Time/div から欲しい per-ch fs(192 点 = 10 div)。"""
    return SAMPLES / (DIVS * float(tdiv))


def acq_time(fs, deep=False):
    """1 回の取得時間(秒)= 記録 + 転送。"""
    return (2 * SAMPLES_DEEP if deep else SAMPLES) / float(fs) + XFER_S


def progressive(fs, free_run, deep=False):
    """取得の途中から描くか。フリーランで、取得が長いときだけ(トリガ待ちの間は op8 が凍結)。"""
    return free_run and not deep and acq_time(fs) > PROGRESSIVE_MIN_S
