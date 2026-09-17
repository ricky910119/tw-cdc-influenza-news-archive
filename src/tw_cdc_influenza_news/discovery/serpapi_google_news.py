"""SerpApi discovery adapters (Google News engine, and the Search News tab).

This is the recommended mode: a structured, paid API with an explicit quota,
which is a far better neighbour than scraping a results page would be.

Two adapters share this module because they share the transport and differ
only in engine parameters:

* ``serpapi-google-news``  -- ``engine=google_news``
* ``serpapi-google-search-news-tab`` -- ``engine=google&tbm=nws``

They are NOT assumed to return the same articles. When both are enabled the
orchestrator merges their hits and records which provider found what, so a
reader can see where each URL came from.

Failure policy: quota exhaustion and authentication errors are FATAL. A
provider that quietly returns nothing after its quota runs out would turn a
billing problem into a fake coverage gap, so we stop the provider, mark the
affected slices incomplete, and say so in the report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from tw_cdc_influenza_news.config import AppConfig, QueryConfig, SerpApiProviderConfig
from tw_cdc_influenza_news.dates import DateSlice
from tw_cdc_influenza_news.database import utc_now
from tw_cdc_influenza_news.discovery.base import DiscoveryProvider, SliceQueryResult
from tw_cdc_influenza_news.errors import ErrorCode
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import DiscoveredItem, DiscoveryEvidence
from tw_cdc_influenza_news.urls import unwrap_redirector

logger = get_logger("discovery.serpapi")

#: Substrings SerpApi uses when the account cannot run the search.
_QUOTA_MARKERS = (
    "run out of searches",
    "exceeded your",
    "plan limit",
    "account limit",
    "insufficient credits",
)
_AUTH_MARKERS = (
    "invalid api key",
    "missing api key",
    "unauthorized",
)


def _safe_slice_token(date_slice: DateSlice) -> str:
    """Filesystem-safe token for the raw-response filename."""
    return date_slice.key.replace(":", "_")


class SerpApiProvider(DiscoveryProvider):
    """Shared implementation for both SerpApi engines."""

    def __init__(
        self,
        config: AppConfig,
        provider_config: SerpApiProviderConfig,
        *,
        name: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(config)
        self.name = name
        self.provider_config = provider_config
        self._client = client
        self._owns_client = client is None

    # --- plumbing --------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.config.access.request_timeout_seconds,
                headers={"User-Agent": self.config.project.user_agent()},
                follow_redirects=True,
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
        if not self.provider_config.has_api_key():
            problems.append(
                f"{self.name}: {self.provider_config.api_key_env} is not set "
                "(set it in .env; the value is never printed or logged)"
            )
        return problems

    # --- date conversion -------------------------------------------------

    def date_params(self, date_slice: DateSlice) -> dict[str, str]:
        """Convert our half-open slice into Google's inclusive ``tbs`` range.

        Google's custom date range (``cdr:1,cd_min,cd_max``) is INCLUSIVE at
        both ends and formatted month/day/year. Our slice is ``[start, end)``,
        so the upper bound is ``last_day``, never ``end``. Passing ``end``
        here is the classic off-by-one that double-counts the first day of
        the next slice.
        """
        cd_min = date_slice.start.strftime("%m/%d/%Y")
        cd_max = date_slice.last_day.strftime("%m/%d/%Y")
        return {"tbs": f"cdr:1,cd_min:{cd_min},cd_max:{cd_max}"}

    def build_params(
        self,
        query: QueryConfig,
        date_slice: DateSlice,
        *,
        start_offset: int = 0,
    ) -> dict[str, str]:
        cfg = self.provider_config
        params: dict[str, str] = {
            "engine": cfg.engine,
            "q": query.text,
            "gl": cfg.gl,
            "hl": cfg.hl,
            "num": str(cfg.page_size),
            "api_key": cfg.api_key() or "",
        }
        if cfg.tbm:
            params["tbm"] = cfg.tbm
        if cfg.sort_by == "date":
            # Google News: sort by date rather than relevance, so a truncated
            # result set is truncated at a knowable boundary.
            params["so"] = "1"
        params.update(self.date_params(date_slice))
        if start_offset:
            params["start"] = str(start_offset)
        return params

    # --- response handling -----------------------------------------------

    @staticmethod
    def classify_error(status_code: int, payload: dict[str, Any] | None) -> ErrorCode | None:
        message = ""
        if payload:
            message = str(payload.get("error") or payload.get("message") or "").lower()
        if status_code in (401, 403) or any(m in message for m in _AUTH_MARKERS):
            return ErrorCode.PROVIDER_AUTH_ERROR
        if status_code == 429 or any(m in message for m in _QUOTA_MARKERS):
            return ErrorCode.PROVIDER_QUOTA_EXHAUSTED
        if status_code >= 500:
            return ErrorCode.PROVIDER_ERROR
        if message:
            return ErrorCode.PROVIDER_ERROR
        return None

    @staticmethod
    def parse_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten SerpApi's news results into plain dicts.

        ``google_news`` sometimes nests related coverage under ``stories``.
        Those are separate articles by separate publishers, so they are
        flattened in rather than dropped -- dropping them would silently
        lose exactly the syndicated copies the archive is meant to keep.
        """
        flattened: list[dict[str, Any]] = []
        for block in payload.get("news_results", []) or []:
            if not isinstance(block, dict):
                continue
            stories = block.get("stories")
            if isinstance(stories, list) and stories:
                flattened.extend(s for s in stories if isinstance(s, dict))
            if block.get("link"):
                flattened.append(block)
        # tbm=nws puts everything in news_results too, but older responses
        # used organic_results; accept both rather than return nothing.
        if not flattened:
            flattened = [
                b
                for b in (payload.get("organic_results") or [])
                if isinstance(b, dict) and b.get("link")
            ]
        return flattened

    @staticmethod
    def next_start_offset(payload: dict[str, Any]) -> int | None:
        """Read the next page offset from the provider's own pagination block.

        Trusting the provider's ``next`` link beats incrementing a parameter
        we guessed the name of: when the block is absent, there is no next
        page, which is a definite answer rather than an assumption.
        """
        pagination = payload.get("serpapi_pagination") or payload.get("pagination")
        if not isinstance(pagination, dict):
            return None
        next_link = pagination.get("next")
        if not isinstance(next_link, str) or not next_link:
            return None
        query = dict(parse_qsl(urlsplit(next_link).query))
        raw = query.get("start")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _raw_path(
        self, run_id: str, query: QueryConfig, date_slice: DateSlice, page: int
    ) -> Path:
        directory = self.config.raw_dir / run_id / self.name / query.id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{_safe_slice_token(date_slice)}-p{page:02d}.json"

    def save_raw(
        self,
        payload: dict[str, Any],
        *,
        run_id: str,
        query: QueryConfig,
        date_slice: DateSlice,
        page: int,
    ) -> str | None:
        """Persist the raw API response, with the key stripped.

        SerpApi echoes the request back in ``search_parameters`` and
        ``search_metadata``, API key included. Saving that verbatim would put
        a live credential in ``data/raw`` -- git-ignored, but still on disk
        and still in any backup. So the key is removed before writing.
        """
        if not self.provider_config.save_raw_response:
            return None
        sanitized = json.loads(json.dumps(payload))
        for block_name in ("search_parameters", "search_metadata"):
            block = sanitized.get(block_name)
            if isinstance(block, dict):
                for key in list(block):
                    if "api_key" in key.lower() or key.lower() in {"raw_html_file", "json_endpoint"}:
                        block.pop(key, None)
        path = self._raw_path(run_id, query, date_slice, page)
        path.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(path.relative_to(self.config.root)) if path.is_relative_to(self.config.root) else str(path)

    def to_item(
        self,
        result: dict[str, Any],
        *,
        query: QueryConfig,
        date_slice: DateSlice,
        position: int,
        raw_path: str | None,
    ) -> DiscoveredItem | None:
        link = result.get("link") or result.get("url")
        if not isinstance(link, str) or not link.startswith(("http://", "https://")):
            return None
        source = result.get("source")
        if isinstance(source, dict):
            source_name = source.get("name")
        else:
            source_name = source if isinstance(source, str) else None

        evidence = DiscoveryEvidence(
            provider=self.name,
            query_id=query.id,
            query_text=query.text,
            slice_key=date_slice.key,
            slice_start=date_slice.start.isoformat(),
            slice_end_exclusive=date_slice.end.isoformat(),
            position=position,
            provider_date_raw=result.get("date") if isinstance(result.get("date"), str) else None,
            provider_title=result.get("title") if isinstance(result.get("title"), str) else None,
            provider_source_name=source_name,
            gl=self.provider_config.gl,
            hl=self.provider_config.hl,
            discovered_at=utc_now(),
            raw_response_path=raw_path,
        )
        return DiscoveredItem(
            url=unwrap_redirector(link),
            title=evidence.provider_title,
            source_name=source_name,
            published_at_raw=evidence.provider_date_raw,
            snippet=result.get("snippet") if isinstance(result.get("snippet"), str) else None,
            evidence=evidence,
        )

    # --- the search itself -----------------------------------------------

    async def search(
        self,
        query: QueryConfig,
        date_slice: DateSlice,
        *,
        run_id: str,
    ) -> SliceQueryResult:
        cfg = self.provider_config
        outcome = SliceQueryResult()

        if not cfg.has_api_key():
            outcome.error_code = ErrorCode.PROVIDER_AUTH_ERROR
            outcome.error_message = f"{cfg.api_key_env} is not set"
            outcome.fatal = True
            return outcome

        client = self._get_client()
        seen_urls: set[str] = set()
        start_offset: int | None = 0
        page = 0

        while start_offset is not None and page < cfg.max_pages_per_slice:
            page += 1
            params = self.build_params(query, date_slice, start_offset=start_offset)
            try:
                response = await client.get(cfg.base_url, params=params)
            except httpx.TimeoutException as exc:
                outcome.error_code = ErrorCode.TIMEOUT
                outcome.error_message = f"timeout after {cfg.base_url} page {page}: {exc}"
                return outcome
            except httpx.HTTPError as exc:
                outcome.error_code = ErrorCode.CONNECTION_ERROR
                outcome.error_message = str(exc)
                return outcome

            try:
                payload = response.json()
            except ValueError:
                outcome.error_code = ErrorCode.PROVIDER_ERROR
                outcome.error_message = (
                    f"non-JSON response (HTTP {response.status_code}) on page {page}"
                )
                return outcome

            error_code = self.classify_error(response.status_code, payload)
            if error_code is not None:
                outcome.error_code = error_code
                # The provider's message can echo the request; keep it short
                # and let the redacting log filter handle the rest.
                outcome.error_message = str(payload.get("error") or f"HTTP {response.status_code}")[:500]
                outcome.fatal = error_code in (
                    ErrorCode.PROVIDER_AUTH_ERROR,
                    ErrorCode.PROVIDER_QUOTA_EXHAUSTED,
                ) and cfg.fail_closed_on_quota
                outcome.page_count = page - 1
                return outcome

            raw_path = self.save_raw(
                payload, run_id=run_id, query=query, date_slice=date_slice, page=page
            )
            if raw_path:
                outcome.raw_response_paths.append(raw_path)

            results = self.parse_results(payload)
            for position, result in enumerate(results, start=1 + len(outcome.items)):
                item = self.to_item(
                    result,
                    query=query,
                    date_slice=date_slice,
                    position=position,
                    raw_path=raw_path,
                )
                if item is None or item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                outcome.items.append(item)

            outcome.page_count = page

            next_offset = self.next_start_offset(payload)
            if next_offset is None or not results:
                start_offset = None
            else:
                start_offset = next_offset

        # Two independent truncation signals; either one means "there may be
        # more results here than we saw".
        hit_page_cap = start_offset is not None and page >= cfg.max_pages_per_slice
        hit_result_cap = outcome.result_count >= self.config.slicing.truncation_threshold
        outcome.result_limit_reached = hit_page_cap or hit_result_cap
        outcome.completed = True
        logger.info(
            "%s %s %s -> %d results over %d page(s)%s",
            self.name,
            query.id,
            date_slice.key,
            outcome.result_count,
            outcome.page_count,
            " (limit reached)" if outcome.result_limit_reached else "",
        )
        return outcome


class SerpApiGoogleNewsProvider(SerpApiProvider):
    """``engine=google_news``. The primary recommended discovery path."""

    def __init__(self, config: AppConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            config,
            config.providers.serpapi_google_news,
            name="serpapi-google-news",
            client=client,
        )


class SerpApiGoogleSearchNewsTabProvider(SerpApiProvider):
    """``engine=google&tbm=nws``. A second view, merged -- never assumed equal."""

    def __init__(self, config: AppConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            config,
            config.providers.serpapi_google_search_news_tab,
            name="serpapi-google-search-news-tab",
            client=client,
        )
