"""外部操作: サーバ・Python クライアント・osc001ctl(偽の Scope で)。"""

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest

from osc001_viewer.core.fake import FakeScope
from osc001_viewer import cli
from osc001_viewer.client import Client, ClientError
from osc001_viewer.core import ops, vertical
from osc001_viewer.core.control import Server
from osc001_viewer.core.device import Device


@pytest.fixture
def server(tmp_path):
    dev = Device(path="/dev/hidraw-fake", zero_store=vertical.ZeroStore(str(tmp_path / "zero.json")),
                 scope_factory=lambda p: FakeScope(p)).open()
    log = []
    srv = Server(dev, path=str(tmp_path / "s.sock"), on_op=lambda *a: log.append(a)).start()
    srv.oplog = log
    yield srv
    srv.shutdown()
    dev.close()


def run_cli(server, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["--socket", server.path] + list(argv))
    return rc, out.getvalue(), err.getvalue()


def test_socket_permissions(server):
    assert oct(os.stat(server.path).st_mode & 0o777) == "0o600"


def test_client_basic(server):
    with Client(server.path, client="test") as c:
        st = c.status()
        assert st["connected"] and st["server"]["kind"] == "daemon" and st["server"]["clients"] == 1
        r = c.set_channel(ch=1, vdiv=1.0, pos_div=-2.0)
        assert r["range"]["label"] == "÷8 段0"
        f = c.capture(samples=True)
        assert f["n"] == 192 and len(f["ch1"]["volts"]) == 192
        assert f["ch1"]["stats"]["freq"] == pytest.approx(1000, rel=0.03)
        assert c.read_voltage(avg=3)["ch1"] == pytest.approx((600 - 495.0) * 0.0479)
    assert ("test", "set_channel", {"ch": 1, "vdiv": 1.0, "pos_div": -2.0}, True) in server.oplog


def test_client_errors(server):
    with Client(server.path) as c:
        with pytest.raises(ClientError) as e:
            c.call("no-such-op")
        assert e.value.code == "unknown_op"
        with pytest.raises(ClientError) as e:
            c.set_channel(ch=3)
        assert e.value.code == "bad_args"
        with pytest.raises(ClientError) as e:
            c.set_channel(ch=1, color="red")
        assert e.value.code == "bad_args" and "使える引数" in e.value.hint
        with pytest.raises(ClientError) as e:
            c.calibrate_zero()
        assert e.value.code == "bad_args" and "yes=true" in e.value.hint
        with pytest.raises(ClientError) as e:
            c.set_trigger(source="ch1", level=40.0)
        assert e.value.code == "device" and e.value.hint
        with pytest.raises(ClientError) as e:
            c.la_capture(edge="auto", channels=[2])
        assert e.value.code == "bad_args" and "ハング" in e.value.hint
        with pytest.raises(AttributeError):
            c.no_such_method


def test_readonly(server):
    server.readonly = True
    with Client(server.path) as c:
        assert c.capture()["n"] == 192                      # 読み取りは許す
        with pytest.raises(ClientError) as e:
            c.set_timebase(tdiv=0.01)
        assert e.value.code == "locked"


def test_trigger_and_calibrate(server):
    with Client(server.path) as c:
        t = c.set_trigger(source="ext", edge="falling")
        assert t["kind"] == "hw" and "1.45" in t["note"]
        f = c.capture()
        assert f["triggered"] is True and f["trigger"]["index"] == 0
        r = c.calibrate_zero(channels=[1], yes=True)
        assert os.path.isfile(r["saved"])


def test_la_capture(server):
    with Client(server.path) as c:
        r = c.la_capture()
        assert r["n"] == 844 and set(r["bits"]["d1"]) <= {"0", "1"} and len(r["bits"]["d4"]) == 844
        assert r["latency_s"] == 7.3e-6 and r["transitions"]["d1"] == 17
        r = c.la_capture(channels=[2], edge="falling", bits=False,
                         decode={"protocol": "uart", "rx": 2, "baud": 115200})
        assert "bits" not in r
        d = r["decode"]
        assert [x["text"].split()[1] for x in d["items"]] == ["0x4F", "0x4B", "0x0D", "0x0A"]
        assert d["samples_per_bit"] == 8.5 and d["errors"] == 0
        with pytest.raises(ClientError) as e:
            c.la_capture(decode={"protocol": "usb"})
        assert e.value.code == "bad_args"


def test_no_server(tmp_path):
    with pytest.raises(ClientError) as e:
        Client(str(tmp_path / "none.sock")).connect()
    assert e.value.code == "no_server" and "osc001d" in e.value.hint


def test_second_server_refused(server):
    from osc001_viewer.core.device import DeviceError
    with pytest.raises(DeviceError):
        Server(server.device, path=server.path).start()


def test_cli_json_and_csv(server, tmp_path):
    rc, out, _ = run_cli(server, "read-voltage", "--avg", "2")
    assert rc == 0 and json.loads(out)["unit"] == "V"
    rc, out, _ = run_cli(server, "set-trigger", "ch1", "--edge", "rising", "--level", "2.5")
    assert rc == 0 and json.loads(out)["source"] == "ch1"
    rc, out, _ = run_cli(server, "--csv", "capture")
    lines = out.strip().splitlines()
    assert rc == 0 and lines[0] == "# osc001 capture" and "t,ch1,ch2" in lines and len(lines) == 8 + 192
    assert "# first_valid: 1" in out
    p = tmp_path / "w.csv"
    rc, out, _ = run_cli(server, "--csv", "--out", str(p), "capture")
    d = json.loads(out)
    assert rc == 0 and d["file"] == str(p) and d["summary"]["n"] == 192 and p.read_text().startswith("# osc001")
    rc, out, _ = run_cli(server, "--csv", "la-capture")
    assert rc == 0 and out.splitlines()[2] == "t,d1,d2,d3,d4" and len(out.splitlines()) == 3 + 844


def test_cli_call_bridge_and_exit_codes(server):
    rc, out, _ = run_cli(server, "call", "read_voltage", '{"avg": 2, "tap": 8}')
    assert rc == 0 and "ch1" in json.loads(out)
    rc, out, err = run_cli(server, "call", "set_channel", '{"ch": 9}')
    assert rc == 4 and json.loads(out)["error"]["code"] == "bad_args" and err
    rc, out, _ = run_cli(server, "call", "read_voltage", "{oops")
    assert rc == 4
    server.readonly = True
    rc, out, _ = run_cli(server, "set-timebase", "0.01")
    assert rc == 3
    server.readonly = False
    rc, out, _ = run_cli(server, "set-timebase", "0.01")
    assert rc == 0 and json.loads(out)["fs"] == pytest.approx(1920, rel=0.01)


def test_cli_no_server(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["--socket", str(tmp_path / "x.sock"), "status"])
    assert rc == 5 and json.loads(out.getvalue())["error"]["code"] == "no_server"


def test_schema_formats():
    names = {o["function"]["name"] for o in ops.schemas("openai")}
    assert {"capture", "read_voltage", "set_trigger", "calibrate_zero", "la_capture"} <= names
    mcp = {o["name"]: o for o in ops.schemas("mcp", prefix="osc001_")}
    sch = mcp["osc001_set_channel"]["inputSchema"]
    assert sch["required"] == ["ch"] and sch["properties"]["ch"]["enum"] == [1, 2]
    plain = {o["name"]: o for o in ops.schemas("json")}
    assert plain["read_voltage"]["cli"] == "read-voltage" and plain["set_trigger"]["mutating"]


def test_array_args_from_cli_strings():
    o = ops.OPS["log_start"]
    assert ops.validate(o, {"alerts": '["ch1<3.6", "ch2>1.4"]'})["alerts"] == ["ch1<3.6", "ch2>1.4"]
    assert ops.validate(o, {"alerts": "ch1<3.6, ch2>1.4"})["alerts"] == ["ch1<3.6", "ch2>1.4"]
    la = ops.OPS["la_capture"]
    assert ops.validate(la, {"channels": "[2, 3]"})["channels"] == [2, 3]
    assert ops.validate(la, {"channels": "2,3"})["channels"] == [2, 3]
