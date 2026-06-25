"""Accounts CLI subcommands for igx."""

from __future__ import annotations

import logging
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from igautomation.db.store import AsyncDatabaseStore
from igautomation.daemon.account_prober import probe_port

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[logging.StreamHandler()],
    )


accounts_app = typer.Typer(
    name="accounts",
    help="Manage IG accounts across CDP ports.",
    no_args_is_help=True,
)


@accounts_app.command("list")
def accounts_list(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """List all tracked IG accounts and their statuses."""
    import asyncio as _aio

    async def _run():
        db = AsyncDatabaseStore(db_path)
        await db.initialize()
        try:
            accounts = await db.get_all_ig_accounts()
            if not accounts:
                console.print(
                    "[yellow]No IG accounts tracked yet. Use 'igx accounts add <port>'[/yellow]"
                )
                return

            table = Table(title="IG Accounts")
            table.add_column("Port", style="bold")
            table.add_column("Username")
            table.add_column("User ID")
            table.add_column("Status")
            table.add_column("Sessions Today")
            table.add_column("Last Used")

            for a in accounts:
                table.add_row(
                    str(a["port"]),
                    a.get("username") or "—",
                    a.get("user_id") or "—",
                    a.get("status") or "active",
                    str(a.get("daily_session_count") or 0),
                    a.get("last_used_at") or "never",
                )
            console.print(table)
        finally:
            await db.close()

    _aio.run(_run())


@accounts_app.command("add")
def accounts_add(
    port: Annotated[int, typer.Argument(help="CDP port to probe")],
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """Probe a CDP port for IG login and register the account."""
    import asyncio as _aio

    _setup_logging(False)
    console.print(f"[bold]Probing port {port}...[/bold]")

    result = probe_port(port)

    if result.error:
        console.print(f"[red]Probe failed: {result.error}[/red]")
        raise typer.Exit(1)

    console.print(f"  User ID: {result.user_id}")
    console.print(f"  Username: @{result.username or 'unknown'}")
    if result.full_name:
        console.print(f"  Full Name: {result.full_name}")
    if result.follower_count:
        console.print(f"  Followers: {result.follower_count}")

    async def _save():
        db = AsyncDatabaseStore(db_path)
        await db.initialize()
        try:
            data: dict = {
                "port": port,
                "status": "active",
            }
            if result.user_id:
                data["user_id"] = result.user_id
            if result.username:
                data["username"] = result.username
            if result.full_name:
                data["full_name"] = result.full_name
            if result.profile_pic_url:
                data["profile_pic_url"] = result.profile_pic_url
            if result.follower_count:
                data["follower_count"] = result.follower_count
            data["is_private"] = int(result.is_private)
            data["is_verified"] = int(result.is_verified)

            ig_id = await db.upsert_ig_account(data)
            return ig_id
        finally:
            await db.close()

    ig_id = _aio.run(_save())
    console.print(f"[green]Account saved (ig_account_id={ig_id})[/green]")


@accounts_app.command("refresh")
def accounts_refresh(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
    ports: Annotated[
        str,
        typer.Option("--ports", "-p", help="Comma-separated ports (default: all tracked)"),
    ] = "",
) -> None:
    """Re-probe all tracked accounts and update their status."""
    import asyncio as _aio

    _setup_logging(False)

    async def _run():
        db = AsyncDatabaseStore(db_path)
        await db.initialize()
        try:
            if ports:
                port_list = [int(p.strip()) for p in ports.split(",")]
            else:
                existing = await db.get_all_ig_accounts()
                port_list = [a["port"] for a in existing]

            if not port_list:
                console.print(
                    "[yellow]No ports to probe. Add accounts first with 'igx accounts add'[/yellow]"
                )
                return

            console.print(f"[bold]Probing {len(port_list)} port(s)...[/bold]")

            for p in port_list:
                result = probe_port(p)
                if result.error:
                    console.print(f"  Port {p}: [red]{result.error}[/red]")
                    await db.update_ig_account_status(
                        (await db.get_ig_account_by_port(p) or {}).get("id", 0), "error"
                    )
                else:
                    data: dict = {
                        "port": p,
                        "status": "active",
                    }
                    if result.user_id:
                        data["user_id"] = result.user_id
                    if result.username:
                        data["username"] = result.username
                    if result.follower_count:
                        data["follower_count"] = result.follower_count
                    data["is_private"] = int(result.is_private)
                    data["is_verified"] = int(result.is_verified)
                    await db.upsert_ig_account(data)
                    console.print(f"  Port {p}: @{result.username or '?'} — [green]active[/green]")

            console.print("[green]Refresh complete[/green]")
        finally:
            await db.close()

    _aio.run(_run())
