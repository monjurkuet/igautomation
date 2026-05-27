"""Database CLI subcommands for igx."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from igautomation.db.store import AsyncDatabaseStore
from igautomation.scraper.analyzer import TIER_LABELS, GROWTH_LABELS

console = Console()

db_app = typer.Typer(
    name="db",
    help="Database operations.",
    no_args_is_help=True,
)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@db_app.command()
def stats(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """Show database statistics."""

    async def _show_stats():
        db = AsyncDatabaseStore(db_path)
        await db.initialize()
        try:
            cur = await db.db.execute("SELECT COUNT(*) FROM accounts")
            row = await cur.fetchone()
            total = row[0] if row else 0

            cur = await db.db.execute(
                "SELECT tier, COUNT(*) as cnt FROM accounts WHERE tier IS NOT NULL GROUP BY tier"
            )
            tier_rows = await cur.fetchall()

            disc_stats = await db.get_discovery_stats()

            cur = await db.db.execute("SELECT COUNT(*) FROM interaction_log")
            row = await cur.fetchone()
            interactions = row[0] if row else 0

            cur = await db.db.execute("SELECT COUNT(*) FROM sessions")
            row = await cur.fetchone()
            sessions = row[0] if row else 0

            cur = await db.db.execute(
                "SELECT growth_status, COUNT(*) as cnt FROM accounts WHERE growth_status IS NOT NULL GROUP BY growth_status"
            )
            growth_rows = await cur.fetchall()

            console.print(f"\n[bold]Database: {db_path}[/bold]")
            console.print(f"  Accounts: {total}")
            console.print(f"  Interactions: {interactions}")
            console.print(f"  Sessions: {sessions}")

            if tier_rows:
                console.print("\n[bold]By Tier:[/bold]")
                for r in tier_rows:
                    label = TIER_LABELS.get(r["tier"], r["tier"])
                    console.print(f"  {label}: {r['cnt']}")

            if growth_rows:
                console.print("\n[bold]Growth Status:[/bold]")
                for r in growth_rows:
                    label = GROWTH_LABELS.get(r["growth_status"], r["growth_status"])
                    console.print(f"  {label}: {r['cnt']}")

            if disc_stats:
                console.print("\n[bold]Discovery Strategies:[/bold]")
                for strategy, count in disc_stats.items():
                    console.print(f"  {strategy}: {count}")

        finally:
            await db.close()

    asyncio.run(_show_stats())


@db_app.command()
def export(
    output_file: Annotated[
        str,
        typer.Option("--output", "-o", help="Output JSON file"),
    ] = "igautomation_export.json",
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """Export database to JSON."""

    async def _export():
        db = AsyncDatabaseStore(db_path)
        await db.initialize()
        try:
            cur = await db.db.execute("SELECT * FROM accounts ORDER BY relevance_score DESC")
            rows = await cur.fetchall()
            accounts = [dict(r) for r in rows]

            data = {
                "exported_at": _now_iso(),
                "total_accounts": len(accounts),
                "accounts": accounts,
            }

            Path(output_file).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            console.print(f"[green]Exported {len(accounts)} accounts to {output_file}[/green]")
        finally:
            await db.close()

    asyncio.run(_export())


@db_app.command()
def migrate(
    from_db: Annotated[
        str,
        typer.Option("--from-db", help="Path to old SQLite database"),
    ] = "output/igautomation.db",
    from_json: Annotated[
        str,
        typer.Option("--from-json", help="Path to old JSON export"),
    ] = "output/bd_models.json",
    to_db: Annotated[
        str,
        typer.Option("--to", help="Path to new database"),
    ] = "igautomation.db",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be migrated without writing"),
    ] = False,
) -> None:
    """Migrate data from old schema to the new database."""
    from igautomation.migrate import Migrator

    migrator = Migrator(
        old_db_path=from_db,
        new_db_path=to_db,
        json_path=from_json,
    )

    stats = asyncio.run(migrator.run(dry_run=dry_run))

    table = Table(title="Migration Results")
    table.add_column("Metric", style="dim")
    table.add_column("Count")
    for k, v in stats.items():
        table.add_row(k, str(v))
    console.print(table)