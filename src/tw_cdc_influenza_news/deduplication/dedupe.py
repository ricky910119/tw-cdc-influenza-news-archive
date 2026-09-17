"""Deduplication, applied in a fixed order of decreasing certainty.

1. **Canonical URL** -- the publisher's own claim. Same canonical, same
   article, full stop.
2. **Normalised URL** -- same page reached with different tracking
   parameters, casing or trailing slash.
3. **Normalised title + publication date** -- the same story re-published at
   a new URL by the same outlet.
4. **Body SHA-256** -- byte-identical text.
5. **High title similarity** -- *flagged only*. Two different stories about
   the same press conference legitimately share a headline, so this rule
   records ``possible_duplicate_of`` and never removes anything.

Wire copy gets its own treatment. When the same body appears on two
different domains, that is a CNA/Reuters story a dozen outlets ran, and each
outlet's URL is a real, separately citable publication. Collapsing them to
one record would lose exactly the distribution information an archive is for.
So they are given a shared ``syndication_cluster_id`` and all kept.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

from tw_cdc_influenza_news.urls import content_sha256, registrable_host

#: Trailing " - Publisher" / " ｜ 媒體" suffixes newsrooms add to <title>.
_TITLE_SUFFIX_RE = re.compile(r"\s*[|｜\-–—]\s*[^|｜\-–—]{1,30}$")
_PUNCT_RE = re.compile(r"[\s　!-/:-@\[-`{-~ -⁯　-〿＀-￯]+")


class DuplicateKind(StrEnum):
    NONE = "none"
    CANONICAL_URL = "canonical_url"
    NORMALIZED_URL = "normalized_url"
    TITLE_AND_DATE = "title_and_date"
    CONTENT_HASH = "content_hash"
    SIMILAR_TITLE = "similar_title"
    SYNDICATED = "syndicated"


def normalize_title(title: str | None) -> str:
    """Fold a headline to a comparison key.

    NFKC first, so full-width and half-width punctuation compare equal --
    a real difference between the same headline as rendered by two Taiwanese
    CMSes.
    """
    if not title:
        return ""
    folded = unicodedata.normalize("NFKC", title).strip()
    folded = _TITLE_SUFFIX_RE.sub("", folded)
    folded = _PUNCT_RE.sub("", folded)
    return folded.casefold()


def title_date_key(title: str | None, published_date: str | None) -> str | None:
    """``<normalised title>|<YYYY-MM-DD>``, or ``None`` if either is missing.

    Requiring both is the point: titles alone collide across years for
    recurring stories ("流感疫苗開打"), and a date alone says nothing.
    """
    normalized = normalize_title(title)
    if not normalized or not published_date:
        return None
    return f"{normalized}|{published_date}"


def title_similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def syndication_cluster_id(content: str) -> str:
    """Stable cluster id for a body of wire text."""
    return f"syn-{content_sha256(content)[:16]}"


@dataclass(slots=True)
class DuplicateDecision:
    """What to do with a candidate record."""

    kind: DuplicateKind = DuplicateKind.NONE
    #: The record this one duplicates and should be merged into.
    merge_into: str | None = None
    #: Records this one merely resembles; recorded, never acted on.
    possible_duplicates: list[str] = field(default_factory=list)
    #: Set when this is wire copy carried by more than one outlet.
    cluster_id: str | None = None
    reason: str = ""

    @property
    def is_duplicate(self) -> bool:
        """True only when the record should be MERGED rather than kept."""
        return self.merge_into is not None


@dataclass(slots=True)
class ExistingRecord:
    """The subset of a stored record dedup needs. Keeps this module testable."""

    record_id: str
    canonical_url: str
    normalized_url: str
    domain: str
    title: str | None = None
    published_date: str | None = None
    content_sha256: str | None = None


def decide(
    *,
    record_id: str,
    canonical_url: str,
    normalized_url: str,
    domain: str,
    title: str | None,
    published_date: str | None,
    content: str | None,
    existing: list[ExistingRecord],
    title_similarity_threshold: float = 0.92,
    syndication_enabled: bool = True,
    syndication_min_chars: int = 400,
) -> DuplicateDecision:
    """Classify a candidate against already-known records.

    ``existing`` is the shortlist the caller looked up by the cheap keys
    (canonical URL, normalised URL, title+date key, content hash) -- this
    function does not scan the whole archive, and must not be given it.
    """
    decision = DuplicateDecision()
    others = [e for e in existing if e.record_id != record_id]

    for other in others:
        if other.canonical_url == canonical_url:
            decision.kind = DuplicateKind.CANONICAL_URL
            decision.merge_into = other.record_id
            decision.reason = "same canonical URL"
            return decision

    for other in others:
        if other.normalized_url == normalized_url:
            decision.kind = DuplicateKind.NORMALIZED_URL
            decision.merge_into = other.record_id
            decision.reason = "same URL after removing tracking parameters"
            return decision

    candidate_key = title_date_key(title, published_date)
    if candidate_key:
        for other in others:
            if title_date_key(other.title, other.published_date) == candidate_key:
                decision.kind = DuplicateKind.TITLE_AND_DATE
                decision.merge_into = other.record_id
                decision.reason = "same normalised title and publication date"
                return decision

    body_hash = content_sha256(content) if content else None
    if body_hash:
        identical = [o for o in others if o.content_sha256 == body_hash]
        same_domain = [o for o in identical if o.domain == domain]
        other_domain = [o for o in identical if o.domain != domain]

        if same_domain:
            decision.kind = DuplicateKind.CONTENT_HASH
            decision.merge_into = same_domain[0].record_id
            decision.reason = "identical body text on the same domain"
            return decision

        if other_domain and syndication_enabled and len(content or "") >= syndication_min_chars:
            decision.kind = DuplicateKind.SYNDICATED
            decision.cluster_id = syndication_cluster_id(content or "")
            decision.possible_duplicates = [o.record_id for o in other_domain]
            decision.reason = (
                "identical body text carried by a different outlet: clustered as "
                "syndication, both copies kept"
            )
            return decision

    similar = [
        other.record_id
        for other in others
        if title_similarity(title, other.title) >= title_similarity_threshold
    ]
    if similar:
        decision.kind = DuplicateKind.SIMILAR_TITLE
        decision.possible_duplicates = similar
        decision.reason = (
            f"title similarity >= {title_similarity_threshold:.2f}; flagged only, not merged"
        )
    return decision


def merge_string_lists(*lists: list[str] | None) -> list[str]:
    """Union that preserves first-seen order. Used for merging evidence."""
    merged: list[str] = []
    for entries in lists:
        for entry in entries or []:
            if entry and entry not in merged:
                merged.append(entry)
    return merged


def domain_of(url: str) -> str:
    return registrable_host(url)
