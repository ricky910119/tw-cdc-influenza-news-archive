"""Logging with secret redaction.

The redaction happens in a :class:`logging.Filter`, not at each call site.
A call-site convention is a rule every future contributor has to remember;
a filter on the handler is a rule the logging framework enforces on records
nobody thought about, including third-party ones.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

REDACTED = "[REDACTED]"

DEFAULT_REDACT_KEYS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "serpapi_api_key",
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "token",
    "secret",
    "password",
)

#: Environment variables whose literal values are scrubbed wherever they
#: appear -- a key pasted into a URL never carries its parameter name with it
#: once it lands in an exception string.
SECRET_ENV_VARS: tuple[str, ...] = ("SERPAPI_API_KEY",)

_KV_TEMPLATE = (
    r"(?i)(?P<key>{keys})"
    r"(?P<sep>\s*[:=]\s*|\s*[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>(?:Bearer|Token|Basic)\s+[^\s,;&\"'}}\)]+|[^\s,;&\"'}}\)]+)"
)


def _build_kv_pattern(keys: Iterable[str]) -> re.Pattern[str]:
    escaped = "|".join(sorted((re.escape(k) for k in keys), key=len, reverse=True))
    return re.compile(_KV_TEMPLATE.format(keys=escaped or "__never__"))


class SecretRedactor:
    """Redacts ``key=value`` pairs and known secret values from text."""

    def __init__(self, keys: Iterable[str] = DEFAULT_REDACT_KEYS) -> None:
        self._pattern = _build_kv_pattern(keys)

    def _literal_secrets(self) -> list[str]:
        # Read the environment on every call: a key set after import must
        # still be redacted, and a key long enough to be a key only.
        values = []
        for name in SECRET_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if len(value) >= 8:
                values.append(value)
        return values

    def redact(self, text: str) -> str:
        if not text:
            return text
        redacted = self._pattern.sub(
            lambda m: f"{m.group('key')}{m.group('sep')}{REDACTED}", text
        )
        for secret in self._literal_secrets():
            redacted = redacted.replace(secret, REDACTED)
        return redacted

    def redact_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """Redact by key name, recursively. Used for params and headers."""
        out: dict[str, Any] = {}
        for key, value in mapping.items():
            if self._pattern.search(f"{key}=x"):
                out[key] = REDACTED
            elif isinstance(value, dict):
                out[key] = self.redact_mapping(value)
            elif isinstance(value, str):
                out[key] = self.redact(value)
            else:
                out[key] = value
        return out


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or SecretRedactor()

    def filter(self, record: logging.LogRecord) -> bool:
        # Render args into the message first: a secret passed as a %-arg is
        # not visible in record.msg alone.
        try:
            rendered = record.getMessage()
        except Exception:  # a broken format string must not lose the record
            rendered = str(record.msg)
        record.msg = self.redactor.redact(rendered)
        record.args = ()
        return True


def configure_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS,
    quiet_console: bool = False,
) -> logging.Logger:
    """Configure the package logger. Idempotent within a process."""
    logger = logging.getLogger("tw_cdc_influenza_news")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    redacting = RedactingFilter(SecretRedactor(redact_keys))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    if not quiet_console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.addFilter(redacting)
        logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redacting)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    base = "tw_cdc_influenza_news"
    return logging.getLogger(f"{base}.{name}" if name else base)
