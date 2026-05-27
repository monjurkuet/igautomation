"""systemd user service renderer for igautomation daemon."""

from __future__ import annotations


def render_service(
    db_path: str = "igautomation.db",
    project_dir: str = "/root/projects/igautomation",
) -> str:
    return f"""[Unit]
Description=igautomation daemon
After=network.target

[Service]
Type=simple
WorkingDirectory={project_dir}
ExecStart=uv run igx daemon start --foreground --db {db_path}
Restart=on-failure
RestartSec=60
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def service_file_name() -> str:
    return "igautomation-daemon.service"


def user_systemd_dir() -> str:
    import os
    return os.path.expanduser("~/.config/systemd/user")