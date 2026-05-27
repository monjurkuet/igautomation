"""Tests for daemon process management, cron config, and systemd service."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from igautomation.daemon.process import pid_path_for, read_pid, write_pid, remove_pid, is_process_running
from igautomation.daemon.cron_config import (
    CronJob,
    default_cron_jobs,
    render_crontab,
    next_runs,
    validate_schedule,
)
from igautomation.daemon.service_config import render_service, service_file_name, user_systemd_dir


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------


class TestPidPath:
    def test_default_db(self):
        assert pid_path_for("igautomation.db") == Path("daemon.pid")

    def test_custom_db_path(self):
        assert pid_path_for("/tmp/x/ig.db") == Path("/tmp/x/daemon.pid")

    def test_nested_path(self):
        assert pid_path_for("data/dbs/main.db") == Path("data/dbs/daemon.pid")


class TestPidReadWrite:
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "daemon.pid"
            write_pid(p, 12345)
            assert read_pid(p) == 12345

    def test_missing_file(self):
        assert read_pid(Path("/nonexistent/daemon.pid")) is None

    def test_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "daemon.pid"
            write_pid(p, 42)
            assert p.exists()
            remove_pid(p)
            assert not p.exists()

    def test_remove_nonexistent_does_not_raise(self):
        remove_pid(Path("/nonexistent/daemon.pid"))


class TestProcessRunning:
    def test_current_process(self):
        pid = os.getpid()
        assert is_process_running(pid) is True

    def test_nonexistent_pid(self):
        pid = 999999999
        assert is_process_running(pid) is False

    def test_negative_pid(self):
        # os.kill(-1, 0) returns True for some systems (it sends to all processes)
        result = is_process_running(-1)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Cron config
# ---------------------------------------------------------------------------


class TestCronJob:
    def test_defaults(self):
        job = CronJob(name="test", schedule="0 * * * *", command="echo hello")
        assert job.name == "test"
        assert job.enabled is True
        assert job.description == ""

    def test_custom(self):
        job = CronJob(
            name="quality",
            schedule="0 */6 * * *",
            command="igx analyze --type quality",
            description="Quality check",
            enabled=False,
        )
        assert job.enabled is False


class TestValidateSchedule:
    def test_valid(self):
        assert validate_schedule("0 * * * *") is True
        assert validate_schedule("*/15 * * * *") is True

    def test_invalid(self):
        assert validate_schedule("not-a-schedule") is False
        assert validate_schedule("") is False


class TestDefaultCronJobs:
    def test_returns_list(self):
        jobs = default_cron_jobs()
        assert len(jobs) >= 4
        assert all(isinstance(j, CronJob) for j in jobs)

    def test_all_enabled_by_default(self):
        jobs = default_cron_jobs()
        assert all(j.enabled for j in jobs)


class TestNextRuns:
    def test_returns_list_of_dicts(self):
        jobs = default_cron_jobs()
        runs = next_runs(jobs)
        assert len(runs) >= 4
        for r in runs:
            assert "name" in r
            assert "next_run" in r

    def test_disabled_jobs_excluded(self):
        jobs = [CronJob(name="test", schedule="0 * * * *", command="echo", enabled=False)]
        runs = next_runs(jobs)
        assert len(runs) == 0


class TestRenderCrontab:
    def test_contains_managed_blocks(self):
        jobs = default_cron_jobs()
        crontab = render_crontab(jobs, project_dir="/root/projects/igautomation")
        assert "# BEGIN igautomation managed cron" in crontab
        assert "# END igautomation managed cron" in crontab

    def test_includes_uv_run(self):
        crontab = render_crontab(default_cron_jobs(), project_dir="/root/projects/igautomation")
        assert "uv run igx" in crontab

    def test_no_project_dir(self):
        crontab = render_crontab(default_cron_jobs(), project_dir="")
        assert "cd " not in crontab

    def test_deterministic(self):
        j = default_cron_jobs("test.db")
        r1 = render_crontab(j, project_dir="/p")
        r2 = render_crontab(j, project_dir="/p")
        assert r1 == r2


# ---------------------------------------------------------------------------
# Systemd service
# ---------------------------------------------------------------------------


class TestServiceConfig:
    def test_render_contains_section(self):
        svc = render_service()
        assert "[Unit]" in svc
        assert "[Service]" in svc
        assert "[Install]" in svc

    def test_includes_igx_command(self):
        svc = render_service(db_path="custom.db")
        assert "igx daemon start --foreground --db custom.db" in svc

    def test_includes_working_directory(self):
        svc = render_service(project_dir="/myproject")
        assert "WorkingDirectory=/myproject" in svc

    def test_restart_on_failure(self):
        svc = render_service()
        assert "Restart=on-failure" in svc
        assert "RestartSec=60" in svc

    def test_file_name(self):
        assert service_file_name() == "igautomation-daemon.service"

    def test_user_systemd_dir(self):
        d = user_systemd_dir()
        assert ".config/systemd/user" in d