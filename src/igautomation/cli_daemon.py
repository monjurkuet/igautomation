"""Daemon CLI subcommands for igx."""
from __future__ import annotations

import json
import asyncio
import sys
import os
import signal
import subprocess
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from igautomation.daemon.loop import DaemonLoop
from igautomation.daemon.strategies import DaemonConfig
from igautomation.daemon.cron_config import default_cron_jobs, render_crontab, next_runs
from igautomation.daemon.service_config import render_service, service_file_name, user_systemd_dir

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[logging.StreamHandler()],
    )


daemon_app = typer.Typer(
    name="daemon",
    help="Manage the IG intelligence daemon.",
    no_args_is_help=True,
)


@daemon_app.command()
def start(
    config_file: Annotated[
        str,
        typer.Option("--config", "-c", help="YAML config file"),
    ] = "",
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
    background: Annotated[
        bool,
        typer.Option("--background/--foreground", help="Run as background process"),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
) -> None:
    """Start the IG intelligence daemon."""
    _setup_logging(verbose)

    cfg = DaemonConfig.from_yaml(config_file) if config_file else DaemonConfig(db_path=db_path)
    daemon_loop = DaemonLoop(cfg)

    if background:
        cmd = [sys.executable, "-m", "igautomation.daemon", "--db", db_path]
        if config_file:
            cmd.extend(["--config", config_file])
        if verbose:
            cmd.append("--verbose")
        console.print("[bold]Starting daemon in background...[/bold]")
        proc = subprocess.Popen(cmd, start_new_session=True)
        console.print(f" PID: {proc.pid}")
        pid_path = Path(cfg.db_path).parent / "daemon.pid"
        pid_path.write_text(str(proc.pid))
        console.print(f" PID file: {pid_path}")
    else:
        console.print("[bold]Starting daemon (Ctrl+C to stop)...[/bold]")
        daemon_loop.run_forever()


@daemon_app.command()
def stop() -> None:
    """Stop the running daemon."""
    pid_path = Path("daemon.pid")
    if not pid_path.exists():
        console.print("[yellow]No daemon.pid file found — daemon may not be running.[/yellow]")
        raise typer.Exit()

    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent SIGTERM to daemon (PID {pid})[/green]")
        pid_path.unlink(missing_ok=True)
    except ProcessLookupError:
        console.print(f"[yellow]Process {pid} not found — already stopped?[/yellow]")
        pid_path.unlink(missing_ok=True)
    except PermissionError:
        console.print(f"[red]Permission denied killing PID {pid}[/red]")
        raise typer.Exit(1)


@daemon_app.command()
def status(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """Show current daemon status and database statistics."""

    cfg = DaemonConfig(db_path=db_path)
    daemon_loop = DaemonLoop(cfg)

    result = asyncio.run(daemon_loop.get_status())

    from rich.table import Table
    table = Table(title="Daemon Status")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    for k, v in result.items():
        if isinstance(v, dict):
            v = json.dumps(v, indent=2)
        elif isinstance(v, list):
            v = ", ".join(str(i) for i in v)
        table.add_row(k, str(v))
    console.print(table)


@daemon_app.command()
def analyze_cmd(
    analysis_type: Annotated[
        str,
        typer.Option("--type", "-t", help="Analysis type: quality|strategy|tier"),
    ] = "quality",
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """Run LLM analysis on collected data."""
    from igautomation.analysis.analyzer import AnalysisEngine

    async def _run():
        engine = AnalysisEngine(db_path=db_path)

        if analysis_type == "quality":
            result = await engine.run_quality_review()
        elif analysis_type == "strategy":
            result = await engine.run_strategy_optimization()
        elif analysis_type == "tier":
            result = await engine.run_tier_analysis()
        else:
            console.print(f"[red]Unknown analysis type: {analysis_type}[/red]")
            console.print("Valid types: quality, strategy, tier")
            raise typer.Exit(1)

        from rich.table import Table
        table = Table(title=f"Analysis: {analysis_type}")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_row("analysis_type", result.analysis_type)
        table.add_row("summary", result.summary)
        if result.findings:
            table.add_row("findings", json.dumps(result.findings, indent=2))
        if result.recommendations:
            table.add_row("recommendations", json.dumps(result.recommendations, indent=2))
        if result.metrics:
            table.add_row("metrics", json.dumps(result.metrics, indent=2, default=str))
        console.print(table)

    asyncio.run(_run())


analyze_cmd.__name__ = "analyze"


@daemon_app.command()
def cron_show(
    db_path: Annotated[str, typer.Option("--db", help="Database path")] = "igautomation.db",
    project_dir: Annotated[str, typer.Option("--project-dir", help="Project directory for crontab cd")] = str(Path.cwd()),
) -> None:
    """Print rendered crontab entries."""
    jobs = default_cron_jobs(db_path=db_path)
    crontab = render_crontab(jobs, project_dir=project_dir)
    console.print(crontab)


@daemon_app.command()
def cron_next(
    db_path: Annotated[str, typer.Option("--db", help="Database path")] = "igautomation.db",
) -> None:
    """Print next run times for each cron job."""
    jobs = default_cron_jobs(db_path=db_path)
    runs = next_runs(jobs)
    for r in runs:
        console.print(f"{r['name']}: next at {r['next_run']} ({r['schedule']})")


@daemon_app.command()
def cron_install(
    db_path: Annotated[str, typer.Option("--db", help="Database path")] = "igautomation.db",
    project_dir: Annotated[str, typer.Option("--project-dir", help="Project directory")] = str(Path.cwd()),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print without modifying")] = False,
) -> None:
    """Install cron jobs into user crontab (managed block)."""
    jobs = default_cron_jobs(db_path=db_path)
    crontab_block = render_crontab(jobs, project_dir=project_dir)
    if dry_run:
        console.print("[yellow]Dry run — would install this crontab block:[/yellow]")
        console.print(crontab_block)
        return
    import subprocess as sp
    existing = sp.run(["crontab", "-l"], capture_output=True, text=True).stdout
    new_lines = []
    in_block = False
    for line in existing.splitlines(keepends=True):
        if line.strip().startswith("# BEGIN igautomation managed cron"):
            in_block = True
            continue
        if line.strip().startswith("# END igautomation managed cron"):
            in_block = False
            continue
        if not in_block:
            new_lines.append(line)
    new_cron = "".join(new_lines) + "\n" + crontab_block
    cron_proc = sp.run(["crontab", "-"], input=new_cron, capture_output=True, text=True)
    if cron_proc.returncode == 0:
        console.print("[green]Cron jobs installed[/green]")
    else:
        console.print(f"[red]Failed: {cron_proc.stderr}[/red]")


@daemon_app.command()
def cron_uninstall(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print without modifying")] = False,
) -> None:
    """Remove igautomation managed cron block."""
    import subprocess as sp
    existing = sp.run(["crontab", "-l"], capture_output=True, text=True).stdout
    new_lines = []
    in_block = False
    for line in existing.splitlines(keepends=True):
        if line.strip().startswith("# BEGIN igautomation managed cron"):
            in_block = True
            continue
        if line.strip().startswith("# END igautomation managed cron"):
            in_block = False
            continue
        if not in_block:
            new_lines.append(line)
    new_cron = "".join(new_lines)
    if dry_run:
        console.print("[yellow]Dry run — would install this crontab:[/yellow]")
        console.print(new_cron)
        return
    cron_proc = sp.run(["crontab", "-"], input=new_cron, capture_output=True, text=True)
    if cron_proc.returncode == 0:
        console.print("[green]Cron uninstalled[/green]")
    else:
        console.print(f"[red]Failed: {cron_proc.stderr}[/red]")


@daemon_app.command()
def service_show(
    db_path: Annotated[str, typer.Option("--db", help="Database path")] = "igautomation.db",
    project_dir: Annotated[str, typer.Option("--project-dir", help="Project directory")] = str(Path.cwd()),
) -> None:
    """Print systemd user service file content."""
    svc = render_service(db_path=db_path, project_dir=project_dir)
    console.print(svc)


@daemon_app.command()
def service_install(
    db_path: Annotated[str, typer.Option("--db", help="Database path")] = "igautomation.db",
    project_dir: Annotated[str, typer.Option("--project-dir", help="Project directory")] = str(Path.cwd()),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print without modifying")] = False,
) -> None:
    """Install systemd user service file."""
    svc = render_service(db_path=db_path, project_dir=project_dir)
    if dry_run:
        console.print("[yellow]Dry run — would write:[/yellow]")
        console.print(svc)
        return
    svc_dir = user_systemd_dir()
    Path(svc_dir).mkdir(parents=True, exist_ok=True)
    svc_path = Path(svc_dir) / service_file_name()
    svc_path.write_text(svc)
    console.print(f"[green]Service file written to {svc_path}[/green]")


@daemon_app.command()
def service_uninstall(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print without modifying")] = False,
) -> None:
    """Remove systemd user service file."""
    svc_path = Path(user_systemd_dir()) / service_file_name()
    if dry_run:
        console.print(f"[yellow]Dry run — would remove {svc_path}[/yellow]")
        return
    if svc_path.exists():
        svc_path.unlink()
        console.print(f"[green]Removed {svc_path}[/green]")
    else:
        console.print("[yellow]Service file not found[/yellow]")