"""Resume: state survives an interruption, and re-running does not duplicate."""

from __future__ import annotations

from datetime import date

import pytest

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.database import Database
from tw_cdc_influenza_news.dates import DateSlice, Granularity
from tw_cdc_influenza_news.discovery.base import SliceQueryResult
from tw_cdc_influenza_news.models import (
    RESUMABLE_STATUSES,
    TERMINAL_STATUSES,
    DiscoveredItem,
    DiscoveryEvidence,
    UrlStatus,
)
from tw_cdc_influenza_news.orchestrator import Orchestrator

JANUARY = DateSlice(date(2024, 1, 1), date(2024, 2, 1), Granularity.MONTH)


def evidence(provider="serpapi-google-news", query_id="q_quoted_tw_flu") -> DiscoveryEvidence:
    return DiscoveryEvidence(
        provider=provider,
        query_id=query_id,
        query_text='"疾管署" "流感"',
        slice_key=JANUARY.key,
        slice_start=JANUARY.start.isoformat(),
        slice_end_exclusive=JANUARY.end.isoformat(),
        discovered_at="2024-02-01T03:00:00+00:00",
    )


def item(url: str, **kwargs) -> DiscoveredItem:
    return DiscoveredItem(url=url, evidence=evidence(**kwargs))


# --- registration is idempotent ---------------------------------------------


def test_registering_the_same_url_twice_creates_one_row(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)

    first_id, first_new = orchestrator.register_item(item("https://news.example.tw/a"))
    second_id, second_new = orchestrator.register_item(
        item("https://news.example.tw/a?utm_source=x")
    )

    assert first_id == second_id
    assert first_new is True
    assert second_new is False
    assert sum(db.status_counts().values()) == 1


def test_the_same_article_found_by_two_queries_keeps_both_evidences(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)

    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    orchestrator.register_item(item("https://news.example.tw/a", query_id="q_bare_full_flu"))
    orchestrator.register_item(item("https://news.example.tw/a", provider="google-news-rss"))

    stored = db.evidence_for(record_id)

    assert {e.query_id for e in stored} == {"q_quoted_tw_flu", "q_bare_full_flu"}
    assert {e.provider for e in stored} == {"serpapi-google-news", "google-news-rss"}


def test_re_registering_the_same_evidence_does_not_multiply_it(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)

    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    orchestrator.register_item(item("https://news.example.tw/a"))

    assert len(db.evidence_for(record_id)) == 1


def test_an_already_successful_url_is_not_reset_by_rediscovery(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    db.set_status(record_id, UrlStatus.SUCCESS)

    orchestrator.register_item(item("https://news.example.tw/a", query_id="q_bare_full_flu"))

    assert db.get_url(record_id).status is UrlStatus.SUCCESS


# --- state transitions are persisted immediately -----------------------------


def test_each_transition_is_visible_to_a_second_connection(
    db: Database, config: AppConfig, run_id: str
):
    """Simulates the kill: a separate connection must already see the write."""
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    db.set_status(record_id, UrlStatus.FETCHING, increment_attempts=True)

    other = Database(config.database_path)
    try:
        row = other.get_url(record_id)
        assert row is not None
        assert row.status is UrlStatus.FETCHING
        assert row.attempts == 1
    finally:
        other.close()


def test_rows_stuck_in_fetching_are_requeued_not_lost(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    db.set_status(record_id, UrlStatus.FETCHING, increment_attempts=True)

    requeued = db.reset_stuck_fetching(run_id)

    assert requeued == 1
    assert db.get_url(record_id).status is UrlStatus.QUEUED
    assert [r.record_id for r in db.pending_urls()] == [record_id]


# --- what resume picks up, and what it leaves alone --------------------------


@pytest.mark.parametrize("status", sorted(RESUMABLE_STATUSES, key=str))
def test_resumable_statuses_are_picked_up_again(
    db: Database, config: AppConfig, run_id: str, status: UrlStatus
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    db.set_status(record_id, status)

    pending = db.pending_urls(statuses=RESUMABLE_STATUSES)

    assert [r.record_id for r in pending] == [record_id]


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES, key=str))
def test_terminal_statuses_are_left_alone(
    db: Database, config: AppConfig, run_id: str, status: UrlStatus
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    db.set_status(record_id, status)

    assert db.pending_urls(statuses=RESUMABLE_STATUSES) == []


def test_a_url_that_exhausted_its_attempts_stops_being_offered(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    for _ in range(3):
        db.set_status(record_id, UrlStatus.RETRYABLE_ERROR, increment_attempts=True)

    assert db.pending_urls(max_attempts=3) == []
    assert db.pending_urls(max_attempts=4)


def test_blocked_domains_are_excluded_from_the_queue(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    orchestrator.register_item(item("https://blocked.example.tw/a"))
    keep_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))

    pending = db.pending_urls(exclude_domains=["blocked.example.tw"])

    assert [r.record_id for r in pending] == [keep_id]


# --- forced re-fetch ---------------------------------------------------------


def test_requeue_brings_a_successful_url_back_and_clears_its_error(
    db: Database, config: AppConfig, run_id: str
):
    orchestrator = Orchestrator(config, db, run_id=run_id)
    record_id, _ = orchestrator.register_item(item("https://news.example.tw/a"))
    db.set_status(
        record_id, UrlStatus.PERMANENT_ERROR, error_code="client_error", increment_attempts=True
    )

    assert db.requeue([record_id]) == 1

    row = db.get_url(record_id)
    assert row.status is UrlStatus.QUEUED
    assert row.attempts == 0
    assert row.error_code is None


def test_requeue_by_date_range_is_half_open(
    db: Database, config: AppConfig, run_id: str
):
    from .test_schema import make_article

    orchestrator = Orchestrator(config, db, run_id=run_id)
    inside, _ = orchestrator.register_item(item("https://news.example.tw/inside"))
    outside, _ = orchestrator.register_item(item("https://news.example.tw/outside"))

    db.save_article(make_article(record_id=inside, published_date="2024-01-31"), run_id)
    db.save_article(make_article(record_id=outside, published_date="2024-02-01"), run_id)
    db.set_status(inside, UrlStatus.SUCCESS)
    db.set_status(outside, UrlStatus.SUCCESS)

    requeued = db.requeue_date_range("2024-01-01", "2024-02-01")

    assert requeued == 1
    assert db.get_url(inside).status is UrlStatus.QUEUED
    assert db.get_url(outside).status is UrlStatus.SUCCESS


# --- discovery slices are not redone -----------------------------------------


def test_completed_slices_are_recorded_and_skipped_on_resume(
    db: Database, config: AppConfig, run_id: str
):
    db.record_slice(
        run_id,
        {
            "provider": "serpapi-google-news",
            "query_id": "q_quoted_tw_flu",
            "query_text": '"疾管署" "流感"',
            "slice_key": JANUARY.key,
            "slice_start": JANUARY.start.isoformat(),
            "slice_end_exclusive": JANUARY.end.isoformat(),
            "granularity": "month",
            "month_key": "2024-01",
            "result_count": 12,
            "completed": True,
        },
    )

    assert ("serpapi-google-news", "q_quoted_tw_flu", JANUARY.key) in db.completed_slice_keys(
        run_id
    )


def test_an_incomplete_slice_is_not_treated_as_done(
    db: Database, config: AppConfig, run_id: str
):
    db.record_slice(
        run_id,
        {
            "provider": "serpapi-google-news",
            "query_id": "q_quoted_tw_flu",
            "query_text": "x",
            "slice_key": JANUARY.key,
            "slice_start": JANUARY.start.isoformat(),
            "slice_end_exclusive": JANUARY.end.isoformat(),
            "granularity": "month",
            "month_key": "2024-01",
            "completed": False,
            "error_code": "timeout",
        },
    )

    assert db.completed_slice_keys(run_id) == set()


def test_re_recording_a_slice_updates_rather_than_duplicates(
    db: Database, config: AppConfig, run_id: str
):
    payload = {
        "provider": "serpapi-google-news",
        "query_id": "q_quoted_tw_flu",
        "query_text": "x",
        "slice_key": JANUARY.key,
        "slice_start": JANUARY.start.isoformat(),
        "slice_end_exclusive": JANUARY.end.isoformat(),
        "granularity": "month",
        "month_key": "2024-01",
        "result_count": 3,
        "completed": False,
    }
    db.record_slice(run_id, payload)
    db.record_slice(run_id, {**payload, "result_count": 9, "completed": True})

    rows = db.slices_for_run(run_id)

    assert len(rows) == 1
    assert rows[0]["result_count"] == 9
    assert rows[0]["completed"] == 1


# --- truncation drives splitting ---------------------------------------------


class _StubProvider:
    """Returns a ceiling-length result set for the month, then small ones."""

    name = "stub"
    best_effort = False

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self.seen: list[str] = []

    async def search(self, query, date_slice, *, run_id) -> SliceQueryResult:
        self.seen.append(date_slice.key)
        limited = date_slice.granularity is Granularity.MONTH
        return SliceQueryResult(
            items=[],
            page_count=1,
            result_limit_reached=limited,
            completed=True,
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_truncated_month_is_split_into_weeks(
    db: Database, config: AppConfig, run_id: str
):
    provider = _StubProvider(config.slicing.truncation_threshold)
    orchestrator = Orchestrator(config, db, run_id=run_id)

    await orchestrator.discover_with_provider(
        provider, start_date=date(2024, 1, 1), end_date_inclusive=date(2024, 1, 31)
    )

    granularities = [key.split(":")[0] for key in provider.seen]
    assert granularities.count("month") == len(config.queries)
    assert "week" in granularities
    assert orchestrator.stats.slices_split == len(config.queries)
