"""Configuration loading and validation.

Every path in the config is resolved against the repository root (or an
explicit ``NEWS_DATA_ROOT``), never against the current working directory and
never against a developer machine's absolute path. That is what makes the
checkout portable: clone it anywhere, run it from anywhere.

Secrets are read from the environment by NAME. The config stores the variable
name (``api_key_env``), never the value, so the config file is safe to commit
and safe to print.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tw_cdc_influenza_news import __version__
from tw_cdc_influenza_news.dates import DEFAULT_TIMEZONE, Granularity
from tw_cdc_influenza_news.errors import ConfigError

DEFAULT_CONFIG_RELPATH = "configs/default.yaml"
CONFIG_PATH_ENV = "NEWS_CONFIG_PATH"
DATA_ROOT_ENV = "NEWS_DATA_ROOT"


def repository_root() -> Path:
    """Best guess at the repository root.

    ``src/tw_cdc_influenza_news/config.py`` -> two levels up from ``src``.
    An installed (non-editable) copy has no repository, so ``NEWS_DATA_ROOT``
    overrides, and the cwd is the last resort.
    """
    override = os.environ.get(DATA_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    candidate = here.parents[2]
    if (candidate / "pyproject.toml").exists():
        return candidate
    return Path.cwd().resolve()


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "tw-cdc-influenza-news-archive"
    version: str = __version__
    contact_email_env: str = "CRAWLER_CONTACT_EMAIL"
    user_agent_template: str = "{name}/{version} (+{contact_email})"
    require_contact_email: bool = True

    def contact_email(self) -> str | None:
        value = os.environ.get(self.contact_email_env, "").strip()
        return value or None

    def user_agent(self) -> str:
        """Identifiable UA. Never a browser string, never Googlebot."""
        email = self.contact_email() or "contact-not-configured"
        return self.user_agent_template.format(
            name=self.name, version=self.version, contact_email=email
        )


class DateRangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date = date(2023, 1, 1)
    end_date: date = date(2026, 12, 31)
    timezone: str = DEFAULT_TIMEZONE

    @model_validator(mode="after")
    def _ordered(self) -> DateRangeConfig:
        if self.end_date < self.start_date:
            raise ValueError("date_range.end_date precedes date_range.start_date")
        return self


class QueryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    language: str = "zh-TW"

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query text must not be empty")
        return value


class SlicingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_granularity: Granularity = Granularity.MONTH
    max_results_per_slice: int = Field(default=100, gt=0)
    result_limit_ratio: float = Field(default=0.9, gt=0.0, le=1.0)
    split_month_to_week: bool = True
    split_week_to_day: bool = True

    @property
    def truncation_threshold(self) -> int:
        """Result count at or above which a slice is suspected truncated."""
        return max(1, int(self.max_results_per_slice * self.result_limit_ratio))


class SerpApiProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_key_env: str = "SERPAPI_API_KEY"
    base_url: str = "https://serpapi.com/search.json"
    engine: str = "google_news"
    tbm: str | None = None
    gl: str = "tw"
    hl: str = "zh-TW"
    sort_by: str | None = "date"
    page_size: int = Field(default=100, gt=0, le=100)
    max_pages_per_slice: int = Field(default=10, gt=0)
    save_raw_response: bool = True
    fail_closed_on_quota: bool = True

    def api_key(self) -> str | None:
        value = os.environ.get(self.api_key_env, "").strip()
        return value or None

    def has_api_key(self) -> bool:
        return self.api_key() is not None


class GoogleNewsRssConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str = "https://news.google.com/rss/search"
    hl: str = "zh-TW"
    gl: str = "TW"
    ceid: str = "TW:zh-Hant"
    requests_per_minute: int = Field(default=3, gt=0)
    cache_raw_xml: bool = True
    stop_on_block: bool = True


class ManualUrlsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serpapi_google_news: SerpApiProviderConfig = Field(
        default_factory=SerpApiProviderConfig
    )
    serpapi_google_search_news_tab: SerpApiProviderConfig = Field(
        default_factory=lambda: SerpApiProviderConfig(
            enabled=False, engine="google", tbm="nws", sort_by=None
        )
    )
    google_news_rss: GoogleNewsRssConfig = Field(default_factory=GoogleNewsRssConfig)
    manual_urls: ManualUrlsConfig = Field(default_factory=ManualUrlsConfig)


class AccessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=2, gt=0, le=8)
    requests_per_domain_per_minute: int = Field(default=6, gt=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    backoff_base_seconds: float = Field(default=2.0, gt=0)
    backoff_max_seconds: float = Field(default=60.0, gt=0)
    backoff_jitter_seconds: float = Field(default=1.5, ge=0)
    max_redirects: int = Field(default=5, ge=0, le=20)
    respect_robots_txt: bool = True
    robots_cache_ttl_seconds: int = Field(default=86400, gt=0)
    obey_retry_after: bool = True
    stop_domain_after_block: bool = True


class PlaywrightConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    browser: str = "chromium"
    headless: bool = True
    navigation_timeout_seconds: float = Field(default=30.0, gt=0)
    wait_until: str = "domcontentloaded"
    content_locators: list[str] = Field(
        default_factory=lambda: ["article", "main", "[itemprop='articleBody']"]
    )
    max_pages_per_run: int = Field(default=200, ge=0)


class ExtractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_content_chars: int = Field(default=200, ge=0)
    recrawl_after_days: int = Field(default=30, ge=0)
    strip_boilerplate: bool = True
    playwright: PlaywrightConfig = Field(default_factory=PlaywrightConfig)


class SyndicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    min_content_chars: int = Field(default=400, ge=0)


class DeduplicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking_params: list[str] = Field(default_factory=list)
    strip_fragment: bool = True
    lowercase_host: bool = True
    drop_www: bool = True
    title_similarity_threshold: float = Field(default=0.92, gt=0.0, le=1.0)
    syndication: SyndicationConfig = Field(default_factory=SyndicationConfig)


class CoverageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_parse_failure_threshold: float = Field(default=0.20, ge=0.0, le=1.0)
    content_unavailable_threshold: float = Field(default=0.30, ge=0.0, le=1.0)


class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: str = "data/output"
    jsonl_name: str = "articles.jsonl"
    json_name: str = "articles.json"
    coverage_name: str = "coverage_report.json"
    coverage_markdown_name: str = "coverage_report.md"
    failed_name: str = "failed_urls.jsonl"
    manifest_name: str = "run_manifest.json"
    json_indent: int = Field(default=2, ge=0, le=8)


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_dir: str = "data/raw"
    state_dir: str = "data/state"
    database: str = "data/state/archive.sqlite3"


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    file: str = "logs/run.log"
    redact_keys: list[str] = Field(
        default_factory=lambda: [
            "api_key",
            "apikey",
            "serpapi_api_key",
            "authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "token",
            "secret",
        ]
    )


class AppConfig(BaseModel):
    """The whole configuration, plus where it came from."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    date_range: DateRangeConfig = Field(default_factory=DateRangeConfig)
    queries: list[QueryConfig] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    slicing: SlicingConfig = Field(default_factory=SlicingConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Populated by the loader, not by the YAML file.
    source_path: Path | None = Field(default=None, exclude=True)
    source_sha256: str = Field(default="", exclude=True)
    root: Path = Field(default_factory=repository_root, exclude=True)

    @model_validator(mode="after")
    def _at_least_one_query(self) -> AppConfig:
        if not self.queries:
            raise ValueError("config defines no queries")
        ids = [q.id for q in self.queries]
        duplicates = {qid for qid in ids if ids.count(qid) > 1}
        if duplicates:
            raise ValueError(f"duplicate query ids: {sorted(duplicates)}")
        return self

    # --- path resolution -------------------------------------------------

    def resolve(self, relative: str | Path) -> Path:
        candidate = Path(relative).expanduser()
        return candidate if candidate.is_absolute() else (self.root / candidate)

    @property
    def raw_dir(self) -> Path:
        return self.resolve(self.paths.raw_dir)

    @property
    def state_dir(self) -> Path:
        return self.resolve(self.paths.state_dir)

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.output.directory)

    @property
    def database_path(self) -> Path:
        return self.resolve(self.paths.database)

    @property
    def log_path(self) -> Path:
        return self.resolve(self.logging.file)

    def ensure_directories(self) -> None:
        for path in (self.raw_dir, self.state_dir, self.output_dir, self.log_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    # --- helpers ---------------------------------------------------------

    def query_by_id(self, query_id: str) -> QueryConfig | None:
        return next((q for q in self.queries if q.id == query_id), None)

    def config_hash(self) -> str:
        """Hash of the file as read.

        Stored on the run so a resumed run can tell whether the config moved
        under it -- a changed query list means the coverage report is no
        longer describing the same thing.
        """
        return self.source_sha256

    def fingerprint(self) -> dict[str, Any]:
        """Small, secret-free summary for the manifest and for doctor."""
        return {
            "config_path": str(self.source_path) if self.source_path else "<defaults>",
            "config_sha256": self.source_sha256,
            "queries": [q.id for q in self.queries],
            "start_date": self.date_range.start_date.isoformat(),
            "end_date": self.date_range.end_date.isoformat(),
            "timezone": self.date_range.timezone,
        }


def default_config_path(root: Path | None = None) -> Path:
    root = root or repository_root()
    env_path = os.environ.get(CONFIG_PATH_ENV, "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        return candidate if candidate.is_absolute() else (root / candidate)
    return root / DEFAULT_CONFIG_RELPATH


def load_config(path: str | Path | None = None, *, root: Path | None = None) -> AppConfig:
    """Read, validate and path-resolve the configuration.

    Raises :class:`ConfigError` with a message a user can act on -- this is
    the first thing ``doctor`` runs, and a pydantic traceback is not an
    error message.
    """
    root = root or repository_root()
    config_path = Path(path).expanduser() if path else default_config_path(root)
    if not config_path.is_absolute():
        config_path = root / config_path

    if not config_path.exists():
        raise ConfigError(
            f"config file not found: {config_path}\n"
            f"Copy config.example.yaml to {DEFAULT_CONFIG_RELPATH} or set "
            f"{CONFIG_PATH_ENV}."
        )

    raw_bytes = config_path.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {config_path}\n{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file is not valid UTF-8: {config_path}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a mapping at the top level: {config_path}")

    try:
        config = AppConfig(**data)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"config file is invalid: {config_path}\n{exc}") from exc

    config.source_path = config_path
    config.source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    config.root = root
    return config
