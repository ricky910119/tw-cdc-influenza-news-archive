"""Coverage reporting: what we looked for, what we found, what we missed.

The report's job is to make the archive's gaps legible. Every month in the
REQUESTED range gets a row -- including months that were never attempted,
because the difference between "no coverage that month" and "we never asked"
is the whole point.

A month is marked incomplete when any of these held:

* a provider error touched it
* the provider's quota ran out
* a CAPTCHA, 403 or 429 stopped a source
* a slice hit the result ceiling and could not be split any further
* too large a share of its articles had unparseable dates
* too large a share of its URLs yielded no body
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.database import Database, utc_now
from tw_cdc_influenza_news.dates import month_keys_in_range
from tw_cdc_influenza_news.models import (
    COMPLETENESS_STATEMENT,
    CoverageReport,
    MonthCoverage,
    SliceCoverage,
)

#: Slice-level error codes that make the whole month suspect.
_MONTH_POISONING_CODES = {
    "provider_error",
    "provider_auth_error",
    "provider_quota_exhausted",
    "http_403",
    "http_429",
    "captcha",
    "unusual_traffic",
    "access_denied",
    "robots_disallowed",
    "timeout",
    "connection_error",
}


def _slice_from_row(row: sqlite3.Row) -> SliceCoverage:
    return SliceCoverage(
        provider=row["provider"],
        query_id=row["query_id"],
        query=row["query_text"],
        date_slice={
            "granularity": row["granularity"],
            "start": row["slice_start"],
            "end_exclusive": row["slice_end_exclusive"],
        },
        granularity=row["granularity"],
        month_key=row["month_key"],
        result_count=row["result_count"],
        page_count=row["page_count"],
        new_url_count=row["new_url_count"],
        duplicate_count=row["duplicate_count"],
        result_limit_reached=bool(row["result_limit_reached"]),
        slice_was_split=bool(row["slice_was_split"]),
        completed=bool(row["completed"]),
        completeness_warning=row["completeness_warning"],
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _article_stats_by_month(db: Database, run_id: str | None) -> dict[str, dict[str, int]]:
    """Per-month article counts, keyed on the resolved publication date."""
    sql = """
        SELECT
            COALESCE(substr(a.published_date, 1, 7), 'unknown') AS month,
            COUNT(*)                                            AS article_count,
            SUM(CASE WHEN u.status = 'success' THEN 1 ELSE 0 END)              AS success_count,
            SUM(CASE WHEN u.status = 'blocked' THEN 1 ELSE 0 END)              AS blocked_count,
            SUM(CASE WHEN u.status IN ('permanent_error','retryable_error')
                     THEN 1 ELSE 0 END)                                        AS failed_count,
            SUM(CASE WHEN u.status = 'content_unavailable' THEN 1 ELSE 0 END)  AS unavailable_count,
            SUM(CASE WHEN a.published_date IS NULL OR a.published_date = ''
                     THEN 1 ELSE 0 END)                                        AS undated_count
        FROM articles a
        JOIN urls u ON u.record_id = a.record_id
    """
    params: tuple[object, ...] = ()
    if run_id:
        sql += " WHERE a.run_id = ?"
        params = (run_id,)
    sql += " GROUP BY month"

    stats: dict[str, dict[str, int]] = {}
    for row in db._conn.execute(sql, params):  # noqa: SLF001 - same package
        stats[row["month"]] = {
            "article_count": row["article_count"] or 0,
            "success_count": row["success_count"] or 0,
            "blocked_count": row["blocked_count"] or 0,
            "failed_count": row["failed_count"] or 0,
            "content_unavailable_count": row["unavailable_count"] or 0,
            "date_parse_failure_count": row["undated_count"] or 0,
        }
    return stats


def build_coverage_report(
    db: Database,
    config: AppConfig,
    *,
    run_id: str,
    requested_start: date,
    requested_end: date,
    effective_end: date,
    providers: list[str],
) -> CoverageReport:
    slice_rows = db.slices_for_run(run_id)
    slices = [_slice_from_row(row) for row in slice_rows]

    by_month: dict[str, list[SliceCoverage]] = defaultdict(list)
    for entry in slices:
        by_month[entry.month_key].append(entry)

    article_stats = _article_stats_by_month(db, run_id)
    thresholds = config.coverage

    months: list[MonthCoverage] = []
    for month_key in month_keys_in_range(requested_start, requested_end):
        month_slices = by_month.get(month_key, [])
        stats = article_stats.get(month_key, {})
        reasons: list[str] = []

        attempted = bool(month_slices)
        if not attempted:
            # A month past the effective end was never in scope; say which.
            if month_key > effective_end.isoformat()[:7]:
                reasons.append(
                    "not attempted: beyond the effective end date "
                    f"({effective_end.isoformat()}); future dates are never queried"
                )
            else:
                reasons.append("not attempted in this run")

        article_count = stats.get("article_count", 0)
        undated = stats.get("date_parse_failure_count", 0)
        unavailable = stats.get("content_unavailable_count", 0)
        blocked = stats.get("blocked_count", 0)
        failed = stats.get("failed_count", 0)

        date_ratio = (undated / article_count) if article_count else 0.0
        unavailable_ratio = (unavailable / article_count) if article_count else 0.0

        for entry in month_slices:
            if entry.error_code in _MONTH_POISONING_CODES:
                reasons.append(f"{entry.provider}/{entry.query_id}: {entry.error_code}")
            elif entry.error_code:
                reasons.append(f"{entry.provider}/{entry.query_id}: {entry.error_code}")
            if not entry.completed:
                reasons.append(f"{entry.provider}/{entry.query_id}: slice did not complete")
            if entry.result_limit_reached and entry.granularity == "day":
                reasons.append(
                    f"{entry.provider}/{entry.query_id}: result ceiling hit at day "
                    "granularity, which cannot be split further"
                )
            if entry.completeness_warning:
                reasons.append(f"{entry.provider}/{entry.query_id}: {entry.completeness_warning}")

        if date_ratio > thresholds.date_parse_failure_threshold:
            reasons.append(
                f"{date_ratio:.0%} of articles have no parseable publication date "
                f"(threshold {thresholds.date_parse_failure_threshold:.0%})"
            )
        if unavailable_ratio > thresholds.content_unavailable_threshold:
            reasons.append(
                f"{unavailable_ratio:.0%} of URLs yielded no body "
                f"(threshold {thresholds.content_unavailable_threshold:.0%})"
            )
        if blocked:
            reasons.append(f"{blocked} URL(s) blocked")

        months.append(
            MonthCoverage(
                month=month_key,
                attempted=attempted,
                complete=attempted and not reasons,
                reasons=list(dict.fromkeys(reasons)),
                slice_count=len(month_slices),
                result_count=sum(s.result_count for s in month_slices),
                article_count=article_count,
                success_count=stats.get("success_count", 0),
                blocked_count=blocked,
                failed_count=failed,
                content_unavailable_count=unavailable,
                date_parse_failure_count=undated,
                date_parse_failure_ratio=round(date_ratio, 4),
                content_unavailable_ratio=round(unavailable_ratio, 4),
            )
        )

    block_rows = db.blocks_for_run(run_id)
    blocked_domains = sorted(
        {row["target"] for row in block_rows if row["scope"] == "domain"}
    )
    stopped_providers = [
        {
            "provider": row["target"],
            "error_code": row["error_code"],
            "detail": row["detail"] or "",
            "occurred_at": row["occurred_at"],
        }
        for row in block_rows
        if row["scope"] == "provider"
    ]

    status_counts = db.status_counts()
    totals = {
        "slices": len(slices),
        "results_seen": sum(s.result_count for s in slices),
        "articles": db.article_count(run_id),
        "urls_total": sum(status_counts.values()),
        **{f"urls_{status}": count for status, count in sorted(status_counts.items())},
        "months_in_range": len(months),
        "months_complete": sum(1 for m in months if m.complete),
        "months_incomplete": sum(1 for m in months if not m.complete),
    }

    return CoverageReport(
        run_id=run_id,
        generated_at=utc_now(),
        requested_start_date=requested_start.isoformat(),
        requested_end_date=requested_end.isoformat(),
        effective_end_date=effective_end.isoformat(),
        timezone=config.date_range.timezone,
        providers=providers,
        queries=[q.id for q in config.queries],
        completeness_statement=COMPLETENESS_STATEMENT,
        slices=slices,
        months=months,
        totals=totals,
        blocked_domains=blocked_domains,
        stopped_providers=stopped_providers,
    )


def render_markdown(report: CoverageReport) -> str:
    """Human-readable coverage summary, grouped by year then month."""
    lines: list[str] = [
        "# Coverage report",
        "",
        f"- Run: `{report.run_id}`",
        f"- Generated: {report.generated_at}",
        f"- Requested range: {report.requested_start_date} to {report.requested_end_date}",
        f"- Effective end date: {report.effective_end_date} ({report.timezone})",
        f"- Providers: {', '.join(report.providers) or 'none'}",
        f"- Queries: {', '.join(report.queries) or 'none'}",
        "",
        "> " + report.completeness_statement,
        "",
        "## Totals",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in report.totals.items():
        lines.append(f"| {key.replace('_', ' ')} | {value} |")

    if report.blocked_domains:
        lines += [
            "",
            "## Stopped domains",
            "",
            "Requests to these domains stopped after an access restriction. No "
            "attempt was made to work around any of them.",
            "",
        ]
        lines += [f"- `{domain}`" for domain in report.blocked_domains]

    if report.stopped_providers:
        lines += ["", "## Stopped providers", ""]
        lines += [
            f"- `{entry['provider']}`: {entry['error_code']} - {entry['detail']}"
            for entry in report.stopped_providers
        ]

    lines += ["", "## Months", ""]
    current_year = ""
    for month in report.months:
        year = month.month[:4]
        if year != current_year:
            current_year = year
            lines += [
                "",
                f"### {year}",
                "",
                "| Month | Status | Slices | Results | Articles | Blocked | Failed | No body | Notes |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        if not month.attempted:
            status = "NOT ATTEMPTED"
        elif month.complete:
            status = "complete"
        else:
            status = "INCOMPLETE"
        notes = "; ".join(month.reasons) if month.reasons else ""
        lines.append(
            f"| {month.month} | {status} | {month.slice_count} | {month.result_count} | "
            f"{month.article_count} | {month.blocked_count} | {month.failed_count} | "
            f"{month.content_unavailable_count} | {notes} |"
        )

    gaps = [m for m in report.months if not m.complete]
    lines += [
        "",
        "## Gap summary",
        "",
        f"{len(gaps)} of {len(report.months)} months in the requested range are "
        "incomplete or were not attempted.",
        "",
    ]
    for month in gaps:
        lines.append(f"- **{month.month}**: {'; '.join(month.reasons) or 'no detail recorded'}")

    return "\n".join(lines) + "\n"
