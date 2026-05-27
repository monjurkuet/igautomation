"""Daemon cron configuration — Hermes integration for scheduled analysis and maintenance."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from croniter import croniter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class CronJob(BaseModel):
    name: str
    schedule: str
    command: str
    description: str = ""
    enabled: bool = True


def default_cron_jobs(db_path: str = "igautomation.db") -> list[CronJob]:
    return [
        CronJob(
            name="quality_analysis",
            schedule="0 */6 * * *",
            command=f"igx daemon analyze --type quality --db {db_path}",
            description="Quality review every 6 hours",
        ),
        CronJob(
            name="strategy_analysis",
            schedule="0 */12 * * *",
            command=f"igx daemon analyze --type strategy --db {db_path}",
            description="Strategy optimization every 12 hours",
        ),
        CronJob(
            name="tier_analysis",
            schedule="0 4 * * *",
            command=f"igx daemon analyze --type tier --db {db_path}",
            description="Tier analysis daily at 4 AM UTC",
        ),
        CronJob(
            name="db_export",
            schedule="0 5 * * 0",
            command=f"igx db export --output output/accounts-backup.json --db {db_path}",
            description="Weekly DB export Sunday 5 AM UTC",
        ),
    ]


def validate_schedule(schedule: str) -> bool:
    try:
        croniter(schedule)
        return True
    except (ValueError, KeyError):
        return False


def next_run(schedule: str, now: datetime | None = None) -> datetime | None:
    try:
        c = croniter(schedule, now or datetime.now(timezone.utc))
        return c.get_next(datetime)
    except (ValueError, KeyError):
        return None


def next_runs(jobs: list[CronJob], now: datetime | None = None) -> list[dict]:
    results = []
    for job in jobs:
        if not job.enabled:
            continue
        nxt = next_run(job.schedule, now)
        results.append({
            "name": job.name,
            "schedule": job.schedule,
            "next_run": nxt.isoformat() if nxt else "invalid_schedule",
            "description": job.description,
        })
    return results


def render_crontab(jobs: list[CronJob], project_dir: str, uv_run: bool = True) -> str:
    prefix = f"cd {project_dir} && " if project_dir else ""
    run_cmd = "uv run " if uv_run else ""
    lines = [
        "# BEGIN igautomation managed cron",
        "",
    ]
    for job in jobs:
        if not job.enabled:
            continue
        log_prefix = f"logs/{job.name}.log"
        lines.append(f"{job.schedule} {prefix}{run_cmd}{job.command} >> {log_prefix} 2>&1")
    lines.append("")
    lines.append("# END igautomation managed cron")
    lines.append("")
    return "\n".join(lines)