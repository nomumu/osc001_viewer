"""ロガー(偽の Scope で)。間隔は 0.1 s にして短く回す。"""

import csv
import os
import time

import pytest

from osc001_viewer.client import Client, ClientError
from osc001_viewer.core import vertical
from osc001_viewer.core.control import Server
from osc001_viewer.core.device import Device, DeviceError
from osc001_viewer.core.fake import FakeScope
from osc001_viewer.core.logger import Logger, parse_alert, parse_duration


@pytest.fixture
def dev(tmp_path):
    d = Device(path="/dev/hidraw-fake", zero_store=vertical.ZeroStore(str(tmp_path / "zero.json")),
               scope_factory=lambda p: FakeScope(p)).open()
    yield d
    d.close()


def read_rows(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    head = [x for x in lines if x.startswith("#")]
    rows = list(csv.DictReader([x for x in lines if not x.startswith("#")]))
    return head, rows


def wait_until(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_parse():
    assert parse_alert("ch1<3.6") == {"ch": 1, "op": "<", "value": 3.6}
    assert parse_alert(" ch2 > -1.5 ")["value"] == -1.5
    with pytest.raises(DeviceError):
        parse_alert("ch3<1")
    assert parse_duration("10m") == 600 and parse_duration("2h") == 7200 and parse_duration("90") == 90


def test_record_marker_alert_file(dev, tmp_path):
    lg = Logger(dev)
    st = lg.start(interval=0.1, avg=10, alerts=["ch1>6", "ch2<0.5"], directory=str(tmp_path))
    assert st["running"] and st["file"].startswith(str(tmp_path))
    assert wait_until(lambda: lg.state["points"] >= 3)
    lg.marker("負荷 ON", by="local-llm")
    assert wait_until(lambda: any("負荷 ON" in p["event"] for p in lg.points), 1.0)
    dev.scope.mon[5] = 650                                   # ch1 = (650 − 495) × 47.9 mV ≒ 7.4 V → アラート
    assert wait_until(lambda: lg.state["alert_count"] >= 2)          # ch2 は最初から 1 回、ch1 でもう 1 回
    n_alert = lg.state["alert_count"]
    time.sleep(0.3)
    assert lg.state["alert_count"] == n_alert                # 条件に入ったときだけ数える
    lg.stop()
    head, rows = read_rows(st["file"])
    assert head[0] == "# osc001 log v1" and any(h.startswith("# alerts: ch1 > 6 V, ch2 < 0.5 V") for h in head)
    assert head[-1].startswith("# end:") and "手動で停止" in head[-1]
    assert rows[0]["event"] == "marker: 記録開始 (user) | alert: ch2 < 0.5 V"   # ch2 ≒ 0.02 V は最初から条件内
    assert any(r["event"] == "marker: 負荷 ON (local-llm)" for r in rows)
    assert any(r["event"] == "alert: ch1 > 6 V" for r in rows)
    r = rows[1]
    assert float(r["ch1_avg"]) == pytest.approx((600 - 495) * 0.0479, abs=1e-4)
    assert float(r["ch1_min"]) <= float(r["ch1_avg"]) <= float(r["ch1_max"])
    assert list(rows[0].keys()) == ["time", "elapsed_s", "ch1_avg", "ch1_min", "ch1_max",
                                    "ch2_avg", "ch2_min", "ch2_max", "event"]


def test_end_conditions_and_validation(dev, tmp_path):
    lg = Logger(dev)
    lg.start(interval=0.1, max_points=3, directory=str(tmp_path))
    assert wait_until(lambda: not lg.state["running"])
    assert lg.state["points"] == 3 and "3 点" in lg.state["end_reason"]
    lg.start(interval=0.1, duration="0.25", directory=str(tmp_path))
    assert wait_until(lambda: not lg.state["running"])
    assert "予定の時刻" in lg.state["end_reason"]
    with pytest.raises(DeviceError):
        lg.start(interval=0.05)                              # 0.1 s 未満
    with pytest.raises(DeviceError):
        lg.start(interval=0.1, avg=100)                      # 100 回 ≒ 0.13 s の読み取りより短い
    with pytest.raises(DeviceError):
        lg.start(avg=7)
    with pytest.raises(DeviceError):
        lg.stop()                                            # 記録していない
    with pytest.raises(DeviceError):
        lg.marker("x")
    files = Logger.files(str(tmp_path))["files"]
    assert len(files) == 2 and all(f["complete"] for f in files)     # 同じ秒に始めても上書きしない


def test_get_downsample(dev, tmp_path):
    lg = Logger(dev)
    lg.start(interval=0.1, max_points=8, directory=str(tmp_path))
    assert wait_until(lambda: not lg.state["running"])
    g = lg.get(max_points=3)
    assert g["points_total"] == 8 and g["points"] == 3
    assert g["rows"][0]["event"].startswith("marker: 記録開始")
    assert all(r["ch1_min"] <= r["ch1_avg"] <= r["ch1_max"] for r in g["rows"])


def test_ops_via_socket(dev, tmp_path, monkeypatch):
    monkeypatch.setenv("OSC001_LOG_DIR", str(tmp_path / "logs"))
    lg = Logger(dev)
    srv = Server(dev, path=str(tmp_path / "s.sock"), logger=lg).start()
    try:
        with Client(srv.path, client="script") as c:
            st = c.log_start(interval=0.1, alerts=["ch1<1"])
            assert st["alerts"] == ["ch1 < 1 V"]
            assert wait_until(lambda: lg.state["points"] >= 2)
            assert c.marker(text="測定開始")["by"] == "script"
            assert c.log_status()["running"]
            with pytest.raises(ClientError) as e:
                c.log_start()
            assert e.value.code == "device"
            g = c.log_get(max_points=50)
            assert g["points"] >= 2
            c.log_stop()
            files = c.log_files()["files"]
            assert files and files[0]["complete"] and os.path.dirname(files[0]["file"]) == str(tmp_path / "logs")
    finally:
        srv.shutdown()


def test_cli_log(dev, tmp_path, monkeypatch):
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout
    from osc001_viewer import cli
    monkeypatch.setenv("OSC001_LOG_DIR", str(tmp_path / "logs"))
    lg = Logger(dev)
    srv = Server(dev, path=str(tmp_path / "s.sock"), logger=lg).start()

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--socket", srv.path, "--client", "sh"] + list(argv))
        return rc, out.getvalue()
    try:
        rc, out = run("log-start", "--interval", "0.1", "--alerts", "ch1<1,ch2>9")
        assert rc == 0 and json.loads(out)["alerts"] == ["ch1 < 1 V", "ch2 > 9 V"]
        assert wait_until(lambda: lg.state["points"] >= 2)
        rc, out = run("marker", "負荷 OFF")
        assert rc == 0 and json.loads(out)["by"] == "sh"
        rc, out = run("--csv", "log-get")
        lines = out.splitlines()
        assert rc == 0 and lines[0].startswith("# osc001 log-get") and lines[1].startswith("time,elapsed_s,ch1_avg")
        rc, out = run("log-stop")
        assert rc == 0 and not json.loads(out)["running"]
        rc, out = run("log-stop")
        assert rc == 2                                            # 記録していない → 装置側のエラー
    finally:
        srv.shutdown()
