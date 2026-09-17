"""The discovery provider interface.

A provider answers one question: *which URLs can you surface for this query,
in this half-open date window?* Everything downstream -- fetching,
extraction, dedup, coverage -- is provider-agnostic.

Each adapter converts the date window into its own parameter syntax and
validates the result. Nothing here assumes two providers speak the same
date dialect, because they do not: SerpApi's Google News engine wants an
inclusive ``cdr`` range in US date order, Google News RSS wants ``after:``
and ``before:`` operators inside the query string with different
inclusivity, and both differ from the ``[start, end)`` slices we reason in.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from tw_cdc_influenza_news.config import AppConfig, QueryConfig
from tw_cdc_influenza_news.dates import DateSlice
from tw_cdc_influenza_news.errors import ErrorCode
from tw_cdc_influenza_news.models import DiscoveredItem


@dataclass(slots=True)
class SliceQueryResult:
    """What one provider returned for one (query, slice)."""

    items: list[DiscoveredItem] = field(default_factory=list)
    page_count: int = 0
    #: The provider hit a cap we can see -- more pages existed, or the result
    #: count sat at the configured ceiling. The orchestrator responds by
    #: splitting the slice, not by assuming the extra results do not exist.
    result_limit_reached: bool = False
    completed: bool = False
    error_code: ErrorCode | None = None
    error_message: str | None = None
    #: The provider must stop for the rest of the run (quota, auth, a block).
    fatal: bool = False
    raw_response_paths: list[str] = field(default_factory=list)

    @property
    def result_count(self) -> int:
        return len(self.items)


class DiscoveryProvider(abc.ABC):
    """Base class for every discovery provider.

    Subclasses must be safe to construct without network access -- ``doctor``
    constructs them purely to run :meth:`preflight`.
    """

    #: Stable identifier written into ``discovery_providers`` and the report.
    name: str = "base"

    #: Whether the provider can restrict results to a date window at all. A
    #: provider that cannot must say so, because slicing is meaningless for
    #: it and the coverage report has to explain why.
    supports_date_range: bool = True

    #: Set when the provider guarantees nothing about completeness.
    best_effort: bool = False

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    @abc.abstractmethod
    async def search(
        self,
        query: QueryConfig,
        date_slice: DateSlice,
        *,
        run_id: str,
    ) -> SliceQueryResult:
        """Return the hits this provider can surface for the slice."""

    def preflight(self) -> list[str]:
        """Problems that would stop this provider working, for ``doctor``.

        Returns human-readable strings. An empty list means ready. This must
        never print or return a secret -- only whether one is present.
        """
        return []

    async def aclose(self) -> None:
        """Release network resources. Safe to call more than once."""
        return None

    def describe(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "supports_date_range": self.supports_date_range,
            "best_effort": self.best_effort,
        }
