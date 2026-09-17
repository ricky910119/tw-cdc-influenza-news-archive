"""URL normalisation, canonicalisation and identity helpers.

Deduplication leans on these functions, so they are deliberately boring and
total: every one of them accepts whatever a search provider handed us and
returns something usable, or raises :class:`ValueError` once, at the edge.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_TRACKING_PARAMS: tuple[str, ...] = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
    "from",
    "share_source",
    "yptr",
)

#: Prefix wildcards: any parameter starting with one of these is tracking.
TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "_hs", "vero_", "mtm_")

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_WS_RE = re.compile(r"\s+")


def is_probably_tracking(param: str, extra: frozenset[str]) -> bool:
    lowered = param.lower()
    if lowered in extra:
        return True
    return any(lowered.startswith(prefix) for prefix in TRACKING_PREFIXES)


def registrable_host(url: str) -> str:
    """Lowercased hostname with a leading ``www.`` removed; ``""`` if absent.

    This is the rate-limiter and block-list key. It is deliberately not a
    public-suffix-aware registrable domain: per-subdomain limiting is the
    politer failure mode, and getting PSL wrong would silently merge two
    unrelated hosts into one bucket.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(
    url: str,
    *,
    tracking_params: tuple[str, ...] | frozenset[str] = DEFAULT_TRACKING_PARAMS,
    strip_fragment: bool = True,
    drop_www: bool = True,
    lowercase_host: bool = True,
) -> str:
    """Normalise a URL for identity comparison.

    Applied, in order: strip whitespace, lowercase scheme and host, drop a
    default port, drop ``www.``, remove tracking parameters, sort the
    remaining query, collapse an empty query or fragment, and remove a
    trailing slash from a non-root path.

    Everything here is identity-preserving for the article: none of it
    changes which page you land on. Path case is left alone -- plenty of
    CMSes serve case-sensitive slugs.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("empty URL")

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"URL is not absolute: {url!r}")

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower() if lowercase_host else (parts.hostname or "")
    if drop_www and host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port is not None and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    # Credentials in a URL are never part of an article's identity, and we
    # do not want them reaching the output or the log.
    extra = frozenset(p.lower() for p in tracking_params)
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not is_probably_tracking(k, extra)
    ]
    query = urlencode(sorted(query_pairs), doseq=True)

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    fragment = "" if strip_fragment else parts.fragment
    return urlunsplit((scheme, netloc, path, query, fragment))


def canonicalize_url(
    url: str,
    *,
    canonical_hint: str | None = None,
    **normalize_kwargs: object,
) -> str:
    """The best available canonical form.

    ``canonical_hint`` is the page's own ``<link rel="canonical">`` or
    ``og:url``. A publisher's own claim about its canonical URL beats our
    normalisation -- but only when it is absolute and points at the same
    host, otherwise a mis-templated canonical tag pointing at a site's
    homepage would collapse every article on that site into one record.
    """
    normalized = normalize_url(url, **normalize_kwargs)  # type: ignore[arg-type]
    if not canonical_hint:
        return normalized
    try:
        hinted = normalize_url(canonical_hint, **normalize_kwargs)  # type: ignore[arg-type]
    except ValueError:
        return normalized
    if registrable_host(hinted) != registrable_host(normalized):
        return normalized
    return hinted


def url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def content_sha256(text: str) -> str:
    """Hash of the normalised body text.

    Whitespace is collapsed first so that two renderings of the same wire
    story -- one with hard line wraps, one without -- hash identically.
    """
    return hashlib.sha256(_WS_RE.sub(" ", text or "").strip().encode("utf-8")).hexdigest()


def record_id_for(canonical: str) -> str:
    """Stable record identifier: sha256 of the canonical URL.

    Stable across runs, which is what makes re-running the crawler idempotent
    rather than duplicative.
    """
    return url_sha256(canonical)


def unwrap_redirector(url: str) -> str:
    """Unwrap a single layer of common aggregator redirect wrappers.

    Google News item links are often ``news.google.com/rss/articles/...``
    with the publisher URL in a ``url=`` parameter. Reading the parameter we
    were given is not a circumvention of anything: it is the destination the
    aggregator is telling us about, and it saves a needless request.
    Anything we do not recognise is returned untouched and resolved by an
    ordinary HTTP redirect instead.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if host not in {"news.google.com", "www.google.com", "google.com"}:
        return url
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in ("url", "u", "q"):
        candidate = params.get(key)
        if candidate:
            candidate = unquote(candidate)
            if candidate.startswith(("http://", "https://")):
                return candidate
    return url
