"""Loading .env, and the precedence rules around it.

The bug these cover: ``run.ps1`` loaded ``.env`` but ``python -m
tw_cdc_influenza_news doctor`` did not, so the documented install flow
("fill in .env, then run doctor") reported the contact address as missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tw_cdc_influenza_news.config import (
    AppConfig,
    QueryConfig,
    load_config,
    load_env_file,
)

MINIMAL_CONFIG = """
schema_version: 1
queries:
  - id: q1
    text: '"疾管署" "流感"'
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("SERPAPI_API_KEY", "CRAWLER_CONTACT_EMAIL", "NEWS_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "default.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return tmp_path


def write_env(root: Path, body: str) -> None:
    (root / ".env").write_text(body, encoding="utf-8")


# --- the reported failure -----------------------------------------------------


def test_loading_the_config_picks_up_the_contact_email_from_dotenv(repo: Path):
    write_env(repo, "CRAWLER_CONTACT_EMAIL=ops@example.gov.tw\n")

    config = load_config(root=repo)

    assert config.project.contact_email() == "ops@example.gov.tw"
    assert "ops@example.gov.tw" in config.project.user_agent()


def test_a_missing_dotenv_is_not_an_error(repo: Path):
    assert load_env_file(repo) == []
    assert load_config(root=repo).project.contact_email() is None


# --- format handling ------------------------------------------------------------


def test_comments_blank_lines_and_export_prefixes_are_handled(repo: Path):
    write_env(
        repo,
        "\n".join(
            [
                "# a comment",
                "",
                "   ",
                "export SERPAPI_API_KEY=sk-from-dotenv",
                "CRAWLER_CONTACT_EMAIL = ops@example.gov.tw ",
                "NOT_AN_ASSIGNMENT",
            ]
        ),
    )

    loaded = load_env_file(repo)

    assert set(loaded) == {"SERPAPI_API_KEY", "CRAWLER_CONTACT_EMAIL"}
    import os

    assert os.environ["SERPAPI_API_KEY"] == "sk-from-dotenv"
    assert os.environ["CRAWLER_CONTACT_EMAIL"] == "ops@example.gov.tw"


@pytest.mark.parametrize("quoted", ['"ops@example.gov.tw"', "'ops@example.gov.tw'"])
def test_surrounding_quotes_are_stripped(repo: Path, quoted: str):
    write_env(repo, f"CRAWLER_CONTACT_EMAIL={quoted}\n")

    load_env_file(repo)

    import os

    assert os.environ["CRAWLER_CONTACT_EMAIL"] == "ops@example.gov.tw"


def test_an_empty_placeholder_stays_empty(repo: Path):
    """.env.example ships `SERPAPI_API_KEY=`; that must read as 'not set'."""
    write_env(repo, "SERPAPI_API_KEY=\n")

    config = load_config(root=repo)

    assert config.providers.serpapi_google_news.has_api_key() is False


# --- precedence -------------------------------------------------------------------


def test_the_real_environment_wins_over_dotenv(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """A scheduled task or CI job that sets a variable explicitly must not be
    overridden by a stale file on disk."""
    monkeypatch.setenv("CRAWLER_CONTACT_EMAIL", "from-the-shell@example.gov.tw")
    write_env(repo, "CRAWLER_CONTACT_EMAIL=from-the-file@example.gov.tw\n")

    load_env_file(repo)

    import os

    assert os.environ["CRAWLER_CONTACT_EMAIL"] == "from-the-shell@example.gov.tw"


def test_news_config_path_from_dotenv_selects_the_config_file(repo: Path):
    """.env is read BEFORE the config path is resolved, because the path can
    itself come from .env."""
    alternative = repo / "configs" / "other.yaml"
    alternative.write_text(
        MINIMAL_CONFIG.replace("q1", "q_alternative"), encoding="utf-8"
    )
    write_env(repo, "NEWS_CONFIG_PATH=configs/other.yaml\n")

    config = load_config(root=repo)

    assert config.source_path == alternative
    assert [q.id for q in config.queries] == ["q_alternative"]


# --- it never leaks ------------------------------------------------------------------


def test_load_env_file_returns_names_only_never_values(repo: Path):
    write_env(repo, "SERPAPI_API_KEY=sk-secret-value-0123456789\n")

    loaded = load_env_file(repo)

    assert loaded == ["SERPAPI_API_KEY"]
    assert "sk-secret-value-0123456789" not in repr(loaded)


def test_a_dotenv_value_is_not_written_into_the_config_fingerprint(repo: Path):
    write_env(
        repo,
        "SERPAPI_API_KEY=sk-secret-value-0123456789\n"
        "CRAWLER_CONTACT_EMAIL=ops@example.gov.tw\n",
    )

    config = load_config(root=repo)

    import json

    assert "sk-secret-value-0123456789" not in json.dumps(config.fingerprint())


def test_an_unreadable_dotenv_does_not_crash_the_tool(repo: Path):
    (repo / ".env").write_bytes(b"\xff\xfe\x00invalid utf-8")

    assert load_env_file(repo) == []
    assert isinstance(load_config(root=repo), AppConfig)


def test_query_config_still_requires_non_empty_text():
    with pytest.raises(ValueError):
        QueryConfig(id="q", text="   ")
