"""Persistent configuration for nanoleaf-ctl.

Stores device IP and auth token in ~/.config/nanoleaf-ctl/config.json
so you don't have to re-authenticate every time.
"""

import fcntl
import json
import os
import socket
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "nanoleaf-ctl"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOCK_FILE = CONFIG_DIR / "sunlight.lock"


def load() -> dict:
    """Load saved configuration, returning empty dict if none exists."""
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def save(cfg: dict) -> None:
    """Persist configuration to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def get_device() -> tuple[str | None, str | None]:
    """Return (ip, auth_token) from saved config."""
    cfg = load()
    return cfg.get("ip"), cfg.get("auth_token")


def save_device(ip: str, auth_token: str) -> None:
    """Save device IP and auth token."""
    cfg = load()
    cfg["ip"] = ip
    cfg["auth_token"] = auth_token
    save(cfg)


def clear() -> None:
    """Remove saved configuration."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()


def acquire_sunlight_lock():
    """Acquire an exclusive lock to prevent duplicate sunlight instances.

    Uses fcntl.flock which is automatically released when the process
    exits (even on crash/kill/OOM), so stale lock files aren't possible.

    Returns the lock file descriptor on success, or None if another
    instance is already running.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        lock_fd.close()
        return None
    lock_fd.write(f"{socket.gethostname()}:{os.getpid()}")
    lock_fd.flush()
    return lock_fd


def read_lock_info() -> str | None:
    """Read the hostname:pid from the lock file, if it exists."""
    try:
        return LOCK_FILE.read_text().strip() or None
    except (OSError, FileNotFoundError):
        return None


def release_sunlight_lock(lock_fd):
    """Release the sunlight simulator lock."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except (IOError, OSError):
            pass
