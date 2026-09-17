"""Discovery adapters, driven entirely from saved response fixtures.

No network. Each adapter is checked on the two things only it can get wrong:
how it converts a date window into its own parameter syntax, and how it reads
its own response shape.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.dates import DateSlice, Granularity
from tw_cdc_influenza_news.discovery import (
    PROVIDER_REGISTRY,
    SEARCHABLE_PROVIDERS,
    build_provider,
    enabled_provider_names,
)
from tw_cdc_influenza_news.discovery.google_news_rss import GoogleNewsRssProvider
from tw_cdc_influenza_news.discovery.manual_urls import ManualUrlsProvider
from tw_cdc_influenza_news.discovery.serpapi_google_news import (
    SerpApiGoogleNewsProvider,
    SerpApiGoogleSearchNewsTabProvider,
)
from tw_cdc_influenza_news.errors import ConfigError

from .conftest import read_fixture

JANUARY = DateSlice(date(2024, 1, 1), date(2024, 2, 1), Granularity.MONTH)
LEAP_FEBRUARY = DateSlice(date(2024, 2, 1), date(2024, 3, 1), Granularity.MONTH)


# --- registry ---------------------------------------------------------------


def test_every_registered_provider_can_be_built(config: AppConfig):
    for name in PROVIDER_REGISTRY:
        provider = build_provider(name, config)
        assert provider.name == name


def test_an_unknown_provider_name_is_a_clear_error(config: AppConfig):
    with pytest.raises(ConfigError) as excinfo:
        build_provider("bing-news", config)

    assert "bing-news" in str(excinfo.value)
    assert "serpapi-google-news" in str(excinfo.value)


def test_manual_urls_is_not_a_searchable_provider():
    assert "manual-urls" not in SEARCHABLE_PROVIDERS


def test_enabled_providers_follow_the_config(config: AppConfig):
    config.providers.serpapi_google_news.enabled = True
    config.providers.google_news_rss.enabled = False

    assert enabled_provider_names(config) == ["serpapi-google-news"]


# --- serpapi: date conversion ------------------------------------------------


def test_serpapi_uses_the_inclusive_last_day_not_the_exclusive_end(config: AppConfig):
    """Google's cdr range is inclusive at both ends; passing our exclusive
    bound would pull in the first day of the next slice."""
    params = SerpApiGoogleNewsProvider(config).date_params(JANUARY)

    assert params["tbs"] == "cdr:1,cd_min:01/01/2024,cd_max:01/31/2024"


def test_serpapi_date_conversion_handles_the_leap_day(config: AppConfig):
    params = SerpApiGoogleNewsProvider(config).date_params(LEAP_FEBRUARY)

    assert params["tbs"].endswith("cd_max:02/29/2024")


def test_the_news_tab_adapter_sends_its_own_engine_parameters(config: AppConfig):
    config.providers.serpapi_google_search_news_tab.enabled = True
    provider = SerpApiGoogleSearchNewsTabProvider(config)

    params = provider.build_params(config.queries[0], JANUARY)

    assert params["engine"] == "google"
    assert params["tbm"] == "nws"


def test_the_two_serpapi_adapters_are_distinct_providers(config: AppConfig):
    assert (
        SerpApiGoogleNewsProvider(config).name
        != SerpApiGoogleSearchNewsTabProvider(config).name
    )


# --- serpapi: response parsing ------------------------------------------------


def test_serpapi_results_including_nested_stories_are_flattened():
    payload = json.loads(read_fixture("providers", "serpapi_google_news_page1.json"))

    results = SerpApiGoogleNewsProvider.parse_results(payload)
    links = [r["link"] for r in results]

    assert "https://carrier.example.tw/realtimenews/20240116001" in links
    assert "https://www.wire.example.tw/news/aipl/202401160123.aspx" in links
    assert len(links) == 3


def test_the_next_page_offset_comes_from_the_providers_own_pagination():
    payload = json.loads(read_fixture("providers", "serpapi_google_news_page1.json"))

    assert SerpApiGoogleNewsProvider.next_start_offset(payload) == 100


def test_no_pagination_block_means_no_next_page():
    payload = json.loads(read_fixture("providers", "serpapi_google_news_page2.json"))

    assert SerpApiGoogleNewsProvider.next_start_offset(payload) is None


def test_a_result_item_carries_its_evidence(config: AppConfig):
    payload = json.loads(read_fixture("providers", "serpapi_google_news_page1.json"))
    provider = SerpApiGoogleNewsProvider(config)
    result = SerpApiGoogleNewsProvider.parse_results(payload)[0]

    item = provider.to_item(
        result, query=config.queries[0], date_slice=JANUARY, position=1, raw_path=None
    )

    assert item is not None
    assert item.evidence.provider == "serpapi-google-news"
    assert item.evidence.query_id == config.queries[0].id
    assert item.evidence.slice_start == "2024-01-01"
    assert item.evidence.slice_end_exclusive == "2024-02-01"
    assert item.evidence.gl == "tw" and item.evidence.hl == "zh-TW"
    assert item.published_at_raw  # the provider's own date string is kept verbatim


# --- google news rss ----------------------------------------------------------


def test_rss_puts_the_date_window_in_the_query_with_matching_inclusivity(
    config: AppConfig,
):
    """after: is inclusive, before: is exclusive -- which is exactly our
    [start, end) slice, unlike the SerpApi adapter above."""
    query_string = GoogleNewsRssProvider(config).build_query_string(
        config.queries[0], JANUARY
    )

    assert "after:2024-01-01" in query_string
    assert "before:2024-02-01" in query_string
    assert config.queries[0].text in query_string


def test_rss_sends_the_taiwan_locale(config: AppConfig):
    params = GoogleNewsRssProvider(config).build_params(config.queries[0], JANUARY)

    assert params["gl"] == "TW"
    assert params["hl"] == "zh-TW"
    assert params["ceid"] == "TW:zh-Hant"


def test_rss_feed_parsing_extracts_link_title_date_and_source():
    entries = GoogleNewsRssProvider.parse_feed(
        read_fixture("providers", "google_news_rss_sample.xml")
    )

    assert len(entries) == 2
    assert entries[0]["source"] == "範例新聞網"
    assert entries[0]["pub_date"] == "Mon, 15 Jan 2024 00:30:00 GMT"
    assert entries[1]["link"].startswith("https://news.example.tw/")


def test_rss_is_declared_best_effort(config: AppConfig):
    provider = GoogleNewsRssProvider(config)

    assert provider.best_effort is True
    assert provider.describe()["best_effort"] is True


# --- manual urls ---------------------------------------------------------------


def test_manual_jsonl_import_accepts_objects_and_bare_strings(config: AppConfig):
    result = ManualUrlsProvider(config).load_file(
        _fixture_path("providers", "manual_urls.jsonl")
    )

    urls = [item.url for item in result.items]
    assert "https://news.example.tw/article/2023/05/01/cdc-briefing" in urls
    assert "https://third.example.tw/news/12345" in urls
    assert len(urls) == 3  # one invalid, one duplicate


def test_manual_import_reports_what_it_skipped_and_why(config: AppConfig):
    result = ManualUrlsProvider(config).load_file(
        _fixture_path("providers", "manual_urls.jsonl")
    )

    reasons = {reason for _line, _value, reason in result.skipped}
    assert "not an absolute http(s) URL" in reasons
    assert "duplicate within this file" in reasons


def test_manual_txt_import_ignores_comments_and_blank_lines(config: AppConfig):
    result = ManualUrlsProvider(config).load_file(
        _fixture_path("providers", "manual_urls.txt")
    )

    assert len(result.items) == 2
    assert all(item.url.startswith("https://") for item in result.items)


def test_imported_urls_enter_the_pipeline_as_manual_urls_evidence(config: AppConfig):
    result = ManualUrlsProvider(config).load_file(
        _fixture_path("providers", "manual_urls.jsonl"), note="從同事收到的清單"
    )

    evidence = result.items[0].evidence
    assert evidence.provider == "manual-urls"
    assert evidence.query_text == "從同事收到的清單"
    assert evidence.slice_start == ""  # manual imports have no date window


def test_a_missing_import_file_is_a_clear_error(config: AppConfig):
    with pytest.raises(FileNotFoundError):
        ManualUrlsProvider(config).load_file("does-not-exist.jsonl")


def _fixture_path(*parts: str):
    from pathlib import Path

    return Path(__file__).parent / "fixtures" / Path(*parts)
