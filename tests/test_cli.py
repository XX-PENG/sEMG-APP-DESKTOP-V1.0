import pytest

from semg_desktop.cli import main
from semg_desktop.sources import SimulateSource


def test_cli_is_importable():
    assert callable(main)


def test_cli_selftest_passes(capsys):
    code = main(["selftest", "--duration", "0.6"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS" in out
    assert "8 路 sEMG + 1 路 IMU" in out
    assert "垃圾行=0" in out


def test_cli_parse_on_generated_log(tmp_path, capsys):
    path = tmp_path / "gw.log"
    path.write_bytes(SimulateSource().render(20))
    code = main(["parse", str(path)])
    out = capsys.readouterr().out
    assert code == 0
    rows = [ln for ln in out.splitlines() if ln.startswith(("A4:CF:", "12:34:"))]
    assert len(rows) == 9
    assert "imu块截断=0" in out and "垃圾行=0" in out
    assert "2000.0/s" in out


def test_default_config_path_finds_bundled_config(tmp_path, monkeypatch):
    """回归：冻结态配置在 _MEIPASS/config/，漏找会回落默认值（配色全白）。"""
    from semg_desktop import cli

    bundled = tmp_path / "bundle" / "config" / "default.json"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("{}", encoding="utf-8")
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    assert cli._default_config_path() == str(bundled)


def test_cli_ports_runs(capsys):
    assert main(["ports"]) == 0
