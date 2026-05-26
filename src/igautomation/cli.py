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

import json
import logging
import asyncio
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from igautomation import __version__
from igautomation.cdp.client import CDPClient
from igautomation.cdp.discovery import TabDiscovery
from igautomation.daemon.loop import DaemonLoop
from igautomation.daemon.strategies import DaemonConfig
from igautomation.graphql.client import GraphQLClient
from igautomation.scraper.analyzer import ProfileAnalyzer
from igautomation.scraper.collector import AccountCollector
from igautomation.cli_content import content_app, collections_app
from igautomation.storage.store import CSVStore, JSONStore, SQLiteStore

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

    # Progress callback
    def on_progress(msg: str, total: int) -> None:
        console.print(f"  [{total}] {msg}")

    collector.on_progress(on_progress)

    strategy_list = [s.strip() for s in strategies.split(",")]
    console.print(f"[bold]Discovering accounts[/bold] (target: {count}, strategies: {strategy_list})")

    accounts = collector.collect(
        seed_usernames=seeds,
        target_count=count,
        strategies=strategy_list,
    )

    console.print(f"\n[bold green]Collected {len(accounts)} accounts[/bold green]")

    # Optional analysis
    if analyze and accounts:
        console.print("\n[bold]Analyzing profiles...[/bold]")
        analyzer = ProfileAnalyzer(cdp)
        profiles = analyzer.analyze(accounts[:count])

        # Save to stores
        store = JSONStore()
        profile_dicts = ProfileAnalyzer.to_dicts(profiles)
        path = store.save(profile_dicts, filename=output, extra={"user_ids": collector.user_ids})
        console.print(f"  JSON saved: {path}")

        csv_store = CSVStore()
        csv_path = csv_store.save(profile_dicts, filename=output.replace(".json", ".csv"))
        console.print(f"  CSV saved: {csv_path}")

        # SQLite
        db = SQLiteStore()
        db.upsert_accounts(profile_dicts)
        db.save_user_ids(collector.user_ids)
        console.print(f"  SQLite: {db.count()} total records")

        # Print summary
        bd_models = ProfileAnalyzer.filter_bd_models(profiles)
        console.print("\n[bold]Results:[/bold]")
        console.print(f"  Total collected: {len(accounts)}")
        console.print(f"  Verified (exist): {len(profiles)}")
        console.print(f"  BD/Model matches: {len(bd_models)}")

        # Print BD models table
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
                    "✓" if p.is_bd else "",
                    "✓" if p.is_model else "",
                )
            console.print(table)
    else:
        # Save just the usernames
        store = JSONStore()
        account_dicts = [{"username": u, "url": f"https://www.instagram.com/{u}/"} for u in accounts]
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

    # Navigate to IG first so fetch() works
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
        verified = "✓" if user.get("is_verified") else ""
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
    console.print(f"\n[bold]Results:[/bold] {len(profiles)} verified, {len(bd_models)} BD/model matches")

    # Persist to database if requested
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
                # Also take a follower snapshot for growth tracking
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
                "✓" if p.is_bd else "",
                "✓" if p.is_model else "",
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
        typer.Option("--strategy", "-s", help="Strategy: feed_browsing|reel_browsing|explore_browsing|discovery|profiling|monitoring|engagement|content_engagement"),
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

    cfg = DaemonConfig.from_yaml(config_file) if config_file else DaemonConfig(db_path=db_path)
    daemon = DaemonLoop(cfg)

    console.print(f"[bold]Running single session[/bold] (strategy: {strategy})")
    result = daemon.run_one(strategy=strategy)

    # Display results
    table = Table(title="Session Results")
    table.add_column("Key", style="dim")
    table.add_column("Value")
    for k, v in result.items():
        table.add_row(k, str(v))
    console.print(table)


# -----------------------------------------------------------------------
# daemon subgroup
# -----------------------------------------------------------------------
daemon_app = typer.Typer(
    name="daemon",
    help="Manage the IG intelligence daemon.",
    no_args_is_help=True,
)
app.add_typer(daemon_app, name="daemon")


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
        import subprocess
        cmd = [sys.executable, "-m", "igautomation.daemon", "--db", db_path]
        if config_file:
            cmd.extend(["--config", config_file])
        if verbose:
            cmd.append("--verbose")
        console.print("[bold]Starting daemon in background...[/bold]")
        proc = subprocess.Popen(cmd, start_new_session=True)
        console.print(f" PID: {proc.pid}")
        # Write PID file
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
        import os
        import signal as sig
        os.kill(pid, sig.SIGTERM)
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
    import asyncio

    cfg = DaemonConfig(db_path=db_path)
    daemon_loop = DaemonLoop(cfg)

    result = asyncio.run(daemon_loop.get_status())

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
    import asyncio
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


# Rename so typer picks up the command name properly
analyze_cmd.__name__ = "analyze"


# -----------------------------------------------------------------------
# db subgroup
# -----------------------------------------------------------------------
db_app = typer.Typer(
    name="db",
    help="Database operations.",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")
app.add_typer(content_app, name="content")
app.add_typer(collections_app, name="collections")

# -----------------------------------------------------------------------
# accounts subgroup — multi-account management
# -----------------------------------------------------------------------
accounts_app = typer.Typer(
    name="accounts",
    help="Manage IG accounts across CDP ports.",
    no_args_is_help=True,
)
app.add_typer(accounts_app, name="accounts")


@accounts_app.command("list")
def accounts_list(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """List all tracked IG accounts and their statuses."""
    import asyncio as _aio
    from igautomation.db.store import AsyncDatabaseStore

    async def _run():
        db = AsyncDatabaseStore(db_path)
        await db.initialize()
        try:
            accounts = await db.get_all_ig_accounts()
            if not accounts:
                console.print("[yellow]No IG accounts tracked yet. Use 'igx accounts add <port>'[/yellow]")
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
    from igautomation.daemon.account_prober import probe_port
    from igautomation.db.store import AsyncDatabaseStore

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
    from igautomation.daemon.account_prober import probe_port
    from igautomation.db.store import AsyncDatabaseStore

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
                console.print("[yellow]No ports to probe. Add accounts first with 'igx accounts add'[/yellow]")
                return

            console.print(f"[bold]Probing {len(port_list)} port(s)...[/bold]")

            for p in port_list:
                result = probe_port(p)
                if result.error:
                    console.print(f"  Port {p}: [red]{result.error}[/red]")
                    await db.update_ig_account_status(
                        (await db.get_ig_account_by_port(p) or {}).get("id", 0),
                        "error"
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
                    if result.full_name:
                        data["full_name"] = result.full_name
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


@db_app.command()
def stats(
    db_path: Annotated[
        str,
        typer.Option("--db", help="Database path"),
    ] = "igautomation.db",
) -> None:
    """Show database statistics."""
    import asyncio
    from igautomation.db.store import AsyncDatabaseStore

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
                from igautomation.scraper.analyzer import TIER_LABELS
                for r in tier_rows:
                    label = TIER_LABELS.get(r["tier"], r["tier"])
                    console.print(f"  {label}: {r['cnt']}")

            if growth_rows:
                console.print("\n[bold]Growth Status:[/bold]")
                from igautomation.scraper.analyzer import GROWTH_LABELS
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
    import asyncio
    from igautomation.db.store import AsyncDatabaseStore

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
    import asyncio
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


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

