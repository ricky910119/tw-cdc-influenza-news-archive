"""Secrets must not reach logs, output files, or cached raw responses."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from tw_cdc_influenza_news.config import AppConfig
from tw_cdc_influenza_news.dates import DateSlice, Granularity
from tw_cdc_influenza_news.discovery.serpapi_google_news import SerpApiGoogleNewsProvider
from tw_cdc_influenza_news.logging_utils import REDACTED, SecretRedactor, configure_logging

from .conftest import read_fixture

FAKE_KEY = "sk-test-0123456789abcdef0123456789abcdef"
JANUARY = DateSlice(date(2024, 1, 1), date(2024, 2, 1), Granularity.MONTH)


# --- redaction --------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"GET https://serpapi.com/search.json?q=x&api_key={FAKE_KEY}",
        f'{{"api_key": "{FAKE_KEY}"}}',
        f"Authorization: Bearer {FAKE_KEY}",
        f"cookie=session-{FAKE_KEY}",
        f"X-API-Key: {FAKE_KEY}",
    ],
)
def test_key_value_secrets_are_redacted(line: str):
    redacted = SecretRedactor().redact(line)

    assert FAKE_KEY not in redacted
    assert REDACTED in redacted


def test_a_bare_key_value_is_redacted_when_the_env_var_is_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """A key pasted into an error string carries no parameter name with it."""
    monkeypatch.setenv("SERPAPI_API_KEY", FAKE_KEY)

    redacted = SecretRedactor().redact(f"request failed for url ...{FAKE_KEY}...")

    assert FAKE_KEY not in redacted


def test_redaction_keeps_the_rest_of_the_message_intact():
    redacted = SecretRedactor().redact(f"provider=serpapi api_key={FAKE_KEY} slice=2024-01")

    assert "provider=serpapi" in redacted
    assert "slice=2024-01" in redacted


def test_mapping_redaction_is_recursive():
    redacted = SecretRedactor().redact_mapping(
        {"engine": "google_news", "auth": {"api_key": FAKE_KEY}, "q": "疾管署"}
    )

    assert redacted["engine"] == "google_news"
    assert redacted["q"] == "疾管署"
    assert redacted["auth"]["api_key"] == REDACTED


# --- the logging pipeline ----------------------------------------------------


def test_the_api_key_never_reaches_the_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SERPAPI_API_KEY", FAKE_KEY)
    log_file = tmp_path / "logs" / "run.log"
    logger = configure_logging(level="INFO", log_file=log_file, quiet_console=True)

    logger.info("calling https://serpapi.com/search.json?api_key=%s&q=x", FAKE_KEY)
    logger.warning("payload: %s", json.dumps({"api_key": FAKE_KEY}))
    logger.error("bare leak %s", FAKE_KEY)
    for handler in logger.handlers:
        handler.flush()

    contents = log_file.read_text(encoding="utf-8")

    assert FAKE_KEY not in contents
    assert REDACTED in contents
    logging.getLogger("tw_cdc_influenza_news").handlers.clear()


def test_redaction_survives_a_broken_format_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SERPAPI_API_KEY", FAKE_KEY)
    log_file = tmp_path / "logs" / "run.log"
    logger = configure_logging(level="INFO", log_file=log_file, quiet_console=True)

    logger.info("too few args %s %s", FAKE_KEY)  # deliberately malformed
    for handler in logger.handlers:
        handler.flush()

    assert FAKE_KEY not in log_file.read_text(encoding="utf-8")
    logging.getLogger("tw_cdc_influenza_news").handlers.clear()


# --- raw response caching ------------------------------------------------------


def test_the_saved_serpapi_response_has_no_api_key(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    """SerpApi echoes the request back, key included. It must be stripped
    before anything is written to data/raw."""
    monkeypatch.setenv("SERPAPI_API_KEY", FAKE_KEY)
    payload = json.loads(read_fixture("providers", "serpapi_google_news_page1.json"))
    assert "api_key" in payload["search_parameters"]  # the fixture really has one

    provider = SerpApiGoogleNewsProvider(config)
    path = provider.save_raw(
        payload,
        run_id="run-test",
        query=config.queries[0],
        date_slice=JANUARY,
        page=1,
    )

    assert path is not None
    written = (config.root / path).read_text(encoding="utf-8")
    assert "api_key" not in written
    assert "SHOULD_NEVER_BE_WRITTEN_TO_DISK" not in written
    assert "news_results" in written  # the useful part survived


def test_the_request_params_carry_the_key_but_the_config_does_not(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SERPAPI_API_KEY", FAKE_KEY)
    provider = SerpApiGoogleNewsProvider(config)

    params = provider.build_params(config.queries[0], JANUARY)

    assert params["api_key"] == FAKE_KEY  # it has to go on the wire
    # ...but it is nowhere in anything we persist or print:
    assert FAKE_KEY not in json.dumps(config.fingerprint())
    assert FAKE_KEY not in json.dumps(provider.describe())


def test_doctor_style_reporting_says_only_whether_the_key_exists(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SERPAPI_API_KEY", FAKE_KEY)
    serp = config.providers.serpapi_google_news

    assert serp.has_api_key() is True
    problems = SerpApiGoogleNewsProvider(config).preflight()

    assert all(FAKE_KEY not in problem for problem in problems)


def test_a_missing_key_is_a_clear_message_not_a_crash(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    config.providers.serpapi_google_news.enabled = True

    problems = SerpApiGoogleNewsProvider(config).preflight()

    assert any("SERPAPI_API_KEY" in p for p in problems)


# --- the example env file --------------------------------------------------------


def test_env_example_contains_placeholders_only():
    root = Path(__file__).resolve().parents[1]
    contents = (root / ".env.example").read_text(encoding="utf-8")

    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, value = stripped.partition("=")
        assert name
        # Only the config path is allowed a real value.
        if name != "NEWS_CONFIG_PATH":
            assert value == "", f"{name} has a non-placeholder value in .env.example"


def test_gitignore_excludes_secrets_state_and_output():
    root = Path(__file__).resolve().parents[1]
    contents = (root / ".gitignore").read_text(encoding="utf-8")

    for pattern in (".env", ".venv/", "data/raw/**", "data/state/**", "data/output/**"):
        assert pattern in contents
    for pattern in ("*.sqlite3", "*.log", "cookies.json", "browser-profile/"):
        assert pattern in contents
