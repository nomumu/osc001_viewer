"""Device(偽の Scope で)。"""

import os
import time

import numpy as np
import pytest

from osc001_viewer.core.fake import FakeScope
from osc001_viewer.core import vertical
from osc001_viewer.core.device import Device, DeviceError


@pytest.fixture
def dev(tmp_path):
    d = Device(path="/dev/hidraw-fake", zero_store=vertical.ZeroStore(str(tmp_path / "zero.json")),
               scope_factory=lambda p: FakeScope(p))
    d.open()
    yield d
    d.close()


def test_open_defaults(dev):
    st = dev.status()
    assert st["connected"] and st["timebase"]["tdiv"] == 1e-3
    assert st["channels"]["ch1"]["range"]["label"] == "÷8 段0"         # −2〜6 V の画面
    assert dev.scope.b[1] == 5 and dev.scope.b[8] == 0


def test_capture_free_run_levels(dev):
    f = dev.capture()
    assert f.trig_status == "free" and f.first_valid == 0 and f.n == 192
    v1 = f.volts[1]
    assert v1.min() == pytest.approx(0.0, abs=0.06) and v1.max() == pytest.approx(5.0, abs=0.06)
    st = f.stats(1)
    assert st["freq"] == pytest.approx(1000.0, rel=0.03)


def test_probe_scales_display(dev):
    dev.set_channel(1, probe=10)                                       # 入力 1 V/div のまま、表示は 10 V/div
    assert dev.axis[1].vdiv == 10.0
    f = dev.capture()
    assert f.volts[1].max() == pytest.approx(50.0, abs=0.6)
    with pytest.raises(DeviceError):
        dev.set_channel(1, vdiv=1.5)
    dev.set_channel(1, vdiv=50.0)


def test_hw_trigger_first_sample_skipped(dev):
    dev.set_trigger("ch1", "rising", level=2.5, mode="auto")
    f = dev.capture()
    assert f.trig_kind == "hw" and f.trig_status == "triggered" and f.trig_index == 0 and f.first_valid == 1
    assert len(f.measured(1)) == 191


def test_hw_trigger_normal_timeout_returns_none(dev):
    dev.scope.op6 = 0
    dev.set_trigger("ch1", "rising", level=2.5, mode="normal")
    assert dev.capture(timeout=0.05) is None


def test_hw_trigger_level_out_of_window(dev):
    with pytest.raises(DeviceError) as e:
        dev.set_trigger("ch1", "rising", level=30.0)
    assert e.value.hint


def test_sw_trigger_ch2(dev):
    dev.set_channel(2, vdiv=0.5, pos_div=-2.0)
    dev.set_trigger("ch2", "rising", level=1.0, mode="normal")
    f = dev.capture()
    assert f.trig_kind == "sw" and f.trig_status == "triggered" and f.n == 211
    assert f.start == f.trig_index - 19
    assert f.volts[2][f.trig_index - 1] < 1.0 <= f.volts[2][f.trig_index]
    dev.set_trigger("ch2", "rising", level=2.5, mode="normal")       # 届かないレベル
    assert dev.capture() is None
    dev.set_trigger("ch2", "rising", level=2.5, mode="auto")
    assert dev.capture().trig_status == "auto_free"


def test_read_voltage(dev):
    r = dev.read_voltage(avg=4)
    assert r["ch1"] == pytest.approx((600 - 495.0) * 0.0479)
    assert r["sat"] == [False, False]
    with pytest.raises(DeviceError):
        dev.read_voltage(tap=3)


def test_calibrate_zero_saves_and_reapplies(dev, tmp_path):
    dev.scope.signal = lambda ch, t: 0.0
    r = dev.calibrate_zero(channels=(1, 2))
    assert os.path.isfile(r["saved"]) and r["m0"]["ch1_div8"] == 600
    st = dev.status()
    assert st["zero"]["calibrated_at"]
    d2 = Device(path="/dev/hidraw-fake", zero_store=vertical.ZeroStore(r["saved"]),
                scope_factory=lambda p: FakeScope(p)).open()
    assert d2.scope.m0[1][8] == 600 and d2.notes == []


def test_bad_arguments(dev):
    with pytest.raises(DeviceError):
        dev.set_channel(3)
    with pytest.raises(DeviceError):
        dev.set_channel(1, vdiv=0.3)
    with pytest.raises(DeviceError):
        dev.set_timebase(3e-3)
    with pytest.raises(DeviceError):
        dev.set_trigger("ch3")


def test_progressive(dev):
    dev.set_timebase(1.0)                                              # 19.2 SPS、記録 10 s
    dev.begin_progressive()
    dev._prog -= 2.0                                                   # 2 秒経ったことにする
    dev.scope._armed_at -= 2.0
    f, done = dev.poll_progressive()
    assert not done and 35 <= f.valid <= 39 and len(f.measured(1)) == f.valid
    dev._prog -= 20.0
    f, done = dev.poll_progressive()
    assert done and f.valid is None
    dev.set_trigger("ch1", "rising", level=2.5)
    with pytest.raises(DeviceError):
        dev.begin_progressive()


def test_frame_as_dict(dev):
    d = dev.capture().as_dict(samples=True)
    assert len(d["t"]) == 192 and len(d["ch1"]["volts"]) == 192
    assert d["ch1"]["range"]["label"] == "÷8 段0"


def test_autoset(dev):
    from osc001_viewer.core.autoset import autoset
    r = autoset(dev)
    assert r["channels"]["ch1"]["vdiv"] == 1.0                     # 0/5 V の矩形 → 1 V/div
    assert r["channels"]["ch1"]["pos_div"] == pytest.approx(-2.5)  # 中央 2.5 V を画面中央へ
    assert r["channels"]["ch2"]["vdiv"] == 0.5                     # 1.6 Vpp × 1.25 = 2 V → 0.5 V/div
    assert r["channels"]["ch2"]["pos_div"] == pytest.approx(-2.0)  # 中央 1 V を画面中央へ
    assert r["tdiv"] == 1e-3                                       # 1 kHz の 3 周期は 0.3 ms → 最小の 1 ms/div
    assert r["trigger"]["source"] == "ch1" and r["trigger"]["level"] == pytest.approx(2.5, abs=0.1)
    f = dev.capture()
    assert f.trig_status == "triggered"
