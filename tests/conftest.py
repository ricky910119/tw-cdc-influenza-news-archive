"""Shared test fixtures.

Every test in this suite is offline. Nothing here opens a socket, launches a
browser, or reads a real API key: the providers are exercised through saved
response fixtures, and the fetcher through injected fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tw_cdc_influenza_news.config import AppConfig, QueryConfig
from tw_cdc_influenza_news.database import Database

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """A config rooted in a temp directory, with no secrets in the environment."""
    for name in ("SERPAPI_API_KEY", "CRAWLER_CONTACT_EMAIL", "NEWS_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRAWLER_CONTACT_EMAIL", "tests@example.invalid")

    cfg = AppConfig(
        queries=[
            QueryConfig(id="q_quoted_tw_flu", text='"疾管署" "流感"'),
            QueryConfig(id="q_bare_full_flu", text="疾病管制署 流感"),
        ],
        keywords=["疾管署", "疾病管制署", "流感", "influenza"],
    )
    cfg.root = tmp_path
    cfg.source_path = tmp_path / "configs" / "default.yaml"
    cfg.source_sha256 = "0" * 64
    cfg.ensure_directories()
    return cfg


@pytest.fixture
def db(config: AppConfig) -> Database:
    database = Database(config.database_path)
    database.initialize()
    yield database
    database.close()


@pytest.fixture
def run_id(db: Database, config: AppConfig) -> str:
    rid = "run-test-000001"
    db.create_run(
        rid,
        tool_version="0.0.0-test",
        schema_version=1,
        config_path=str(config.source_path),
        config_sha256=config.source_sha256,
        command="pytest",
        providers=["serpapi-google-news"],
        requested_start="2024-01-01",
        requested_end="2024-01-31",
        effective_end="2024-01-31",
        timezone="Asia/Taipei",
    )
    return rid
