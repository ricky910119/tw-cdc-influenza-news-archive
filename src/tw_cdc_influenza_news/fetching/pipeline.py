"""Per-URL processing: fetch, extract, deduplicate, persist.

One URL at a time, and the database is written at every transition. That is
what makes the tool interruptible: kill it mid-run and the only work lost is
the single URL that was in flight.

A URL we cannot read the body of is still recorded. The search metadata, the
discovery evidence and the failure reason are all kept, with status
``content_unavailable`` or ``blocked``. An empty ``content`` field with a
named reason is useful; a fabricated one would be worse than nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.database import Database, UrlRow, utc_now
from tw_cdc_influenza_news.deduplication import (
    DuplicateKind,
    ExistingRecord,
    decide,
    merge_string_lists,
    title_date_key,
)
from tw_cdc_influenza_news.errors import ErrorCode, is_blocked, is_retryable
from tw_cdc_influenza_news.extraction import ExtractionOutcome, extract_article
from tw_cdc_influenza_news.fetching.http_client import FetchOutcome, PoliteFetcher
from tw_cdc_influenza_news.fetching.playwright_fallback import PlaywrightRenderer
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import (
    Article,
    ExtractionMethod,
    UrlStatus,
)
from tw_cdc_influenza_news.urls import canonicalize_url, content_sha256, registrable_host

logger = get_logger("fetching.pipeline")


@dataclass(slots=True)
class ProcessResult:
    record_id: str
    status: UrlStatus
    article: Article | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    used_playwright: bool = False
    duplicate_of: str | None = None
    notes: list[str] = field(default_factory=list)


def matched_keywords_in(text: str, keywords: list[str]) -> list[str]:
    """Which configured keywords appear in the article. Annotation, not a filter."""
    lowered = text.casefold()
    return [kw for kw in keywords if kw.casefold() in lowered]


class UrlPipeline:
    """Turns one discovered URL into a stored article, or a stored failure."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        fetcher: PoliteFetcher,
        *,
        run_id: str,
        renderer: PlaywrightRenderer | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.fetcher = fetcher
        self.run_id = run_id
        self.renderer = renderer

    # --- helpers ---------------------------------------------------------

    def _normalize_kwargs(self) -> dict[str, object]:
        dedup = self.config.deduplication
        return {
            "tracking_params": tuple(dedup.tracking_params),
            "strip_fragment": dedup.strip_fragment,
            "drop_www": dedup.drop_www,
            "lowercase_host": dedup.lowercase_host,
        }

    def _dedup_shortlist(
        self,
        *,
        canonical_url: str,
        normalized_url: str,
        key: str | None,
        body_hash: str | None,
    ) -> list[ExistingRecord]:
        """Look up only the rows the decision could possibly involve."""
        rows: dict[str, ExistingRecord] = {}

        def add(url_rows) -> None:  # type: ignore[no-untyped-def]
            for row in url_rows:
                if row is None:
                    continue
                article_row = self.db._conn.execute(  # noqa: SLF001 - same package
                    "SELECT title, published_date FROM articles WHERE record_id = ?",
                    (row.record_id,),
                ).fetchone()
                rows[row.record_id] = ExistingRecord(
                    record_id=row.record_id,
                    canonical_url=row.canonical_url,
                    normalized_url=row.normalized_url,
                    domain=row.domain,
                    title=article_row["title"] if article_row else None,
                    published_date=article_row["published_date"] if article_row else None,
                    content_sha256=row.content_sha256,
                )

        add([self.db.find_by_canonical(canonical_url)])
        add([self.db.find_by_normalized(normalized_url)])
        if key:
            add(self.db.find_by_title_date_key(key))
        if body_hash:
            add(self.db.find_by_content_sha(body_hash))
        return list(rows.values())

    def _build_article(
        self,
        row: UrlRow,
        extraction: ExtractionOutcome,
        fetch: FetchOutcome,
        *,
        status: UrlStatus,
        error_code: ErrorCode | None,
        error_message: str | None,
        canonical_url: str,
    ) -> Article:
        evidence = self.db.evidence_for(row.record_id)
        body = extraction.content or None
        haystack = " ".join(filter(None, [extraction.title, body]))

        return Article(
            record_id=row.record_id,
            published_at=extraction.published_at,
            published_date=extraction.published_date,
            published_at_raw=extraction.published_at_raw,
            date_source=extraction.date_source,
            date_confidence=extraction.date_confidence,
            modified_at=extraction.modified_at,
            title=extraction.title,
            content=body,
            source_name=extraction.source_name
            or next((e.provider_source_name for e in evidence if e.provider_source_name), None)
            or registrable_host(fetch.final_url or row.url),
            authors=extraction.authors,
            url=row.url,
            final_url=fetch.final_url or row.final_url,
            canonical_url=canonical_url,
            language=extraction.language,
            matched_queries=merge_string_lists([e.query_id for e in evidence]),
            matched_keywords=matched_keywords_in(haystack, self.config.keywords),
            discovery_providers=merge_string_lists([e.provider for e in evidence]),
            discovery_evidence=evidence,
            first_discovered_at=row.first_discovered_at,
            last_checked_at=utc_now(),
            extraction_method=extraction.method,
            content_sha256=content_sha256(body) if body else None,
            content_char_count=len(body or ""),
            status=status,
            error_code=error_code.value if error_code else None,
            error_message=error_message,
        )

    # --- the pipeline ----------------------------------------------------

    async def process(self, row: UrlRow) -> ProcessResult:
        self.db.set_status(
            row.record_id,
            UrlStatus.FETCHING,
            increment_attempts=True,
            run_id=self.run_id,
        )

        fetch = await self.fetcher.fetch(row.url)

        if fetch.error_code is not None and fetch.html is None:
            return self._persist_failure(row, fetch)

        extraction = extract_article(
            fetch.html or "",
            fetch.final_url or row.url,
            min_content_chars=self.config.extraction.min_content_chars,
            tz_name=self.config.date_range.timezone,
            provider_date_raw=next(
                (e.provider_date_raw for e in self.db.evidence_for(row.record_id) if e.provider_date_raw),
                None,
            ),
            provider_title=next(
                (e.provider_title for e in self.db.evidence_for(row.record_id) if e.provider_title),
                None,
            ),
            provider_source_name=next(
                (
                    e.provider_source_name
                    for e in self.db.evidence_for(row.record_id)
                    if e.provider_source_name
                ),
                None,
            ),
            strip_furniture=self.config.extraction.strip_boilerplate,
        )

        used_playwright = False
        min_chars = self.config.extraction.min_content_chars
        if (
            extraction.needs_javascript
            and not extraction.is_usable(min_chars)
            and self.renderer is not None
            and extraction.block_code is None
        ):
            render = await self.renderer.render(fetch.final_url or row.url)
            if render.html:
                used_playwright = True
                rendered = extract_article(
                    render.html,
                    render.final_url or fetch.final_url or row.url,
                    min_content_chars=min_chars,
                    tz_name=self.config.date_range.timezone,
                    strip_furniture=self.config.extraction.strip_boilerplate,
                )
                if rendered.content_length > extraction.content_length:
                    rendered.method = ExtractionMethod.PLAYWRIGHT_TRAFILATURA
                    extraction = rendered
                    fetch.final_url = render.final_url or fetch.final_url
            elif render.error_message:
                fetch.notes.append(render.error_message)

        canonical_url = canonicalize_url(
            fetch.final_url or row.url,
            canonical_hint=extraction.canonical_url,
            **self._normalize_kwargs(),  # type: ignore[arg-type]
        )

        # --- access control found during extraction ----------------------
        if extraction.block_code is not None:
            article = self._build_article(
                row,
                extraction,
                fetch,
                status=UrlStatus.BLOCKED,
                error_code=extraction.block_code,
                error_message=f"access control detected: {extraction.block_code.value}",
                canonical_url=canonical_url,
            )
            article.content = None  # never store a teaser as if it were the body
            self.db.save_article(article, self.run_id)
            self.db.set_status(
                row.record_id,
                UrlStatus.BLOCKED,
                error_code=extraction.block_code.value,
                error_message=article.error_message,
                http_status=fetch.status_code,
                final_url=fetch.final_url,
                run_id=self.run_id,
            )
            self.db.record_block(
                self.run_id,
                scope="url",
                target=row.record_id,
                error_code=extraction.block_code.value,
                detail=article.error_message or "",
                url=row.url,
            )
            return ProcessResult(
                row.record_id,
                UrlStatus.BLOCKED,
                article=article,
                error_code=extraction.block_code,
                error_message=article.error_message,
                used_playwright=used_playwright,
            )

        # --- body too short or absent ------------------------------------
        if not extraction.is_usable(min_chars):
            code = (
                ErrorCode.CONTENT_TOO_SHORT
                if extraction.content
                else ErrorCode.EXTRACTION_FAILED
            )
            message = (
                f"extracted {extraction.content_length} chars, minimum is {min_chars}"
                if extraction.content
                else "no article body could be extracted"
            )
            article = self._build_article(
                row,
                extraction,
                fetch,
                status=UrlStatus.CONTENT_UNAVAILABLE,
                error_code=code,
                error_message=message,
                canonical_url=canonical_url,
            )
            self.db.save_article(article, self.run_id)
            self.db.set_status(
                row.record_id,
                UrlStatus.CONTENT_UNAVAILABLE,
                error_code=code.value,
                error_message=message,
                http_status=fetch.status_code,
                final_url=fetch.final_url,
                title_date_key=title_date_key(extraction.title, extraction.published_date),
                run_id=self.run_id,
            )
            return ProcessResult(
                row.record_id,
                UrlStatus.CONTENT_UNAVAILABLE,
                article=article,
                error_code=code,
                error_message=message,
                used_playwright=used_playwright,
            )

        # --- success ------------------------------------------------------
        article = self._build_article(
            row,
            extraction,
            fetch,
            status=UrlStatus.SUCCESS,
            error_code=None,
            error_message=None,
            canonical_url=canonical_url,
        )

        normalized_url = canonicalize_url(
            fetch.final_url or row.url, **self._normalize_kwargs()  # type: ignore[arg-type]
        )
        key = title_date_key(article.title, article.published_date)
        shortlist = self._dedup_shortlist(
            canonical_url=canonical_url,
            normalized_url=normalized_url,
            key=key,
            body_hash=article.content_sha256,
        )
        decision = decide(
            record_id=row.record_id,
            canonical_url=canonical_url,
            normalized_url=normalized_url,
            domain=row.domain,
            title=article.title,
            published_date=article.published_date,
            content=article.content,
            existing=shortlist,
            title_similarity_threshold=self.config.deduplication.title_similarity_threshold,
            syndication_enabled=self.config.deduplication.syndication.enabled,
            syndication_min_chars=self.config.deduplication.syndication.min_content_chars,
        )

        if decision.kind is DuplicateKind.SYNDICATED and decision.cluster_id:
            article.syndication_cluster_id = decision.cluster_id
            # Backfill the cluster onto the copies already stored, so the
            # cluster is complete no matter which copy arrived first.
            for other_id in decision.possible_duplicates:
                self.db.set_cluster(other_id, decision.cluster_id)
        if decision.possible_duplicates and decision.kind is not DuplicateKind.SYNDICATED:
            article.possible_duplicate_of = decision.possible_duplicates

        duplicate_of: str | None = None
        if decision.is_duplicate:
            # Merge the evidence into the record we keep, then store this one
            # as a pointer rather than a second copy of the story.
            duplicate_of = decision.merge_into
            article.possible_duplicate_of = merge_string_lists(
                article.possible_duplicate_of, [decision.merge_into or ""]
            )
            logger.info(
                "%s duplicates %s (%s)", row.url, decision.merge_into, decision.reason
            )

        self.db.save_article(article, self.run_id)
        self.db.set_status(
            row.record_id,
            UrlStatus.SUCCESS,
            http_status=fetch.status_code,
            final_url=fetch.final_url,
            content_sha256=article.content_sha256,
            title_date_key=key,
            run_id=self.run_id,
        )
        return ProcessResult(
            row.record_id,
            UrlStatus.SUCCESS,
            article=article,
            used_playwright=used_playwright,
            duplicate_of=duplicate_of,
            notes=list(fetch.notes),
        )

    def _persist_failure(self, row: UrlRow, fetch: FetchOutcome) -> ProcessResult:
        code = fetch.error_code or ErrorCode.CONNECTION_ERROR
        if is_blocked(code):
            status = UrlStatus.BLOCKED
            self.db.record_block(
                self.run_id,
                scope="domain" if self.fetcher.blocks.is_domain_blocked(row.domain) else "url",
                target=row.domain if self.fetcher.blocks.is_domain_blocked(row.domain) else row.record_id,
                error_code=code.value,
                detail=fetch.error_message or "",
                url=row.url,
            )
        elif is_retryable(code):
            status = UrlStatus.RETRYABLE_ERROR
        else:
            status = UrlStatus.PERMANENT_ERROR

        self.db.set_status(
            row.record_id,
            status,
            error_code=code.value,
            error_message=fetch.error_message,
            http_status=fetch.status_code,
            final_url=fetch.final_url,
            run_id=self.run_id,
        )
        return ProcessResult(
            row.record_id,
            status,
            error_code=code,
            error_message=fetch.error_message,
            notes=list(fetch.notes),
        )
