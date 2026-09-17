"""HTTP fetching, robots handling and the per-URL processing pipeline."""

from tw_cdc_influenza_news.fetching.http_client import (
    FetchOutcome,
    PoliteFetcher,
    classify_response,
    parse_retry_after,
)
from tw_cdc_influenza_news.fetching.pipeline import ProcessResult, UrlPipeline
from tw_cdc_influenza_news.fetching.playwright_fallback import (
    PlaywrightRenderer,
    RenderOutcome,
    chromium_installed,
    playwright_available,
)
from tw_cdc_influenza_news.fetching.robots import RobotsCache, RobotsDecision

__all__ = [
    "FetchOutcome",
    "PlaywrightRenderer",
    "PoliteFetcher",
    "ProcessResult",
    "RenderOutcome",
    "RobotsCache",
    "RobotsDecision",
    "UrlPipeline",
    "chromium_installed",
    "classify_response",
    "parse_retry_after",
    "playwright_available",
]
