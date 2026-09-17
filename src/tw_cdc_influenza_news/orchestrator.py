"""Run orchestration: discovery, adaptive slicing, then fetching.

Discovery walks (provider x query x date slice). A slice that comes back at
the provider's ceiling is split -- month into weeks, week into days -- and
the children are queued in its place. A day slice that still hits the
ceiling cannot be split further, so it is recorded with a completeness
warning instead of being quietly accepted.

Fetching drains the URL queue afterwards, writing state per URL. Both phases
are resumable: discovery skips (provider, query, slice) triples already
marked complete for the run, and fetching only picks up URLs that are not in
a terminal state.
"""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from tw_cdc_influenza_news import __version__
from tw_cdc_influenza_news.config import AppConfig, QueryConfig
from tw_cdc_influenza_news.database import Database, UrlRow, utc_now
from tw_cdc_influenza_news.dates import (
    DateSlice,
    build_month_slices,
    can_split,
    effective_end_date,
    split_slice,
)
from tw_cdc_influenza_news.discovery import DiscoveryProvider, build_provider
from tw_cdc_influenza_news.errors import PROVIDER_FATAL_CODES, ErrorCode
from tw_cdc_influenza_news.fetching import PlaywrightRenderer, PoliteFetcher, UrlPipeline
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import (
    COMPLETENESS_STATEMENT,
    DiscoveredItem,
    RunManifest,
    UrlStatus,
)
from tw_cdc_influenza_news.ratelimit import BlockRegistry
from tw_cdc_influenza_news.urls import (
    canonicalize_url,
    normalize_url,
    record_id_for,
    registrable_host,
)

logger = get_logger("orchestrator")


def new_run_id(now: datetime | None = None) -> str:
    """Sortable, collision-resistant run id: ``run-YYYYmmddTHHMMSS-xxxxxx``."""
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(3)}"


@dataclass(slots=True)
class RunStats:
    slices_attempted: int = 0
    slices_split: int = 0
    slices_failed: int = 0
    results_seen: int = 0
    urls_new: int = 0
    urls_duplicate: int = 0
    fetched_success: int = 0
    fetched_blocked: int = 0
    fetched_failed: int = 0
    fetched_content_unavailable: int = 0
    notes: list[str] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {
            "slices_attempted": self.slices_attempted,
            "slices_split": self.slices_split,
            "slices_failed": self.slices_failed,
            "results_seen": self.results_seen,
            "urls_new": self.urls_new,
            "urls_duplicate": self.urls_duplicate,
            "fetched_success": self.fetched_success,
            "fetched_blocked": self.fetched_blocked,
            "fetched_failed": self.fetched_failed,
            "fetched_content_unavailable": self.fetched_content_unavailable,
        }


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        run_id: str,
        blocks: BlockRegistry | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.run_id = run_id
        self.blocks = blocks or BlockRegistry()
        self.stats = RunStats()

    # --- URL registration -------------------------------------------------

    def _normalize_kwargs(self) -> dict[str, object]:
        dedup = self.config.deduplication
        return {
            "tracking_params": tuple(dedup.tracking_params),
            "strip_fragment": dedup.strip_fragment,
            "drop_www": dedup.drop_www,
            "lowercase_host": dedup.lowercase_host,
        }

    def register_item(self, item: DiscoveredItem) -> tuple[str | None, bool]:
        """Store a discovered URL and its evidence.

        Returns ``(record_id, was_new)``. A URL seen before is not re-queued;
        only its evidence grows, which is how one article can carry several
        ``matched_queries`` without being fetched twice.
        """
        try:
            normalized = normalize_url(item.url, **self._normalize_kwargs())  # type: ignore[arg-type]
            canonical = canonicalize_url(item.url, **self._normalize_kwargs())  # type: ignore[arg-type]
        except ValueError as exc:
            logger.debug("skipping unusable URL %r: %s", item.url, exc)
            return None, False

        record_id = record_id_for(canonical)
        was_new = self.db.upsert_url(
            record_id=record_id,
            url=item.url,
            normalized_url=normalized,
            canonical_url=canonical,
            domain=registrable_host(canonical),
            run_id=self.run_id,
            discovered_at=item.evidence.discovered_at,
        )
        self.db.add_evidence(record_id, item.evidence, self.run_id)
        return record_id, was_new

    # --- discovery --------------------------------------------------------

    async def discover_with_provider(
        self,
        provider: DiscoveryProvider,
        *,
        start_date: date,
        end_date_inclusive: date,
        resume: bool = False,
    ) -> None:
        """Walk every query x slice for one provider, splitting as needed."""
        already_done = self.db.completed_slice_keys(self.run_id) if resume else set()

        for query in self.config.queries:
            if self.blocks.is_provider_stopped(provider.name):
                break
            queue: deque[DateSlice] = deque(
                build_month_slices(start_date, end_date_inclusive)
            )
            while queue:
                if self.blocks.is_provider_stopped(provider.name):
                    break
                date_slice = queue.popleft()
                if (provider.name, query.id, date_slice.key) in already_done:
                    logger.debug(
                        "skipping completed slice %s %s %s",
                        provider.name,
                        query.id,
                        date_slice.key,
                    )
                    continue
                children = await self._run_slice(provider, query, date_slice)
                queue.extendleft(reversed(children))

    async def _run_slice(
        self,
        provider: DiscoveryProvider,
        query: QueryConfig,
        date_slice: DateSlice,
    ) -> list[DateSlice]:
        self.stats.slices_attempted += 1
        outcome = await provider.search(query, date_slice, run_id=self.run_id)

        new_count = 0
        duplicate_count = 0
        for item in outcome.items:
            record_id, was_new = self.register_item(item)
            if record_id is None:
                continue
            if was_new:
                new_count += 1
            else:
                duplicate_count += 1

        self.stats.results_seen += outcome.result_count
        self.stats.urls_new += new_count
        self.stats.urls_duplicate += duplicate_count

        children: list[DateSlice] = []
        warning: str | None = None
        splittable = can_split(date_slice) and self._splitting_allowed(date_slice)

        if outcome.result_limit_reached and splittable:
            children = split_slice(date_slice)
            self.stats.slices_split += 1
        elif outcome.result_limit_reached:
            warning = (
                "result ceiling reached at the finest available granularity "
                f"({date_slice.granularity.value}); some results in this window "
                "are likely missing"
            )

        if provider.best_effort and not warning:
            from tw_cdc_influenza_news.discovery.google_news_rss import (
                RSS_COMPLETENESS_WARNING,
            )

            warning = RSS_COMPLETENESS_WARNING

        if outcome.error_code is not None:
            self.stats.slices_failed += 1
        if outcome.fatal or (outcome.error_code in PROVIDER_FATAL_CODES):
            code = outcome.error_code or ErrorCode.PROVIDER_ERROR
            self.blocks.stop_provider(
                provider.name, code, outcome.error_message or "", occurred_at=utc_now()
            )
            self.db.record_block(
                self.run_id,
                scope="provider",
                target=provider.name,
                error_code=code.value,
                detail=outcome.error_message or "",
            )
            children = []

        self.db.record_slice(
            self.run_id,
            {
                "provider": provider.name,
                "query_id": query.id,
                "query_text": query.text,
                "slice_key": date_slice.key,
                "slice_start": date_slice.start.isoformat(),
                "slice_end_exclusive": date_slice.end.isoformat(),
                "granularity": date_slice.granularity.value,
                "month_key": date_slice.month_key,
                "result_count": outcome.result_count,
                "page_count": outcome.page_count,
                "new_url_count": new_count,
                "duplicate_count": duplicate_count,
                "result_limit_reached": outcome.result_limit_reached,
                "slice_was_split": bool(children),
                "completed": outcome.completed and outcome.error_code is None,
                "completeness_warning": warning,
                "error_code": outcome.error_code.value if outcome.error_code else None,
                "error_message": outcome.error_message,
            },
        )
        return children

    def _splitting_allowed(self, date_slice: DateSlice) -> bool:
        slicing = self.config.slicing
        if date_slice.granularity.value == "month":
            return slicing.split_month_to_week
        if date_slice.granularity.value == "week":
            return slicing.split_week_to_day
        return False

    # --- fetching ---------------------------------------------------------

    async def fetch_pending(
        self,
        *,
        limit: int | None = None,
        renderer: PlaywrightRenderer | None = None,
        progress=None,
    ) -> None:
        """Drain the URL queue, writing state after every URL."""
        requeued = self.db.reset_stuck_fetching(self.run_id)
        if requeued:
            logger.info("requeued %d URL(s) left mid-fetch by an earlier run", requeued)

        async with PoliteFetcher(self.config, block_registry=self.blocks) as fetcher:
            pipeline = UrlPipeline(
                self.config, self.db, fetcher, run_id=self.run_id, renderer=renderer
            )
            processed = 0
            while True:
                batch: list[UrlRow] = self.db.pending_urls(
                    limit=self.config.access.max_concurrency * 4,
                    max_attempts=self.config.access.max_retries + 1,
                    exclude_domains=sorted(self.blocks.blocked_domains),
                )
                if not batch:
                    break
                if limit is not None:
                    batch = batch[: max(0, limit - processed)]
                    if not batch:
                        break

                semaphore = asyncio.Semaphore(self.config.access.max_concurrency)

                async def run_one(row: UrlRow) -> None:
                    async with semaphore:
                        result = await pipeline.process(row)
                        self._tally(result.status)
                        if progress is not None:
                            progress(result)

                await asyncio.gather(*(run_one(row) for row in batch))
                processed += len(batch)
                if limit is not None and processed >= limit:
                    break

    def _tally(self, status: UrlStatus) -> None:
        if status is UrlStatus.SUCCESS:
            self.stats.fetched_success += 1
        elif status is UrlStatus.BLOCKED:
            self.stats.fetched_blocked += 1
        elif status is UrlStatus.CONTENT_UNAVAILABLE:
            self.stats.fetched_content_unavailable += 1
        else:
            self.stats.fetched_failed += 1


async def run_crawl(
    config: AppConfig,
    db: Database,
    *,
    run_id: str,
    provider_names: list[str],
    start_date: date,
    end_date: date,
    command: str,
    resume: bool = False,
    discovery_only: bool = False,
    fetch_only: bool = False,
    now: datetime | None = None,
    progress=None,
) -> tuple[RunManifest, RunStats, BlockRegistry]:
    """One full crawl: create the run, discover, fetch, return the manifest."""
    effective_end = effective_end_date(
        end_date, tz_name=config.date_range.timezone, now=now
    )
    if effective_end < start_date:
        raise ValueError(
            f"effective end date {effective_end} precedes start date {start_date}; "
            "nothing to do (the requested range lies entirely in the future)"
        )

    manifest = RunManifest(
        run_id=run_id,
        tool_version=__version__,
        schema_version=config.schema_version,
        db_schema_version=db.schema_version() or 0,
        config_path=str(config.source_path or ""),
        config_sha256=config.config_hash(),
        command=command,
        providers=provider_names,
        requested_start_date=start_date.isoformat(),
        requested_end_date=end_date.isoformat(),
        effective_end_date=effective_end.isoformat(),
        timezone=config.date_range.timezone,
        started_at=utc_now(),
        notes=[COMPLETENESS_STATEMENT],
    )

    if not resume:
        db.create_run(
            run_id,
            tool_version=__version__,
            schema_version=config.schema_version,
            config_path=manifest.config_path,
            config_sha256=manifest.config_sha256,
            command=command,
            providers=provider_names,
            requested_start=start_date.isoformat(),
            requested_end=end_date.isoformat(),
            effective_end=effective_end.isoformat(),
            timezone=config.date_range.timezone,
            started_at=manifest.started_at,
        )

    orchestrator = Orchestrator(config, db, run_id=run_id)
    providers: list[DiscoveryProvider] = []
    renderer: PlaywrightRenderer | None = None

    try:
        if not fetch_only:
            for name in provider_names:
                provider = build_provider(name, config)
                providers.append(provider)
                problems = provider.preflight()
                if problems:
                    for problem in problems:
                        logger.warning("provider not ready: %s", problem)
                        orchestrator.stats.notes.append(problem)
                    orchestrator.blocks.stop_provider(
                        name, ErrorCode.PROVIDER_ERROR, "; ".join(problems)
                    )
                    db.record_block(
                        run_id,
                        scope="provider",
                        target=name,
                        error_code=ErrorCode.PROVIDER_ERROR.value,
                        detail="; ".join(problems),
                    )
                    continue
                await orchestrator.discover_with_provider(
                    provider,
                    start_date=start_date,
                    end_date_inclusive=effective_end,
                    resume=resume,
                )

        if not discovery_only:
            if config.extraction.playwright.enabled:
                renderer = PlaywrightRenderer(config)
            await orchestrator.fetch_pending(renderer=renderer, progress=progress)
    finally:
        for provider in providers:
            await provider.aclose()
        if renderer is not None:
            await renderer.aclose()

    manifest.finished_at = utc_now()
    manifest.status = "completed"
    manifest.counts = orchestrator.stats.as_counts()
    manifest.notes.extend(orchestrator.stats.notes)

    db.finish_run(
        run_id,
        status=manifest.status,
        counts=manifest.counts,
        notes=manifest.notes,
    )
    return manifest, orchestrator.stats, orchestrator.blocks
