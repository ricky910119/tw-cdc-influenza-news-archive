"""Access restrictions: detect, stop, record. Never work around.

These tests assert the *absence* of circumvention as much as the presence of
detection: a block must not be retried, and the run must carry the reason
into the coverage report instead of quietly producing a smaller archive.
"""

from __future__ import annotations

from datetime import date

import pytest

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.database import Database
from tw_cdc_influenza_news.dates import DateSlice, Granularity
from tw_cdc_influenza_news.discovery.google_news_rss import detect_block
from tw_cdc_influenza_news.discovery.serpapi_google_news import SerpApiProvider
from tw_cdc_influenza_news.errors import (
    BLOCKED_CODES,
    RETRYABLE_CODES,
    ErrorCode,
    is_blocked,
    is_domain_level_block,
    is_retryable,
)
from tw_cdc_influenza_news.fetching import classify_response, parse_retry_after
from tw_cdc_influenza_news.ratelimit import BlockRegistry, backoff_delay
from tw_cdc_influenza_news.reporting import build_coverage_report

from .conftest import read_fixture

JANUARY = DateSlice(date(2024, 1, 1), date(2024, 2, 1), Granularity.MONTH)


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, ErrorCode.HTTP_403),
        (429, ErrorCode.HTTP_429),
        (401, ErrorCode.LOGIN_REQUIRED),
        (451, ErrorCode.ACCESS_DENIED),
        (404, ErrorCode.NOT_FOUND),
        (503, ErrorCode.SERVER_ERROR),
        (200, None),
    ],
)
def test_status_codes_map_to_the_right_error(status: int, expected: ErrorCode | None):
    assert classify_response(status, "<html><p>ok</p></html>", "text/html") is expected


def test_a_200_carrying_a_captcha_is_still_a_block():
    page = read_fixture("html", "captcha_interstitial.html")

    code = classify_response(200, page, "text/html")

    assert code in (ErrorCode.CAPTCHA, ErrorCode.UNUSUAL_TRAFFIC)
    assert is_blocked(code)


def test_both_detection_paths_share_one_marker_list():
    """Two copies of this list would let the browser path drift behind the
    HTTP path, which is exactly how a rendered CAPTCHA gets stored as news."""
    from tw_cdc_influenza_news.errors import INTERSTITIAL_MARKERS
    from tw_cdc_influenza_news.fetching.http_client import BODY_BLOCK_MARKERS

    assert BODY_BLOCK_MARKERS is INTERSTITIAL_MARKERS


def test_a_pdf_is_rejected_as_unsupported_not_as_a_block():
    code = classify_response(200, "%PDF-1.7", "application/pdf")

    assert code is ErrorCode.UNSUPPORTED_CONTENT_TYPE
    assert not is_blocked(code)


def test_blocks_are_never_classified_as_retryable():
    assert BLOCKED_CODES.isdisjoint(RETRYABLE_CODES)
    for code in BLOCKED_CODES:
        assert is_retryable(code) is False


def test_paywall_and_login_do_not_stop_the_whole_domain():
    """One article behind a paywall says nothing about the rest of the site."""
    assert is_blocked(ErrorCode.PAYWALL)
    assert is_domain_level_block(ErrorCode.PAYWALL) is False
    assert is_domain_level_block(ErrorCode.LOGIN_REQUIRED) is False


def test_403_429_captcha_and_robots_do_stop_the_domain():
    for code in (
        ErrorCode.HTTP_403,
        ErrorCode.HTTP_429,
        ErrorCode.CAPTCHA,
        ErrorCode.ROBOTS_DISALLOWED,
    ):
        assert is_domain_level_block(code) is True


# --- retry-after -------------------------------------------------------------


def test_retry_after_seconds_is_honoured_as_given():
    assert parse_retry_after("120") == 120.0


def test_retry_after_http_date_is_parsed_not_guessed():
    delay = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")

    assert delay is not None and delay > 0


def test_a_missing_or_nonsense_retry_after_is_none():
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None


def test_backoff_grows_and_is_capped():
    base, cap = 2.0, 10.0
    delays = [backoff_delay(n, base=base, maximum=cap, jitter=0.0) for n in range(1, 6)]

    assert delays[:3] == [2.0, 4.0, 8.0]
    assert all(d <= cap for d in delays)
    assert delays == sorted(delays)


# --- the registry ------------------------------------------------------------


def test_a_blocked_domain_stays_blocked_for_the_run():
    registry = BlockRegistry()

    registry.block_domain("news.example.tw", ErrorCode.HTTP_429, "rate limited")

    assert registry.is_domain_blocked("news.example.tw")
    assert registry.domain_reason("news.example.tw").error_code is ErrorCode.HTTP_429


def test_the_first_reason_for_a_block_is_the_one_kept():
    registry = BlockRegistry()
    registry.block_domain("x.example", ErrorCode.HTTP_429, "first")
    registry.block_domain("x.example", ErrorCode.HTTP_403, "second")

    assert registry.domain_reason("x.example").detail == "first"
    assert len(registry.events) == 2  # both are still recorded as events


def test_a_stopped_provider_is_reported():
    registry = BlockRegistry()
    registry.stop_provider(
        "serpapi-google-news", ErrorCode.PROVIDER_QUOTA_EXHAUSTED, "out of searches"
    )

    report = registry.as_report()

    assert registry.is_provider_stopped("serpapi-google-news")
    assert report["stopped_providers"][0]["error_code"] == "provider_quota_exhausted"


# --- rss block detection -----------------------------------------------------


def test_rss_consent_redirect_is_a_block():
    result = detect_block(
        status_code=200, final_url="https://consent.google.com/m?continue=x", body="<html>"
    )

    assert result is not None and result[0] is ErrorCode.ACCESS_DENIED


def test_rss_html_instead_of_xml_is_a_block():
    result = detect_block(
        status_code=200, final_url="https://news.google.com/rss/search", body="<!doctype html><html>"
    )

    assert result is not None


def test_rss_429_is_a_block():
    result = detect_block(status_code=429, final_url="x", body="")

    assert result is not None and result[0] is ErrorCode.HTTP_429


def test_a_real_feed_is_not_flagged():
    xml = read_fixture("providers", "google_news_rss_sample.xml")

    assert detect_block(status_code=200, final_url="https://news.google.com/rss", body=xml) is None


# --- serpapi quota fails closed ----------------------------------------------


def test_quota_exhaustion_is_fatal_not_an_empty_result():
    payload = {"error": "Your account has run out of searches."}

    assert SerpApiProvider.classify_error(200, payload) is ErrorCode.PROVIDER_QUOTA_EXHAUSTED


def test_a_bad_api_key_is_an_auth_error():
    assert (
        SerpApiProvider.classify_error(401, {"error": "Invalid API key"})
        is ErrorCode.PROVIDER_AUTH_ERROR
    )


def test_a_good_response_has_no_error():
    assert SerpApiProvider.classify_error(200, {"news_results": []}) is None


# --- the report tells the truth about the gap ---------------------------------


def test_a_blocked_month_is_reported_incomplete(
    db: Database, config: AppConfig, run_id: str
):
    db.record_slice(
        run_id,
        {
            "provider": "serpapi-google-news",
            "query_id": "q_quoted_tw_flu",
            "query_text": "x",
            "slice_key": JANUARY.key,
            "slice_start": "2024-01-01",
            "slice_end_exclusive": "2024-02-01",
            "granularity": "month",
            "month_key": "2024-01",
            "completed": False,
            "error_code": "http_429",
            "error_message": "rate limited",
        },
    )
    db.record_block(
        run_id,
        scope="domain",
        target="news.example.tw",
        error_code="http_429",
        detail="rate limited",
    )

    report = build_coverage_report(
        db,
        config,
        run_id=run_id,
        requested_start=date(2024, 1, 1),
        requested_end=date(2024, 1, 31),
        effective_end=date(2024, 1, 31),
        providers=["serpapi-google-news"],
    )

    january = next(m for m in report.months if m.month == "2024-01")
    assert january.complete is False
    assert any("http_429" in reason for reason in january.reasons)
    assert report.blocked_domains == ["news.example.tw"]


def test_a_day_slice_at_the_ceiling_is_flagged_as_unresolvable(
    db: Database, config: AppConfig, run_id: str
):
    db.record_slice(
        run_id,
        {
            "provider": "serpapi-google-news",
            "query_id": "q_quoted_tw_flu",
            "query_text": "x",
            "slice_key": "day:2024-01-15:2024-01-16",
            "slice_start": "2024-01-15",
            "slice_end_exclusive": "2024-01-16",
            "granularity": "day",
            "month_key": "2024-01",
            "result_count": 100,
            "result_limit_reached": True,
            "completed": True,
        },
    )

    report = build_coverage_report(
        db,
        config,
        run_id=run_id,
        requested_start=date(2024, 1, 1),
        requested_end=date(2024, 1, 31),
        effective_end=date(2024, 1, 31),
        providers=["serpapi-google-news"],
    )

    january = next(m for m in report.months if m.month == "2024-01")
    assert january.complete is False
    assert any("cannot be split further" in r for r in january.reasons)


# --- no circumvention surface exists ------------------------------------------


def test_no_captcha_solving_or_stealth_machinery_is_shipped():
    """A grep-style guard: these names must never appear in the package."""
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "tw_cdc_influenza_news"
    banned = (
        "undetected_chromedriver",
        "undetected-chromedriver",
        "playwright_stealth",
        "playwright-stealth",
        "2captcha",
        "anticaptcha",
        "anti-captcha",
        "capsolver",
        "solve_captcha",
        "rotate_proxy",
        "proxy_pool",
        "residential_proxy",
        "user_data_dir",
    )
    offenders: list[str] = []
    for path in package.rglob("*.py"):
        lowered = path.read_text(encoding="utf-8").lower()
        offenders += [f"{path.name}: {name}" for name in banned if name in lowered]

    assert offenders == []


def test_the_user_agent_does_not_impersonate_anyone(config: AppConfig):
    agent = config.project.user_agent().lower()

    assert "tw-cdc-influenza-news-archive" in agent
    for impersonation in ("googlebot", "bingbot", "mozilla/5.0", "chrome/", "safari/"):
        assert impersonation not in agent
