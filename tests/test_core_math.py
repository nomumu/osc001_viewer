"""縦軸・時間軸・トリガ・測定(装置なし)。"""

import math

import numpy as np
import pytest

from osc001_viewer.core import measure, timebase, trigger, vertical

M0 = {(8, 0): 495.0, (8, 1): 495.0, (8, 2): 495.0, (1, 0): 379.3, (1, 1): 379.3, (1, 2): 379.3}
m0_of = lambda tap, g: M0[(tap, g)]                                   # noqa: E731


def test_vlsb_shared_constant():
    assert vertical.vlsb(8, 0) == pytest.approx(47.9e-3)
    assert vertical.vlsb(8, 2) == pytest.approx(191.6e-3)
    assert vertical.vlsb(1, 1) == pytest.approx(10.08e-3)


def test_screen_axis_probe():
    a = vertical.ScreenAxis(vdiv=10.0, pos_div=-2.0, probe=10.0)
    assert a.input_range() == pytest.approx((-2.0, 6.0))


@pytest.mark.parametrize("lo,hi,want", [
    (-2.0, 6.0, (8, 0)),          # 0/5 V の矩形を 1 V/div で
    (0.0, 1.6, (1, 1)),           # 小振幅は ×1(上端 3.35 V 以下)
    (-20.0, 20.0, (8, 2)),        # ±20 V は ÷8 段 2
    (-0.2, 0.2, (1, 0)),
])
def test_choose_finest_cover(lo, hi, want):
    r = vertical.choose(lo, hi, m0_of)
    assert (r.tap, r.gain) == want
    assert r.covers
    assert r.win_lo <= lo and r.win_hi >= hi


def test_choose_x1_skipped_above_clamp():
    r = vertical.choose(0.0, 3.4, m0_of)
    assert r.tap == 8


def test_choose_out_of_range_uses_coarsest():
    r = vertical.choose(0.0, 40.0, m0_of)          # ADC の上限 ≒ +25.3 V を超える
    assert (r.tap, r.gain) == (8, 2) and r.out_high and not r.out_low
    assert r.win_hi == pytest.approx((1023 - 495.0) * 47.9e-3)


def test_choose_fixed_range():
    r = vertical.choose(-2.0, 6.0, m0_of, fixed=(8, 2))
    assert (r.tap, r.gain) == (8, 2)


def test_position_centers_window():
    r = vertical.choose(-2.0, 6.0, m0_of)
    assert (r.win_lo + r.win_hi) / 2 == pytest.approx(2.0, abs=r.vlsb * 2.5)


def test_zero_store_roundtrip(tmp_path):
    from osc001_viewer.core.fake import FakeScope
    sc = FakeScope()
    sc.set_zero_m0(1, 8, 497.25)
    sc.m0_gain[(1, 8, 1)] = 498.0
    st = vertical.ZeroStore(str(tmp_path / "zero.json"))
    st.save(sc, port="1-3")
    sc2 = FakeScope()
    data, note = st.apply(sc2, port="1-3")
    assert sc2.m0[1][8] == 497.25 and sc2.m0_gain[(1, 8, 1)] == 498.0 and note is None
    _, note = st.apply(FakeScope(), port="2-1")
    assert note and "別の USB ポート" in note


def test_timebase():
    assert timebase.target_fs(1e-3) == pytest.approx(19200.0)
    assert timebase.acq_time(100.0) == pytest.approx(1.92 + timebase.XFER_S)
    assert timebase.acq_time(100.0, deep=True) == pytest.approx(4.22 + timebase.XFER_S)
    assert timebase.progressive(19.2, free_run=True)
    assert not timebase.progressive(19.2, free_run=False)
    assert not timebase.progressive(19200.0, free_run=True)


def test_find_edge_and_sw_trigger():
    v = [0, 0, 1, 2, 3, 2, 1, 0, 0]
    assert trigger.find_edge(v, 1.5, "rising") == 3
    assert trigger.find_edge(v, 1.5, "falling") == 6
    assert trigger.find_edge(v, 5.0, "rising") == -1
    s = np.sin(2 * np.pi * np.arange(211) / 40.0)
    idx, start = trigger.sw_trigger(s, 0.0, "rising")
    assert idx >= trigger.SW_PRE and start == idx - trigger.SW_PRE and s[idx - 1] < 0 <= s[idx]


def test_level_warnings():
    assert trigger.level_warnings("ch1", 2.5, 0.0, 5.0, "auto") == []
    assert "上端" in trigger.level_warnings("ch1", 4.6, 0.0, 5.0, "auto")[0]
    assert "Normal" in trigger.level_warnings("ch2", 6.0, 0.0, 5.0, "normal")[0]
    assert trigger.level_warnings("ext", 0.0, 0.0, 5.0, "auto") == []


def test_measure_sine():
    fs = 9600.0                                                        # 192 点 = 500 Hz の 10 周期
    t = np.arange(192) / fs
    st = measure.stats(1.0 + 2.0 * np.sin(2 * np.pi * 500.0 * t), fs)
    assert st["vpp"] == pytest.approx(4.0, rel=0.01)
    assert st["mean"] == pytest.approx(1.0, abs=0.05)
    assert st["freq"] == pytest.approx(500.0, rel=0.01)
    assert st["rms"] == pytest.approx(math.sqrt(1 + 2.0), rel=0.02)
    assert measure.stats([1.0] * 10, fs)["freq"] is None


def test_measure_rejects_noise_frequency():
    rng = np.random.default_rng(1)
    noise = 0.11 + rng.integers(-2, 3, 192) * 0.0479          # ±2 コードのノイズ(Vpp ≒ 0.19 V)
    assert measure.stats(noise, 19200.0, lsb=0.0479)["freq"] is None
    t = np.arange(192) / 19200.0
    sq = np.where((t * 1000) % 1 < 0.5, 5.0, 0.0) + rng.integers(-1, 2, 192) * 0.0479
    assert measure.stats(sq, 19200.0, lsb=0.0479)["freq"] == pytest.approx(1000, rel=0.02)
