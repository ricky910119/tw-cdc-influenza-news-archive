"""Output schema: required fields, JSON validity, and no NaN anywhere."""

from __future__ import annotations

import json
import math
from datetime import date

import pytest
from pydantic import ValidationError

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.database import Database
from tw_cdc_influenza_news.models import (
    COMPLETENESS_STATEMENT,
    Article,
    DateConfidence,
    DateSource,
    DiscoveryEvidence,
    ExtractionMethod,
    MonthCoverage,
    UrlStatus,
)
from tw_cdc_influenza_news.reporting import build_coverage_report, dump_json, export_run

REQUIRED_FIELDS = {
    "record_id",
    "published_at",
    "published_date",
    "published_at_raw",
    "date_source",
    "date_confidence",
    "title",
    "content",
    "source_name",
    "authors",
    "url",
    "final_url",
    "canonical_url",
    "language",
    "matched_queries",
    "matched_keywords",
    "discovery_providers",
    "first_discovered_at",
    "last_checked_at",
    "extraction_method",
    "content_sha256",
    "status",
    "error_code",
    "error_message",
}


def make_article(**overrides) -> Article:
    base = dict(
        record_id="a" * 64,
        published_at="2024-01-15T08:30:00+08:00",
        published_date="2024-01-15",
        published_at_raw="2024-01-15T08:30:00+08:00",
        date_source=DateSource.JSON_LD,
        date_confidence=DateConfidence.HIGH,
        title="疾管署：流感疫情持續升溫",
        content="疾管署今日公布最新流感監測資料。" * 20,
        source_name="範例新聞網",
        authors=["陳記者"],
        url="https://news.example.tw/a?utm_source=google",
        final_url="https://news.example.tw/a",
        canonical_url="https://news.example.tw/a",
        language="zh-TW",
        matched_queries=["q_quoted_tw_flu"],
        matched_keywords=["疾管署", "流感"],
        discovery_providers=["serpapi-google-news"],
        first_discovered_at="2024-02-01T03:00:00+00:00",
        last_checked_at="2024-02-01T03:00:05+00:00",
        extraction_method=ExtractionMethod.TRAFILATURA,
        content_sha256="b" * 64,
        status=UrlStatus.SUCCESS,
    )
    base.update(overrides)
    return Article(**base)


def test_every_contract_field_is_present():
    payload = make_article().model_dump(mode="json")

    assert REQUIRED_FIELDS <= set(payload)


def test_article_round_trips_through_json():
    article = make_article()

    restored = Article.model_validate_json(article.model_dump_json())

    assert restored == article


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        make_article(surprise_field="nope")


def test_non_finite_floats_are_refused_at_construction():
    with pytest.raises(ValidationError):
        MonthCoverage(month="2024-01", date_parse_failure_ratio=float("nan"))
    with pytest.raises(ValidationError):
        MonthCoverage(month="2024-01", content_unavailable_ratio=math.inf)


def test_dump_json_refuses_nan():
    with pytest.raises(ValueError):
        dump_json({"ratio": float("nan")})


def test_dump_json_keeps_chinese_readable():
    text = dump_json({"title": "疾管署"}, indent=None)

    assert "疾管署" in text
    assert "\\u" not in text


def test_timestamps_are_iso_8601_with_offsets():
    article = make_article()

    assert article.published_at is not None and article.published_at.endswith("+08:00")
    assert date.fromisoformat(article.published_date or "")
    assert article.first_discovered_at.endswith("+00:00")


def test_an_unparseable_date_is_null_not_guessed():
    """A date we could not read stays empty, with the raw text preserved."""
    article = make_article(
        published_at=None,
        published_date=None,
        published_at_raw="上週三",
        date_source=DateSource.UNKNOWN,
        date_confidence=DateConfidence.NONE,
    )

    payload = article.model_dump(mode="json")

    assert payload["published_at"] is None
    assert payload["published_date"] is None
    assert payload["published_at_raw"] == "上週三"
    assert payload["date_confidence"] == "none"


def test_content_unavailable_record_keeps_its_metadata():
    article = make_article(
        content=None,
        content_sha256=None,
        status=UrlStatus.CONTENT_UNAVAILABLE,
        error_code="extraction_failed",
        error_message="no article body could be extracted",
        extraction_method=ExtractionMethod.NONE,
    )
    payload = article.model_dump(mode="json")

    assert payload["content"] is None
    assert payload["url"] and payload["title"] and payload["matched_queries"]
    assert payload["error_code"] == "extraction_failed"


def test_exported_files_parse_as_strict_json(
    db: Database, config: AppConfig, run_id: str
) -> None:
    db.upsert_url(
        record_id="a" * 64,
        url="https://news.example.tw/a",
        normalized_url="https://news.example.tw/a",
        canonical_url="https://news.example.tw/a",
        domain="news.example.tw",
        run_id=run_id,
    )
    db.add_evidence(
        "a" * 64,
        DiscoveryEvidence(
            provider="serpapi-google-news",
            query_id="q_quoted_tw_flu",
            query_text='"疾管署" "流感"',
            slice_key="month:2024-01-01:2024-02-01",
            slice_start="2024-01-01",
            slice_end_exclusive="2024-02-01",
            discovered_at="2024-02-01T03:00:00+00:00",
        ),
        run_id,
    )
    db.save_article(make_article(), run_id)
    db.set_status("a" * 64, UrlStatus.SUCCESS)

    coverage = build_coverage_report(
        db,
        config,
        run_id=run_id,
        requested_start=date(2024, 1, 1),
        requested_end=date(2024, 1, 31),
        effective_end=date(2024, 1, 31),
        providers=["serpapi-google-news"],
    )
    written = export_run(db, config, run_id=run_id, coverage=coverage, coverage_markdown="# x\n")

    for name in ("articles_json", "coverage_report_json"):
        raw = open(written[name], encoding="utf-8").read()
        assert "NaN" not in raw and "Infinity" not in raw
        json.loads(raw, parse_constant=_reject_constant)

    with open(written["articles_jsonl"], encoding="utf-8") as handle:
        rows = [json.loads(line, parse_constant=_reject_constant) for line in handle if line.strip()]
    assert len(rows) == 1
    assert rows[0]["record_id"] == "a" * 64


def _reject_constant(name: str):
    raise AssertionError(f"JSON output contains the non-standard constant {name!r}")


def test_coverage_report_states_the_completeness_limit(
    db: Database, config: AppConfig, run_id: str
) -> None:
    coverage = build_coverage_report(
        db,
        config,
        run_id=run_id,
        requested_start=date(2023, 1, 1),
        requested_end=date(2026, 12, 31),
        effective_end=date(2026, 9, 17),
        providers=["serpapi-google-news"],
    )

    assert coverage.completeness_statement == COMPLETENESS_STATEMENT
    assert "not, and does not claim to be, every news article" in coverage.completeness_statement
    # Every month of the requested range has a row, attempted or not.
    assert len(coverage.months) == 48
    assert {m.month for m in coverage.months} >= {"2023-01", "2024-02", "2026-12"}
    assert all(not m.attempted for m in coverage.months)
    assert any("beyond the effective end date" in " ".join(m.reasons) for m in coverage.months)
