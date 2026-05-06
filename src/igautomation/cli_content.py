"""Content and collections CLI subcommands for igx."""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from igautomation.cdp.discovery import TabDiscovery
from igautomation.content.loader import load_csv
from igautomation.content.models import ContentItem, ContentType, EngagementStatus
from igautomation.content.analyzer import analyze_content, analyze_content_browse, batch_analyze
from igautomation.db.store import AsyncDatabaseStore

console = Console()
CDP_BASE_URL = "http://localhost:9224"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[logging.StreamHandler()],
    )


content_app = typer.Typer(
    name="content",
    help="Content engagement: load, analyze, and interact with IG posts/reels.",
    no_args_is_help=True,
)

collections_app = typer.Typer(
    name="collections",
    help="Manage IG Saved collections.",
    no_args_is_help=True,
)


@content_app.command("load")
def content_load(
    csv_path: Annotated[str, typer.Argument(help="Path to CSV file with content URLs")],
    db_path: Annotated[str, typer.Option("--db", help="SQLite database path")] = "igautomation.db",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Load content items from a CSV file into the database."""
    _setup_logging(verbose)
    items = load_csv(csv_path)
    console.print(f"[bold green]Loaded {len(items)} content items from {csv_path}[/]")

    type_counts: dict[str, int] = {}
    for item in items:
        type_counts[item.content_type.value] = type_counts.get(item.content_type.value, 0) + 1

    table = Table(title="Content Summary")
    table.add_column("Type", style="cyan")
    table.add_column("Count", justify="right")
    for ct, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        table.add_row(ct, str(count))
    table.add_row("[bold]Total[/]", f"[bold]{len(items)}[/]")
    console.print(table)

    async def _import():
        store = AsyncDatabaseStore(db_path)
        await store.initialize()
        imported = 0
        skipped = 0
        for item in items:
            try:
                await store.upsert_content_item({
                    "url": item.url.strip(),
                    "content_type": item.content_type.value,
                    "category": item.category,
                    "notes": item.notes,
                    "priority": item.priority,
                })
                imported += 1
            except Exception as exc:
                skipped += 1
                if verbose:
                    console.print(f"[dim]Skip {item.url}: {exc}[/]")
        await store.close()
        return imported, skipped

    imported, skipped = asyncio.run(_import())
    console.print(f"[green]Imported: {imported}[/]  [dim]Skipped: {skipped}[/]")


@content_app.command("analyze")
def content_analyze(
    db_path: Annotated[str, typer.Option("--db", help="SQLite database path")] = "igautomation.db",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max items to analyze")] = 50,
    delay: Annotated[float, typer.Option("--delay", help="Seconds between items (min human-like gap)")] = 2.0,
    dwell: Annotated[float, typer.Option("--dwell", help="Seconds to dwell on each post (read/watch)")] = 3.0,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Skip CDP browsing, use API-only analysis")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Analyze pending content by browsing each post in Chrome, then LLM.

    By default, navigates to each post/reel in Chrome like a real user,
    extracts the caption, hashtags, alt text, and visible context, then
    sends that real content to the LLM for categorization. Use --no-browser
    for API-only fallback (no page context, just URL-based guessing).
    """
    _setup_logging(verbose)

    async def _run():
        store = AsyncDatabaseStore(db_path)
        await store.initialize()

        rows = await store.get_content_items_by_status("pending", limit=limit)
        if not rows:
            console.print("[yellow]No pending content items to analyze.[/]")
            await store.close()
            return None

        mode = "browser" if not no_browser else "api-only"
        console.print(f"[bold]Analyzing {len(rows)} items ({mode} mode)...[/]")

        items = []
        for row in rows:
            items.append(ContentItem(
                url=row["url"],
                content_type=ContentType(row.get("content_type", "unknown") or "unknown"),
                category=row.get("category", "") or "",
                notes=row.get("notes", "") or "",
                priority=row.get("priority", 5) or 5,
            ))

        # Connect to Chrome CDP for browser-based analysis
        cdp = None
        if not no_browser:
            try:
                from igautomation.cdp.client import CDPClient
                ig_tab = TabDiscovery.find_ig_tab()
                if ig_tab:
                    cdp = CDPClient()
                    cdp.connect(ig_tab["webSocketDebuggerUrl"])
                    console.print("[dim]Connected to Chrome — browsing each post...[/]")
                else:
                    console.print("[yellow]No IG tab found — falling back to API-only[/]")
            except Exception as exc:
                console.print(f"[yellow]CDP connection failed: {exc} — falling back to API-only[/]")
                cdp = None

        analyzed = batch_analyze(items, delay=delay, cdp=cdp, dwell=dwell)

        if cdp:
            try:
                cdp.close()
            except Exception:
                pass

        updated = 0
        for item in analyzed:
            try:
                await store.upsert_content_item({
                    "url": item.url.strip(),
                    "llm_analysis": item.llm_analysis,
                    "llm_collection_suggestion": item.llm_collection_suggestion,
                    "llm_tags": ", ".join(item.llm_tags),
                    "is_bd_relevant": 1 if item.is_bd_relevant else 0,
                    "content_niche": item.content_niche,
                    "engagement_status": "analyzed",
                })

                if item.llm_collection_suggestion:
                    await store.upsert_collection(
                        name=item.llm_collection_suggestion,
                        description="Auto-created collection for " + (item.content_niche or "content"),
                    )

                updated += 1
            except Exception as exc:
                if verbose:
                    console.print(f"[red]Error updating {item.url}: {exc}[/]")

        await store.close()
        return updated, len(analyzed)

    result = asyncio.run(_run())
    if result:
        updated, total = result
        console.print(f"[green]Analyzed: {updated}/{total}[/] items updated in DB")


@content_app.command("stats")
def content_stats(
    db_path: Annotated[str, typer.Option("--db", help="SQLite database path")] = "igautomation.db",
) -> None:
    """Show content engagement statistics."""
    async def _run():
        store = AsyncDatabaseStore(db_path)
        await store.initialize()
        stats = await store.get_content_stats()
        await store.close()
        return stats

    stats = asyncio.run(_run())

    console.print("")
    console.print("[bold]Content Stats[/]")
    console.print("  Total items: " + str(stats.get("total_items", 0)))
    console.print("  Total collections: " + str(stats.get("total_collections", 0)))
    console.print("  Total engagement actions: " + str(stats.get("total_engagement_actions", 0)))

    by_status = stats.get("by_status")
    if by_status:
        console.print("")
        console.print("  [cyan]By Status:[/]")
        for status, count in by_status.items():
            console.print("    " + status + ": " + str(count))

    by_type = stats.get("by_type")
    if by_type:
        console.print("")
        console.print("  [cyan]By Type:[/]")
        for ct, count in by_type.items():
            console.print("    " + ct + ": " + str(count))

    by_niche = stats.get("by_niche")
    if by_niche:
        console.print("")
        console.print("  [cyan]Top Niches:[/]")
        for niche, count in list(by_niche.items())[:10]:
            console.print("    " + niche + ": " + str(count))

    by_col = stats.get("by_collection_suggestion")
    if by_col:
        console.print("")
        console.print("  [cyan]Suggested Collections:[/]")
        for col, count in list(by_col.items())[:10]:
            console.print("    " + col + ": " + str(count))

    eng = stats.get("engagement_actions")
    if eng:
        console.print("")
        console.print("  [cyan]Engagement Actions:[/]")
        for action, statuses in eng.items():
            parts = ", ".join(s + "=" + str(c) for s, c in statuses.items())
            console.print("    " + action + ": " + parts)


@content_app.command("engage")
def content_engage(
    db_path: Annotated[str, typer.Option("--db", help="SQLite database path")] = "igautomation.db",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max items to engage")] = 20,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Simulate without actual engagement")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Engage with analyzed content: like, save, add to collections."""
    _setup_logging(verbose)
    console.print("[bold yellow]Content engagement - uses Chrome CDP on localhost:9224[/]")
    if dry_run:
        console.print("[dim]DRY RUN - no actual engagement will be performed[/]")

    async def _run():
        store = AsyncDatabaseStore(db_path)
        await store.initialize()

        rows = await store.get_content_items_by_status("analyzed", limit=limit)
        if not rows:
            console.print("[yellow]No analyzed content items ready for engagement.[/]")
            await store.close()
            return None

        console.print(f"[bold]Engaging with {len(rows)} content items...[/]")

        if dry_run:
            for row in rows[:10]:
                col = row.get("llm_collection_suggestion", "no collection") or "no collection"
                console.print("  [dim]" + row["url"][:60] + "... -> " + col + "[/]")
            await store.close()
            return None

        from igautomation.behavior.config import BehaviorConfig
        from igautomation.content.engager import ContentEngager

        discovery = TabDiscovery(CDP_BASE_URL)
        ig_tab = discovery.find_ig_tab()
        if not ig_tab:
            console.print("[red]No Instagram tab found in Chrome![/]")
            await store.close()
            return None

        from igautomation.cdp.client import CDPClient
        cdp = CDPClient()
        cdp.connect(ig_tab["webSocketDebuggerUrl"])
        config = BehaviorConfig()
        session = config.new_session()

        engager = ContentEngager(cdp, store, config, session)

        results = []
        for i, row in enumerate(rows):
            item = ContentItem(
                url=row["url"],
                content_type=ContentType(row.get("content_type", "unknown") or "unknown"),
                category=row.get("category", "") or "",
                notes=row.get("notes", "") or "",
                llm_collection_suggestion=row.get("llm_collection_suggestion", "") or "",
                is_bd_relevant=bool(row.get("is_bd_relevant", 0)),
                content_niche=row.get("content_niche", "") or "",
            )

            console.print(f"  [{i+1}/{len(rows)}] " + row["url"][:50] + "...")
            result = engager.engage_content(item)
            results.append((item, result))

            content_db = await store.get_content_item_by_url(item.url)
            if content_db:
                eng_status = "engaged" if result.like.value == "done" else "partial"
                await store.update_content_engagement_status(content_db["id"], eng_status)

            if result.collection and result.collection_added.value == "done":
                col = await store.get_collection_by_name(result.collection)
                if col and content_db:
                    await store.add_content_to_collection(content_db["id"], col["id"])

        cdp.close()
        await store.close()
        return results

    result = asyncio.run(_run())
    if result:
        done = sum(1 for _, r in result if r.like.value == "done")
        saved = sum(1 for _, r in result if r.save.value == "done")
        console.print("")
        console.print(f"[bold green]Engagement complete: {done} liked, {saved} saved[/]")


# ------------------------------------------------------------------
# collections
# ------------------------------------------------------------------


@collections_app.command("list")
def collections_list(
    db_path: Annotated[str, typer.Option("--db", help="SQLite database path")] = "igautomation.db",
) -> None:
    """List all collections and their item counts."""
    async def _run():
        store = AsyncDatabaseStore(db_path)
        await store.initialize()
        cols = await store.get_all_collections()
        await store.close()
        return cols

    cols = asyncio.run(_run())

    if not cols:
        console.print("[yellow]No collections found.[/]")
        return

    table = Table(title="Collections")
    table.add_column("Name", style="cyan")
    table.add_column("Items", justify="right")
    table.add_column("Description", style="dim")
    for c in cols:
        desc = (c.get("description", "") or "")[:40]
        table.add_row(c["name"], str(c.get("item_count", 0)), desc)
    console.print(table)


@collections_app.command("create")
def collections_create(
    name: Annotated[str, typer.Argument(help="Collection name")],
    description: Annotated[str, typer.Option("--desc", help="Description")] = "",
    db_path: Annotated[str, typer.Option("--db", help="SQLite database path")] = "igautomation.db",
) -> None:
    """Create a new collection in the database."""
    async def _run():
        store = AsyncDatabaseStore(db_path)
        await store.initialize()
        col_id = await store.upsert_collection(name, description=description)
        await store.close()
        return col_id

    col_id = asyncio.run(_run())
    console.print(f"[green]Collection created: {name} (id={col_id})[/]")
