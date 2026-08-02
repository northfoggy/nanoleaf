from pathlib import Path


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
    assert "nanoleafapi" not in script.lower()
    assert 'reconnect_threshold="${NANOLEAF_RECONNECT_THRESHOLD:-3}"' in script
    assert 'reboot_threshold="${NANOLEAF_REBOOT_THRESHOLD:-8}"' in script
    assert 'reboot_cooldown="${NANOLEAF_REBOOT_COOLDOWN_SECONDS:-21600}"' in script
    assert 'nmcli device connect "$interface"' in script
    assert "systemctl reboot" in script

    assert "Type=oneshot" in service
    assert "StateDirectory=nanoleaf-network-recovery" in service
    assert "ProtectSystem=strict" in service
    assert "OnBootSec=5min" in timer
    assert "OnUnitActiveSec=2min" in timer


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
