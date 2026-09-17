"""robots.txt fetching, caching and evaluation.

A disallow is treated as a block: the URL is recorded as ``blocked`` with
``robots_disallowed`` and never fetched. There is no override flag, because
an override flag is how a polite crawler becomes an impolite one.

Fetch failures are handled asymmetrically and deliberately:

* 404 / 410 -- no robots.txt exists, so everything is allowed. That is what
  the standard says.
* 401 / 403 -- the file exists and we are not allowed to read it. Treated as
  disallow-all for that host.
* timeout / 5xx -- unknown. We allow, but remember the uncertainty so the
  coverage report can mention it, rather than silently skipping a domain
  because its robots endpoint was briefly down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.urls import registrable_host

logger = get_logger("fetching.robots")


@dataclass(slots=True)
class RobotsDecision:
    allowed: bool
    reason: str
    crawl_delay: float | None = None
    uncertain: bool = False


@dataclass(slots=True)
class _CacheEntry:
    parser: RobotFileParser | None
    allow_all: bool
    disallow_all: bool
    fetched_at: float
    uncertain: bool
    reason: str


def robots_url_for(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


class RobotsCache:
    """One robots.txt per scheme+host, cached for a configurable TTL."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        ttl_seconds: int = 86400,
        timeout_seconds: float = 15.0,
        enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._enabled = enabled
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    def _cache_key(self, url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc.lower()}"

    def _fresh(self, entry: _CacheEntry) -> bool:
        return (self._clock() - entry.fetched_at) < self._ttl

    async def _load(self, url: str) -> _CacheEntry:
        key = self._cache_key(url)
        cached = self._cache.get(key)
        if cached is not None and self._fresh(cached):
            return cached

        robots_url = robots_url_for(url)
        try:
            response = await self._client.get(
                robots_url,
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            entry = _CacheEntry(
                parser=None,
                allow_all=True,
                disallow_all=False,
                fetched_at=self._clock(),
                uncertain=True,
                reason=f"robots.txt unreachable ({exc.__class__.__name__}); proceeding",
            )
            self._cache[key] = entry
            return entry

        if response.status_code in (401, 403):
            entry = _CacheEntry(
                parser=None,
                allow_all=False,
                disallow_all=True,
                fetched_at=self._clock(),
                uncertain=False,
                reason=f"robots.txt returned HTTP {response.status_code}: treating as disallow-all",
            )
        elif response.status_code in (404, 410):
            entry = _CacheEntry(
                parser=None,
                allow_all=True,
                disallow_all=False,
                fetched_at=self._clock(),
                uncertain=False,
                reason="no robots.txt; nothing disallowed",
            )
        elif response.status_code >= 400:
            entry = _CacheEntry(
                parser=None,
                allow_all=True,
                disallow_all=False,
                fetched_at=self._clock(),
                uncertain=True,
                reason=f"robots.txt returned HTTP {response.status_code}; proceeding",
            )
        else:
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                parser.parse(response.text.splitlines())
            except Exception as exc:  # a malformed file must not stop the run
                entry = _CacheEntry(
                    parser=None,
                    allow_all=True,
                    disallow_all=False,
                    fetched_at=self._clock(),
                    uncertain=True,
                    reason=f"robots.txt unparseable ({exc}); proceeding",
                )
            else:
                entry = _CacheEntry(
                    parser=parser,
                    allow_all=False,
                    disallow_all=False,
                    fetched_at=self._clock(),
                    uncertain=False,
                    reason="robots.txt parsed",
                )

        self._cache[key] = entry
        return entry

    async def check(self, url: str) -> RobotsDecision:
        if not self._enabled:
            return RobotsDecision(True, "robots checking disabled in config")

        entry = await self._load(url)
        if entry.disallow_all:
            logger.info("robots disallows %s: %s", registrable_host(url), entry.reason)
            return RobotsDecision(False, entry.reason)
        if entry.parser is None:
            return RobotsDecision(True, entry.reason, uncertain=entry.uncertain)

        allowed = entry.parser.can_fetch(self._user_agent, url)
        delay = entry.parser.crawl_delay(self._user_agent)
        try:
            crawl_delay = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            crawl_delay = None

        return RobotsDecision(
            allowed=allowed,
            reason="allowed by robots.txt" if allowed else "disallowed by robots.txt",
            crawl_delay=crawl_delay,
        )
