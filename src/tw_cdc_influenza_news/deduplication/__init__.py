"""Deduplication and syndication clustering."""

from tw_cdc_influenza_news.deduplication.dedupe import (
    DuplicateDecision,
    DuplicateKind,
    ExistingRecord,
    decide,
    domain_of,
    merge_string_lists,
    normalize_title,
    syndication_cluster_id,
    title_date_key,
    title_similarity,
)

__all__ = [
    "DuplicateDecision",
    "DuplicateKind",
    "ExistingRecord",
    "decide",
    "domain_of",
    "merge_string_lists",
    "normalize_title",
    "syndication_cluster_id",
    "title_date_key",
    "title_similarity",
]
