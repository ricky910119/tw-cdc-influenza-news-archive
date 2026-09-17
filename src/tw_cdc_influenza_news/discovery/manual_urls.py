"""Manual URL import.

Known articles -- ones a colleague sent you, ones from a newsroom index, ones
a previous run missed -- enter the archive through exactly the same fetch,
extract, dedup and export path as a search hit. That is the point: an
imported URL is not a second-class record, it just has a different discovery
provider, and the coverage report says so.

Three input formats, chosen by extension and then by sniffing:

``.jsonl``  one JSON value per line: ``{"url": "...", "title": "..."}`` or a
            bare ``"https://..."`` string.
``.json``   a list of those values, or ``{"urls": [...]}``.
``.txt``    one URL per line; ``#`` starts a comment, blank lines ignored.

Recognised optional keys: ``title``, ``source_name``, ``published_at`` (or
``date``), ``note``, ``query_id``. Anything else is ignored rather than
rejected, so a file exported from someone else's tool is usable as-is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from tw_cdc_influenza_news.config import AppConfig, QueryConfig
from tw_cdc_influenza_news.database import utc_now
from tw_cdc_influenza_news.dates import DateSlice
from tw_cdc_influenza_news.discovery.base import DiscoveryProvider, SliceQueryResult
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import DiscoveredItem, DiscoveryEvidence

logger = get_logger("discovery.manual")

PROVIDER_NAME = "manual-urls"
MANUAL_QUERY_ID = "manual"
MANUAL_SLICE_KEY = "manual:none:none"


@dataclass(slots=True)
class ImportResult:
    items: list[DiscoveredItem] = field(default_factory=list)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)  # line, value, reason

    @property
    def count(self) -> int:
        return len(self.items)


def _coerce_entry(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"url": value.strip()}
    if isinstance(value, dict):
        url = value.get("url") or value.get("link") or value.get("final_url")
        if not isinstance(url, str):
            return None
        entry = dict(value)
        entry["url"] = url.strip()
        return entry
    return None


def _iter_raw_entries(path: Path) -> Iterable[tuple[int, Any]]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("urls") or payload.get("items") or []
        if not isinstance(payload, list):
            raise ValueError("a .json import file must hold a list, or {'urls': [...]}")
        yield from enumerate(payload, start=1)
        return

    if suffix == ".jsonl":
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            yield lineno, json.loads(stripped)
        return

    # Plain text, and anything unrecognised: sniff each line so a .txt full
    # of JSON objects still works.
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("{"):
            yield lineno, json.loads(stripped)
        else:
            yield lineno, stripped


class ManualUrlsProvider(DiscoveryProvider):
    """A provider with no search. It only ever returns what you hand it."""

    name = PROVIDER_NAME
    supports_date_range = False
    best_effort = False

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.provider_config = config.providers.manual_urls

    def preflight(self) -> list[str]:
        return [] if self.provider_config.enabled else [f"{self.name}: disabled in config"]

    async def search(
        self,
        query: QueryConfig,
        date_slice: DateSlice,
        *,
        run_id: str,
    ) -> SliceQueryResult:
        """Never searches. Present so the provider satisfies the interface.

        Returning an empty completed result (rather than raising) keeps the
        orchestrator free of provider special-cases.
        """
        _ = (query, date_slice, run_id)
        return SliceQueryResult(completed=True)

    def load_file(self, path: Path | str, *, note: str | None = None) -> ImportResult:
        """Parse an import file into discovery items. No network access."""
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"import file not found: {path}")

        result = ImportResult()
        seen: set[str] = set()

        for lineno, raw in _iter_raw_entries(path):
            entry = _coerce_entry(raw)
            if entry is None:
                result.skipped.append((lineno, str(raw)[:200], "no usable url field"))
                continue

            url = entry["url"]
            if not url.startswith(("http://", "https://")):
                result.skipped.append((lineno, url[:200], "not an absolute http(s) URL"))
                continue
            if url in seen:
                result.skipped.append((lineno, url[:200], "duplicate within this file"))
                continue
            seen.add(url)

            published_raw = entry.get("published_at") or entry.get("date")
            evidence = DiscoveryEvidence(
                provider=self.name,
                query_id=str(entry.get("query_id") or MANUAL_QUERY_ID),
                query_text=note or entry.get("note") or f"imported from {path.name}",
                slice_key=MANUAL_SLICE_KEY,
                slice_start="",
                slice_end_exclusive="",
                position=lineno,
                provider_date_raw=published_raw if isinstance(published_raw, str) else None,
                provider_title=entry.get("title") if isinstance(entry.get("title"), str) else None,
                provider_source_name=(
                    entry.get("source_name") if isinstance(entry.get("source_name"), str) else None
                ),
                gl=None,
                hl=None,
                discovered_at=utc_now(),
                raw_response_path=str(path),
            )
            result.items.append(
                DiscoveredItem(
                    url=url,
                    title=evidence.provider_title,
                    source_name=evidence.provider_source_name,
                    published_at_raw=evidence.provider_date_raw,
                    snippet=None,
                    evidence=evidence,
                )
            )

        logger.info(
            "manual import %s: %d URL(s) accepted, %d skipped",
            path.name,
            result.count,
            len(result.skipped),
        )
        return result
