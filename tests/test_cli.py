import subprocess
import sys


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
