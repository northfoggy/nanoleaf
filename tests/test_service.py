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
