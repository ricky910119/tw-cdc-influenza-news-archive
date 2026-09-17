"""Deduplication: URL identity, tracking parameters, syndication, flags only."""

from __future__ import annotations

from tw_cdc_influenza_news.deduplication import (
    DuplicateKind,
    ExistingRecord,
    decide,
    merge_string_lists,
    normalize_title,
    syndication_cluster_id,
    title_date_key,
    title_similarity,
)
from tw_cdc_influenza_news.urls import (
    canonicalize_url,
    normalize_url,
    record_id_for,
    registrable_host,
    unwrap_redirector,
)

BODY = "疾管署今日公布最新流感監測資料，呼籲民眾接種疫苗。" * 20


# --- URL normalisation ------------------------------------------------------


def test_utm_parameters_are_stripped():
    dirty = "https://news.example.tw/a?utm_source=google&utm_medium=rss&id=7"

    assert normalize_url(dirty) == "https://news.example.tw/a?id=7"


def test_every_common_tracker_family_is_stripped():
    dirty = (
        "https://news.example.tw/a?fbclid=x&gclid=y&utm_campaign=z&pk_source=q"
        "&mtm_medium=w&spm=1.2&real=keep"
    )

    assert normalize_url(dirty) == "https://news.example.tw/a?real=keep"


def test_host_case_www_port_slash_and_fragment_are_normalised():
    variants = [
        "https://WWW.News.Example.tw/a/",
        "https://news.example.tw:443/a#section-2",
        "https://www.news.example.tw/a",
    ]

    assert {normalize_url(v) for v in variants} == {"https://news.example.tw/a"}


def test_query_parameter_order_does_not_change_identity():
    a = normalize_url("https://news.example.tw/a?b=2&a=1")
    b = normalize_url("https://news.example.tw/a?a=1&b=2")

    assert a == b


def test_path_case_is_preserved():
    """Plenty of CMSes serve case-sensitive slugs; folding them would merge
    two genuinely different articles."""
    assert normalize_url("https://news.example.tw/Flu-Surge") != normalize_url(
        "https://news.example.tw/flu-surge"
    )


def test_publisher_canonical_tag_wins_over_our_normalisation():
    canonical = canonicalize_url(
        "https://news.example.tw/a?utm_source=x",
        canonical_hint="https://news.example.tw/article/2024/01/15/flu-surge",
    )

    assert canonical == "https://news.example.tw/article/2024/01/15/flu-surge"


def test_a_canonical_tag_pointing_at_another_host_is_ignored():
    """A mis-templated canonical tag must not collapse a whole site into one
    record."""
    canonical = canonicalize_url(
        "https://news.example.tw/a", canonical_hint="https://aggregator.example.com/"
    )

    assert canonical == "https://news.example.tw/a"


def test_record_id_is_stable_across_runs():
    assert record_id_for("https://news.example.tw/a") == record_id_for(
        "https://news.example.tw/a"
    )
    assert len(record_id_for("https://news.example.tw/a")) == 64


def test_google_news_redirect_is_unwrapped():
    wrapped = (
        "https://news.google.com/rss/articles/CBMi?oc=5&url="
        "https://news.example.tw/article/2024/01/15/flu-surge"
    )

    assert unwrap_redirector(wrapped) == "https://news.example.tw/article/2024/01/15/flu-surge"


def test_a_publisher_url_is_never_rewritten():
    url = "https://news.example.tw/a?url=https://other.example/x"

    assert unwrap_redirector(url) == url


# --- title handling ---------------------------------------------------------


def test_publisher_suffix_and_width_differences_fold_together():
    assert normalize_title("疾管署：流感疫情升溫 - 範例新聞網") == normalize_title(
        "疾管署：流感疫情升溫｜範例新聞網"
    )


def test_title_date_key_needs_both_parts():
    assert title_date_key("疾管署籲接種疫苗", "2024-01-15")
    assert title_date_key("疾管署籲接種疫苗", None) is None
    assert title_date_key(None, "2024-01-15") is None


def test_same_title_in_different_years_is_not_the_same_key():
    assert title_date_key("流感疫苗開打", "2023-10-02") != title_date_key(
        "流感疫苗開打", "2024-10-02"
    )


def test_title_similarity_is_between_zero_and_one():
    assert title_similarity("疾管署籲接種流感疫苗", "疾管署籲接種流感疫苗") == 1.0
    assert 0.0 <= title_similarity("疾管署開記者會", "颱風假消息") < 0.5


# --- the decision ladder ----------------------------------------------------


def existing(**kwargs) -> ExistingRecord:
    base = dict(
        record_id="existing-1",
        canonical_url="https://news.example.tw/a",
        normalized_url="https://news.example.tw/a",
        domain="news.example.tw",
        title="疾管署：流感疫情升溫",
        published_date="2024-01-15",
        content_sha256=None,
    )
    base.update(kwargs)
    return ExistingRecord(**base)


def test_same_canonical_url_merges():
    decision = decide(
        record_id="new-1",
        canonical_url="https://news.example.tw/a",
        normalized_url="https://news.example.tw/a?x=1",
        domain="news.example.tw",
        title="別的標題",
        published_date="2024-01-16",
        content=BODY,
        existing=[existing()],
    )

    assert decision.kind is DuplicateKind.CANONICAL_URL
    assert decision.merge_into == "existing-1"
    assert decision.is_duplicate


def test_same_url_after_tracking_removal_merges():
    decision = decide(
        record_id="new-1",
        canonical_url="https://news.example.tw/a-other-canonical",
        normalized_url="https://news.example.tw/a",
        domain="news.example.tw",
        title=None,
        published_date=None,
        content=None,
        existing=[existing()],
    )

    assert decision.kind is DuplicateKind.NORMALIZED_URL
    assert decision.merge_into == "existing-1"


def test_same_title_and_date_at_a_new_url_merges():
    decision = decide(
        record_id="new-1",
        canonical_url="https://news.example.tw/reprint",
        normalized_url="https://news.example.tw/reprint",
        domain="news.example.tw",
        title="疾管署：流感疫情升溫｜範例新聞網",
        published_date="2024-01-15",
        content=BODY,
        existing=[existing()],
    )

    assert decision.kind is DuplicateKind.TITLE_AND_DATE


def test_identical_body_on_the_same_domain_merges():
    from tw_cdc_influenza_news.urls import content_sha256

    decision = decide(
        record_id="new-1",
        canonical_url="https://news.example.tw/b",
        normalized_url="https://news.example.tw/b",
        domain="news.example.tw",
        title="不同標題",
        published_date="2024-02-01",
        content=BODY,
        existing=[existing(record_id="old", canonical_url="https://news.example.tw/c",
                           normalized_url="https://news.example.tw/c",
                           title="又一個標題", published_date="2024-02-02",
                           content_sha256=content_sha256(BODY))],
    )

    assert decision.kind is DuplicateKind.CONTENT_HASH
    assert decision.merge_into == "old"


# --- syndication ------------------------------------------------------------


def test_wire_copy_on_two_outlets_is_clustered_and_both_are_kept():
    from tw_cdc_influenza_news.urls import content_sha256

    other_outlet = existing(
        record_id="cna-original",
        canonical_url="https://wire.example.tw/news/1",
        normalized_url="https://wire.example.tw/news/1",
        domain="wire.example.tw",
        content_sha256=content_sha256(BODY),
    )

    decision = decide(
        record_id="carrier-copy",
        canonical_url="https://carrier.example.tw/realtime/1",
        normalized_url="https://carrier.example.tw/realtime/1",
        domain="carrier.example.tw",
        title="疾管署：流感疫情升溫",
        published_date="2024-01-16",
        content=BODY,
        existing=[other_outlet],
    )

    assert decision.kind is DuplicateKind.SYNDICATED
    # The decisive assertion: nothing is discarded.
    assert decision.is_duplicate is False
    assert decision.merge_into is None
    assert decision.cluster_id == syndication_cluster_id(BODY)
    assert "cna-original" in decision.possible_duplicates


def test_cluster_id_is_stable_for_the_same_text():
    assert syndication_cluster_id(BODY) == syndication_cluster_id(BODY + "  ")
    assert syndication_cluster_id(BODY) != syndication_cluster_id(BODY + " 追加段落。")


def test_short_shared_text_is_not_treated_as_syndication():
    from tw_cdc_influenza_news.urls import content_sha256

    stub = "疾管署說明。"
    decision = decide(
        record_id="new-1",
        canonical_url="https://carrier.example.tw/x",
        normalized_url="https://carrier.example.tw/x",
        domain="carrier.example.tw",
        title="甲標題",
        published_date="2024-01-16",
        content=stub,
        existing=[
            existing(
                record_id="other",
                domain="wire.example.tw",
                canonical_url="https://wire.example.tw/y",
                normalized_url="https://wire.example.tw/y",
                title="乙標題",
                published_date="2024-01-17",
                content_sha256=content_sha256(stub),
            )
        ],
        syndication_min_chars=400,
    )

    assert decision.kind is not DuplicateKind.SYNDICATED


# --- similar titles are flagged, never deleted ------------------------------


def test_a_very_similar_title_is_only_flagged():
    decision = decide(
        record_id="new-1",
        canonical_url="https://other.example.tw/z",
        normalized_url="https://other.example.tw/z",
        domain="other.example.tw",
        title="疾管署：流感疫情升溫。",
        published_date="2024-03-01",
        content="完全不同的內文。" * 40,
        existing=[existing()],
        title_similarity_threshold=0.9,
    )

    assert decision.kind is DuplicateKind.SIMILAR_TITLE
    assert decision.is_duplicate is False
    assert decision.merge_into is None
    assert decision.possible_duplicates == ["existing-1"]


# --- evidence merging -------------------------------------------------------


def test_one_article_found_by_two_queries_keeps_both_in_order():
    merged = merge_string_lists(
        ["q_quoted_tw_flu"], ["q_bare_full_flu", "q_quoted_tw_flu"], None
    )

    assert merged == ["q_quoted_tw_flu", "q_bare_full_flu"]


def test_provider_lists_merge_without_duplicates():
    merged = merge_string_lists(
        ["serpapi-google-news"], ["google-news-rss"], ["serpapi-google-news"]
    )

    assert merged == ["serpapi-google-news", "google-news-rss"]


def test_registrable_host_drops_www():
    assert registrable_host("https://www.News.Example.tw/a") == "news.example.tw"
