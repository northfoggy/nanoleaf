import subprocess
import sys
from types import SimpleNamespace

from nanoleaf_ctl import cli


def test_cli_module_entrypoint_runs():
    result = subprocess.run(
        [sys.executable, "-m", "nanoleaf_ctl.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "Control Nanoleaf panels" in result.stdout


def test_pair_warns_user_and_never_prints_token(monkeypatch, capsys):
    secret = "device-secret-that-must-not-be-printed"
    monkeypatch.setattr(cli.client, "pair", lambda ip: secret)

    assert cli.cmd_pair(SimpleNamespace(ip="192.0.2.10")) == 0

    output = capsys.readouterr().out
    assert "support.nanoleaf.me" in output
    assert "not affiliated" in output
    assert secret not in output
