import os
import stat

from nanoleaf_ctl import config


def _use_temp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "LOCK_FILE", tmp_path / "sunlight.lock")


def test_save_is_atomic_and_private(monkeypatch, tmp_path):
    _use_temp_config(monkeypatch, tmp_path)

    config.save_device("192.0.2.10", "secret-token")

    assert config.get_device() == ("192.0.2.10", "secret-token")
    assert list(tmp_path.glob("*.tmp")) == []
    if os.name != "nt":
        assert stat.S_IMODE(config.CONFIG_FILE.stat().st_mode) == 0o600


def test_lock_preserves_holder_and_can_be_reacquired(monkeypatch, tmp_path):
    _use_temp_config(monkeypatch, tmp_path)

    first = config.acquire_sunlight_lock()
    try:
        assert first is not None
        assert config.read_lock_info()
        assert config.acquire_sunlight_lock() is None
        assert config.read_lock_info()
    finally:
        config.release_sunlight_lock(first)

    second = config.acquire_sunlight_lock()
    assert second is not None
    config.release_sunlight_lock(second)
