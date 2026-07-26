from pathlib import Path


def test_systemd_unit_provisions_private_state_directory():
    unit = (Path(__file__).parents[1] / "nanoleaf.service").read_text(
        encoding="utf-8"
    )

    assert "User=nanoleaf" in unit
    assert "Group=nanoleaf" in unit
    assert "WorkingDirectory=/opt/nanoleaf" in unit
    assert "Environment=HOME=/var/lib/nanoleaf" in unit
    assert "StateDirectory=nanoleaf" in unit
    assert "StateDirectoryMode=0700" in unit
    assert "ReadWritePaths=" not in unit
