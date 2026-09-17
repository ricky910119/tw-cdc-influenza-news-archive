"""Google News RSS discovery -- best effort, no API key.

**This provider is not a completeness guarantee.** Google News RSS caps and
reshapes result sets in ways a client cannot observe: there is no total
count, no page cursor, and no way to tell a genuinely empty window from a
truncated one. Every slice it fills is therefore marked with a completeness
warning, and the coverage report repeats it. It exists so the tool is usable
without a paid key, not so a run without a key can be called complete.

Access policy, in full: one low-rate request at a time, raw XML cached, and
the moment the response looks like a consent page, a CAPTCHA, an
"unusual traffic" interstitial, a 403 or a 429, the provider stops for the
rest of the run. There is no retry loop around a block and no attempt to
look like anything other than what we are.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import httpx

from tw_cdc_influenza_news.config import AppConfig, QueryConfig
from tw_cdc_influenza_news.database import utc_now
from tw_cdc_influenza_news.dates import DateSlice
from tw_cdc_influenza_news.discovery.base import DiscoveryProvider, SliceQueryResult
from tw_cdc_influenza_news.errors import ErrorCode
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import DiscoveredItem, DiscoveryEvidence
from tw_cdc_influenza_news.ratelimit import DomainRateLimiter
from tw_cdc_influenza_news.urls import unwrap_redirector

logger = get_logger("discovery.rss")

RSS_COMPLETENESS_WARNING = (
    "google_news_rss is best-effort: the feed exposes no result total and no "
    "pagination, so a short result set cannot be distinguished from a "
    "truncated one."
)

#: Markers that mean "you are being asked to stop". Any hit stops the provider.
BLOCK_MARKERS: tuple[tuple[str, ErrorCode], ...] = (
    ("unusual traffic", ErrorCode.UNUSUAL_TRAFFIC),
    ("our systems have detected", ErrorCode.UNUSUAL_TRAFFIC),
    ("recaptcha", ErrorCode.CAPTCHA),
    ("g-recaptcha", ErrorCode.CAPTCHA),
    ("/sorry/index", ErrorCode.CAPTCHA),
    ("captcha", ErrorCode.CAPTCHA),
    ("consent.google.com", ErrorCode.ACCESS_DENIED),
    ("before you continue", ErrorCode.ACCESS_DENIED),
    ("access denied", ErrorCode.ACCESS_DENIED),
)


def detect_block(
    *, status_code: int, final_url: str, body: str
) -> tuple[ErrorCode, str] | None:
    """Classify a response as a block, or ``None`` if it looks like a feed.

    Deliberately generous: a false positive costs one stopped provider and an
    honest gap in the report, a false negative means hammering a service that
    already told us to stop.
    """
    if status_code == 429:
        return ErrorCode.HTTP_429, "HTTP 429 rate limited"
    if status_code == 403:
        return ErrorCode.HTTP_403, "HTTP 403 forbidden"

    haystack = f"{final_url}\n{body[:4000]}".lower()
    for marker, code in BLOCK_MARKERS:
        if marker in haystack:
            return code, f"response matched block marker {marker!r}"

    # A feed endpoint that answers with an HTML document is an interstitial
    # of some kind even when we cannot name it.
    stripped = body.lstrip()
    if stripped[:200].lower().startswith(("<!doctype html", "<html")):
        return ErrorCode.ACCESS_DENIED, "feed endpoint returned an HTML document"
    return None


class GoogleNewsRssProvider(DiscoveryProvider):
    name = "google-news-rss"
    supports_date_range = True
    best_effort = True

    def __init__(self, config: AppConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config)
        self.provider_config = config.providers.google_news_rss
        self._client = client
        self._owns_client = client is None
        self._limiter = DomainRateLimiter(self.provider_config.requests_per_minute)
        self._stopped: tuple[ErrorCode, str] | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.access.request_timeout_seconds,
                headers={"User-Agent": self.config.project.user_agent()},
                follow_redirects=True,
                max_redirects=self.config.access.max_redirects,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def preflight(self) -> list[str]:
        problems: list[str] = []
        if not self.provider_config.enabled:
            problems.append(f"{self.name}: disabled in config")
        return problems

    # --- date conversion -------------------------------------------------

    def build_query_string(self, query: QueryConfig, date_slice: DateSlice) -> str:
        """Embed the date window in the query with Google's search operators.

        ``after:`` is inclusive of its date and ``before:`` is exclusive of
        its date, which happens to match our ``[start, end)`` slice exactly --
        so the slice bounds go in unchanged. That coincidence is worth
        stating: the SerpApi adapter in this package needs ``last_day``
        instead, and copying either conversion into the other adapter would
        shift a day's worth of articles.
        """
        return (
            f"{query.text} "
            f"after:{date_slice.start.isoformat()} "
            f"before:{date_slice.end.isoformat()}"
        )

    def build_params(self, query: QueryConfig, date_slice: DateSlice) -> dict[str, str]:
        cfg = self.provider_config
        return {
            "q": self.build_query_string(query, date_slice),
            "hl": cfg.hl,
            "gl": cfg.gl,
            "ceid": cfg.ceid,
        }

    # --- parsing ---------------------------------------------------------

    @staticmethod
    def parse_feed(xml_text: str) -> list[dict[str, Any]]:
        """Parse an RSS 2.0 feed into plain dicts. Raises on malformed XML."""
        root = ElementTree.fromstring(xml_text)
        channel = root.find("channel")
        if channel is None:
            return []
        entries: list[dict[str, Any]] = []
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            source_el = item.find("source")
            entries.append(
                {
                    "link": link,
                    "title": (item.findtext("title") or "").strip() or None,
                    "pub_date": (item.findtext("pubDate") or "").strip() or None,
                    "source": (source_el.text or "").strip() if source_el is not None else None,
                    "description": (item.findtext("description") or "").strip() or None,
                }
            )
        return entries

    def _cache_path(self, run_id: str, query: QueryConfig, date_slice: DateSlice) -> Path:
        directory = self.config.raw_dir / run_id / self.name / query.id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{date_slice.key.replace(':', '_')}.xml"

    # --- search ----------------------------------------------------------

    async def search(
        self,
        query: QueryConfig,
        date_slice: DateSlice,
        *,
        run_id: str,
    ) -> SliceQueryResult:
        outcome = SliceQueryResult()

        if self._stopped is not None:
            code, detail = self._stopped
            outcome.error_code = code
            outcome.error_message = f"provider already stopped: {detail}"
            outcome.fatal = True
            return outcome

        await self._limiter.acquire("news.google.com")

        params = self.build_params(query, date_slice)
        try:
            response = await self._get_client().get(self.provider_config.base_url, params=params)
        except httpx.TimeoutException as exc:
            outcome.error_code = ErrorCode.TIMEOUT
            outcome.error_message = str(exc)
            return outcome
        except httpx.HTTPError as exc:
            outcome.error_code = ErrorCode.CONNECTION_ERROR
            outcome.error_message = str(exc)
            return outcome

        body = response.text
        block = detect_block(
            status_code=response.status_code,
            final_url=str(response.url),
            body=body,
        )
        if block is not None:
            code, detail = block
            self._stopped = block
            outcome.error_code = code
            outcome.error_message = detail
            outcome.fatal = self.provider_config.stop_on_block
            logger.warning(
                "%s stopping: %s (%s). Not attempting to work around it.",
                self.name,
                code.value,
                detail,
            )
            return outcome

        if response.status_code >= 400:
            outcome.error_code = (
                ErrorCode.SERVER_ERROR if response.status_code >= 500 else ErrorCode.CLIENT_ERROR
            )
            outcome.error_message = f"HTTP {response.status_code}"
            return outcome

        if self.provider_config.cache_raw_xml:
            cache_path = self._cache_path(run_id, query, date_slice)
            cache_path.write_text(body, encoding="utf-8")
            outcome.raw_response_paths.append(
                str(cache_path.relative_to(self.config.root))
                if cache_path.is_relative_to(self.config.root)
                else str(cache_path)
            )

        try:
            entries = self.parse_feed(body)
        except ElementTree.ParseError as exc:
            outcome.error_code = ErrorCode.PROVIDER_ERROR
            outcome.error_message = f"malformed RSS: {exc}"
            return outcome

        raw_path = outcome.raw_response_paths[0] if outcome.raw_response_paths else None
        for position, entry in enumerate(entries, start=1):
            evidence = DiscoveryEvidence(
                provider=self.name,
                query_id=query.id,
                query_text=query.text,
                slice_key=date_slice.key,
                slice_start=date_slice.start.isoformat(),
                slice_end_exclusive=date_slice.end.isoformat(),
                position=position,
                provider_date_raw=entry["pub_date"],
                provider_title=entry["title"],
                provider_source_name=entry["source"],
                gl=self.provider_config.gl,
                hl=self.provider_config.hl,
                discovered_at=utc_now(),
                raw_response_path=raw_path,
            )
            outcome.items.append(
                DiscoveredItem(
                    url=unwrap_redirector(entry["link"]),
                    title=entry["title"],
                    source_name=entry["source"],
                    published_at_raw=entry["pub_date"],
                    snippet=entry["description"],
                    evidence=evidence,
                )
            )

        outcome.page_count = 1
        outcome.completed = True
        # The feed is always potentially truncated, so splitting the slice is
        # always worth trying while a finer granularity exists.
        outcome.result_limit_reached = (
            outcome.result_count >= self.config.slicing.truncation_threshold
        )
        logger.info(
            "%s %s %s -> %d results (best effort)",
            self.name,
            query.id,
            date_slice.key,
            outcome.result_count,
        )
        return outcome

    async def cooldown(self) -> None:
        """Small pause between slices, on top of the rate limiter."""
        await asyncio.sleep(0)
