"""Per-domain rate limiting and the block registry.

Two separate jobs that both key on the domain:

* :class:`DomainRateLimiter` spaces requests out so we are unlikely to
  trigger a restriction in the first place.
* :class:`BlockRegistry` records the restrictions we triggered anyway, and
  stops us touching that domain or provider again for the rest of the run.

The registry is the enforcement point for the project's access policy: when
a site says no, the answer is to stop asking, and the coverage report says
which months that cost us.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from tw_cdc_influenza_news.errors import ErrorCode
from tw_cdc_influenza_news.logging_utils import get_logger

logger = get_logger("ratelimit")

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class DomainRateLimiter:
    """Sliding-window limiter, one independent window per domain.

    A shared global limiter would let one busy domain starve every other, and
    would also be the wrong politeness unit: the limit a site cares about is
    the load on *that* site.

    ``clock`` and ``sleeper`` are injectable so tests can drive the limiter
    without spending real seconds.
    """

    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = requests_per_minute
        # time.monotonic, not the event loop's clock: providers are
        # constructed outside any running loop (doctor does it just to run
        # preflight), and asking for the loop there is an error in 3.12.
        self._clock: Clock = clock or time.monotonic
        self._sleeper: Sleeper = sleeper or asyncio.sleep
        self._windows: dict[str, deque[float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._penalty_until: dict[str, float] = {}
        self._overrides: dict[str, int] = {}

    def set_domain_limit(self, domain: str, requests_per_minute: int) -> None:
        """Override the limit for one domain (e.g. a provider's own rate)."""
        self._overrides[domain] = requests_per_minute

    def _limit_for(self, domain: str) -> int:
        return self._overrides.get(domain, self.requests_per_minute)

    def _lock(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[domain] = lock
        return lock

    def penalize(self, domain: str, seconds: float) -> None:
        """Hold a domain off for ``seconds`` -- this is how Retry-After lands.

        We extend an existing penalty rather than replace it, so two
        concurrent 429s cannot shorten the wait.
        """
        if seconds <= 0:
            return
        until = self._clock() + seconds
        self._penalty_until[domain] = max(self._penalty_until.get(domain, 0.0), until)
        logger.info("holding %s for %.1fs (Retry-After / backoff)", domain, seconds)

    async def acquire(self, domain: str) -> float:
        """Wait until a request to ``domain`` is permitted.

        Returns the number of seconds waited, which the caller logs so a slow
        run is explainable rather than mysterious.
        """
        waited = 0.0
        async with self._lock(domain):
            while True:
                now = self._clock()

                penalty = self._penalty_until.get(domain, 0.0)
                if penalty > now:
                    delay = penalty - now
                    await self._sleeper(delay)
                    waited += delay
                    continue

                window = self._windows.setdefault(domain, deque())
                while window and now - window[0] >= 60.0:
                    window.popleft()

                if len(window) < self._limit_for(domain):
                    window.append(now)
                    return waited

                # Wait exactly until the oldest request leaves the window.
                # This is a state-driven wait, not a fixed sleep.
                delay = 60.0 - (now - window[0])
                await self._sleeper(max(delay, 0.0))
                waited += max(delay, 0.0)


def backoff_delay(
    attempt: int,
    *,
    base: float,
    maximum: float,
    jitter: float,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with a small additive jitter.

    ``attempt`` is 1-based. Jitter keeps a batch of simultaneous failures
    from retrying in lockstep.
    """
    rng = rng or random
    raw = base * (2 ** max(0, attempt - 1))
    return min(raw, maximum) + (rng.uniform(0, jitter) if jitter > 0 else 0.0)


@dataclass(frozen=True, slots=True)
class BlockEvent:
    scope: str  # "domain" | "provider"
    target: str
    error_code: ErrorCode
    detail: str
    url: str | None
    occurred_at: str


@dataclass
class BlockRegistry:
    """Domains and providers we have stopped touching, and why.

    Nothing here is ever "retried later in the run". A block is a decision to
    stop, recorded so the coverage report can be honest about the gap.
    """

    blocked_domains: dict[str, BlockEvent] = field(default_factory=dict)
    stopped_providers: dict[str, BlockEvent] = field(default_factory=dict)
    events: list[BlockEvent] = field(default_factory=list)

    def block_domain(
        self,
        domain: str,
        code: ErrorCode,
        detail: str,
        *,
        url: str | None = None,
        occurred_at: str = "",
    ) -> BlockEvent:
        event = BlockEvent("domain", domain, code, detail, url, occurred_at)
        self.blocked_domains.setdefault(domain, event)
        self.events.append(event)
        logger.warning("stopping domain %s: %s (%s)", domain, code.value, detail)
        return event

    def stop_provider(
        self,
        provider: str,
        code: ErrorCode,
        detail: str,
        *,
        occurred_at: str = "",
    ) -> BlockEvent:
        event = BlockEvent("provider", provider, code, detail, None, occurred_at)
        self.stopped_providers.setdefault(provider, event)
        self.events.append(event)
        logger.warning("stopping provider %s: %s (%s)", provider, code.value, detail)
        return event

    def is_domain_blocked(self, domain: str) -> bool:
        return domain in self.blocked_domains

    def is_provider_stopped(self, provider: str) -> bool:
        return provider in self.stopped_providers

    def domain_reason(self, domain: str) -> BlockEvent | None:
        return self.blocked_domains.get(domain)

    def as_report(self) -> dict[str, list[dict[str, str]]]:
        def render(event: BlockEvent) -> dict[str, str]:
            return {
                "scope": event.scope,
                "target": event.target,
                "error_code": event.error_code.value,
                "detail": event.detail,
                "url": event.url or "",
                "occurred_at": event.occurred_at,
            }

        return {
            "blocked_domains": [render(e) for e in self.blocked_domains.values()],
            "stopped_providers": [render(e) for e in self.stopped_providers.values()],
        }
