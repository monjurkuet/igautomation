"""systemd user service renderer for igautomation daemon."""

from __future__ import annotations


def render_service(
    db_path: str = "igautomation.db",
    project_dir: str = "/root/projects/igautomation",
) -> str:
    return f"""[Unit]
Description=IG Automation Daemon — organic Instagram engagement
After=network.target

[Service]
Type=simple
WorkingDirectory={project_dir}
ExecStart={project_dir}/.venv/bin/python3 -m igautomation.daemon --db {db_path} --verbose
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


def service_file_name() -> str:
    return "igautomation-daemon.service"


def user_systemd_dir() -> str:
    import os
    return os.path.expanduser("~/.config/systemd/user")