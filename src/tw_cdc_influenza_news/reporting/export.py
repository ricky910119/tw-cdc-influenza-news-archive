"""Writing the output files.

Every writer here goes through :func:`dump_json`, which sets
``allow_nan=False``. Python's ``json`` module emits ``NaN`` and ``Infinity``
by default -- tokens that are not in the JSON grammar and that most strict
parsers reject. Centralising the call means a future field cannot
reintroduce them by accident.

All files are UTF-8 with ``ensure_ascii=False``: an archive of Chinese-language
news stored as ``\\u7591\\u7ba1\\u7f72`` is technically valid and practically
unreadable.

Writes are atomic (write a temp file, then replace). A crash during export
leaves the previous export intact instead of a half-written file that looks
like a complete one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.database import Database
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import (
    Article,
    CoverageReport,
    FailedUrl,
    RunManifest,
    UrlStatus,
)

logger = get_logger("reporting.export")


def dump_json(payload: Any, *, indent: int | None = 2) -> str:
    """Serialise to JSON, refusing NaN/Infinity."""
    return json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=False)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle as fh:
            fh.write(text)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def write_jsonl(path: Path, rows: Iterable[Any]) -> int:
    count = 0
    lines: list[str] = []
    for row in rows:
        payload = row.model_dump(mode="json") if hasattr(row, "model_dump") else row
        lines.append(dump_json(payload, indent=None))
        count += 1
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return count


def failed_rows(db: Database, run_id: str | None) -> Iterator[FailedUrl]:
    for row in db.failed_url_rows(run_id):
        evidence = db.evidence_for(row["record_id"])
        yield FailedUrl(
            record_id=row["record_id"],
            url=row["url"],
            final_url=row["final_url"],
            canonical_url=row["canonical_url"],
            domain=row["domain"],
            status=UrlStatus(row["status"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            http_status=row["http_status"],
            attempts=row["attempts"],
            first_discovered_at=row["first_discovered_at"],
            last_checked_at=row["last_checked_at"],
            matched_queries=sorted({e.query_id for e in evidence}),
            discovery_providers=sorted({e.provider for e in evidence}),
        )


def export_run(
    db: Database,
    config: AppConfig,
    *,
    run_id: str | None,
    coverage: CoverageReport | None = None,
    manifest: RunManifest | None = None,
    coverage_markdown: str | None = None,
) -> dict[str, str]:
    """Write every output file. Returns ``name -> path``.

    ``run_id`` of ``None`` exports the whole archive rather than one run,
    which is what you want when several resumed runs contributed to it.
    """
    out = config.output_dir
    out.mkdir(parents=True, exist_ok=True)
    indent = config.output.json_indent or None
    written: dict[str, str] = {}

    articles: list[Article] = list(db.iter_articles(run_id))

    jsonl_path = out / config.output.jsonl_name
    write_jsonl(jsonl_path, articles)
    written["articles_jsonl"] = str(jsonl_path)

    json_path = out / config.output.json_name
    atomic_write_text(
        json_path,
        dump_json([a.model_dump(mode="json") for a in articles], indent=indent),
    )
    written["articles_json"] = str(json_path)

    failed_path = out / config.output.failed_name
    failed_count = write_jsonl(failed_path, failed_rows(db, run_id))
    written["failed_urls_jsonl"] = str(failed_path)

    if coverage is not None:
        coverage_path = out / config.output.coverage_name
        atomic_write_text(coverage_path, dump_json(coverage.model_dump(mode="json"), indent=indent))
        written["coverage_report_json"] = str(coverage_path)

        md_path = out / config.output.coverage_markdown_name
        atomic_write_text(md_path, coverage_markdown or "")
        written["coverage_report_md"] = str(md_path)

    if manifest is not None:
        manifest.output_files = dict(written)
        manifest_path = out / config.output.manifest_name
        atomic_write_text(manifest_path, dump_json(manifest.model_dump(mode="json"), indent=indent))
        written["run_manifest_json"] = str(manifest_path)

    logger.info(
        "exported %d article(s) and %d failed URL(s) to %s",
        len(articles),
        failed_count,
        out,
    )
    return written
