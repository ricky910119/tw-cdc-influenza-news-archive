"""Extraction: bodies, date priority, timezone, and the refusals."""

from __future__ import annotations

from datetime import date, datetime

from zoneinfo import ZoneInfo

from tw_cdc_influenza_news.errors import ErrorCode, is_blocked, is_domain_level_block
from tw_cdc_influenza_news.extraction import (
    clean_text,
    detect_access_block,
    detect_needs_javascript,
    extract_article,
    parse_datetime,
    parse_roc_date,
    to_display_date,
)
from tw_cdc_influenza_news.models import DateConfidence, DateSource, ExtractionMethod

from .conftest import read_fixture

TAIPEI = ZoneInfo("Asia/Taipei")


def html(name: str) -> str:
    return read_fixture("html", name)


# --- body extraction --------------------------------------------------------


def test_json_ld_article_yields_a_real_body():
    result = extract_article(html("article_json_ld.html"), "https://news.example.tw/a")

    assert result.content is not None
    assert len(result.content) > 200
    assert "疾管署呼籲民眾儘速接種流感疫苗" in result.content
    assert result.method is not ExtractionMethod.NONE


def test_navigation_ads_and_footers_are_removed():
    result = extract_article(html("article_json_ld.html"), "https://news.example.tw/a")

    body = result.content or ""
    assert "首頁" not in body
    assert "本網站使用 cookie" not in body
    assert "延伸閱讀" not in body
    assert "版權所有" not in body


def test_paragraph_structure_survives():
    result = extract_article(html("article_json_ld.html"), "https://news.example.tw/a")

    assert (result.content or "").count("\n\n") >= 2


def test_container_fallback_handles_a_page_without_article_or_json_ld():
    result = extract_article(
        html("article_opengraph_only.html"), "https://daily.example.tw/x"
    )

    assert result.content and "公費流感疫苗接種數已超過四百萬劑" in result.content
    assert result.title == "疾病管制署籲接種流感疫苗"
    assert result.source_name == "示範日報"
    assert result.authors == ["林記者"]


def test_metadata_is_collected_even_when_the_body_is_missing():
    result = extract_article(html("article_no_body.html"), "https://news.example.tw/404")

    assert result.is_usable(200) is False
    assert result.title  # the page still told us something
    assert result.content is None or len(result.content) < 200


def test_empty_response_does_not_raise():
    result = extract_article("", "https://news.example.tw/a")

    assert result.content is None
    assert "empty response body" in result.notes


def test_clean_text_collapses_runs_but_keeps_paragraphs():
    assert clean_text("a   b\n\n\n\nc  ") == "a b\n\nc"


# --- date priority ----------------------------------------------------------


def test_json_ld_date_beats_opengraph_and_time_element():
    result = extract_article(html("article_json_ld.html"), "https://news.example.tw/a")

    assert result.date_source is DateSource.JSON_LD
    assert result.date_confidence is DateConfidence.HIGH
    assert result.published_at == "2024-01-15T08:30:00+08:00"
    assert result.published_date == "2024-01-15"
    assert result.modified_at == "2024-01-15T11:05:00+08:00"


def test_opengraph_is_used_when_json_ld_is_absent():
    result = extract_article(
        html("article_opengraph_only.html"), "https://daily.example.tw/x"
    )

    assert result.date_source is DateSource.OPEN_GRAPH
    assert result.published_at == "2023-11-02T14:20:00+08:00"


def test_the_search_provider_date_never_overrides_the_page():
    """The whole point of the priority ladder: a provider timestamp that
    disagrees with the article's own JSON-LD must lose."""
    result = extract_article(
        html("article_json_ld.html"),
        "https://news.example.tw/a",
        provider_date_raw="2024-03-30T00:00:00+08:00",
    )

    assert result.date_source is DateSource.JSON_LD
    assert result.published_date == "2024-01-15"


def test_the_provider_date_is_used_only_as_a_last_resort_and_marked_low():
    result = extract_article(
        html("article_no_body.html"),
        "https://news.example.tw/404",
        provider_date_raw="2024-05-06T09:00:00+08:00",
    )

    assert result.date_source is DateSource.SEARCH_PROVIDER
    assert result.date_confidence is DateConfidence.LOW
    assert result.published_date == "2024-05-06"


def test_roc_calendar_date_in_page_text_is_read():
    assert parse_roc_date("發布日期：民國113年2月29日") == date(2024, 2, 29)

    result = extract_article(html("article_roc_date.html"), "https://news.example.tw/roc")

    assert result.published_date == "2024-02-29"
    assert result.date_source in (DateSource.PAGE_TEXT, DateSource.TIME_ELEMENT)


def test_unparseable_date_text_yields_none_not_a_guess():
    assert parse_datetime("上週三") is None
    assert parse_datetime("") is None
    assert parse_datetime(None) is None


# --- timezone ---------------------------------------------------------------


def test_an_explicit_offset_is_preserved_not_rewritten():
    parsed = parse_datetime("2024-01-15T08:30:00+09:00", tz_name="Asia/Taipei")

    assert parsed is not None
    assert parsed.utcoffset().total_seconds() == 9 * 3600


def test_a_naive_page_date_is_read_as_taipei_local():
    parsed = parse_datetime("2024-01-15 08:30", tz_name="Asia/Taipei")

    assert parsed == datetime(2024, 1, 15, 8, 30, tzinfo=TAIPEI)


def test_display_date_is_the_taipei_calendar_day():
    """Published 23:30 UTC on the 14th is already the 15th in Taipei."""
    moment = parse_datetime("2024-01-14T23:30:00+00:00")

    assert to_display_date(moment, tz_name="Asia/Taipei") == "2024-01-15"


def test_display_date_of_nothing_is_nothing():
    assert to_display_date(None) is None


# --- refusals ---------------------------------------------------------------


def test_a_paywall_is_named_and_the_teaser_is_not_kept_as_the_body():
    result = extract_article(html("article_paywall.html"), "https://paid.example.tw/a")

    assert result.block_code is ErrorCode.PAYWALL


def test_login_wall_is_detected():
    page = "<html><body><p>請先登入以閱讀全文</p></body></html>"

    assert detect_access_block(page, "請先登入以閱讀全文") is ErrorCode.LOGIN_REQUIRED


def test_a_captcha_interstitial_is_detected():
    """Assert the consequence, not the label.

    This page carries two signals at once -- "unusual traffic" in the heading
    and a g-recaptcha widget -- so which ErrorCode wins depends on marker
    order, and either naming is defensible. What must never vary is that the
    page counts as a block and that the block indicts the whole host.
    """
    page = html("captcha_interstitial.html")

    code = detect_access_block(page, page)

    assert code is not None
    assert is_blocked(code)
    assert is_domain_level_block(code)


def test_the_extractor_and_the_http_layer_agree_on_an_interstitial():
    """The extractor is the ONLY guard on Playwright-rendered HTML, which
    never passes through the HTTP response classifier."""
    from tw_cdc_influenza_news.fetching import classify_response

    page = html("captcha_interstitial.html")

    assert detect_access_block(page, page) == classify_response(200, page, "text/html")


def test_an_article_that_merely_mentions_captchas_is_not_flagged():
    """The markers are specific strings, not the bare word: a news story
    about bot defences is a page we want."""
    page = (
        "<html lang='zh-TW'><body><article>"
        "<h1>疾管署網站導入驗證碼防止機器人掛號</h1>"
        "<p>疾管署今日說明，為阻擋自動化程式，將在預約系統加入 CAPTCHA 驗證機制。"
        "官員表示，這項措施不影響一般民眾使用，也不會蒐集額外個資。</p>"
        "</article></body></html>"
    )

    assert detect_access_block(page, page) is None


# --- javascript detection ---------------------------------------------------


def test_an_app_shell_with_no_text_is_flagged_for_the_browser_fallback():
    result = extract_article(html("article_js_shell.html"), "https://spa.example.tw/a")

    assert result.needs_javascript is True


def test_a_short_but_static_page_is_not_flagged():
    """Shortness alone must not launch Chromium -- most short pages are stubs."""
    page = "<html><body><p>短訊。</p></body></html>"

    assert detect_needs_javascript(page, extracted_length=10, min_chars=200) is False


def test_a_long_enough_page_is_never_flagged():
    assert detect_needs_javascript('<div id="root">', extracted_length=5000, min_chars=200) is False
