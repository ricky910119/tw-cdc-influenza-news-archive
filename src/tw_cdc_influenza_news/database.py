"""SQLite state store: the canonical record of what happened.

Design rules:

* Every URL state transition is committed immediately. A run killed with
  Ctrl-C loses at most the one URL that was in flight, and the next run
  picks up exactly where this one stopped.
* The database is the source of truth; the JSON outputs are a projection of
  it produced by ``export``. That is why ``export`` can run long after the
  crawl, and why re-running a crawl cannot duplicate articles: identity is a
  primary key here, not a de-duplication pass over a file.
* ``record_id`` is sha256 of the canonical URL, so it is stable across runs.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from tw_cdc_influenza_news import DB_SCHEMA_VERSION
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import (
    RESUMABLE_STATUSES,
    Article,
    DiscoveryEvidence,
    UrlStatus,
)

logger = get_logger("database")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    tool_version        TEXT NOT NULL,
    schema_version      INTEGER NOT NULL,
    db_schema_version   INTEGER NOT NULL,
    config_path         TEXT,
    config_sha256       TEXT,
    command             TEXT,
    providers_json      TEXT NOT NULL DEFAULT '[]',
    requested_start     TEXT,
    requested_end       TEXT,
    effective_end       TEXT,
    timezone            TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',
    counts_json         TEXT NOT NULL DEFAULT '{}',
    notes_json          TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS urls (
    record_id           TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    normalized_url      TEXT NOT NULL,
    canonical_url       TEXT NOT NULL,
    final_url           TEXT,
    domain              TEXT NOT NULL,
    status              TEXT NOT NULL,
    error_code          TEXT,
    error_message       TEXT,
    http_status         INTEGER,
    attempts            INTEGER NOT NULL DEFAULT 0,
    first_discovered_at TEXT NOT NULL,
    last_checked_at     TEXT NOT NULL,
    last_success_at     TEXT,
    content_sha256      TEXT,
    title_date_key      TEXT,
    first_run_id        TEXT,
    last_run_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status);
CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain);
CREATE INDEX IF NOT EXISTS idx_urls_content_sha ON urls(content_sha256);
CREATE INDEX IF NOT EXISTS idx_urls_title_date ON urls(title_date_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_urls_canonical ON urls(canonical_url);

CREATE TABLE IF NOT EXISTS discovery_evidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id           TEXT NOT NULL,
    run_id              TEXT,
    provider            TEXT NOT NULL,
    query_id            TEXT NOT NULL,
    query_text          TEXT NOT NULL,
    slice_key           TEXT NOT NULL,
    slice_start         TEXT NOT NULL,
    slice_end_exclusive TEXT NOT NULL,
    position            INTEGER,
    provider_date_raw   TEXT,
    provider_title      TEXT,
    provider_source_name TEXT,
    gl                  TEXT,
    hl                  TEXT,
    discovered_at       TEXT NOT NULL,
    raw_response_path   TEXT,
    FOREIGN KEY (record_id) REFERENCES urls(record_id) ON DELETE CASCADE
);

-- One row per (article, provider, query, slice): re-running a slice updates
-- rather than multiplies the evidence.
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique
    ON discovery_evidence(record_id, provider, query_id, slice_key);

CREATE TABLE IF NOT EXISTS articles (
    record_id            TEXT PRIMARY KEY,
    run_id               TEXT,
    published_at         TEXT,
    published_date       TEXT,
    date_source          TEXT,
    date_confidence      TEXT,
    title                TEXT,
    source_name          TEXT,
    content              TEXT,
    content_sha256       TEXT,
    content_char_count   INTEGER NOT NULL DEFAULT 0,
    extraction_method    TEXT,
    language             TEXT,
    syndication_cluster_id TEXT,
    payload_json         TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES urls(record_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_date);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(syndication_cluster_id);

CREATE TABLE IF NOT EXISTS slices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    provider            TEXT NOT NULL,
    query_id            TEXT NOT NULL,
    query_text          TEXT NOT NULL,
    slice_key           TEXT NOT NULL,
    slice_start         TEXT NOT NULL,
    slice_end_exclusive TEXT NOT NULL,
    granularity         TEXT NOT NULL,
    month_key           TEXT NOT NULL,
    result_count        INTEGER NOT NULL DEFAULT 0,
    page_count          INTEGER NOT NULL DEFAULT 0,
    new_url_count       INTEGER NOT NULL DEFAULT 0,
    duplicate_count     INTEGER NOT NULL DEFAULT 0,
    result_limit_reached INTEGER NOT NULL DEFAULT 0,
    slice_was_split     INTEGER NOT NULL DEFAULT 0,
    completed           INTEGER NOT NULL DEFAULT 0,
    completeness_warning TEXT,
    error_code          TEXT,
    error_message       TEXT,
    updated_at          TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_slices_unique
    ON slices(run_id, provider, query_id, slice_key);

CREATE TABLE IF NOT EXISTS block_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    scope        TEXT NOT NULL,
    target       TEXT NOT NULL,
    error_code   TEXT NOT NULL,
    detail       TEXT,
    url          TEXT,
    occurred_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_block_target ON block_events(scope, target);
"""


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class UrlRow:
    record_id: str
    url: str
    normalized_url: str
    canonical_url: str
    final_url: str | None
    domain: str
    status: UrlStatus
    error_code: str | None
    error_message: str | None
    http_status: int | None
    attempts: int
    first_discovered_at: str
    last_checked_at: str
    last_success_at: str | None
    content_sha256: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> UrlRow:
        return cls(
            record_id=row["record_id"],
            url=row["url"],
            normalized_url=row["normalized_url"],
            canonical_url=row["canonical_url"],
            final_url=row["final_url"],
            domain=row["domain"],
            status=UrlStatus(row["status"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            http_status=row["http_status"],
            attempts=row["attempts"],
            first_discovered_at=row["first_discovered_at"],
            last_checked_at=row["last_checked_at"],
            last_success_at=row["last_success_at"],
            content_sha256=row["content_sha256"],
        )


class Database:
    """Thin, explicit wrapper over sqlite3. No ORM, no lazy sessions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")

    # --- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def initialize(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        current = self.schema_version()
        if current is None:
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('db_schema_version', ?)",
                (str(DB_SCHEMA_VERSION),),
            )
        elif current != DB_SCHEMA_VERSION:
            # No migration path exists yet. Saying so is more useful than
            # silently operating on a shape we do not understand.
            raise RuntimeError(
                f"database at {self.path} has schema version {current}, "
                f"this build expects {DB_SCHEMA_VERSION}. "
                "Move the file aside and re-run, or migrate it by hand."
            )

    def schema_version(self) -> int | None:
        try:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='db_schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row["value"]) if row else None

    # --- runs ------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        *,
        tool_version: str,
        schema_version: int,
        config_path: str,
        config_sha256: str,
        command: str,
        providers: Sequence[str],
        requested_start: str,
        requested_end: str,
        effective_end: str,
        timezone: str,
        started_at: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO runs (
                run_id, tool_version, schema_version, db_schema_version,
                config_path, config_sha256, command, providers_json,
                requested_start, requested_end, effective_end, timezone,
                started_at, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'running')
            """,
            (
                run_id,
                tool_version,
                schema_version,
                DB_SCHEMA_VERSION,
                config_path,
                config_sha256,
                command,
                json.dumps(list(providers), ensure_ascii=False),
                requested_start,
                requested_end,
                effective_end,
                timezone,
                started_at or utc_now(),
            ),
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        counts: dict[str, int] | None = None,
        notes: Sequence[str] = (),
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, counts_json = ?, notes_json = ?
             WHERE run_id = ?
            """,
            (
                utc_now(),
                status,
                json.dumps(counts or {}, ensure_ascii=False),
                json.dumps(list(notes), ensure_ascii=False),
                run_id,
            ),
        )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    def unfinished_runs(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM runs WHERE finished_at IS NULL ORDER BY started_at DESC"
            )
        )

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        )

    # --- urls ------------------------------------------------------------

    def upsert_url(
        self,
        *,
        record_id: str,
        url: str,
        normalized_url: str,
        canonical_url: str,
        domain: str,
        run_id: str,
        discovered_at: str | None = None,
    ) -> bool:
        """Insert a newly discovered URL. Returns True when it was new.

        An already-known URL keeps its status and its first_discovered_at: a
        second query finding the same article is new *evidence*, not a new
        article, and must not reset a completed fetch.
        """
        now = discovered_at or utc_now()
        # Ask first. rowcount is 1 for both the INSERT and the DO UPDATE
        # branch, and comparing timestamps afterwards misreads two sightings
        # that share a discovery timestamp as two new URLs.
        existed = (
            self._conn.execute(
                "SELECT 1 FROM urls WHERE record_id = ?", (record_id,)
            ).fetchone()
            is not None
        )
        self._conn.execute(
            """
            INSERT INTO urls (
                record_id, url, normalized_url, canonical_url, domain,
                status, attempts, first_discovered_at, last_checked_at,
                first_run_id, last_run_id
            ) VALUES (?,?,?,?,?,?,0,?,?,?,?)
            ON CONFLICT(record_id) DO UPDATE SET
                last_run_id = excluded.last_run_id
            """,
            (
                record_id,
                url,
                normalized_url,
                canonical_url,
                domain,
                UrlStatus.DISCOVERED.value,
                now,
                now,
                run_id,
                run_id,
            ),
        )
        return not existed

    def get_url(self, record_id: str) -> UrlRow | None:
        row = self._conn.execute(
            "SELECT * FROM urls WHERE record_id = ?", (record_id,)
        ).fetchone()
        return UrlRow.from_row(row) if row else None

    def find_by_canonical(self, canonical_url: str) -> UrlRow | None:
        row = self._conn.execute(
            "SELECT * FROM urls WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        return UrlRow.from_row(row) if row else None

    def find_by_normalized(self, normalized_url: str) -> UrlRow | None:
        row = self._conn.execute(
            "SELECT * FROM urls WHERE normalized_url = ? LIMIT 1", (normalized_url,)
        ).fetchone()
        return UrlRow.from_row(row) if row else None

    def find_by_title_date_key(self, key: str) -> list[UrlRow]:
        return [
            UrlRow.from_row(r)
            for r in self._conn.execute(
                "SELECT * FROM urls WHERE title_date_key = ?", (key,)
            )
        ]

    def find_by_content_sha(self, content_sha: str) -> list[UrlRow]:
        return [
            UrlRow.from_row(r)
            for r in self._conn.execute(
                "SELECT * FROM urls WHERE content_sha256 = ?", (content_sha,)
            )
        ]

    def set_status(
        self,
        record_id: str,
        status: UrlStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        http_status: int | None = None,
        final_url: str | None = None,
        content_sha256: str | None = None,
        title_date_key: str | None = None,
        increment_attempts: bool = False,
        run_id: str | None = None,
    ) -> None:
        """Persist one state transition. Committed immediately, by design."""
        now = utc_now()
        self._conn.execute(
            """
            UPDATE urls SET
                status          = ?,
                error_code      = ?,
                error_message   = ?,
                http_status     = COALESCE(?, http_status),
                final_url       = COALESCE(?, final_url),
                content_sha256  = COALESCE(?, content_sha256),
                title_date_key  = COALESCE(?, title_date_key),
                attempts        = attempts + ?,
                last_checked_at = ?,
                last_success_at = CASE WHEN ? = 'success' THEN ? ELSE last_success_at END,
                last_run_id     = COALESCE(?, last_run_id)
            WHERE record_id = ?
            """,
            (
                status.value,
                error_code,
                error_message,
                http_status,
                final_url,
                content_sha256,
                title_date_key,
                1 if increment_attempts else 0,
                now,
                status.value,
                now,
                run_id,
                record_id,
            ),
        )

    def reset_stuck_fetching(self, run_id: str | None = None) -> int:
        """Return rows abandoned mid-fetch to the queue.

        A process killed during a fetch leaves FETCHING rows that no longer
        have a worker. Nothing was written for them, so requeueing is safe.
        """
        cursor = self._conn.execute(
            "UPDATE urls SET status = ? WHERE status = ?",
            (UrlStatus.QUEUED.value, UrlStatus.FETCHING.value),
        )
        _ = run_id
        return cursor.rowcount or 0

    def pending_urls(
        self,
        *,
        limit: int | None = None,
        max_attempts: int = 3,
        statuses: Iterable[UrlStatus] = RESUMABLE_STATUSES,
        exclude_domains: Iterable[str] = (),
    ) -> list[UrlRow]:
        status_values = [s.value for s in statuses]
        params: list[Any] = [*status_values, max_attempts]
        sql = (
            "SELECT * FROM urls WHERE status IN "
            f"({','.join('?' * len(status_values))}) AND attempts < ?"
        )
        excluded = list(exclude_domains)
        if excluded:
            sql += f" AND domain NOT IN ({','.join('?' * len(excluded))})"
            params.extend(excluded)
        sql += " ORDER BY first_discovered_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [UrlRow.from_row(r) for r in self._conn.execute(sql, params)]

    def urls_needing_refresh(self, older_than_days: int) -> list[UrlRow]:
        """Successful URLs whose content is older than the recrawl window."""
        if older_than_days <= 0:
            return []
        cutoff = (datetime.now(tz=UTC) - timedelta(days=older_than_days)).isoformat(
            timespec="seconds"
        )
        return [
            UrlRow.from_row(r)
            for r in self._conn.execute(
                "SELECT * FROM urls WHERE status = ? AND COALESCE(last_success_at, '') < ?",
                (UrlStatus.SUCCESS.value, cutoff),
            )
        ]

    def requeue(self, record_ids: Sequence[str]) -> int:
        if not record_ids:
            return 0
        cursor = self._conn.execute(
            f"""
            UPDATE urls
               SET status = ?, attempts = 0, error_code = NULL, error_message = NULL
             WHERE record_id IN ({','.join('?' * len(record_ids))})
            """,
            (UrlStatus.QUEUED.value, *record_ids),
        )
        return cursor.rowcount or 0

    def requeue_date_range(self, start_date: str, end_exclusive: str) -> int:
        """Requeue every article published within ``[start, end)``."""
        rows = self._conn.execute(
            """
            SELECT u.record_id FROM urls u
              JOIN articles a ON a.record_id = u.record_id
             WHERE a.published_date >= ? AND a.published_date < ?
            """,
            (start_date, end_exclusive),
        ).fetchall()
        return self.requeue([r["record_id"] for r in rows])

    def status_counts(self) -> dict[str, int]:
        return {
            row["status"]: row["n"]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM urls GROUP BY status"
            )
        }

    # --- discovery evidence ----------------------------------------------

    def add_evidence(self, record_id: str, evidence: DiscoveryEvidence, run_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO discovery_evidence (
                record_id, run_id, provider, query_id, query_text, slice_key,
                slice_start, slice_end_exclusive, position, provider_date_raw,
                provider_title, provider_source_name, gl, hl, discovered_at,
                raw_response_path
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(record_id, provider, query_id, slice_key) DO UPDATE SET
                position          = excluded.position,
                provider_date_raw = excluded.provider_date_raw,
                provider_title    = excluded.provider_title,
                raw_response_path = excluded.raw_response_path
            """,
            (
                record_id,
                run_id,
                evidence.provider,
                evidence.query_id,
                evidence.query_text,
                evidence.slice_key,
                evidence.slice_start,
                evidence.slice_end_exclusive,
                evidence.position,
                evidence.provider_date_raw,
                evidence.provider_title,
                evidence.provider_source_name,
                evidence.gl,
                evidence.hl,
                evidence.discovered_at,
                evidence.raw_response_path,
            ),
        )

    def evidence_for(self, record_id: str) -> list[DiscoveryEvidence]:
        rows = self._conn.execute(
            "SELECT * FROM discovery_evidence WHERE record_id = ? ORDER BY id",
            (record_id,),
        )
        return [
            DiscoveryEvidence(
                provider=r["provider"],
                query_id=r["query_id"],
                query_text=r["query_text"],
                slice_key=r["slice_key"],
                slice_start=r["slice_start"],
                slice_end_exclusive=r["slice_end_exclusive"],
                position=r["position"],
                provider_date_raw=r["provider_date_raw"],
                provider_title=r["provider_title"],
                provider_source_name=r["provider_source_name"],
                gl=r["gl"],
                hl=r["hl"],
                discovered_at=r["discovered_at"],
                raw_response_path=r["raw_response_path"],
            )
            for r in rows
        ]

    # --- articles --------------------------------------------------------

    def save_article(self, article: Article, run_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO articles (
                record_id, run_id, published_at, published_date, date_source,
                date_confidence, title, source_name, content, content_sha256,
                content_char_count, extraction_method, language,
                syndication_cluster_id, payload_json, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(record_id) DO UPDATE SET
                run_id                 = excluded.run_id,
                published_at           = excluded.published_at,
                published_date         = excluded.published_date,
                date_source            = excluded.date_source,
                date_confidence        = excluded.date_confidence,
                title                  = excluded.title,
                source_name            = excluded.source_name,
                content                = excluded.content,
                content_sha256         = excluded.content_sha256,
                content_char_count     = excluded.content_char_count,
                extraction_method      = excluded.extraction_method,
                language               = excluded.language,
                syndication_cluster_id = excluded.syndication_cluster_id,
                payload_json           = excluded.payload_json,
                updated_at             = excluded.updated_at
            """,
            (
                article.record_id,
                run_id,
                article.published_at,
                article.published_date,
                article.date_source.value,
                article.date_confidence.value,
                article.title,
                article.source_name,
                article.content,
                article.content_sha256,
                article.content_char_count,
                article.extraction_method.value,
                article.language,
                article.syndication_cluster_id,
                article.model_dump_json(),
                utc_now(),
            ),
        )

    def set_cluster(self, record_id: str, cluster_id: str | None) -> None:
        self._conn.execute(
            "UPDATE articles SET syndication_cluster_id = ? WHERE record_id = ?",
            (cluster_id, record_id),
        )

    def iter_articles(self, run_id: str | None = None) -> Iterator[Article]:
        if run_id:
            rows = self._conn.execute(
                "SELECT payload_json FROM articles WHERE run_id = ? ORDER BY published_date, record_id",
                (run_id,),
            )
        else:
            rows = self._conn.execute(
                "SELECT payload_json FROM articles ORDER BY published_date, record_id"
            )
        for row in rows:
            yield Article.model_validate_json(row["payload_json"])

    def article_count(self, run_id: str | None = None) -> int:
        if run_id:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM articles WHERE run_id = ?", (run_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()
        return int(row["n"])

    # --- slices ----------------------------------------------------------

    def record_slice(self, run_id: str, coverage: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO slices (
                run_id, provider, query_id, query_text, slice_key, slice_start,
                slice_end_exclusive, granularity, month_key, result_count,
                page_count, new_url_count, duplicate_count, result_limit_reached,
                slice_was_split, completed, completeness_warning, error_code,
                error_message, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, provider, query_id, slice_key) DO UPDATE SET
                result_count         = excluded.result_count,
                page_count           = excluded.page_count,
                new_url_count        = excluded.new_url_count,
                duplicate_count      = excluded.duplicate_count,
                result_limit_reached = excluded.result_limit_reached,
                slice_was_split      = excluded.slice_was_split,
                completed            = excluded.completed,
                completeness_warning = excluded.completeness_warning,
                error_code           = excluded.error_code,
                error_message        = excluded.error_message,
                updated_at           = excluded.updated_at
            """,
            (
                run_id,
                coverage["provider"],
                coverage["query_id"],
                coverage["query_text"],
                coverage["slice_key"],
                coverage["slice_start"],
                coverage["slice_end_exclusive"],
                coverage["granularity"],
                coverage["month_key"],
                coverage.get("result_count", 0),
                coverage.get("page_count", 0),
                coverage.get("new_url_count", 0),
                coverage.get("duplicate_count", 0),
                int(bool(coverage.get("result_limit_reached"))),
                int(bool(coverage.get("slice_was_split"))),
                int(bool(coverage.get("completed"))),
                coverage.get("completeness_warning"),
                coverage.get("error_code"),
                coverage.get("error_message"),
                utc_now(),
            ),
        )

    def completed_slice_keys(self, run_id: str) -> set[tuple[str, str, str]]:
        """(provider, query_id, slice_key) triples already finished this run.

        This is what makes ``resume`` skip discovery work rather than redo it.
        """
        return {
            (r["provider"], r["query_id"], r["slice_key"])
            for r in self._conn.execute(
                "SELECT provider, query_id, slice_key FROM slices "
                "WHERE run_id = ? AND completed = 1",
                (run_id,),
            )
        }

    def slices_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM slices WHERE run_id = ? ORDER BY slice_start, provider, query_id",
                (run_id,),
            )
        )

    # --- block events ----------------------------------------------------

    def record_block(
        self,
        run_id: str,
        *,
        scope: str,
        target: str,
        error_code: str,
        detail: str,
        url: str | None = None,
        occurred_at: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO block_events (run_id, scope, target, error_code, detail, url, occurred_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (run_id, scope, target, error_code, detail, url, occurred_at or utc_now()),
        )

    def blocks_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM block_events WHERE run_id = ? ORDER BY id", (run_id,)
            )
        )

    def failed_url_rows(self, run_id: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM urls WHERE status IN ('retryable_error','permanent_error',"
            "'blocked','content_unavailable')"
        )
        params: tuple[Any, ...] = ()
        if run_id:
            sql += " AND last_run_id = ?"
            params = (run_id,)
        sql += " ORDER BY domain, url"
        return list(self._conn.execute(sql, params))
