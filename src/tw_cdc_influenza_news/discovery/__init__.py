"""Discovery providers and the registry that resolves CLI names to them."""

from __future__ import annotations

from typing import Callable

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.discovery.base import DiscoveryProvider, SliceQueryResult
from tw_cdc_influenza_news.discovery.cdc_bulletin import CdcBulletinProvider
from tw_cdc_influenza_news.discovery.google_news_rss import GoogleNewsRssProvider
from tw_cdc_influenza_news.discovery.manual_urls import ManualUrlsProvider
from tw_cdc_influenza_news.discovery.serpapi_google_news import (
    SerpApiGoogleNewsProvider,
    SerpApiGoogleSearchNewsTabProvider,
)
from tw_cdc_influenza_news.errors import ConfigError

#: CLI name -> constructor. The CLI name is the one users type and the one
#: written into ``discovery_providers`` in the output, so it is stable.
PROVIDER_REGISTRY: dict[str, Callable[[AppConfig], DiscoveryProvider]] = {
    "serpapi-google-news": SerpApiGoogleNewsProvider,
    "serpapi-google-search-news-tab": SerpApiGoogleSearchNewsTabProvider,
    "google-news-rss": GoogleNewsRssProvider,
    "cdc-bulletin": CdcBulletinProvider,
    "manual-urls": ManualUrlsProvider,
}

#: Providers that actually run during ``crawl``. manual-urls is excluded: it
#: has nothing to search, and is driven by ``import-urls`` instead.
SEARCHABLE_PROVIDERS: tuple[str, ...] = (
    "serpapi-google-news",
    "serpapi-google-search-news-tab",
    "google-news-rss",
    "cdc-bulletin",
)


def build_provider(name: str, config: AppConfig) -> DiscoveryProvider:
    try:
        factory = PROVIDER_REGISTRY[name]
    except KeyError:
        raise ConfigError(
            f"unknown provider {name!r}. Known providers: "
            + ", ".join(sorted(PROVIDER_REGISTRY))
        ) from None
    return factory(config)


def enabled_provider_names(config: AppConfig) -> list[str]:
    """Searchable providers switched on in the config, in registry order."""
    flags = {
        "serpapi-google-news": config.providers.serpapi_google_news.enabled,
        "serpapi-google-search-news-tab": config.providers.serpapi_google_search_news_tab.enabled,
        "google-news-rss": config.providers.google_news_rss.enabled,
        "cdc-bulletin": config.providers.cdc_bulletin.enabled,
    }
    return [name for name in SEARCHABLE_PROVIDERS if flags.get(name)]


__all__ = [
    "PROVIDER_REGISTRY",
    "SEARCHABLE_PROVIDERS",
    "DiscoveryProvider",
    "SliceQueryResult",
    "SerpApiGoogleNewsProvider",
    "SerpApiGoogleSearchNewsTabProvider",
    "GoogleNewsRssProvider",
    "CdcBulletinProvider",
    "ManualUrlsProvider",
    "build_provider",
    "enabled_provider_names",
]
