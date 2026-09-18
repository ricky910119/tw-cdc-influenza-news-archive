"""Article extraction: trafilatura first, structured fallbacks after.

Order of attempts, stopping at the first result long enough to be a body:

1. ``trafilatura`` -- best general-purpose reader, handles most newsrooms.
2. JSON-LD ``articleBody`` -- publishers that ship structured data.
3. ``<article>`` -- the semantic element, when the site uses it properly.
4. Known content containers -- a short list of class/id patterns.

Metadata (title, authors, canonical URL, language, dates) is collected from
all sources regardless of which body won, because a page whose body needed
the fourth fallback usually still has a perfectly good ``og:title``.

Two things this module deliberately does NOT do: it never fabricates a body,
and it never tries to get past an access control. When it sees a paywall, a
login wall or an interstitial, it says so and the caller records
``content_unavailable`` or ``blocked`` with the search metadata intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from tw_cdc_influenza_news.dates import DEFAULT_TIMEZONE
from tw_cdc_influenza_news.errors import INTERSTITIAL_MARKERS, ErrorCode
from tw_cdc_influenza_news.extraction.dateparse import (
    DateCandidate,
    json_ld_dates,
    parse_datetime,
    resolve_published_at,
    text_date_candidates,
    to_display_date,
)
from tw_cdc_influenza_news.logging_utils import get_logger
from tw_cdc_influenza_news.models import (
    DateConfidence,
    DateSource,
    ExtractionMethod,
)

logger = get_logger("extraction")

try:  # trafilatura is a hard dependency, but extraction must degrade, not crash
    import trafilatura
except Exception:  # pragma: no cover - exercised only on a broken install
    trafilatura = None  # type: ignore[assignment]

#: Elements that are never article body.
_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "aside",
    "header",
    "footer",
    "form",
    "iframe",
    "figure",
    "svg",
    "button",
)

#: class/id substrings that mark navigation, promos and other furniture.
_BOILERPLATE_HINTS = (
    "nav",
    "menu",
    "banner",
    "advert",
    "ads",
    "ad-",
    "promo",
    "related",
    "recommend",
    "sidebar",
    "footer",
    "header",
    "share",
    "social",
    "subscribe",
    "newsletter",
    "cookie",
    "consent",
    "comment",
    "breadcrumb",
    "tag-list",
    "popular",
    "更多",
    "推薦",
    "延伸閱讀",
    "熱門",
)

#: Containers worth trying when nothing semantic is present.
_CONTENT_SELECTORS = (
    "[itemprop='articleBody']",
    "article",
    "div.article-body",
    "div.article-content",
    "div.story-body",
    "div.post-content",
    "div.entry-content",
    "div#article-content",
    "div#story_body",
    "div.text",
    "main",
)

_PAYWALL_MARKERS = (
    "訂閱後即可閱讀",
    "本文為訂閱內容",
    "付費閱讀",
    "會員專屬",
    "subscribe to continue",
    "subscribers only",
    "this article is for subscribers",
)

_LOGIN_MARKERS = (
    "請先登入",
    "登入後即可閱讀",
    "please sign in to continue",
    "log in to read",
    "sign in to read",
)

_JS_REQUIRED_MARKERS = (
    "please enable javascript",
    "enable javascript to",
    "javascript is required",
    "請開啟 javascript",
    "您的瀏覽器不支援 javascript",
)

_JS_APP_MARKERS = (
    '__next_data__',
    'id="root"',
    'id="app"',
    "ng-app",
    "data-reactroot",
    "window.__nuxt__",
)

_WS_RE = re.compile(r"[ \t　]+")
_BLANKS_RE = re.compile(r"\n{3,}")


@dataclass(slots=True)
class ExtractionOutcome:
    """Everything the extractor could establish about one page."""

    title: str | None = None
    content: str | None = None
    authors: list[str] = field(default_factory=list)
    source_name: str | None = None
    language: str | None = None
    canonical_url: str | None = None

    published_at: str | None = None
    published_date: str | None = None
    published_at_raw: str | None = None
    date_source: DateSource = DateSource.UNKNOWN
    date_confidence: DateConfidence = DateConfidence.NONE
    modified_at: str | None = None

    method: ExtractionMethod = ExtractionMethod.NONE
    #: Positive evidence that the body is rendered client-side. Only this
    #: flag may trigger the Playwright fallback.
    needs_javascript: bool = False
    #: Set when the page is behind an access control. Never worked around.
    block_code: ErrorCode | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def content_length(self) -> int:
        return len(self.content or "")

    def is_usable(self, min_chars: int) -> bool:
        return self.content is not None and self.content_length >= min_chars


def clean_text(text: str) -> str:
    """Collapse runs of spaces and blank lines, keep paragraph breaks."""
    if not text:
        return ""
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    joined = "\n".join(lines)
    return _BLANKS_RE.sub("\n\n", joined).strip()


def _is_boilerplate(tag) -> bool:  # type: ignore[no-untyped-def]
    identifiers = " ".join(
        [
            " ".join(tag.get("class", [])) if tag.has_attr("class") else "",
            tag.get("id", "") or "",
            tag.get("role", "") or "",
        ]
    ).lower()
    return any(hint in identifiers for hint in _BOILERPLATE_HINTS)


def strip_boilerplate(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove furniture in place and return the same soup, for chaining."""
    for tag_name in _STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.find_all(True):
        if tag.name in {"html", "body"}:
            continue
        if _is_boilerplate(tag):
            tag.decompose()
    return soup


def _text_from(node) -> str:  # type: ignore[no-untyped-def]
    """Paragraph-preserving text. Block elements become their own lines."""
    for br in node.find_all("br"):
        br.replace_with("\n")
    chunks: list[str] = []
    blocks = node.find_all(["p", "h2", "h3", "h4", "li", "blockquote"])
    if blocks:
        for block in blocks:
            text = block.get_text(" ", strip=True)
            if text:
                chunks.append(text)
    else:
        text = node.get_text("\n", strip=True)
        if text:
            chunks.append(text)
    return clean_text("\n\n".join(chunks))


def detect_access_block(html: str, text: str) -> ErrorCode | None:
    """Name the access control on the page, if there is one.

    Interstitials are checked first and by the SHARED marker list: HTML that
    came back through the Playwright fallback never passed the HTTP response
    classifier, so this is the only place a browser-rendered CAPTCHA or
    "unusual traffic" page gets recognised. Without it such a page is parsed
    as if it were an article.
    """
    haystack = f"{html[:20000]}\n{text[:5000]}".lower()
    for marker, code in INTERSTITIAL_MARKERS:
        if marker in haystack:
            return code
    if any(marker.lower() in haystack for marker in _PAYWALL_MARKERS):
        return ErrorCode.PAYWALL
    if any(marker.lower() in haystack for marker in _LOGIN_MARKERS):
        return ErrorCode.LOGIN_REQUIRED
    return None


def detect_needs_javascript(html: str, extracted_length: int, min_chars: int) -> bool:
    """Positive evidence that a browser is required -- not merely "short".

    Requiring evidence matters: launching Chromium for every short page would
    turn a polite crawl into a heavy one, and most short pages are short
    because they are a redirect stub or a 404, not because they are an SPA.
    """
    if extracted_length >= min_chars:
        return False
    lowered = html.lower()
    if any(marker in lowered for marker in _JS_REQUIRED_MARKERS):
        return True
    has_app_shell = any(marker in lowered for marker in _JS_APP_MARKERS)
    # An app shell plus almost no extracted text is the SPA signature.
    return has_app_shell and extracted_length < max(min_chars // 4, 50)


def _meta(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    if tag is None:
        return None
    value = tag.get("content")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _collect_json_ld_blocks(soup: BeautifulSoup) -> list[str]:
    return [
        tag.string or tag.get_text() or ""
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"})
    ]


def _json_ld_body(blocks: list[str]) -> str | None:
    import json

    for block in blocks:
        try:
            payload = json.loads(block)
        except (ValueError, TypeError):
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                body = node.get("articleBody")
                if isinstance(body, str) and body.strip():
                    return clean_text(body)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def _json_ld_authors(blocks: list[str]) -> list[str]:
    import json

    authors: list[str] = []
    for block in blocks:
        try:
            payload = json.loads(block)
        except (ValueError, TypeError):
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                author = node.get("author")
                if isinstance(author, str) and author.strip():
                    authors.append(author.strip())
                elif isinstance(author, dict):
                    name = author.get("name")
                    if isinstance(name, str) and name.strip():
                        authors.append(name.strip())
                elif isinstance(author, list):
                    for entry in author:
                        if isinstance(entry, str) and entry.strip():
                            authors.append(entry.strip())
                        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                            authors.append(entry["name"].strip())
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    # Preserve order, drop repeats.
    return list(dict.fromkeys(a for a in authors if a))


def extract_article(
    html: str,
    url: str,
    *,
    min_content_chars: int = 200,
    tz_name: str = DEFAULT_TIMEZONE,
    provider_date_raw: str | None = None,
    provider_title: str | None = None,
    provider_source_name: str | None = None,
    strip_furniture: bool = True,
) -> ExtractionOutcome:
    """Extract one article from raw HTML. Never raises on bad markup."""
    outcome = ExtractionOutcome()
    if not html or not html.strip():
        outcome.notes.append("empty response body")
        return outcome

    soup = BeautifulSoup(html, "lxml")
    json_ld_blocks = _collect_json_ld_blocks(soup)

    # --- metadata, gathered before the body is chosen --------------------
    outcome.canonical_url = None
    canonical_tag = soup.find("link", attrs={"rel": lambda v: v and "canonical" in v})
    if canonical_tag is not None and isinstance(canonical_tag.get("href"), str):
        outcome.canonical_url = canonical_tag["href"].strip() or None
    if not outcome.canonical_url:
        outcome.canonical_url = _meta(soup, property="og:url")

    outcome.title = (
        _meta(soup, property="og:title")
        or _meta(soup, name="twitter:title")
        or (soup.title.get_text(strip=True) if soup.title else None)
        or provider_title
    )
    outcome.source_name = (
        _meta(soup, property="og:site_name") or provider_source_name
    )
    html_tag = soup.find("html")
    outcome.language = (
        (html_tag.get("lang") if html_tag is not None else None)
        or _meta(soup, property="og:locale")
        or None
    )
    if isinstance(outcome.language, str):
        outcome.language = outcome.language.strip() or None

    author_meta = _meta(soup, name="author") or _meta(soup, property="article:author")
    outcome.authors = _json_ld_authors(json_ld_blocks) or (
        [author_meta] if author_meta else []
    )

    # --- body ------------------------------------------------------------
    body, method = _extract_body(
        html, url, soup, json_ld_blocks, strip_furniture=strip_furniture
    )
    outcome.content = body
    outcome.method = method

    # Newline separator, not space: the page-text date scan works line by line,
    # and a single 40,000-character line has no lines to scan.
    visible_text = soup.get_text("\n", strip=True) if soup else ""
    outcome.block_code = detect_access_block(html, visible_text)
    outcome.needs_javascript = detect_needs_javascript(
        html, len(body or ""), min_content_chars
    )

    # --- dates -------------------------------------------------------------
    ld_published, ld_modified = json_ld_dates(json_ld_blocks)
    og_published = _meta(soup, property="article:published_time") or _meta(
        soup, name="pubdate"
    )
    og_modified = _meta(soup, property="article:modified_time")

    time_raw: str | None = None
    for time_tag in soup.find_all("time"):
        value = time_tag.get("datetime") or time_tag.get_text(strip=True)
        if isinstance(value, str) and value.strip():
            time_raw = value.strip()
            break

    text_raw = next(iter(text_date_candidates(visible_text)), None)

    candidates = [
        DateCandidate(DateSource.JSON_LD, ld_published or "", parse_datetime(ld_published, tz_name=tz_name)),
        DateCandidate(DateSource.OPEN_GRAPH, og_published or "", parse_datetime(og_published, tz_name=tz_name)),
        DateCandidate(DateSource.TIME_ELEMENT, time_raw or "", parse_datetime(time_raw, tz_name=tz_name)),
        DateCandidate(DateSource.PAGE_TEXT, text_raw or "", parse_datetime(text_raw, tz_name=tz_name)),
        DateCandidate(
            DateSource.SEARCH_PROVIDER,
            provider_date_raw or "",
            parse_datetime(provider_date_raw, tz_name=tz_name),
        ),
    ]
    moment, raw, source, confidence = resolve_published_at(candidates, tz_name=tz_name)
    outcome.published_at = moment.isoformat() if moment else None
    outcome.published_date = to_display_date(moment, tz_name=tz_name)
    outcome.published_at_raw = raw or None
    outcome.date_source = source
    outcome.date_confidence = confidence

    modified = parse_datetime(ld_modified or og_modified, tz_name=tz_name)
    outcome.modified_at = modified.isoformat() if modified else None

    return outcome


def _extract_body(
    html: str,
    url: str,
    soup: BeautifulSoup,
    json_ld_blocks: list[str],
    *,
    strip_furniture: bool,
) -> tuple[str | None, ExtractionMethod]:
    """Run the fallback chain and return the first body that looks real."""
    best: tuple[str, ExtractionMethod] | None = None

    if trafilatura is not None:
        try:
            extracted = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception as exc:  # trafilatura raises on some malformed input
            logger.debug("trafilatura failed for %s: %s", url, exc)
            extracted = None
        if extracted:
            # trafilatura separates paragraphs with a single newline; the
            # output contract keeps them as blank-line-separated blocks.
            paragraphs = [ln.strip() for ln in extracted.splitlines() if ln.strip()]
            cleaned = clean_text("\n\n".join(paragraphs))
            if cleaned:
                best = (cleaned, ExtractionMethod.TRAFILATURA)

    def better(candidate: str | None, method: ExtractionMethod) -> None:
        nonlocal best
        if not candidate:
            return
        if best is None or len(candidate) > len(best[0]):
            best = (candidate, method)

    better(_json_ld_body(json_ld_blocks), ExtractionMethod.JSON_LD)

    working = BeautifulSoup(html, "lxml")
    if strip_furniture:
        working = strip_boilerplate(working)

    article_tag = working.find("article")
    if article_tag is not None:
        better(_text_from(article_tag), ExtractionMethod.ARTICLE_TAG)

    for selector in _CONTENT_SELECTORS:
        node = working.select_one(selector)
        if node is None:
            continue
        better(_text_from(node), ExtractionMethod.CONTENT_CONTAINER)

    if best is None:
        # Last resort: the whole body, with the furniture already stripped.
        # Government and older CMS pages routinely carry no semantic markup
        # at all -- no <article>, no itemprop, no recognisable class -- and
        # for those the choice is this or nothing. It runs ONLY when every
        # targeted attempt found nothing, so it can never outrank a real
        # container merely by being longer.
        body = working.find("body")
        if body is not None:
            better(_text_from(body), ExtractionMethod.CONTENT_CONTAINER)

    _ = soup
    if best is None:
        return None, ExtractionMethod.NONE
    return best[0], best[1]
