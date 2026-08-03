import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_systemd_unit_provisions_private_state_directory():
    root = Path(__file__).parents[1]
    unit = (root / "nanoleaf.service").read_text(encoding="utf-8")

    assert "User=nanoleaf" in unit
    assert "Group=nanoleaf" in unit
    assert "WorkingDirectory=/opt/nanoleaf" in unit
    assert "Environment=HOME=/var/lib/nanoleaf" in unit
    assert "StateDirectory=nanoleaf" in unit
    assert "StateDirectoryMode=0700" in unit
    assert "ReadWritePaths=" not in unit


def test_reference_deployment_documentation_matches_systemd_unit():
    root = Path(__file__).parents[1]
    security = (root / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    troubleshooting = (root / "docs" / "TROUBLESHOOTING.md").read_text(
        encoding="utf-8"
    )

    assert "`ProtectHome=true`" in security
    assert "`StateDirectory=nanoleaf`" in security
    assert "`StateDirectoryMode=0700`" in security
    assert "`ProtectHome=read-only`" not in security
    assert "`ReadWritePaths=...`" not in security

    assert "git -C /opt/nanoleaf log -1 --oneline" in troubleshooting
    assert "/opt/nanoleaf/venv/bin/python --version" in troubleshooting
    assert '$HOME/nanoleaf' not in troubleshooting


def test_network_recovery_is_gateway_scoped_and_guarded():
    root = Path(__file__).parents[1]
    script = (root / "deploy" / "nanoleaf-network-recovery").read_text(
        encoding="utf-8"
    )
    service = (root / "deploy" / "nanoleaf-network-recovery.service").read_text(
        encoding="utf-8"
    )
    timer = (root / "deploy" / "nanoleaf-network-recovery.timer").read_text(
        encoding="utf-8"
    )

    assert 'ip -4 route show default dev "$interface"' in script
    assert 'ping -c 1 -W 2 "$gateway"' in script
    assert 'nmcli -g GENERAL.STATE device show "$interface"' in script
    assert 'ip neigh show to "$gateway" dev "$interface"' in script
    assert "REACHABLE|PERMANENT|NOARP" in script
    assert "REACHABLE|STALE" not in script
    assert "nanoleafapi" not in script.lower()
    assert 'reconnect_threshold="${NANOLEAF_RECONNECT_THRESHOLD:-3}"' in script
    assert 'reboot_threshold="${NANOLEAF_REBOOT_THRESHOLD:-8}"' in script
    assert 'reboot_cooldown="${NANOLEAF_REBOOT_COOLDOWN_SECONDS:-1800}"' in script
    assert 'nmcli connection up "$connection" ifname "$interface"' in script
    assert 'second_reconnect_threshold=$((reboot_threshold - 2))' in script
    assert "systemctl reboot" in script

    assert "Type=oneshot" in service
    assert "StateDirectory=nanoleaf-network-recovery" in service
    assert "RuntimeDirectoryPreserve=yes" in service
    assert "ProtectSystem=strict" in service
    assert "OnBootSec=5min" in timer
    assert "OnUnitActiveSec=2min" in timer


def test_network_recovery_accepts_gateway_that_rejects_icmp(tmp_path):
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("POSIX shell is unavailable")

    root = Path(__file__).parents[1]
    script = root / "deploy" / "nanoleaf-network-recovery"
    fake_bin = tmp_path / "bin"
    runtime_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    action_log = tmp_path / "actions"
    fake_bin.mkdir()
    runtime_dir.mkdir()
    state_dir.mkdir()

    commands = {
        "ip": """#!/bin/sh
case "$1" in
    -4) printf '%s\n' 'default via 192.0.2.1 dev wlan0' ;;
    neigh) printf '%s\n' '192.0.2.1 dev wlan0 lladdr 00:11:22:33:44:55 REACHABLE' ;;
esac
""",
        "nmcli": """#!/bin/sh
if [ "$1" = "-g" ]; then
    printf '%s\n' '100 (connected)'
else
    printf '%s\n' "$*" >>"$ACTION_LOG"
fi
""",
        "ping": "#!/bin/sh\nexit 1\n",
        "logger": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
        "sync": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nprintf '%s\n' \"$*\" >>\"$ACTION_LOG\"\n",
    }
    for name, body in commands.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8", newline="\n")
        command.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "RUNTIME_DIRECTORY": str(runtime_dir),
            "STATE_DIRECTORY": str(state_dir),
            "ACTION_LOG": str(action_log),
        }
    )
    result = subprocess.run(
        [shell, str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (runtime_dir / "failures").read_text(encoding="utf-8") == "0\n"
    assert not action_log.exists(), "healthy link must not reconnect or reboot"


def test_network_recovery_reactivates_saved_profile_for_stale_gateway(tmp_path):
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("POSIX shell is unavailable")

    root = Path(__file__).parents[1]
    script = root / "deploy" / "nanoleaf-network-recovery"
    fake_bin = tmp_path / "bin"
    runtime_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    action_log = tmp_path / "actions"
    fake_bin.mkdir()
    runtime_dir.mkdir()
    state_dir.mkdir()
    (runtime_dir / "failures").write_text("2\n", encoding="utf-8")

    commands = {
        "ip": """#!/bin/sh
case "$1" in
    -4) printf '%s\n' 'default via 192.0.2.1 dev wlan0' ;;
    neigh) printf '%s\n' '192.0.2.1 dev wlan0 lladdr 00:11:22:33:44:55 STALE' ;;
esac
""",
        "nmcli": """#!/bin/sh
if [ "$1" = "-g" ]; then
    case "$2" in
        GENERAL.STATE) printf '%s\n' '100 (connected)' ;;
        GENERAL.CONNECTION) printf '%s\n' 'saved-mesh-profile' ;;
    esac
elif [ "$1" = "-t" ]; then
    printf '%s\n' '*:aa\\:bb\\:cc\\:dd\\:ee\\:ff:55'
else
    printf '%s\n' "$*" >>"$ACTION_LOG"
fi
""",
        "ping": "#!/bin/sh\nexit 1\n",
        "logger": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
        "sync": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\nprintf '%s\n' \"$*\" >>\"$ACTION_LOG\"\n",
    }
    for name, body in commands.items():
        command = fake_bin / name
        command.write_text(body, encoding="utf-8", newline="\n")
        command.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "RUNTIME_DIRECTORY": str(runtime_dir),
            "STATE_DIRECTORY": str(state_dir),
            "ACTION_LOG": str(action_log),
        }
    )
    result = subprocess.run(
        [shell, str(script)], check=False, capture_output=True, text=True, env=env,
    )

    assert result.returncode == 0, result.stderr
    actions = action_log.read_text(encoding="utf-8")
    assert "device disconnect wlan0" in actions
    assert "connection up saved-mesh-profile ifname wlan0" in actions
    assert "device connect wlan0" not in actions


def test_pi_observability_and_wifi_policy_are_bounded():
    root = Path(__file__).parents[1]
    journal = (root / "deploy" / "60-nanoleaf-persistent-journal.conf").read_text(
        encoding="utf-8"
    )
    wifi = (root / "deploy" / "90-nanoleaf-wifi-powersave.conf").read_text(
        encoding="utf-8"
    )

    assert "Storage=persistent" in journal
    assert "SystemMaxUse=64M" in journal
    assert "MaxRetentionSec=14day" in journal
    assert "wifi.powersave=2" in wifi
