"""The CDC press-release provider, driven from a saved copy of the real page.

The fixtures in ``tests/fixtures/html/cdc_bulletin_*.html`` are trimmed but
otherwise untouched markup captured from cdc.gov.tw on 2026-09-18, so the
selectors here are tested against what the site actually serves rather than
against markup invented to match the parser.
"""

from __future__ import annotations

from datetime import date

import pytest

from tw_cdc_influenza_news.config import AppConfig, QueryConfig
from tw_cdc_influenza_news.dates import DateSlice, Granularity
from tw_cdc_influenza_news.discovery import build_provider, enabled_provider_names
from tw_cdc_influenza_news.discovery.cdc_bulletin import (
    NO_KEYWORD_WARNING,
    CdcBulletinProvider,
    has_next_page,
    parse_listing,
)
from tw_cdc_influenza_news.extraction import extract_article

from .conftest import read_fixture

MARCH_2023 = DateSlice(date(2023, 3, 1), date(2023, 4, 1), Granularity.MONTH)
LEAP_FEB = DateSlice(date(2024, 2, 1), date(2024, 3, 1), Granularity.MONTH)
BASE = "https://www.cdc.gov.tw/Bulletin/List/MmgtpeidAR5Ooai4-fgHzQ"


@pytest.fixture
def cdc_config(config: AppConfig) -> AppConfig:
    config.providers.cdc_bulletin.enabled = True
    config.queries = [
        QueryConfig(id="q_bare_tw_flu", text="疾管署 流感", site_keyword="流感"),
        QueryConfig(id="q_quoted_tw_flu", text='"疾管署" "流感"', site_keyword="流感"),
        QueryConfig(id="q_no_site_keyword", text='"疾管署" influenza'),
    ]
    return config


# --- parsing the real listing ------------------------------------------------


def test_listing_rows_carry_url_title_and_date():
    rows = parse_listing(read_fixture("html", "cdc_bulletin_list.html"), base_url=BASE)

    assert len(rows) == 4
    assert rows[0]["date"] == "2023-03-28"
    assert rows[0]["url"].startswith("https://www.cdc.gov.tw/Bulletin/Detail/")
    assert rows[0]["title"]
    assert [r["date"] for r in rows] == [
        "2023-03-28",
        "2023-03-24",
        "2023-03-21",
        "2023-03-17",
    ]


def test_relative_hrefs_become_absolute():
    rows = parse_listing(read_fixture("html", "cdc_bulletin_list.html"), base_url=BASE)

    assert all(r["url"].startswith("https://www.cdc.gov.tw/") for r in rows)


def test_the_untruncated_headline_comes_from_the_title_attribute():
    """The visible text is clipped by a JS ellipsis helper; the attribute is not."""
    rows = parse_listing(read_fixture("html", "cdc_bulletin_list.html"), base_url=BASE)

    assert all(len(r["title"]) > 12 for r in rows)
    assert not any(r["title"].endswith("...") for r in rows)


def test_a_listing_with_no_rows_parses_to_nothing():
    assert parse_listing("<html><body><p>查無資料</p></body></html>", base_url=BASE) == []


def test_pagination_is_read_from_the_pager():
    html = read_fixture("html", "cdc_bulletin_list.html")

    assert has_next_page(html, 1) is True
    assert has_next_page(html, 2) is False


@pytest.mark.parametrize(
    "href",
    [
        # The live site's ordering today...
        '<a href="/Bulletin/List/X?page=2&amp;keyword=%E6%B5%81%E6%84%9F">2</a>',
        # ...and the ordering it is free to switch to tomorrow.
        '<a href="/Bulletin/List/X?keyword=%E6%B5%81%E6%84%9F&amp;page=2">2</a>',
        '<a href="/Bulletin/List/X?keyword=x&page=2">2</a>',
    ],
)
def test_the_pager_is_found_whatever_the_parameter_order(href: str):
    """A missed pager link does not raise -- it silently stops at page one and
    returns fewer results, so the detection must not depend on the site
    keeping `page` first or on `&` being unescaped."""
    assert has_next_page(f"<div class='pagination'>{href}</div>", 1) is True


def test_a_page_number_is_not_confused_with_a_longer_one():
    html = '<a href="/Bulletin/List/X?page=20">20</a>'

    assert has_next_page(html, 1) is False  # page=2 must not match page=20


# --- date conversion ----------------------------------------------------------


def test_the_site_range_is_inclusive_so_the_upper_bound_is_last_day(cdc_config: AppConfig):
    """endTime includes its own date. Passing the exclusive end would pull in
    the first day of the next slice and double-count it."""
    params = CdcBulletinProvider(cdc_config).date_params(MARCH_2023)

    assert params == {"startTime": "2023.03.01", "endTime": "2023.03.31"}


def test_date_conversion_handles_the_leap_day(cdc_config: AppConfig):
    params = CdcBulletinProvider(cdc_config).date_params(LEAP_FEB)

    assert params["endTime"] == "2024.02.29"


def test_this_adapter_and_the_rss_adapter_convert_differently(cdc_config: AppConfig):
    """Proof that each adapter owns its conversion: the CDC form is inclusive,
    Google's before: operator is exclusive, for the same slice."""
    from tw_cdc_influenza_news.discovery.google_news_rss import GoogleNewsRssProvider

    cdc = CdcBulletinProvider(cdc_config).date_params(MARCH_2023)
    rss = GoogleNewsRssProvider(cdc_config).build_query_string(
        cdc_config.queries[0], MARCH_2023
    )

    assert cdc["endTime"] == "2023.03.31"
    assert "before:2023-04-01" in rss


# --- request building ------------------------------------------------------------


def test_the_keyword_sent_is_the_site_keyword_not_the_search_syntax(cdc_config: AppConfig):
    """`"疾管署" "流感"` matches nothing on a plain substring search."""
    provider = CdcBulletinProvider(cdc_config)
    quoted_query = cdc_config.queries[1]

    params = provider.build_params(quoted_query.site_keyword or "", MARCH_2023)

    assert params["keyword"] == "流感"
    assert '"' not in params["keyword"]


def test_the_list_url_is_built_from_base_and_category(cdc_config: AppConfig):
    assert CdcBulletinProvider(cdc_config).list_url == BASE


def test_page_size_and_page_are_sent(cdc_config: AppConfig):
    params = CdcBulletinProvider(cdc_config).build_params("流感", MARCH_2023, page=3)

    assert params["PageSize"] == "100"
    assert params["page"] == "3"


# --- queries that cannot be asked of this site -------------------------------------


@pytest.mark.asyncio
async def test_a_query_without_a_site_keyword_is_skipped_and_explained(
    cdc_config: AppConfig,
):
    """Silently stripping the quotes would change what was asked without
    saying so. The gap is recorded instead."""
    provider = CdcBulletinProvider(cdc_config)
    without_keyword = cdc_config.queries[2]

    outcome = await provider.search(without_keyword, MARCH_2023, run_id="run-test")

    assert outcome.items == []
    assert outcome.completed is True
    assert outcome.error_message == NO_KEYWORD_WARNING
    assert outcome.error_code is None  # not a failure, a documented skip


@pytest.mark.asyncio
async def test_two_queries_sharing_a_keyword_cost_one_request(cdc_config: AppConfig):
    """Four configured queries collapse to two site keywords; asking the same
    question twice would only rediscover the same articles."""
    provider = CdcBulletinProvider(cdc_config)
    first, second = cdc_config.queries[0], cdc_config.queries[1]

    provider._seen_requests.add((first.site_keyword or "", MARCH_2023.key))
    outcome = await provider.search(second, MARCH_2023, run_id="run-test")

    assert outcome.items == []
    assert outcome.completed is True


# --- preflight -------------------------------------------------------------------


def test_preflight_passes_when_enabled_with_a_site_keyword(cdc_config: AppConfig):
    assert CdcBulletinProvider(cdc_config).preflight() == []


def test_preflight_complains_when_no_query_can_be_asked(cdc_config: AppConfig):
    for query in cdc_config.queries:
        query.site_keyword = None

    problems = CdcBulletinProvider(cdc_config).preflight()

    assert any("site_keyword" in p for p in problems)


def test_preflight_needs_no_api_key(cdc_config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    assert CdcBulletinProvider(cdc_config).preflight() == []


# --- registry -------------------------------------------------------------------


def test_the_provider_is_registered_and_searchable(cdc_config: AppConfig):
    provider = build_provider("cdc-bulletin", cdc_config)

    assert provider.name == "cdc-bulletin"
    assert provider.best_effort is False  # the publisher's own index
    assert "cdc-bulletin" in enabled_provider_names(cdc_config)


# --- the detail page ----------------------------------------------------------------


def test_the_detail_page_yields_a_body_and_a_date():
    """CDC detail pages have no JSON-LD, no <time> and no <article>, so the
    date has to come from the labelled page text."""
    result = extract_article(
        read_fixture("html", "cdc_bulletin_detail.html"),
        "https://www.cdc.gov.tw/Bulletin/Detail/CqW5UAMFqpDkwJWfWG-WGw?typeid=9",
        min_content_chars=200,
    )

    assert result.published_date == "2026-09-15"
    assert result.content and "疾病管制署" in result.content
    assert result.title


def test_the_listing_date_is_available_as_a_fallback(cdc_config: AppConfig):
    """When the detail page's own date cannot be read, the date the publisher
    printed in its index is still on the record."""
    rows = parse_listing(read_fixture("html", "cdc_bulletin_list.html"), base_url=BASE)
    provider = CdcBulletinProvider(cdc_config)

    item = provider._to_item(
        rows[0],
        query=cdc_config.queries[0],
        keyword="流感",
        date_slice=MARCH_2023,
        position=1,
        raw_path=None,
    )

    assert item.published_at_raw == "2023-03-28"
    assert item.evidence.provider == "cdc-bulletin"
    assert item.evidence.provider_source_name == "衛生福利部疾病管制署"
    assert item.source_name == "衛生福利部疾病管制署"
