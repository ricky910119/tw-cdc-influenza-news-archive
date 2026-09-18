"""Error taxonomy.

The distinction that matters everywhere else in the codebase is
retryable / permanent / blocked:

* retryable  -> transient; back off and try again up to max_retries
* permanent  -> will not improve by retrying (404, malformed URL, ...)
* blocked    -> an access restriction. We STOP the source and record why.
                We never try to get around one.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # --- retryable ---
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SERVER_ERROR = "server_error"  # 5xx

    # --- permanent ---
    NOT_FOUND = "not_found"  # 404/410
    CLIENT_ERROR = "client_error"  # other 4xx that is not a block
    INVALID_URL = "invalid_url"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    EXTRACTION_FAILED = "extraction_failed"
    CONTENT_TOO_SHORT = "content_too_short"

    # --- blocked: stop the source, log the reason, do not circumvent ---
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    CAPTCHA = "captcha"
    UNUSUAL_TRAFFIC = "unusual_traffic"
    ACCESS_DENIED = "access_denied"
    LOGIN_REQUIRED = "login_required"
    PAYWALL = "paywall"
    ROBOTS_DISALLOWED = "robots_disallowed"

    # --- provider-level ---
    PROVIDER_AUTH_ERROR = "provider_auth_error"
    PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
    PROVIDER_ERROR = "provider_error"


RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.TIMEOUT,
        ErrorCode.CONNECTION_ERROR,
        ErrorCode.SERVER_ERROR,
    }
)

BLOCKED_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.HTTP_403,
        ErrorCode.HTTP_429,
        ErrorCode.CAPTCHA,
        ErrorCode.UNUSUAL_TRAFFIC,
        ErrorCode.ACCESS_DENIED,
        ErrorCode.LOGIN_REQUIRED,
        ErrorCode.PAYWALL,
        ErrorCode.ROBOTS_DISALLOWED,
    }
)

# Blocks that indict the whole domain (as opposed to this one URL).
DOMAIN_LEVEL_BLOCKS: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.HTTP_403,
        ErrorCode.HTTP_429,
        ErrorCode.CAPTCHA,
        ErrorCode.UNUSUAL_TRAFFIC,
        ErrorCode.ACCESS_DENIED,
        ErrorCode.ROBOTS_DISALLOWED,
    }
)

#: Text that identifies an interstitial rather than a page, wherever it is
#: found. Shared by the HTTP layer and the extractor on purpose: HTML that
#: arrives through the Playwright fallback never passes through the HTTP
#: response classifier, so a copy of this list in only one of the two places
#: leaves the browser path unguarded.
#:
#: Markers are specific on purpose (``g-recaptcha``, ``recaptcha/api.js``)
#: rather than the bare word "captcha": a news article *about* CAPTCHAs is a
#: page we want, not a block.
INTERSTITIAL_MARKERS: tuple[tuple[str, ErrorCode], ...] = (
    ("unusual traffic", ErrorCode.UNUSUAL_TRAFFIC),
    ("我們的系統偵測到", ErrorCode.UNUSUAL_TRAFFIC),
    ("g-recaptcha", ErrorCode.CAPTCHA),
    ("recaptcha/api.js", ErrorCode.CAPTCHA),
    ("hcaptcha.com", ErrorCode.CAPTCHA),
    ("cf-challenge", ErrorCode.CAPTCHA),
    ("請完成驗證", ErrorCode.CAPTCHA),
    ("are you a robot", ErrorCode.CAPTCHA),
    ("access denied", ErrorCode.ACCESS_DENIED),
    ("拒絕存取", ErrorCode.ACCESS_DENIED),
)


PROVIDER_FATAL_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.PROVIDER_AUTH_ERROR,
        ErrorCode.PROVIDER_QUOTA_EXHAUSTED,
    }
)


def is_retryable(code: ErrorCode | str | None) -> bool:
    return code is not None and ErrorCode(code) in RETRYABLE_CODES


def is_blocked(code: ErrorCode | str | None) -> bool:
    return code is not None and ErrorCode(code) in BLOCKED_CODES


def is_domain_level_block(code: ErrorCode | str | None) -> bool:
    return code is not None and ErrorCode(code) in DOMAIN_LEVEL_BLOCKS


class ArchiveError(Exception):
    """Base class for this package's exceptions."""

    code: ErrorCode = ErrorCode.PROVIDER_ERROR

    def __init__(self, message: str, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ConfigError(ArchiveError):
    """The configuration file or environment is unusable."""


class BlockedError(ArchiveError):
    """An access restriction was detected. Stop; do not circumvent."""

    def __init__(
        self,
        message: str,
        code: ErrorCode,
        *,
        domain: str | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message, code)
        self.domain = domain
        self.provider = provider


class ProviderFatalError(ArchiveError):
    """Quota/auth failure. The provider fails closed for the rest of the run."""


class FetchError(ArchiveError):
    """A fetch failed. `code` says whether it is worth retrying."""
