"""Daemon process management — PID file helpers, process liveness check."""

from __future__ import annotations

import os
from pathlib import Path


def pid_path_for(db_path: str) -> Path:
    p = Path(db_path)
    if p.parent == Path("."):
        return Path("daemon.pid")
    return p.parent / "daemon.pid"


def read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def remove_pid(path: Path) -> None:
    path.unlink(missing_ok=True)
