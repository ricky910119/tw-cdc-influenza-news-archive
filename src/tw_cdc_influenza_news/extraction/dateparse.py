"""Publication-date extraction and resolution.

The whole module exists to keep one rule enforceable: **a date the article
page states about itself outranks the date the search provider reported.**
Providers commonly return an indexing or re-crawl timestamp, and letting that
overwrite a JSON-LD ``datePublished`` would silently move articles between
months -- which then shows up as a coverage anomaly nobody can explain.

Candidates are collected from every source, ranked by
``DATE_SOURCE_PRIORITY``, and the winner keeps a record of where it came from
(``date_source``) and how much to trust it (``date_confidence``). An
unparseable date stays ``None`` with the raw text preserved in
``published_at_raw``. We never invent a date to fill the column.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, tzinfo

from dateutil import parser as dateutil_parser

from tw_cdc_influenza_news.dates import DEFAULT_TIMEZONE, get_timezone
from tw_cdc_influenza_news.models import (
    DATE_SOURCE_CONFIDENCE,
    DATE_SOURCE_PRIORITY,
    DateConfidence,
    DateSource,
)

#: ``民國`` (ROC) years appear in Taiwanese government and some newsroom
#: pages: 民國113年1月15日 is 2024-01-15.
_ROC_RE = re.compile(r"(?:民國)?\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_CJK_YMD_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ISO_LIKE_RE = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")

#: Text near a date that marks it as the publication time rather than any
#: other date on the page.
_PUBLISH_HINTS = (
    "發布",
    "發佈",
    "發稿",
    "刊登",
    "更新",
    "published",
    "posted",
)


@dataclass(frozen=True, slots=True)
class DateCandidate:
    source: DateSource
    raw: str
    parsed: datetime | None

    @property
    def usable(self) -> bool:
        return self.parsed is not None

    @property
    def confidence(self) -> DateConfidence:
        if self.parsed is None:
            return DateConfidence.NONE
        return DATE_SOURCE_CONFIDENCE[self.source]


def parse_roc_date(text: str) -> date | None:
    """Parse a ROC-calendar date. Returns ``None`` when the text is not one."""
    match = _ROC_RE.search(text)
    if not match:
        return None
    roc_year, month, day = (int(g) for g in match.groups())
    # A 4-digit year would not match the 2-3 digit group, so anything here is
    # a ROC year; ROC 1 is 1912.
    try:
        return date(roc_year + 1911, month, day)
    except ValueError:
        return None


def parse_datetime(
    raw: str | None,
    *,
    tz_name: str = DEFAULT_TIMEZONE,
    assume_local: bool = True,
) -> datetime | None:
    """Parse a date string to an aware datetime, or ``None``.

    A string that already carries an offset keeps it -- converting everything
    to one zone would destroy the information that the publisher stated a
    time in their own zone. A string without an offset is read as local time
    in ``tz_name`` when ``assume_local``, which is the right reading for a
    Taiwanese newsroom page.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    local_tz: tzinfo = get_timezone(tz_name)

    # CJK forms first: dateutil mis-reads them or throws.
    cjk = _CJK_YMD_RE.search(text)
    if cjk:
        year, month, day = (int(g) for g in cjk.groups())
        parsed_date: date | None
        try:
            parsed_date = date(year, month, day)
        except ValueError:
            parsed_date = None
    else:
        parsed_date = parse_roc_date(text)

    if parsed_date is not None:
        hour = minute = second = 0
        time_match = _TIME_RE.search(text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            second = int(time_match.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            hour = minute = second = 0
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            hour,
            minute,
            second,
            tzinfo=local_tz,
        )

    try:
        parsed = dateutil_parser.parse(text, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        # Fuzzy parsing is a last resort: it will happily read a number out
        # of a headline, so it only runs when the string also contains
        # something date-shaped.
        if not _ISO_LIKE_RE.search(text):
            return None
        try:
            parsed = dateutil_parser.parse(text, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            return None

    if parsed.tzinfo is None and assume_local:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed


def _walk_json_ld(node: object) -> list[dict[str, object]]:
    """Flatten JSON-LD, which is a list, an object, or an @graph of both."""
    found: list[dict[str, object]] = []
    if isinstance(node, list):
        for child in node:
            found.extend(_walk_json_ld(child))
    elif isinstance(node, dict):
        found.append(node)
        graph = node.get("@graph")
        if graph is not None:
            found.extend(_walk_json_ld(graph))
    return found


def json_ld_dates(blocks: list[str]) -> tuple[str | None, str | None]:
    """Return ``(datePublished, dateModified)`` raw strings from JSON-LD."""
    published: str | None = None
    modified: str | None = None
    for block in blocks:
        try:
            payload = json.loads(block)
        except (ValueError, TypeError):
            continue
        for node in _walk_json_ld(payload):
            if published is None:
                value = node.get("datePublished") or node.get("dateCreated")
                if isinstance(value, str) and value.strip():
                    published = value.strip()
            if modified is None:
                value = node.get("dateModified")
                if isinstance(value, str) and value.strip():
                    modified = value.strip()
        if published and modified:
            break
    return published, modified


def text_date_candidates(text: str, *, limit: int = 4000) -> list[str]:
    """Date-looking strings from visible page text, publication hints first."""
    window = text[:limit]
    hits: list[str] = []
    for line in window.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        if not (
            _CJK_YMD_RE.search(stripped)
            or _ROC_RE.search(stripped)
            or _ISO_LIKE_RE.search(stripped)
        ):
            continue
        hinted = any(hint in stripped.lower() for hint in _PUBLISH_HINTS)
        hits.insert(0, stripped) if hinted else hits.append(stripped)
    return hits


def resolve_published_at(
    candidates: list[DateCandidate],
    *,
    tz_name: str = DEFAULT_TIMEZONE,
) -> tuple[datetime | None, str | None, DateSource, DateConfidence]:
    """Pick the winning candidate by source priority.

    Returns ``(datetime, raw_text, source, confidence)``. Unparseable
    candidates still supply ``raw_text`` when nothing parsed at all, so
    ``published_at_raw`` records what the page actually said even when we
    could not read it.
    """
    by_source = {c.source: c for c in candidates if c.usable}
    for source in DATE_SOURCE_PRIORITY:
        candidate = by_source.get(source)
        if candidate is None or candidate.parsed is None:
            continue
        return (
            candidate.parsed,
            candidate.raw,
            candidate.source,
            DATE_SOURCE_CONFIDENCE[source],
        )

    fallback_raw = next((c.raw for c in candidates if c.raw), None)
    _ = tz_name
    return None, fallback_raw, DateSource.UNKNOWN, DateConfidence.NONE


def to_display_date(moment: datetime | None, *, tz_name: str = DEFAULT_TIMEZONE) -> str | None:
    """The ``published_date`` column: the calendar date in the display zone.

    ``published_at`` keeps the publisher's own offset; ``published_date`` is
    what a Taiwanese reader would call the day it came out. Those disagree for
    anything published near midnight, and both are worth keeping.
    """
    if moment is None:
        return None
    return moment.astimezone(get_timezone(tz_name)).date().isoformat()
