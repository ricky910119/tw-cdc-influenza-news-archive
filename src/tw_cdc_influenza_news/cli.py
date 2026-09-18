"""Command-line interface.

    python -m tw_cdc_influenza_news doctor
    python -m tw_cdc_influenza_news crawl --start-date ... --end-date ... --provider ...
    python -m tw_cdc_influenza_news resume --run-id RUN_ID
    python -m tw_cdc_influenza_news import-urls urls.jsonl
    python -m tw_cdc_influenza_news export --run-id RUN_ID --format json
    python -m tw_cdc_influenza_news report --run-id RUN_ID

``doctor`` is the command to run first on a new machine: it checks the
environment and prints what is missing, without touching the network and
without ever printing a secret.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from tw_cdc_influenza_news import DB_SCHEMA_VERSION, __version__
from tw_cdc_influenza_news.config import (
    CONFIG_PATH_ENV,
    AppConfig,
    load_config,
)
from tw_cdc_influenza_news.database import Database
from tw_cdc_influenza_news.dates import effective_end_date, today_in
from tw_cdc_influenza_news.discovery import (
    PROVIDER_REGISTRY,
    ManualUrlsProvider,
    enabled_provider_names,
)
from tw_cdc_influenza_news.errors import ConfigError
from tw_cdc_influenza_news.logging_utils import configure_logging
from tw_cdc_influenza_news.models import COMPLETENESS_STATEMENT
from tw_cdc_influenza_news.orchestrator import Orchestrator, new_run_id, run_crawl
from tw_cdc_influenza_news.reporting import build_coverage_report, export_run, render_markdown

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Archive discoverable Taiwan CDC / influenza news coverage.",
)
console = Console()

ConfigOption = typer.Option(
    None,
    "--config",
    "-c",
    help=f"Path to the YAML config (default: ${CONFIG_PATH_ENV} or configs/default.yaml).",
)


def _load(config_path: str | None) -> AppConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _open_db(config: AppConfig) -> Database:
    db = Database(config.database_path)
    db.initialize()
    return db


def _setup_logging(config: AppConfig, *, quiet: bool = False) -> None:
    configure_logging(
        level=config.logging.level,
        log_file=config.log_path,
        redact_keys=config.logging.redact_keys,
        quiet_console=quiet,
    )


def _run_providers(row) -> list[str]:
    """Provider names stored on a run row. Stored as JSON, read as JSON."""
    try:
        parsed = json.loads(row["providers_json"] or "[]")
    except (ValueError, TypeError):
        return []
    return [str(p) for p in parsed] if isinstance(parsed, list) else []


def _parse_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not an ISO date (YYYY-MM-DD)") from exc


# ---------------------------------------------------------------- doctor ---


@app.command()
def doctor(
    config_path: Optional[str] = ConfigOption,
    offline: bool = typer.Option(
        False, "--offline", help="Skip every check that would touch the network."
    ),
) -> None:
    """Check that this machine can run a crawl. Never prints a secret."""
    rows: list[tuple[str, str, str]] = []
    failures = 0
    warnings = 0

    def add(name: str, ok: bool | None, detail: str) -> None:
        nonlocal failures, warnings
        if ok is True:
            status = "[green]OK[/green]"
        elif ok is None:
            status = "[yellow]WARN[/yellow]"
            warnings += 1
        else:
            status = "[red]FAIL[/red]"
            failures += 1
        rows.append((name, status, detail))

    # --- python + venv ---------------------------------------------------
    version = sys.version_info
    add(
        "Python version",
        version[:2] == (3, 12),
        f"{sys.version.split()[0]} at {sys.executable}"
        + ("" if version[:2] == (3, 12) else " (this project targets 3.12)"),
    )

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    venv_path = Path(sys.prefix)
    add(
        "Virtual environment",
        in_venv or None,
        str(venv_path) if in_venv else "not running inside a virtualenv; run setup.ps1 / setup.sh",
    )

    # --- packages ---------------------------------------------------------
    required = [
        "httpx",
        "trafilatura",
        "bs4",
        "lxml",
        "pydantic",
        "dateutil",
        "tenacity",
        "typer",
        "rich",
        "yaml",
    ]
    missing = []
    for module in required:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    add(
        "Required packages",
        not missing,
        "all present" if not missing else f"missing: {', '.join(missing)}",
    )

    # --- the CLI stack itself -----------------------------------------------
    # typer 0.12 and click 8.2+ disagree about flag validation badly enough
    # that the CLI cannot be built at all. If that pair is ever installed,
    # doctor never gets to run -- so this row is for the case where something
    # milder is wrong, and for reporting the versions when asking for help.
    try:
        import click as _click

        click_version = _click.__version__
        click_major_minor = tuple(int(p) for p in click_version.split(".")[:2])
        add(
            "CLI stack",
            click_major_minor < (8, 2),
            f"typer {typer.__version__}, click {click_version}"
            + (
                ""
                if click_major_minor < (8, 2)
                else " -- typer 0.12 needs click < 8.2; reinstall from requirements.lock"
            ),
        )
    except Exception as exc:  # pragma: no cover - only on a broken install
        add("CLI stack", None, f"could not determine versions: {exc}")

    # --- config -----------------------------------------------------------
    config: AppConfig | None = None
    try:
        config = load_config(config_path)
        add(
            "Configuration",
            True,
            f"{config.source_path} ({len(config.queries)} queries, sha256 "
            f"{config.source_sha256[:12]}...)",
        )
    except ConfigError as exc:
        add("Configuration", False, str(exc).splitlines()[0])

    if config is None:
        _print_doctor(rows, failures, warnings)
        raise typer.Exit(code=1)

    # --- writable directories ---------------------------------------------
    try:
        config.ensure_directories()
        probe = config.output_dir / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("Output directories", True, f"writable under {config.root}")
    except OSError as exc:
        add("Output directories", False, f"not writable: {exc}")

    # --- .env ---------------------------------------------------------------
    # load_config() has already loaded it; this row exists so that "I filled
    # in .env and it still says the variable is missing" is diagnosable
    # without reading the source.
    env_file = config.root / ".env"
    if env_file.is_file():
        add("Env file", True, f"{env_file} found and loaded")
    else:
        add(
            "Env file",
            None,
            f"no {env_file}. Copy .env.example to .env, or set the variables "
            "in your shell.",
        )

    # --- secrets (presence only) -------------------------------------------
    serp = config.providers.serpapi_google_news
    if serp.enabled:
        add(
            f"{serp.api_key_env}",
            serp.has_api_key() or None,
            "set (value not shown)"
            if serp.has_api_key()
            else "not set: serpapi-google-news cannot run. manual-urls and "
            "google-news-rss (best effort) still work.",
        )
    else:
        add(f"{serp.api_key_env}", None, "serpapi-google-news disabled in config")

    contact = config.project.contact_email()
    add(
        "Contact email",
        bool(contact) or (None if not config.project.require_contact_email else False),
        f"User-Agent: {config.project.user_agent()}"
        if contact
        else f"set {config.project.contact_email_env} in .env so site operators can reach you",
    )

    # --- playwright ---------------------------------------------------------
    if offline:
        add("Playwright Chromium", None, "skipped (--offline)")
    else:
        from tw_cdc_influenza_news.fetching import chromium_installed

        ok, detail = asyncio.run(chromium_installed())
        add("Playwright Chromium", ok or None, detail)

    # --- database -----------------------------------------------------------
    try:
        db = Database(config.database_path)
        db.initialize()
        add(
            "SQLite schema",
            db.schema_version() == DB_SCHEMA_VERSION,
            f"version {db.schema_version()} (expected {DB_SCHEMA_VERSION}) at {config.database_path}",
        )
        unfinished = db.unfinished_runs()
        if unfinished:
            names = ", ".join(r["run_id"] for r in unfinished[:5])
            add(
                "Unfinished runs",
                None,
                f"{len(unfinished)} run(s) never finished: {names}. "
                "Resume with: python -m tw_cdc_influenza_news resume --run-id <id>",
            )
        else:
            add("Unfinished runs", True, "none")
        db.close()
    except Exception as exc:
        add("SQLite schema", False, str(exc))

    # --- date window ---------------------------------------------------------
    today = today_in(config.date_range.timezone)
    effective = effective_end_date(config.date_range.end_date, tz_name=config.date_range.timezone)
    add(
        "Date window",
        True,
        f"{config.date_range.start_date} -> {effective} "
        f"(requested end {config.date_range.end_date}; today is {today} in "
        f"{config.date_range.timezone}; future dates are never queried)",
    )

    _print_doctor(rows, failures, warnings)
    raise typer.Exit(code=1 if failures else 0)


def _print_doctor(rows: list[tuple[str, str, str]], failures: int, warnings: int) -> None:
    table = Table(title=f"tw-cdc-influenza-news-archive doctor (v{__version__})")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for name, status, detail in rows:
        table.add_row(name, status, detail)
    console.print(table)
    if failures:
        console.print(f"[red]{failures} check(s) failed.[/red] Fix those before crawling.")
    elif warnings:
        console.print(f"[yellow]{warnings} warning(s).[/yellow] A crawl can still run.")
    else:
        console.print("[green]All checks passed.[/green]")


# ----------------------------------------------------------------- crawl ---


@app.command()
def crawl(
    config_path: Optional[str] = ConfigOption,
    start_date: Optional[str] = typer.Option(None, "--start-date", help="ISO date, inclusive."),
    end_date: Optional[str] = typer.Option(
        None,
        "--end-date",
        help="ISO date, inclusive. Clamped to today in the configured timezone.",
    ),
    provider: list[str] = typer.Option(
        None,
        "--provider",
        help="Discovery provider; repeatable. Defaults to the enabled ones in the config.",
    ),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Reuse a specific run id."),
    discovery_only: bool = typer.Option(False, "--discovery-only", help="Search, do not fetch."),
    fetch_only: bool = typer.Option(
        False, "--fetch-only", help="Fetch the existing queue, do not search."
    ),
    no_export: bool = typer.Option(False, "--no-export", help="Skip writing output files."),
) -> None:
    """Discover and archive articles for a date range."""
    config = _load(config_path)
    _setup_logging(config)
    config.ensure_directories()

    start = _parse_date(start_date, config.date_range.start_date)
    end = _parse_date(end_date, config.date_range.end_date)
    providers = list(provider) if provider else enabled_provider_names(config)

    unknown = [p for p in providers if p not in PROVIDER_REGISTRY]
    if unknown:
        console.print(
            f"[red]Unknown provider(s):[/red] {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(PROVIDER_REGISTRY))}"
        )
        raise typer.Exit(code=2)
    if not providers and not fetch_only:
        console.print(
            "[red]No discovery provider enabled.[/red] Enable one in the config or pass "
            "--provider. Without a provider you can still use: import-urls."
        )
        raise typer.Exit(code=2)

    rid = run_id or new_run_id()
    console.print(f"[bold]Run:[/bold] {rid}")
    console.print(f"[dim]{COMPLETENESS_STATEMENT}[/dim]")

    db = _open_db(config)
    try:
        manifest, stats, _blocks = asyncio.run(
            run_crawl(
                config,
                db,
                run_id=rid,
                provider_names=providers,
                start_date=start,
                end_date=end,
                command=" ".join(sys.argv),
                discovery_only=discovery_only,
                fetch_only=fetch_only,
            )
        )
        console.print(_stats_table(stats.as_counts()))
        if not no_export:
            _do_export(db, config, rid, manifest=manifest, start=start, end=end, providers=providers)
    finally:
        db.close()


# ---------------------------------------------------------------- resume ---


@app.command()
def resume(
    config_path: Optional[str] = ConfigOption,
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Run to resume; default latest."),
    refetch_url: list[str] = typer.Option(
        None, "--refetch-url", help="Force a re-fetch of this URL; repeatable."
    ),
    refetch_from: Optional[str] = typer.Option(
        None, "--refetch-from", help="Re-fetch articles published on or after this ISO date."
    ),
    refetch_to: Optional[str] = typer.Option(
        None, "--refetch-to", help="Re-fetch articles published before this ISO date (exclusive)."
    ),
    no_export: bool = typer.Option(False, "--no-export", help="Skip writing output files."),
) -> None:
    """Continue an interrupted run without redoing completed work."""
    config = _load(config_path)
    _setup_logging(config)
    config.ensure_directories()

    db = _open_db(config)
    try:
        row = db.get_run(run_id) if run_id else db.latest_run()
        if row is None:
            console.print("[red]No run found to resume.[/red] Start one with: crawl")
            raise typer.Exit(code=2)

        rid = row["run_id"]
        if row["config_sha256"] and row["config_sha256"] != config.config_hash():
            console.print(
                "[yellow]Warning:[/yellow] the config has changed since this run started. "
                "Coverage figures will mix two configurations."
            )

        if refetch_url:
            from tw_cdc_influenza_news.urls import canonicalize_url, record_id_for

            ids = [record_id_for(canonicalize_url(u)) for u in refetch_url]
            console.print(f"Requeued {db.requeue(ids)} URL(s) by --refetch-url")
        if refetch_from or refetch_to:
            start = refetch_from or "0000-01-01"
            end = refetch_to or "9999-12-31"
            console.print(
                f"Requeued {db.requeue_date_range(start, end)} URL(s) published in "
                f"[{start}, {end})"
            )

        providers = _run_providers(row)
        start = date.fromisoformat(row["requested_start"])
        end = date.fromisoformat(row["requested_end"])

        manifest, stats, _blocks = asyncio.run(
            run_crawl(
                config,
                db,
                run_id=rid,
                provider_names=providers,
                start_date=start,
                end_date=end,
                command=" ".join(sys.argv),
                resume=True,
            )
        )
        console.print(_stats_table(stats.as_counts()))
        if not no_export:
            _do_export(db, config, rid, manifest=manifest, start=start, end=end, providers=providers)
    finally:
        db.close()


# ----------------------------------------------------------- import-urls ---


@app.command("import-urls")
def import_urls(
    path: str = typer.Argument(..., help="A .jsonl, .json or .txt file of URLs."),
    config_path: Optional[str] = ConfigOption,
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Attach to this run id."),
    # A single negative flag rather than a --fetch/--no-fetch pair: the
    # paired form is the one construct in this CLI that click and typer
    # disagree about across versions, and fetching is the default anyway.
    no_fetch: bool = typer.Option(
        False, "--no-fetch", help="Import only; do not fetch the URLs now."
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Recorded as the query text."),
) -> None:
    """Import known URLs into the same fetch, dedup and export pipeline."""
    config = _load(config_path)
    _setup_logging(config)
    config.ensure_directories()

    provider = ManualUrlsProvider(config)
    try:
        result = provider.load_file(path, note=note)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Could not read {path}:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    db = _open_db(config)
    try:
        rid = run_id or new_run_id()
        if db.get_run(rid) is None:
            today = today_in(config.date_range.timezone)
            db.create_run(
                rid,
                tool_version=__version__,
                schema_version=config.schema_version,
                config_path=str(config.source_path or ""),
                config_sha256=config.config_hash(),
                command=" ".join(sys.argv),
                providers=["manual-urls"],
                requested_start=today.isoformat(),
                requested_end=today.isoformat(),
                effective_end=today.isoformat(),
                timezone=config.date_range.timezone,
            )

        orchestrator = Orchestrator(config, db, run_id=rid)
        new_count = 0
        for item in result.items:
            _record_id, was_new = orchestrator.register_item(item)
            new_count += int(was_new)

        console.print(
            f"Imported {result.count} URL(s) ({new_count} new, "
            f"{result.count - new_count} already known); {len(result.skipped)} skipped."
        )
        for lineno, value, reason in result.skipped[:20]:
            console.print(f"  [yellow]line {lineno}[/yellow] {reason}: {value}")

        if not no_fetch:
            asyncio.run(orchestrator.fetch_pending())
            console.print(_stats_table(orchestrator.stats.as_counts()))
            db.finish_run(rid, status="completed", counts=orchestrator.stats.as_counts())
            console.print(f"Run id: {rid}")
    finally:
        db.close()


# ----------------------------------------------------------------- export ---


@app.command("export")
def export_cmd(
    config_path: Optional[str] = ConfigOption,
    run_id: Optional[str] = typer.Option(
        None, "--run-id", help="Export one run. Omit to export the whole archive."
    ),
    fmt: str = typer.Option(
        "all", "--format", help="json | jsonl | all (default: all files)."
    ),
) -> None:
    """Write the output files from the database."""
    config = _load(config_path)
    _setup_logging(config, quiet=True)
    config.ensure_directories()

    db = _open_db(config)
    try:
        coverage = None
        markdown = None
        if run_id:
            row = db.get_run(run_id)
            if row is None:
                console.print(f"[red]No such run:[/red] {run_id}")
                raise typer.Exit(code=2)
            coverage = build_coverage_report(
                db,
                config,
                run_id=run_id,
                requested_start=date.fromisoformat(row["requested_start"]),
                requested_end=date.fromisoformat(row["requested_end"]),
                effective_end=date.fromisoformat(row["effective_end"]),
                providers=_run_providers(row),
            )
            markdown = render_markdown(coverage)

        written = export_run(
            db, config, run_id=run_id, coverage=coverage, coverage_markdown=markdown
        )
        wanted = {"json": {"articles_json"}, "jsonl": {"articles_jsonl"}}.get(fmt)
        for name, path in written.items():
            if wanted is None or name in wanted or not name.startswith("articles"):
                console.print(f"{name}: {path}")
    finally:
        db.close()


# ----------------------------------------------------------------- report ---


@app.command()
def report(
    config_path: Optional[str] = ConfigOption,
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Run to report on; default latest."),
    write: bool = typer.Option(
        False, "--write", help="Also write coverage_report.json and .md to the output directory."
    ),
) -> None:
    """Print the coverage report, month by month."""
    config = _load(config_path)
    _setup_logging(config, quiet=True)

    db = _open_db(config)
    try:
        row = db.get_run(run_id) if run_id else db.latest_run()
        if row is None:
            console.print("[red]No runs recorded yet.[/red]")
            raise typer.Exit(code=2)

        providers = _run_providers(row)
        coverage = build_coverage_report(
            db,
            config,
            run_id=row["run_id"],
            requested_start=date.fromisoformat(row["requested_start"]),
            requested_end=date.fromisoformat(row["requested_end"]),
            effective_end=date.fromisoformat(row["effective_end"]),
            providers=providers,
        )
        markdown = render_markdown(coverage)
        console.print(markdown)

        if write:
            written = export_run(
                db, config, run_id=row["run_id"], coverage=coverage, coverage_markdown=markdown
            )
            console.print(f"coverage_report.json: {written.get('coverage_report_json')}")
    finally:
        db.close()


# ------------------------------------------------------------------ misc ---


@app.command("list-runs")
def list_runs(config_path: Optional[str] = ConfigOption) -> None:
    """Show recent runs and whether they finished."""
    config = _load(config_path)
    db = _open_db(config)
    try:
        table = Table(title="Runs")
        for column in ("run_id", "started", "finished", "status", "providers"):
            table.add_column(column, overflow="fold")
        for row in db.list_runs():
            table.add_row(
                row["run_id"],
                row["started_at"],
                row["finished_at"] or "-",
                row["status"],
                row["providers_json"],
            )
        console.print(table)
    finally:
        db.close()


def _stats_table(counts: dict[str, int]) -> Table:
    table = Table(title="Run summary")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for key, value in counts.items():
        table.add_row(key.replace("_", " "), str(value))
    return table


def _do_export(
    db: Database,
    config: AppConfig,
    run_id: str,
    *,
    manifest,
    start: date,
    end: date,
    providers: list[str],
) -> None:
    effective = effective_end_date(end, tz_name=config.date_range.timezone)
    coverage = build_coverage_report(
        db,
        config,
        run_id=run_id,
        requested_start=start,
        requested_end=end,
        effective_end=effective,
        providers=providers,
    )
    markdown = render_markdown(coverage)
    written = export_run(
        db,
        config,
        run_id=run_id,
        coverage=coverage,
        manifest=manifest,
        coverage_markdown=markdown,
    )
    for name, path in written.items():
        console.print(f"{name}: {path}")

    incomplete = [m.month for m in coverage.months if not m.complete]
    if incomplete:
        console.print(
            f"[yellow]{len(incomplete)} month(s) incomplete or not attempted.[/yellow] "
            f"See {written.get('coverage_report_md')}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
