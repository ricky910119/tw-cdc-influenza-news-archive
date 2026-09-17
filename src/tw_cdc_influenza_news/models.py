"""Pydantic models: the output contract and the in-flight records.

:class:`Article` is the published schema. Changing a field here changes
``articles.jsonl`` for every downstream consumer, so treat it as versioned
(``SCHEMA_VERSION`` in ``__init__``) rather than as a convenient dataclass.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tw_cdc_influenza_news import SCHEMA_VERSION


class UrlStatus(StrEnum):
    """Lifecycle of a single URL. Persisted after every transition."""

    DISCOVERED = "discovered"
    QUEUED = "queued"
    FETCHING = "fetching"
    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    PERMANENT_ERROR = "permanent_error"
    BLOCKED = "blocked"
    CONTENT_UNAVAILABLE = "content_unavailable"


#: Statuses a resume pass will pick up again.
RESUMABLE_STATUSES: frozenset[UrlStatus] = frozenset(
    {
        UrlStatus.DISCOVERED,
        UrlStatus.QUEUED,
        # A process killed mid-fetch leaves rows stuck in FETCHING; they are
        # safe to redo because nothing was written for them.
        UrlStatus.FETCHING,
        UrlStatus.RETRYABLE_ERROR,
    }
)

#: Statuses a resume pass leaves alone unless --force-refetch says otherwise.
TERMINAL_STATUSES: frozenset[UrlStatus] = frozenset(
    {
        UrlStatus.SUCCESS,
        UrlStatus.PERMANENT_ERROR,
        UrlStatus.BLOCKED,
        UrlStatus.CONTENT_UNAVAILABLE,
    }
)


class DateSource(StrEnum):
    """Where published_at came from, most trustworthy first."""

    JSON_LD = "json_ld"
    OPEN_GRAPH = "open_graph"
    TIME_ELEMENT = "time_element"
    PAGE_TEXT = "page_text"
    SEARCH_PROVIDER = "search_provider"
    UNKNOWN = "unknown"


class DateConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


#: Confidence implied by each source. A provider-supplied date is LOW: it is
#: often the indexing time rather than the publication time, which is exactly
#: why it must not overwrite a date the article page states itself.
DATE_SOURCE_CONFIDENCE: dict[DateSource, DateConfidence] = {
    DateSource.JSON_LD: DateConfidence.HIGH,
    DateSource.OPEN_GRAPH: DateConfidence.HIGH,
    DateSource.TIME_ELEMENT: DateConfidence.MEDIUM,
    DateSource.PAGE_TEXT: DateConfidence.MEDIUM,
    DateSource.SEARCH_PROVIDER: DateConfidence.LOW,
    DateSource.UNKNOWN: DateConfidence.NONE,
}

#: Priority order used by the extractor. Lower index wins.
DATE_SOURCE_PRIORITY: tuple[DateSource, ...] = (
    DateSource.JSON_LD,
    DateSource.OPEN_GRAPH,
    DateSource.TIME_ELEMENT,
    DateSource.PAGE_TEXT,
    DateSource.SEARCH_PROVIDER,
)


class ExtractionMethod(StrEnum):
    TRAFILATURA = "trafilatura"
    JSON_LD = "json_ld"
    OPEN_GRAPH = "open_graph"
    ARTICLE_TAG = "article_tag"
    CONTENT_CONTAINER = "content_container"
    PLAYWRIGHT_TRAFILATURA = "playwright_trafilatura"
    NONE = "none"


class StrictModel(BaseModel):
    """Base model that refuses unknown fields and non-finite floats.

    NaN and Infinity are not valid JSON. ``json.dumps`` emits them anyway by
    default, producing a file every strict parser rejects, so we refuse them
    at construction time instead of discovering it downstream.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    @field_validator("*", mode="before")
    @classmethod
    def _reject_non_finite(cls, value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite float is not representable in JSON")
        return value


class DiscoveryEvidence(StrictModel):
    """One sighting of a URL by one provider, for one query, in one slice.

    Kept per sighting rather than collapsed, so the coverage report can say
    which query found an article, and a later run can tell a re-discovery
    from a first discovery.
    """

    provider: str
    query_id: str
    query_text: str
    slice_key: str
    slice_start: str
    slice_end_exclusive: str
    position: int | None = None
    provider_date_raw: str | None = None
    provider_title: str | None = None
    provider_source_name: str | None = None
    gl: str | None = None
    hl: str | None = None
    discovered_at: str
    raw_response_path: str | None = None


class DiscoveredItem(StrictModel):
    """A search hit, before any fetching has happened."""

    url: str
    title: str | None = None
    source_name: str | None = None
    published_at_raw: str | None = None
    snippet: str | None = None
    evidence: DiscoveryEvidence


class Article(StrictModel):
    """One archived news article. This is the published output contract."""

    record_id: str
    schema_version: int = SCHEMA_VERSION

    published_at: str | None = None
    published_date: str | None = None
    published_at_raw: str | None = None
    date_source: DateSource = DateSource.UNKNOWN
    date_confidence: DateConfidence = DateConfidence.NONE
    modified_at: str | None = None

    title: str | None = None
    content: str | None = None
    source_name: str | None = None
    authors: list[str] = Field(default_factory=list)

    url: str
    final_url: str | None = None
    canonical_url: str

    language: str | None = None
    matched_queries: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    discovery_providers: list[str] = Field(default_factory=list)
    discovery_evidence: list[DiscoveryEvidence] = Field(default_factory=list)

    first_discovered_at: str
    last_checked_at: str

    extraction_method: ExtractionMethod = ExtractionMethod.NONE
    content_sha256: str | None = None
    content_char_count: int = 0

    syndication_cluster_id: str | None = None
    possible_duplicate_of: list[str] = Field(default_factory=list)

    status: UrlStatus
    error_code: str | None = None
    error_message: str | None = None


class FailedUrl(StrictModel):
    """A row in failed_urls.jsonl.

    Every URL we discovered and could not turn into an article gets one, so a
    gap in the archive is always traceable to a named reason.
    """

    record_id: str
    url: str
    final_url: str | None = None
    canonical_url: str
    domain: str
    status: UrlStatus
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    attempts: int = 0
    first_discovered_at: str
    last_checked_at: str
    matched_queries: list[str] = Field(default_factory=list)
    discovery_providers: list[str] = Field(default_factory=list)


class SliceCoverage(StrictModel):
    """Per (provider, query, date slice) accounting. The unit of coverage."""

    provider: str
    query_id: str
    query: str
    date_slice: dict[str, str | int]
    granularity: str
    month_key: str

    result_count: int = 0
    page_count: int = 0
    new_url_count: int = 0
    duplicate_count: int = 0
    success_count: int = 0
    blocked_count: int = 0
    failed_count: int = 0
    content_unavailable_count: int = 0

    result_limit_reached: bool = False
    slice_was_split: bool = False
    completed: bool = False
    completeness_warning: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class MonthCoverage(StrictModel):
    """Roll-up of every slice touching a month. Present for EVERY month.

    A month nobody attempted still gets a row saying so; omitting it would
    make "no news that month" and "we never asked" indistinguishable.
    """

    month: str
    attempted: bool = False
    complete: bool = False
    reasons: list[str] = Field(default_factory=list)
    slice_count: int = 0
    result_count: int = 0
    article_count: int = 0
    success_count: int = 0
    blocked_count: int = 0
    failed_count: int = 0
    content_unavailable_count: int = 0
    date_parse_failure_count: int = 0
    date_parse_failure_ratio: float = 0.0
    content_unavailable_ratio: float = 0.0


class CoverageReport(StrictModel):
    run_id: str
    generated_at: str
    requested_start_date: str
    requested_end_date: str
    effective_end_date: str
    timezone: str
    providers: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    completeness_statement: str
    slices: list[SliceCoverage] = Field(default_factory=list)
    months: list[MonthCoverage] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)
    blocked_domains: list[str] = Field(default_factory=list)
    stopped_providers: list[dict[str, str]] = Field(default_factory=list)


class RunManifest(StrictModel):
    run_id: str
    tool_version: str
    schema_version: int
    db_schema_version: int
    config_path: str
    config_sha256: str
    command: str
    providers: list[str] = Field(default_factory=list)
    requested_start_date: str
    requested_end_date: str
    effective_end_date: str
    timezone: str
    started_at: str
    finished_at: str | None = None
    status: str = "running"
    counts: dict[str, int] = Field(default_factory=dict)
    output_files: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


#: The one sentence every report and the README repeat. Centralised so it
#: cannot drift into an overclaim in one place and not another.
COMPLETENESS_STATEMENT = (
    "This archive is the set of news articles discoverable under the "
    "configured queries, date range, search provider(s) and execution time. "
    "It is not, and does not claim to be, every news article on the internet "
    "about these topics."
)
