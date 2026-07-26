"""Persistent configuration for nanoleaf-ctl.

Stores device IP and auth token in ~/.config/nanoleaf-ctl/config.json
so you don't have to re-authenticate every time.
"""

import json
import os
import socket
import tempfile
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

CONFIG_DIR = Path.home() / ".config" / "nanoleaf-ctl"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOCK_FILE = CONFIG_DIR / "sunlight.lock"


def load() -> dict:
    """Load saved configuration, returning empty dict if none exists."""
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save(cfg: dict) -> None:
    """Persist configuration atomically with owner-only permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=CONFIG_DIR)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(json.dumps(cfg, indent=2) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CONFIG_FILE)
    finally:
        temp_path.unlink(missing_ok=True)


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

    Uses an OS-level file lock which is automatically released when the
    process exits, so stale lock files aren't possible.

    Returns the lock file descriptor on success, or None if another
    instance is already running.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            lock_fd.seek(0, os.SEEK_END)
            if lock_fd.tell() == 0:
                lock_fd.write("\0")
                lock_fd.flush()
            lock_fd.seek(0)
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        lock_fd.close()
        return None
    lock_fd.seek(1 if os.name == "nt" else 0)
    lock_fd.truncate()
    lock_fd.write(f"{socket.gethostname()}:{os.getpid()}")
    lock_fd.flush()
    return lock_fd


def read_lock_info() -> str | None:
    """Read the hostname:pid from the lock file, if it exists."""
    try:
        with LOCK_FILE.open(encoding="utf-8") as lock_file:
            if os.name == "nt":
                lock_file.seek(1)
            return lock_file.read().strip("\0\r\n ") or None
    except (OSError, FileNotFoundError):
        return None


def release_sunlight_lock(lock_fd):
    """Release the sunlight simulator lock."""
    if lock_fd:
        try:
            if os.name == "nt":
                lock_fd.seek(0)
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except (IOError, OSError):
            pass
