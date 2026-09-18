"""Polite HTTP fetching.

Everything in here is aimed at *not tripping* an access restriction, and at
stopping cleanly when one trips anyway:

* one independent rate limiter per domain, plus a small global concurrency cap
* an identifiable User-Agent carrying the project name and a contact address
* ``Retry-After`` obeyed exactly as given
* exponential backoff with jitter, a bounded number of attempts
* robots.txt consulted before the first request to a host
* block signals (403, 429, CAPTCHA, unusual-traffic, login, paywall)
  classified and escalated, never retried and never circumvented

There is nothing here that randomises identity, rotates an address, or makes
the client look like something it is not. That is a deliberate omission.
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field

import httpx

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.errors import INTERSTITIAL_MARKERS, ErrorCode
from tw_cdc_influenza_news.fetching.robots import RobotsCache
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.ratelimit import BlockRegistry, DomainRateLimiter, backoff_delay
from tw_cdc_influenza_news.urls import registrable_host

logger = get_logger("fetching.http")

#: Interstitial markers, shared with the extractor so the HTTP path and
#: the Playwright path cannot drift apart. See errors.INTERSTITIAL_MARKERS.
BODY_BLOCK_MARKERS: tuple[tuple[str, ErrorCode], ...] = INTERSTITIAL_MARKERS

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_RETRY_AFTER_SECONDS_RE = re.compile(r"^\s*(\d+)\s*$")


@dataclass(slots=True)
class FetchOutcome:
    """The result of one fetch attempt chain for one URL."""

    url: str
    final_url: str | None = None
    status_code: int | None = None
    html: str | None = None
    content_type: str | None = None
    attempts: int = 0
    error_code: ErrorCode | None = None
    error_message: str | None = None
    blocked: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_code is None and bool(self.html)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait, from a ``Retry-After`` header.

    Handles the delta-seconds form. The HTTP-date form is parsed by
    ``email.utils`` rather than guessed at.
    """
    if not value:
        return None
    match = _RETRY_AFTER_SECONDS_RE.match(value)
    if match:
        return float(match.group(1))
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    from datetime import UTC, datetime

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())


def classify_response(
    status_code: int, body_sample: str, content_type: str | None
) -> ErrorCode | None:
    """Map a response to an error code, or ``None`` when it is usable."""
    if status_code == 403:
        return ErrorCode.HTTP_403
    if status_code == 429:
        return ErrorCode.HTTP_429
    if status_code in (401, 407):
        return ErrorCode.LOGIN_REQUIRED
    if status_code in (404, 410):
        return ErrorCode.NOT_FOUND
    if status_code == 451:
        return ErrorCode.ACCESS_DENIED
    if status_code >= 500:
        return ErrorCode.SERVER_ERROR

    lowered = body_sample.lower()
    for marker, code in BODY_BLOCK_MARKERS:
        if marker in lowered:
            return code

    if status_code >= 400:
        return ErrorCode.CLIENT_ERROR

    if content_type and not any(ct in content_type.lower() for ct in _HTML_CONTENT_TYPES):
        return ErrorCode.UNSUPPORTED_CONTENT_TYPE

    return None


class PoliteFetcher:
    """Async HTML fetcher with per-domain limits and block escalation."""

    def __init__(
        self,
        config: AppConfig,
        *,
        block_registry: BlockRegistry | None = None,
        client: httpx.AsyncClient | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.access = config.access
        self.blocks = block_registry or BlockRegistry()
        self._rng = rng or random.Random()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self.access.request_timeout_seconds,
            follow_redirects=True,
            max_redirects=self.access.max_redirects,
            headers={
                "User-Agent": config.project.user_agent(),
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
            },
        )
        self._limiter = DomainRateLimiter(self.access.requests_per_domain_per_minute)
        self._semaphore = asyncio.Semaphore(self.access.max_concurrency)
        self._robots = RobotsCache(
            self._client,
            user_agent=config.project.user_agent(),
            ttl_seconds=self.access.robots_cache_ttl_seconds,
            timeout_seconds=self.access.request_timeout_seconds,
            enabled=self.access.respect_robots_txt,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PoliteFetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _escalate_block(self, domain: str, code: ErrorCode, detail: str, url: str) -> None:
        if not self.access.stop_domain_after_block:
            return
        from tw_cdc_influenza_news.errors import is_domain_level_block

        if is_domain_level_block(code):
            self.blocks.block_domain(domain, code, detail, url=url)

    async def fetch(self, url: str) -> FetchOutcome:
        """Fetch one URL, retrying only what is worth retrying."""
        outcome = FetchOutcome(url=url)
        domain = registrable_host(url)
        if not domain:
            outcome.error_code = ErrorCode.INVALID_URL
            outcome.error_message = "URL has no host"
            return outcome

        if self.blocks.is_domain_blocked(domain):
            event = self.blocks.domain_reason(domain)
            outcome.error_code = event.error_code if event else ErrorCode.ACCESS_DENIED
            outcome.error_message = (
                f"domain already stopped this run: {event.detail}" if event else "domain stopped"
            )
            outcome.blocked = True
            return outcome

        decision = await self._robots.check(url)
        if not decision.allowed:
            outcome.error_code = ErrorCode.ROBOTS_DISALLOWED
            outcome.error_message = decision.reason
            outcome.blocked = True
            self._escalate_block(domain, ErrorCode.ROBOTS_DISALLOWED, decision.reason, url)
            return outcome
        if decision.uncertain:
            outcome.notes.append(decision.reason)
        if decision.crawl_delay:
            # A site asking for more space than our default gets it.
            per_minute = max(1, int(60 // max(decision.crawl_delay, 1)))
            if per_minute < self.access.requests_per_domain_per_minute:
                self._limiter.set_domain_limit(domain, per_minute)

        for attempt in range(1, self.access.max_retries + 2):
            outcome.attempts = attempt
            async with self._semaphore:
                await self._limiter.acquire(domain)
                try:
                    response = await self._client.get(url)
                except httpx.TooManyRedirects as exc:
                    outcome.error_code = ErrorCode.TOO_MANY_REDIRECTS
                    outcome.error_message = str(exc)
                    return outcome
                except httpx.TimeoutException as exc:
                    outcome.error_code = ErrorCode.TIMEOUT
                    outcome.error_message = str(exc)
                    response = None
                except httpx.HTTPError as exc:
                    outcome.error_code = ErrorCode.CONNECTION_ERROR
                    outcome.error_message = str(exc)
                    response = None

            if response is not None:
                outcome.status_code = response.status_code
                outcome.final_url = str(response.url)
                outcome.content_type = response.headers.get("content-type")
                body = response.text
                error_code = classify_response(
                    response.status_code, body[:8000], outcome.content_type
                )

                if error_code is None:
                    outcome.html = body
                    outcome.error_code = None
                    outcome.error_message = None
                    return outcome

                outcome.error_code = error_code
                outcome.error_message = f"HTTP {response.status_code} ({error_code.value})"

                from tw_cdc_influenza_news.errors import is_blocked, is_retryable

                if is_blocked(error_code):
                    outcome.blocked = True
                    if error_code is ErrorCode.HTTP_429 and self.access.obey_retry_after:
                        delay = parse_retry_after(response.headers.get("retry-after"))
                        if delay:
                            self._limiter.penalize(domain, delay)
                            outcome.notes.append(f"Retry-After: {delay:.0f}s honoured")
                    self._escalate_block(domain, error_code, outcome.error_message, url)
                    # A block is never retried. Stopping is the response.
                    return outcome

                if not is_retryable(error_code):
                    return outcome

            if attempt > self.access.max_retries:
                break

            delay = backoff_delay(
                attempt,
                base=self.access.backoff_base_seconds,
                maximum=self.access.backoff_max_seconds,
                jitter=self.access.backoff_jitter_seconds,
                rng=self._rng,
            )
            logger.info(
                "retry %d/%d for %s in %.1fs (%s)",
                attempt,
                self.access.max_retries,
                url,
                delay,
                outcome.error_code.value if outcome.error_code else "unknown",
            )
            await asyncio.sleep(delay)

        return outcome
