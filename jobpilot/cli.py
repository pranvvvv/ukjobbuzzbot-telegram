"""Command-line entry point: `python -m jobpilot <run|doctor|migrate|version>`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jobpilot import __version__
from jobpilot.core.config import load_settings
from jobpilot.core.errors import JobPilotError
from jobpilot.core.logging import configure_logging

app = typer.Typer(
    name="jobpilot",
    help="JobPilot: a personal AI job-search agent with a Telegram front end.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console()
err_console = Console(stderr=True)

_STYLE = {
    "ok": ("PASS", "green"),
    "warn": ("WARN", "yellow"),
    "fail": ("FAIL", "red"),
    "later": ("OPTIONAL", "cyan"),
    "skip": ("SKIP", "dim"),
}


def _fail(exc: JobPilotError) -> typer.Exit:
    err_console.print(f"[bold red]{exc}[/bold red]")
    return typer.Exit(code=exc.exit_code)


@app.command()
def run() -> None:
    """Start the Telegram bot (and, from Milestone 7, the digest scheduler)."""
    from jobpilot.bot.runner import run_bot  # noqa: PLC0415 - keep `doctor` startup light

    try:
        settings = load_settings()
        configure_logging(settings.log_level, settings.logs_dir)
        asyncio.run(run_bot(settings))
    except JobPilotError as exc:
        raise _fail(exc) from None
    except KeyboardInterrupt:
        console.print("Stopped.")


@app.command()
def doctor(
    sources: Annotated[
        bool, typer.Option("--sources", help="Also make one live call per job source.")
    ] = False,
) -> None:
    """Check Python, .env keys, Telegram token, LLM key, database and folders."""
    from jobpilot.doctor import exit_code, run_checks  # noqa: PLC0415

    results = asyncio.run(run_checks(live_sources=sources))
    table = Table(title=f"JobPilot doctor (v{__version__})", show_lines=False)
    table.add_column("", width=8)
    table.add_column("Check", style="bold")
    table.add_column("Details", overflow="fold")
    for r in results:
        label, colour = _STYLE[r.status.value]
        table.add_row(f"[{colour}]{label}[/{colour}]", r.name, r.detail)
    console.print(table)
    code = exit_code(results)
    console.print(
        "[green]All required checks passed.[/green]"
        if code == 0
        else "[red]Fix the FAIL rows above, then run doctor again.[/red]"
    )
    raise typer.Exit(code=code)


@app.command()
def migrate() -> None:
    """Create or upgrade the database schema to the latest version."""
    from jobpilot.db.migrate import head_revision, upgrade_to_head  # noqa: PLC0415

    try:
        settings = load_settings()
    except JobPilotError as exc:
        raise _fail(exc) from None
    settings.ensure_dirs()
    upgrade_to_head(settings.resolved_database_url)
    console.print(f"Database is at revision {head_revision(settings.resolved_database_url)}.")


@app.command()
def whoami(
    save: Annotated[
        bool,
        typer.Option("--save", help="Write the ID into .env (when exactly one person messaged)."),
    ] = False,
    wait: Annotated[
        int, typer.Option("--wait", min=0, help="Seconds to wait for a message to arrive.")
    ] = 120,
) -> None:
    """Show the Telegram user IDs that messaged your bot (setup helper; needs only the token)."""
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from jobpilot.whoami import (  # noqa: PLC0415
        Sender,
        fetch_bot_username,
        fetch_senders,
        load_token,
        save_allowed_ids,
    )

    async def _find() -> list[Sender]:
        token = load_token()
        async with httpx.AsyncClient(timeout=40) as client:
            username = await fetch_bot_username(token, client)
            console.print(f"Your token belongs to [bold]@{username}[/bold].")
            senders = await fetch_senders(token, client)
            if senders or wait == 0:
                return senders
            console.print(
                f"Waiting up to {wait}s: open [bold]t.me/{username}[/bold] in Telegram "
                "and send /start now..."
            )
            deadline = time.monotonic() + wait
            while not senders and (left := int(deadline - time.monotonic())) > 0:
                senders = await fetch_senders(token, client, wait_seconds=min(25, left))
            return senders

    try:
        senders = asyncio.run(_find())
    except JobPilotError as exc:
        raise _fail(exc) from None
    except httpx.HTTPError as exc:
        err_console.print(f"[red]Can't reach Telegram: {type(exc).__name__}[/red]")
        raise typer.Exit(code=1) from None

    if not senders:
        console.print(
            "No message arrived. Check you messaged the bot named above. If you did, "
            "another program may be using this token and taking its messages "
            "(an old bot, n8n, a webhook): stop it, or create a new token with @BotFather."
        )
        raise typer.Exit(code=1)
    table = Table(title="People who messaged your bot")
    table.add_column("User ID", style="bold")
    table.add_column("Name")
    table.add_column("Username")
    for s in senders:
        table.add_row(str(s.id), s.first_name or "", f"@{s.username}" if s.username else "")
    console.print(table)

    if not save:
        console.print("Put your ID in .env as ALLOWED_USER_IDS=<id>, or rerun with --save.")
    elif len(senders) == 1:
        save_allowed_ids(Path(".env"), [senders[0].id])
        console.print(f"[green]Saved ALLOWED_USER_IDS={senders[0].id} to .env[/green]")
    else:
        console.print("[yellow]More than one person messaged the bot; set the ID by hand.[/yellow]")
        raise typer.Exit(code=1)


@app.command(name="eval")
def eval_cmd(
    ai: Annotated[
        bool, typer.Option("--ai", help="Also run stage-2 AI scoring (uses your key).")
    ] = False,
    local_embeddings: Annotated[
        bool,
        typer.Option("--local-embeddings", help="Use the local bge-small model (downloads once)."),
    ] = False,
    out: Annotated[Path, typer.Option(help="Where to write the markdown report.")] = Path(
        "evals/RESULTS.md"
    ),
) -> None:
    """Measure matching quality on the labeled dataset (precision@k, agreement)."""
    from jobpilot.core.config import default_data_dir  # noqa: PLC0415
    from jobpilot.evals import PersonaResult, report_markdown, run_eval  # noqa: PLC0415
    from jobpilot.llm.base import LLMClient  # noqa: PLC0415
    from jobpilot.llm.embeddings import build_embedder  # noqa: PLC0415
    from jobpilot.llm.providers import build_llm  # noqa: PLC0415

    llm: LLMClient | None = None
    if ai:
        try:
            llm = build_llm(load_settings())
        except JobPilotError as exc:
            raise _fail(exc) from None
    embedder = build_embedder(
        local_embeddings, "BAAI/bge-small-en-v1.5", default_data_dir() / "cache" / "models"
    )
    results: list[PersonaResult] = asyncio.run(run_eval(embedder, llm))
    report = report_markdown(
        results,
        embedder_name=embedder.name,
        llm_name=f"{llm.provider}:{llm.model}" if llm else "off (basic scores)",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    table = Table(title="Matching eval")
    for col in ("Persona", "P@5", "P@10", "Pairwise", "Spearman"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.persona,
            f"{r.precision_at(5):.2f}",
            f"{r.precision_at(10):.2f}",
            f"{r.pairwise_accuracy():.2f}",
            f"{r.spearman():.2f}",
        )
    console.print(table)
    console.print(f"Full report: {out}")


@app.command()
def companies() -> None:
    """Check every board in companies.yaml is live (1 request per company)."""
    import httpx  # noqa: PLC0415

    from jobpilot.sources.boards import load_companies  # noqa: PLC0415

    urls = {
        "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
        "lever": "https://api.lever.co/v0/postings/{t}?mode=json&limit=1",
        "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    }

    async def check() -> list[tuple[str, str, str]]:
        rows = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for c in load_companies():
                try:
                    r = await client.get(urls[c.ats].format(t=c.token))
                    status = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
                except httpx.HTTPError as exc:
                    status = type(exc).__name__
                rows.append((c.name, c.ats, status))
        return rows

    rows = asyncio.run(check())
    table = Table(title="Company boards")
    for col in ("Company", "ATS", "Status"):
        table.add_column(col)
    for name, ats, status in rows:
        colour = "green" if status == "ok" else "red"
        table.add_row(name, ats, f"[{colour}]{status}[/{colour}]")
    console.print(table)
    bad = [r for r in rows if r[2] != "ok"]
    if bad:
        console.print(
            f"[yellow]{len(bad)} board(s) failed: fix or disable them in companies.yaml[/yellow]"
        )
    raise typer.Exit(code=1 if bad else 0)


@app.command()
def version() -> None:
    """Print the JobPilot version."""
    console.print(__version__)
