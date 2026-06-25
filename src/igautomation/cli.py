"""igautomation CLI — Instagram automation from the terminal.

Usage::

    # Discover 100+ BD model accounts
    igx discover --seeds z.subha_ anonna_fatima --count 100

    # Analyze collected accounts
    igx analyze --input output/accounts.json

    # Search for users
    igx search "bangladeshi model" --count 50

    # List Chrome tabs
    igx tabs

    # Get suggestions for a specific profile
    igx suggest z.subha_
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from igautomation import __version__
from igautomation.cdp.client import CDPClient
from igautomation.cdp.discovery import TabDiscovery
from igautomation.graphql.client import GraphQLClient
from igautomation.scraper.analyzer import ProfileAnalyzer
from igautomation.scraper.collector import AccountCollector
from igautomation.storage.store import CSVStore, JSONStore, SQLiteStore
from igautomation.cli_content import content_app, collections_app
from igautomation.cli_daemon import daemon_app
from igautomation.cli_db import db_app
from igautomation.cli_accounts import accounts_app

app = typer.Typer(
    name="igx",
    help="Instagram automation, exploration, and scraping framework.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False)],
    )


def _get_cdp(port: int = 9224) -> CDPClient:
    """Find an Instagram tab and return a connected CDPClient."""
    base = f"http://localhost:{port}"
    tab = TabDiscovery.find_ig_tab(base)
    if not tab:
        console.print(f"[red]No Instagram tab found on port {port}[/red]")
        console.print("Make sure Chrome is running with --remote-debugging-port")
        raise typer.Exit(1)
    cdp = CDPClient()
    cdp.connect(tab["webSocketDebuggerUrl"])
    return cdp


def version_callback(value: bool) -> None:
    if value:
        console.print(f"igx {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option("--version", "-v", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """igautomation — Instagram automation framework."""


# -----------------------------------------------------------------------
# tabs
# -----------------------------------------------------------------------
@app.command()
def tabs(
    port: Annotated[int, typer.Option("--port", "-p", help="Chrome debug port")] = 9224,
) -> None:
    """List open Chrome tabs with Instagram."""
    base = f"http://localhost:{port}"
    ig_tabs = TabDiscovery.get_ig_tabs(base)

    if not ig_tabs:
        console.print("[yellow]No Instagram tabs found.[/yellow]")
        raise typer.Exit()

    table = Table(title="Instagram Tabs")
    table.add_column("ID", style="dim", max_width=10)
    table.add_column("Title")
    table.add_column("URL")

    for tab in ig_tabs:
        table.add_row(tab.get("id", "")[:8], tab.get("title", ""), tab.get("url", ""))

    console.print(table)


# -----------------------------------------------------------------------
# discover
# -----------------------------------------------------------------------
@app.command()
def discover(
    seeds: Annotated[
        list[str],
        typer.Argument(help="Seed usernames to bootstrap discovery"),
    ] = None,
    count: Annotated[
        int,
        typer.Option("--count", "-n", help="Target number of accounts"),
    ] = 100,
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Chrome debug port"),
    ] = 9224,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output filename"),
    ] = "accounts.json",
    strategies: Annotated[
        str,
        typer.Option(
            "--strategies",
            "-s",
            help="Comma-separated strategy order",
        ),
    ] = "existing_tabs,graphql_suggestions,search,cascade",
    analyze: Annotated[
        bool,
        typer.Option("--analyze/--no-analyze", help="Analyze profiles after collection"),
    ] = True,
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
) -> None:
    """Discover Instagram accounts using multiple strategies."""
    _setup_logging(verbose)
    cdp = _get_cdp(port)
    collector = AccountCollector(cdp)

    def on_progress(msg: str, total: int) -> None:
        console.print(f"  [{total}] {msg}")

    collector.on_progress(on_progress)

    strategy_list = [s.strip() for s in strategies.split(",")]
    console.print(
        f"[bold]Discovering accounts[/bold] (target: {count}, strategies: {strategy_list})"
    )

    accounts = collector.collect(
        seed_usernames=seeds,
        target_count=count,
        strategies=strategy_list,
    )

    console.print(f"\n[bold green]Collected {len(accounts)} accounts[/bold green]")

    if analyze and accounts:
        console.print("\n[bold]Analyzing profiles...[/bold]")
        analyzer = ProfileAnalyzer(cdp)
        profiles = analyzer.analyze(accounts[:count])

        store = JSONStore()
        profile_dicts = ProfileAnalyzer.to_dicts(profiles)
        path = store.save(profile_dicts, filename=output, extra={"user_ids": collector.user_ids})
        console.print(f"  JSON saved: {path}")

        csv_store = CSVStore()
        csv_path = csv_store.save(profile_dicts, filename=output.replace(".json", ".csv"))
        console.print(f"  CSV saved: {csv_path}")

        db = SQLiteStore()
        db.upsert_accounts(profile_dicts)
        db.save_user_ids(collector.user_ids)
        console.print(f"  SQLite: {db.count()} total records")

        bd_models = ProfileAnalyzer.filter_bd_models(profiles)
        console.print("\n[bold]Results:[/bold]")
        console.print(f"  Total collected: {len(accounts)}")
        console.print(f"  Verified (exist): {len(profiles)}")
        console.print(f"  BD/Model matches: {len(bd_models)}")

        if bd_models:
            table = Table(title="Bangladeshi Models")
            table.add_column("#", style="dim")
            table.add_column("Username")
            table.add_column("Full Name")
            table.add_column("Followers")
            table.add_column("BD")
            table.add_column("Model")
            for i, p in enumerate(bd_models[:50], 1):
                table.add_row(
                    str(i),
                    f"[link={p.url}]{p.username}[/link]",
                    p.full_name,
                    p.follower_count,
                    "\u2713" if p.is_bd else "",
                    "\u2713" if p.is_model else "",
                )
            console.print(table)
    else:
        store = JSONStore()
        account_dicts = [
            {"username": u, "url": f"https://www.instagram.com/{u}/"} for u in accounts
        ]
        path = store.save(account_dicts, filename=output, extra={"user_ids": collector.user_ids})
        console.print(f"  Saved: {path}")

    cdp.close()


# -----------------------------------------------------------------------
# search
# -----------------------------------------------------------------------
@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query")],
    count: Annotated[int, typer.Option("--count", "-n", help="Max results")] = 50,
    port: Annotated[int, typer.Option("--port", "-p")] = 9224,
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
) -> None:
    """Search Instagram users."""
    _setup_logging(verbose)
    cdp = _get_cdp(port)

    cdp.navigate("https://www.instagram.com/explore/", wait=2)

    gql = GraphQLClient(cdp)
    results = gql.search_users(query, count=count)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Search: '{query}'")
    table.add_column("#", style="dim")
    table.add_column("Username")
    table.add_column("Full Name")
    table.add_column("Verified")

    for i, user in enumerate(results, 1):
        verified = "\u2713" if user.get("is_verified") else ""
        table.add_row(str(i), user["username"], user.get("full_name", ""), verified)

    console.print(table)
    cdp.close()


# -----------------------------------------------------------------------
# suggest
# -----------------------------------------------------------------------
@app.command()
def suggest(
    username: Annotated[str, typer.Argument(help="Username to get suggestions for")],
    port: Annotated[int, typer.Option("--port", "-p")] = 9224,
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
) -> None:
    """Get suggested/similar accounts for a profile."""
    _setup_logging(verbose)
    cdp = _get_cdp(port)
    gql = GraphQLClient(cdp)

    console.print(f"Resolving user ID for @{username}...")
    uid = gql.get_user_id(username)
    if not uid:
        console.print(f"[red]Could not resolve @{username}[/red]")
        raise typer.Exit(1)

    console.print(f"  User ID: {uid}")
    console.print("Fetching suggestions...")

    suggested = gql.get_suggested_users(uid)

    if not suggested:
        console.print("[yellow]No suggestions found.[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Suggested for @{username}")
    table.add_column("#", style="dim")
    table.add_column("Username")

    for i, name in enumerate(suggested, 1):
        table.add_row(str(i), name)

    console.print(table)
    console.print(f"\n[bold]{len(suggested)} suggestions found[/bold]")
    cdp.close()


# -----------------------------------------------------------------------
# analyze
# -----------------------------------------------------------------------
@app.command()
def analyze(
    input_file: Annotated[
        str,
        typer.Option("--input", "-i", help="JSON file with accounts to analyze"),
    ] = "output/accounts.json",
    port: Annotated[int, typer.Option("--port", "-p")] = 9224,
    save_db: Annotated[bool, typer.Option("--save-db", help="Persist results to database")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
) -> None:
    """Analyze accounts from a JSON file — verify profiles and check BD/model keywords."""
    _setup_logging(verbose)
    path = Path(input_file)
    if not path.exists():
        console.print(f"[red]File not found: {input_file}[/red]")
        raise typer.Exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))
    accounts = data.get("accounts", [])
    usernames = [a.get("username", a) if isinstance(a, dict) else a for a in accounts]

    console.print(f"Analyzing {len(usernames)} accounts from {input_file}...")

    cdp = _get_cdp(port)
    analyzer = ProfileAnalyzer(cdp)
    profiles = analyzer.analyze(usernames)

    bd_models = ProfileAnalyzer.filter_bd_models(profiles)
    console.print(
        f"\n[bold]Results:[/bold] {len(profiles)} verified, {len(bd_models)} BD/model matches"
    )

    if save_db:

        async def _save():
            from igautomation.db.store import AsyncDatabaseStore

            db = AsyncDatabaseStore("igautomation.db")
            await db.initialize()
            saved = 0
            for p in profiles:
                if not p.exists:
                    continue
                acct_data = {
                    "username": p.username,
                    "full_name": p.full_name,
                    "bio": p.bio,
                    "follower_count": p.follower_count,
                    "following_count": p.following_count,
                    "post_count": p.post_count,
                    "is_private": p.is_private,
                    "is_verified": p.is_verified,
                    "tier": p.tier,
                    "category": p.category,
                    "growth_status": p.growth_status,
                }
                if p.profile_pic_url:
                    acct_data["profile_pic_url"] = p.profile_pic_url
                await db.upsert_account(acct_data)
                acct = await db.get_account_by_username(p.username)
                if acct:
                    await db.add_follower_snapshot(
                        acct["id"], p.follower_count, p.following_count, p.post_count
                    )
                saved += 1
            await db.close()
            return saved

        saved = asyncio.run(_save())
        console.print(f"[green]Saved {saved} profiles to database[/green]")

    if bd_models:
        table = Table(title="BD Models")
        table.add_column("#", style="dim")
        table.add_column("Username")
        table.add_column("Full Name")
        table.add_column("Followers")
        table.add_column("BD")
        table.add_column("Model")

        for i, p in enumerate(bd_models, 1):
            table.add_row(
                str(i),
                p.username,
                p.full_name or "",
                str(p.follower_count or 0),
                "\u2713" if p.is_bd else "",
                "\u2713" if p.is_model else "",
            )
        console.print(table)

    cdp.close()


# -----------------------------------------------------------------------
# session — run a single organic session
# -----------------------------------------------------------------------
@app.command()
def session(
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            "-s",
            help="Strategy: feed_browsing|reel_browsing|explore_browsing|discovery|profiling|monitoring|engagement|content_engagement",
        ),
    ] = "feed_browsing",
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
    config_file: Annotated[
        str,
        typer.Option("--config", help="YAML config file"),
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-V")] = False,
) -> None:
    """Run a single organic session and exit."""
    _setup_logging(verbose)

    from igautomation.daemon.loop import DaemonLoop
    from igautomation.daemon.strategies import DaemonConfig

    cfg = DaemonConfig.from_yaml(config_file) if config_file else DaemonConfig(db_path=db_path)
    daemon = DaemonLoop(cfg)

    console.print(f"[bold]Running single session[/bold] (strategy: {strategy})")
    result = daemon.run_one(strategy=strategy)

    table = Table(title="Session Results")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    for k, v in result.items():
        table.add_row(k, str(v))
    console.print(table)


# -----------------------------------------------------------------------
# register sub-apps
# -----------------------------------------------------------------------
app.add_typer(daemon_app, name="daemon")
app.add_typer(db_app, name="db")
app.add_typer(accounts_app, name="accounts")
app.add_typer(content_app, name="content")
app.add_typer(collections_app, name="collections")
