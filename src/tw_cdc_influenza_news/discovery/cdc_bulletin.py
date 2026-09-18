"""Taiwan CDC press-release listing (cdc.gov.tw/Bulletin).

Unlike the Google-backed providers, this one queries the publisher's own
index. That makes it the authoritative source for CDC press releases: no
search engine sits between us and the newsroom, so nothing is missing
because it was never indexed, and nothing is truncated by a result ceiling
we cannot see.

The listing is a plain server-rendered form. Its parameters, confirmed
against the live site:

``keyword``    plain substring match on the title -- NOT Google syntax.
              ``"疾管署" "流感"`` matches nothing here; ``流感`` matches.
``startTime`` / ``endTime``  ``yyyy.mm.dd``, **inclusive at both ends**.
``PageSize``  rows per page.
``page``      1-based page number.

The inclusive upper bound means the exclusive end of our ``[start, end)``
slice must be converted with ``last_day``, exactly as the SerpApi adapter
does and exactly opposite to the RSS adapter's ``before:``. That is why each
adapter owns its own conversion.

Because the site filters by date itself, a slice maps onto one query and
there is no client-side date filtering to get wrong. robots.txt (checked
2026-09-18) disallows only ``/Uploads/``, ``/LogFiles/``, ``/TTS/`` and
``/File/``; ``/Bulletin/`` is permitted and no Crawl-delay is declared, so
the project's default per-domain rate applies.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from tw_cdc_influenza_news.config import AppConfig, CdcBulletinConfig, QueryConfig
from tw_cdc_influenza_news.database import utc_now
from tw_cdc_influenza_news.dates import DateSlice
from tw_cdc_influenza_news.discovery.base import DiscoveryProvider, SliceQueryResult
from tw_cdc_influenza_news.errors import ErrorCode
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import DiscoveredItem, DiscoveryEvidence
from tw_cdc_influenza_news.ratelimit import DomainRateLimiter

logger = get_logger("discovery.cdc")

PROVIDER_NAME = "cdc-bulletin"
SITE_DATE_FORMAT = "%Y.%m.%d"

#: A query with no site keyword cannot be asked of this site at all. Recorded
#: rather than silently skipped, so the coverage report explains the gap.
NO_KEYWORD_WARNING = (
    "query has no site_keyword; the CDC listing matches plain substrings, not "
    "search-engine syntax, so this query was not sent to it"
)


def parse_listing(html: str, *, base_url: str) -> list[dict[str, str]]:
    """Parse one listing page into ``{url, title, date}`` dicts.

    Each row looks like::

        <div class="cbp-item">
          <a href="/Bulletin/Detail/XXXX?typeid=9" title="full headline">
            <p class="icon-year">2023 - 3</p>
            <p class="icon-date">28</p>

    The ``title`` attribute carries the untruncated headline; the visible
    text is clipped with a JS ellipsis helper, so the attribute is the one
    worth reading.
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, str]] = []

    for item in soup.select("div.cbp-item"):
        anchor = item.find("a", href=True)
        if anchor is None or "/Bulletin/Detail/" not in anchor["href"]:
            continue

        year_el = item.select_one("p.icon-year")
        day_el = item.select_one("p.icon-date")
        published = ""
        if year_el is not None and day_el is not None:
            match = re.match(
                r"\s*(\d{4})\s*-\s*(\d{1,2})\s*", year_el.get_text(strip=True)
            )
            day = day_el.get_text(strip=True)
            if match and day.isdigit():
                published = (
                    f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(day):02d}"
                )

        title = anchor.get("title") or anchor.get_text(" ", strip=True)
        rows.append(
            {
                "url": urljoin(base_url, anchor["href"]),
                "title": (title or "").strip(),
                "date": published,
            }
        )
    return rows


def has_next_page(html: str, current_page: int) -> bool:
    """Whether the pager offers a page after ``current_page``.

    ``&`` inside an href is HTML-escaped, so the live markup reads
    ``?page=3&amp;keyword=...``. Today the site happens to put ``page``
    first, where a naive ``[?&]page=`` still matches -- but if that ordering
    ever changed, pagination would stop at page one and simply return fewer
    results, with no error anywhere. Unescaping first removes the dependency
    on parameter order.
    """
    unescaped = unescape(html)
    return bool(re.search(rf"[?&]page={current_page + 1}(?:\b|&|\")", unescaped))


class CdcBulletinProvider(DiscoveryProvider):
    """Discovery against the CDC's own press-release index."""

    name = PROVIDER_NAME
    supports_date_range = True
    best_effort = False

    def __init__(self, config: AppConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config)
        self.provider_config: CdcBulletinConfig = config.providers.cdc_bulletin
        self._client = client
        self._owns_client = client is None
        self._limiter = DomainRateLimiter(self.provider_config.requests_per_minute)
        # One request per (keyword, slice) even when several configured
        # queries collapse to the same site keyword.
        self._seen_requests: set[tuple[str, str]] = set()

    # --- plumbing --------------------------------------------------------

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
        if not any(q.site_keyword for q in self.config.queries):
            problems.append(
                f"{self.name}: no configured query defines site_keyword, so "
                "there is nothing to ask this site"
            )
        return problems

    # --- request building -------------------------------------------------

    @property
    def list_url(self) -> str:
        return f"{self.provider_config.base_url.rstrip('/')}/{self.provider_config.category_id}"

    def date_params(self, date_slice: DateSlice) -> dict[str, str]:
        """Convert ``[start, end)`` to the site's inclusive ``yyyy.mm.dd`` range.

        ``endTime`` includes its own date, so the upper bound is ``last_day``.
        Passing ``end`` would pull in the first day of the next slice and
        double-count it.
        """
        return {
            "startTime": date_slice.start.strftime(SITE_DATE_FORMAT),
            "endTime": date_slice.last_day.strftime(SITE_DATE_FORMAT),
        }

    def build_params(
        self, keyword: str, date_slice: DateSlice, *, page: int = 1
    ) -> dict[str, str]:
        params = {
            "keyword": keyword,
            "PageSize": str(self.provider_config.page_size),
            "page": str(page),
        }
        params.update(self.date_params(date_slice))
        return params

    def _raw_path(self, run_id: str, query: QueryConfig, date_slice: DateSlice, page: int) -> Path:
        directory = self.config.raw_dir / run_id / self.name / query.id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{date_slice.key.replace(':', '_')}-p{page:02d}.html"

    def save_raw(
        self, html: str, *, run_id: str, query: QueryConfig, date_slice: DateSlice, page: int
    ) -> str | None:
        if not self.provider_config.save_raw_html:
            return None
        path = self._raw_path(run_id, query, date_slice, page)
        path.write_text(html, encoding="utf-8")
        return (
            str(path.relative_to(self.config.root))
            if path.is_relative_to(self.config.root)
            else str(path)
        )

    # --- search ------------------------------------------------------------

    async def search(
        self,
        query: QueryConfig,
        date_slice: DateSlice,
        *,
        run_id: str,
    ) -> SliceQueryResult:
        outcome = SliceQueryResult()
        keyword = (query.site_keyword or "").strip()

        if not keyword:
            outcome.completed = True
            outcome.error_message = NO_KEYWORD_WARNING
            return outcome

        cache_key = (keyword, date_slice.key)
        if cache_key in self._seen_requests:
            # Another query already asked this exact question this run. The
            # articles are already registered; re-asking would only spend a
            # request to rediscover them.
            outcome.completed = True
            return outcome
        self._seen_requests.add(cache_key)

        client = self._get_client()
        seen_urls: set[str] = set()
        page = 0

        while page < self.provider_config.max_pages_per_slice:
            page += 1
            await self._limiter.acquire("cdc.gov.tw")

            try:
                response = await client.get(
                    self.list_url, params=self.build_params(keyword, date_slice, page=page)
                )
            except httpx.TimeoutException as exc:
                outcome.error_code = ErrorCode.TIMEOUT
                outcome.error_message = str(exc)
                return outcome
            except httpx.HTTPError as exc:
                outcome.error_code = ErrorCode.CONNECTION_ERROR
                outcome.error_message = str(exc)
                return outcome

            if response.status_code in (403, 429):
                outcome.error_code = (
                    ErrorCode.HTTP_403 if response.status_code == 403 else ErrorCode.HTTP_429
                )
                outcome.error_message = f"HTTP {response.status_code} from the CDC listing"
                outcome.fatal = True
                logger.warning(
                    "%s stopping: %s. Not attempting to work around it.",
                    self.name,
                    outcome.error_message,
                )
                return outcome
            if response.status_code >= 400:
                outcome.error_code = (
                    ErrorCode.SERVER_ERROR
                    if response.status_code >= 500
                    else ErrorCode.CLIENT_ERROR
                )
                outcome.error_message = f"HTTP {response.status_code}"
                return outcome

            html = response.text
            raw_path = self.save_raw(
                html, run_id=run_id, query=query, date_slice=date_slice, page=page
            )
            if raw_path:
                outcome.raw_response_paths.append(raw_path)

            rows = parse_listing(html, base_url=str(response.url))
            for row in rows:
                if row["url"] in seen_urls:
                    continue
                seen_urls.add(row["url"])
                outcome.items.append(
                    self._to_item(
                        row,
                        query=query,
                        keyword=keyword,
                        date_slice=date_slice,
                        position=len(outcome.items) + 1,
                        raw_path=raw_path,
                    )
                )

            outcome.page_count = page
            if not rows or not has_next_page(html, page):
                break

        # The site paginates rather than capping, so a full page is not a
        # truncation signal on its own -- only exhausting our own page budget
        # while the pager still offers more is.
        outcome.result_limit_reached = page >= self.provider_config.max_pages_per_slice
        outcome.completed = True
        logger.info(
            "%s %s [%s] %s -> %d result(s) over %d page(s)",
            self.name,
            query.id,
            keyword,
            date_slice.key,
            outcome.result_count,
            outcome.page_count,
        )
        return outcome

    def _to_item(
        self,
        row: dict[str, str],
        *,
        query: QueryConfig,
        keyword: str,
        date_slice: DateSlice,
        position: int,
        raw_path: str | None,
    ) -> DiscoveredItem:
        evidence = DiscoveryEvidence(
            provider=self.name,
            query_id=query.id,
            query_text=keyword,
            slice_key=date_slice.key,
            slice_start=date_slice.start.isoformat(),
            slice_end_exclusive=date_slice.end.isoformat(),
            position=position,
            provider_date_raw=row["date"] or None,
            provider_title=row["title"] or None,
            provider_source_name="衛生福利部疾病管制署",
            gl="tw",
            hl="zh-TW",
            discovered_at=utc_now(),
            raw_response_path=raw_path,
        )
        return DiscoveredItem(
            url=row["url"],
            title=row["title"] or None,
            source_name="衛生福利部疾病管制署",
            published_at_raw=row["date"] or None,
            snippet=None,
            evidence=evidence,
        )
