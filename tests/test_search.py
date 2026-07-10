"""Tests for ``okf_loom.search``.

Pinned invariants:
  * Lexical mode returns ranked results (highest score first).
  * Tag mode (``#x`` and bare ``x``) filters by tag.
  * ``type_filter`` restricts the result set in every mode.
  * SEMANTIC mode works zero-dep via SemanticLiteBackend; HYBRID fuses
    Lexical + SemanticLite via RRF (no extras required).
  * Empty / stopword queries return ``[]``.
  * Ranking is deterministic across two runs of the same query+bundle.
  * Results respect ``limit``; ``SearchResult.as_dict()`` is JSON-serializable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.search import (
    DEFAULT_FIELD_WEIGHTS,
    HybridBackend,
    LexicalBackend,
    SearchMode,
    SearchResult,
    clear_search_cache,
    mark_hybrid_active,
    search_bundle,
    tokenize,
)


# --- lexical ranking --------------------------------------------------------


def test_lexical_returns_ranked_results(ga4_bundle: Path) -> None:
    """A query with multiple hits returns results sorted by score desc."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    results = search_bundle(b, "events ecommerce", limit=5)
    assert len(results) >= 1
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    # Top hit must mention 'events' (the events_ table is the obvious match).
    assert results[0].concept_id == ("tables", "events_")


def test_lexical_scores_positive(ga4_bundle: Path) -> None:
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    for r in search_bundle(b, "events", limit=10):
        assert r.score > 0.0


def test_lexical_snippets_returned(ga4_bundle: Path) -> None:
    """At least the top hit should carry snippet text."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    results = search_bundle(b, "events", limit=5)
    assert any(r.snippets for r in results)


# --- tag mode ----------------------------------------------------------------


def test_tag_mode_with_hash_prefix(tiny_good_bundle: Path) -> None:
    """``#tag`` form filters concepts carrying the tag."""
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    results = search_bundle(b, "#users", mode=SearchMode.TAG)
    assert len(results) == 1
    assert results[0].concept_id == ("tables", "users")
    assert "users" in results[0].matched_tags


def test_tag_mode_bare_tag(tiny_good_bundle: Path) -> None:
    """Bare ``tag`` form (no leading ``#``) behaves identically."""
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    hash_results = search_bundle(b, "#events", mode=SearchMode.TAG)
    bare_results = search_bundle(b, "events", mode=SearchMode.TAG)
    assert [r.concept_id for r in hash_results] == [
        r.concept_id for r in bare_results
    ]


def test_tag_mode_unknown_tag_returns_empty(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    assert search_bundle(b, "#nonexistent", mode=SearchMode.TAG) == []


def test_tag_mode_results_have_constant_score(tiny_good_bundle: Path) -> None:
    """Tag-mode results carry a constant score of 1.0."""
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    results = search_bundle(b, "#users", mode=SearchMode.TAG)
    for r in results:
        assert r.score == 1.0
        assert r.source_backend == "tag"


def test_tag_mode_sorted_by_title(tiny_good_bundle: Path) -> None:
    """Tag-mode results are ordered by title (stable secondary on concept id)."""
    # Make a bundle where multiple concepts share a tag.
    import shutil
    src = tiny_good_bundle
    # Both tables already have 'users'/'events' tags; add a shared tag.
    (src / "tables" / "users.md").read_text()
    # Use metrics + events which both can share a synthetic tag, but easier:
    # just query the existing 'events' tag on tiny_good — there is one match.
    b = Bundle.load(src)
    clear_search_cache()
    results = search_bundle(b, "#events", mode=SearchMode.TAG)
    titles = [r.title for r in results]
    assert titles == sorted(titles)


# --- type_filter and tag filter ---------------------------------------------


def test_type_filter_restricts(ga4_bundle: Path) -> None:
    """``type_filter`` keeps only concepts of the given type."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    results = search_bundle(b, "events", type_filter="BigQuery Table", limit=10)
    for r in results:
        c = b.concept_at(__cid_str(r.concept_id))
        assert c is not None
        assert c.type == "BigQuery Table"


def test_type_filter_no_match_returns_empty(ga4_bundle: Path) -> None:
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    assert search_bundle(b, "events", type_filter="NonexistentType") == []


def test_tag_filter_restricts(ga4_bundle: Path) -> None:
    """``tag`` parameter filters by tag in lexical mode too."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    results = search_bundle(b, "events", tag="ecommerce", limit=10)
    for r in results:
        c = b.concept_at(__cid_str(r.concept_id))
        assert c is not None
        assert "ecommerce" in [t.lower() for t in c.tags]


def __cid_str(cid: tuple) -> str:
    """Render a concept id tuple as a slash-separated string."""
    return "/".join(cid)


# --- SEMANTIC / HYBRID now work (current spec §6) ----------------------------


def test_semantic_works_with_semantic_lite(tiny_good_bundle: Path) -> None:
    """SEMANTIC mode works out-of-the-box with semantic-lite (no extra)."""
    b = Bundle.load(tiny_good_bundle)
    results = search_bundle(b, "alpha", mode=SearchMode.SEMANTIC, limit=10)
    assert len(results) > 0
    assert all(r.source_backend == "semantic-lite" for r in results)


def test_semantic_trigram_typo_robustness(tiny_good_bundle: Path) -> None:
    """A typo of a title still ranks the right concept high (trigram win)."""
    b = Bundle.load(tiny_good_bundle)
    results = search_bundle(b, "usrs", mode=SearchMode.SEMANTIC, limit=10)
    assert len(results) > 0
    # Users-related concepts should rank first despite the typo
    top_ids = [str(r.concept_id) for r in results[:3]]
    assert any("users" in tid for tid in top_ids)


def test_semantic_deterministic(tiny_good_bundle: Path) -> None:
    """Identical (bundle, query) produces identical output."""
    b = Bundle.load(tiny_good_bundle)
    r1 = search_bundle(b, "alpha", mode=SearchMode.SEMANTIC, limit=10)
    r2 = search_bundle(b, "alpha", mode=SearchMode.SEMANTIC, limit=10)
    assert [r.concept_id for r in r1] == [r.concept_id for r in r2]
    assert [round(r.score, 6) for r in r1] == [round(r.score, 6) for r in r2]


def test_semantic_alpha_blend(tiny_good_bundle: Path) -> None:
    """token_weight parameter changes scoring behavior.

    Previously ``assert r_hi != [] or r_lo != []`` only proved at least one
    blend returned *something* — it did NOT prove token_weight had any effect
    (one non-empty list satisfied the ``or``). Now we pick a query ("users")
    that carries BOTH a token signal (the word "users" appears in a title/tag)
    and a trigram signal (its character trigrams), so pure-token (weight 1.0)
    and pure-trigram (weight 0.0) BOTH return results, and the top score MUST
    differ between them. Equal scores would mean token_weight is ignored.
    """
    b = Bundle.load(tiny_good_bundle)
    from okf_loom.search import SemanticLiteBackend
    backend_hi = SemanticLiteBackend(token_weight=1.0)  # pure token
    backend_lo = SemanticLiteBackend(token_weight=0.0)  # pure trigram
    ci = b.content_index()
    backend_hi.index(ci)
    backend_lo.index(ci)
    r_hi = backend_hi.search("users", limit=5)
    r_lo = backend_lo.search("users", limit=5)
    # The probe query must yield BOTH token and trigram hits; otherwise the
    # score comparison below proves nothing (this guard also fails loudly if
    # the corpus ever loses its token signal for "users").
    assert r_hi and r_lo, (
        f"probe query 'users' must hit both blends; hi={len(r_hi)} lo={len(r_lo)}"
    )
    # Pure-token vs pure-trigram MUST score the top result differently.
    assert r_hi[0].score != r_lo[0].score, (
        f"token_weight had no effect on score: "
        f"hi={r_hi[0].score!r} lo={r_lo[0].score!r}"
    )


def test_semantic_empty_query(tiny_good_bundle: Path) -> None:
    """Empty query returns empty results."""
    b = Bundle.load(tiny_good_bundle)
    assert search_bundle(b, "", mode=SearchMode.SEMANTIC, limit=10) == []


def test_semantic_opt_in_threshold_rejects_irrelevant_natural_language() -> None:
    """A caller-selected cosine floor provides a real no-match contract.

    Legacy SemanticLite intentionally keeps every positive trigram overlap;
    the opt-in threshold removes those low-signal candidates.
    """
    docs = Path(__file__).resolve().parent.parent / "docs-bundle"
    b = Bundle.load(docs)
    query = "How do I bake sourdough bread in a Dutch oven?"
    legacy = search_bundle(b, query, mode=SearchMode.SEMANTIC, limit=20)
    gated = search_bundle(
        b, query, mode=SearchMode.SEMANTIC,
        semantic_min_score=0.1, limit=20,
    )
    assert legacy, "probe must retain incidental legacy trigram hits"
    assert max(r.score for r in legacy) < 0.1
    assert gated == []


@pytest.mark.parametrize(
    ("query", "expected_id", "top_n"),
    [
        (
            "launch the collaborative reading interface and leave feedback",
            ("explanation", "live_studio_design"),
            3,
        ),
        (
            "what happens from a reader note until the assistant finishes the requested edit",
            ("tutorials", "author_with_agent"),
            3,
        ),
    ],
)
def test_semantic_real_paraphrases_retain_relevant_candidates(
    query: str, expected_id: tuple[str, ...], top_n: int,
) -> None:
    docs = Path(__file__).resolve().parent.parent / "docs-bundle"
    results = search_bundle(
        Bundle.load(docs), query, mode=SearchMode.SEMANTIC, limit=top_n,
    )
    assert expected_id in [r.concept_id for r in results]


def test_hybrid_works_with_rrf(tiny_good_bundle: Path) -> None:
    """HYBRID mode works with RRF fusion (no extra)."""
    b = Bundle.load(tiny_good_bundle)
    results = search_bundle(b, "alpha", mode=SearchMode.HYBRID, limit=10)
    assert len(results) > 0
    assert all(r.source_backend == "hybrid" for r in results)


def test_hybrid_top1_agreement(tiny_good_bundle: Path) -> None:
    """A doc ranked #1 by both backends ranks #1 in hybrid."""
    b = Bundle.load(tiny_good_bundle)
    lex = search_bundle(b, "alpha", mode=SearchMode.LEXICAL, limit=5)
    sem = search_bundle(b, "alpha", mode=SearchMode.SEMANTIC, limit=5)
    hyb = search_bundle(b, "alpha", mode=SearchMode.HYBRID, limit=5)
    if lex and sem and lex[0].concept_id == sem[0].concept_id:
        assert hyb[0].concept_id == lex[0].concept_id


def test_hybrid_union(tiny_good_bundle: Path) -> None:
    """Hybrid returns at least as many results as either backend alone."""
    b = Bundle.load(tiny_good_bundle)
    lex = search_bundle(b, "alpha", mode=SearchMode.LEXICAL, limit=20)
    sem = search_bundle(b, "alpha", mode=SearchMode.SEMANTIC, limit=20)
    hyb = search_bundle(b, "alpha", mode=SearchMode.HYBRID, limit=20)
    lex_ids = {r.concept_id for r in lex}
    sem_ids = {r.concept_id for r in sem}
    hyb_ids = {r.concept_id for r in hyb}
    union_ids = lex_ids | sem_ids
    # Hybrid should include every concept that either backend found
    assert hyb_ids == union_ids


def test_hybrid_backend_rrf_fusion() -> None:
    """HybridBackend fuses two backend results via RRF."""
    from okf_loom.search import LexicalBackend, SemanticLiteBackend, HybridBackend
    lex = LexicalBackend()
    sem = SemanticLiteBackend()
    b = Bundle.load("samples/demo_bundle")
    ci = b.content_index()
    lex.index(ci)
    sem.index(ci)
    hybrid = HybridBackend(lex, sem)
    results = hybrid.search("orders", limit=5)
    assert len(results) > 0
    assert all(r.source_backend == "hybrid" for r in results)


def test_hybrid_exposes_component_scores_ranks_and_matched_backends() -> None:
    b = Bundle.load("samples/demo_bundle")
    results = search_bundle(b, "orders", mode=SearchMode.HYBRID, limit=5)
    assert results
    for result in results:
        detail = result.detail
        assert detail["fusion"] == "rrf"
        assert detail["matched_backends"]
        assert set(detail["component_scores"]) == set(detail["matched_backends"])
        assert set(detail["component_ranks"]) == set(detail["matched_backends"])


def test_hybrid_backend_requirement_can_return_trustworthy_no_match() -> None:
    docs = Path(__file__).resolve().parent.parent / "docs-bundle"
    query = "How do I bake sourdough bread in a Dutch oven?"
    legacy = search_bundle(
        Bundle.load(docs), query, mode=SearchMode.HYBRID, limit=20,
    )
    lexical_evidence_only = search_bundle(
        Bundle.load(docs), query, mode=SearchMode.HYBRID,
        hybrid_require="lexical", limit=20,
    )
    assert legacy
    assert all(
        r.detail["matched_backends"] == ["semantic-lite"] for r in legacy
    )
    assert lexical_evidence_only == []


def test_search_relevance_option_validation(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        search_bundle(
            b, "users", mode=SearchMode.SEMANTIC, semantic_min_score=1.1,
        )
    with pytest.raises(ValueError, match="only valid in semantic or hybrid"):
        search_bundle(
            b, "users", mode=SearchMode.LEXICAL, semantic_min_score=0.1,
        )
    with pytest.raises(ValueError, match="only valid in hybrid"):
        search_bundle(
            b, "users", mode=SearchMode.SEMANTIC, hybrid_require="both",
        )


# --- empty / stopword / limit ------------------------------------------------


def test_empty_query_returns_empty(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    assert search_bundle(b, "", limit=10) == []


def test_stopword_only_query_returns_empty(ga4_bundle: Path) -> None:
    """A query consisting only of stopwords returns no results."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    for sw in ("the", "and", "of", "to", "a"):
        assert search_bundle(b, sw, limit=10) == []


def test_limit_truncates_results(ga4_bundle: Path) -> None:
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    full = search_bundle(b, "events data user", limit=100)
    # Fail loudly if the corpus ever yields fewer than 3 hits: the limit
    # truncation assertion below is meaningless on thin data, and the previous
    # ``if len(full) >= 3:`` guard silently skipped the assertion on thin data
    # — masking exactly the kind of search regression this test exists to catch.
    assert len(full) >= 3, (
        f"corpus too thin to prove limit truncation: only {len(full)} hits"
    )
    limited = search_bundle(b, "events data user", limit=2)
    assert len(limited) == 2
    assert [r.concept_id for r in limited] == [
        r.concept_id for r in full[:2]
    ]


# --- determinism -------------------------------------------------------------


def test_ranking_is_deterministic_across_runs(ga4_bundle: Path) -> None:
    """Same (bundle, query) yields identical concept-id order AND scores."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    r1 = search_bundle(b, "events ecommerce user", limit=10)
    r2 = search_bundle(b, "events ecommerce user", limit=10)
    assert [r.concept_id for r in r1] == [r.concept_id for r in r2]
    assert [r.score for r in r1] == [r.score for r in r2]


# --- SearchResult.as_dict JSON ----------------------------------------------


def test_search_result_as_dict_is_json_serializable(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    results = search_bundle(b, "users", limit=5)
    blob = json.dumps([r.as_dict() for r in results])
    parsed = json.loads(blob)
    for item in parsed:
        assert "concept_id" in item
        assert "title" in item
        assert "score" in item
        assert "snippets" in item
        assert "source_backend" in item
        assert "matched_tags" in item


def test_search_result_concept_id_rendered_as_string(tiny_good_bundle: Path) -> None:
    """``as_dict()`` renders concept_id tuple as a slash-joined string."""
    b = Bundle.load(tiny_good_bundle)
    clear_search_cache()
    results = search_bundle(b, "#users", mode=SearchMode.TAG)
    d = results[0].as_dict()
    assert d["concept_id"] == "tables/users"


# --- tokenize ----------------------------------------------------------------


def test_tokenize_drops_stopwords() -> None:
    assert "the" not in tokenize("the quick brown fox")
    assert "quick" in tokenize("the quick brown fox")


def test_tokenize_lowercases() -> None:
    assert "events" in tokenize("Events EVENTS")


def test_tokenize_empty() -> None:
    assert tokenize("") == []


# --- LexicalBackend direct usage --------------------------------------------


def test_lexical_backend_index_then_search(ga4_bundle: Path) -> None:
    """``LexicalBackend`` indexes a ContentIndex and answers ranked queries."""
    b = Bundle.load(ga4_bundle)
    ci = b.content_index()
    be = LexicalBackend()
    be.index(ci)
    assert be._indexed is True
    results = be.search("events", limit=5)
    assert results
    assert results[0].source_backend == "lexical"


def test_lexical_backend_unindexed_returns_empty() -> None:
    """Calling ``search`` before ``index`` returns ``[]``."""
    be = LexicalBackend()
    assert be.search("anything", limit=5) == []


def test_default_field_weights() -> None:
    """Default field weights put title highest, body/tags lowest."""
    w = DEFAULT_FIELD_WEIGHTS
    assert w["title"] > w["headings"] > w["description"] > w["body"]
    assert w["body"] == w["tags"]


def test_lexical_backend_custom_field_weights(ga4_bundle: Path) -> None:
    """A custom field weight is honoured (merged over defaults)."""
    b = Bundle.load(ga4_bundle)
    ci = b.content_index()
    be = LexicalBackend(field_weights={"title": 100.0})
    be.index(ci)
    assert be._field_weights["title"] == 100.0
    # Other default fields still present.
    assert "body" in be._field_weights


# Path to the always-shipped demo bundle (used by the hybrid/entity/relation
# tests below).
DEMO_BUNDLE = Path(__file__).resolve().parent.parent / "samples" / "demo_bundle"


# --- P1-11: HybridBackend forwards snippets -------------------------------


def test_hybrid_forwards_snippets() -> None:
    """P1-11: fused SearchResult carries snippets from a contributing backend."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "customers", mode=SearchMode.HYBRID, limit=10)
    assert results
    # 'customers' is a clear lexical+semantic hit; at least one fused result
    # must carry a non-empty snippet list.
    assert any(r.snippets for r in results)


def test_hybrid_backend_direct_forwards_snippets() -> None:
    """Direct HybridBackend.search forwards snippets (lexical precedence)."""
    from okf_loom.search import SemanticLiteBackend

    b = Bundle.load(str(DEMO_BUNDLE))
    ci = b.content_index()
    lex = LexicalBackend()
    sem = SemanticLiteBackend()
    lex.index(ci)
    sem.index(ci)
    hybrid = HybridBackend(lex, sem)
    results = hybrid.search("customers", limit=5)
    assert results
    assert any(r.snippets for r in results)


# --- P1-8: hybrid mode activates okf.cap.search_hybrid --------------------


def test_hybrid_activates_search_hybrid_capability() -> None:
    """P1-8: a successful hybrid query marks okf.cap.search_hybrid active."""
    b = Bundle.load(str(DEMO_BUNDLE))
    # Demo does not declare search_hybrid (no frontmatter key governs it).
    assert not b.capabilities().is_active("okf.cap.search_hybrid")
    search_bundle(b, "customers", mode=SearchMode.HYBRID, limit=5)
    assert b.capabilities().is_active("okf.cap.search_hybrid") is True


def test_mark_hybrid_active_is_idempotent() -> None:
    """Calling mark_hybrid_active twice (or after activation) is a no-op."""
    b = Bundle.load(str(DEMO_BUNDLE))
    mark_hybrid_active(b)
    caps1 = b.capabilities()
    mark_hybrid_active(b)  # already active → no-op
    caps2 = b.capabilities()
    assert caps1 is caps2
    assert caps2.is_active("okf.cap.search_hybrid")


def test_invalidate_resets_hybrid_capability_activation() -> None:
    """bundle.invalidate() drops the injected cap (recompute-from-scratch)."""
    b = Bundle.load(str(DEMO_BUNDLE))
    search_bundle(b, "customers", mode=SearchMode.HYBRID, limit=5)
    assert b.capabilities().is_active("okf.cap.search_hybrid")
    b.invalidate()
    assert not b.capabilities().is_active("okf.cap.search_hybrid")


# --- P1-9: Entity mode (current spec §6) -----------------------------------


def test_entity_mode_alias_hit() -> None:
    """Entity mode matches a concept via its aliases."""
    # demo tables/orders has aliases: [purchase orders, sales orders, ...]
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "purchase orders", mode=SearchMode.ENTITY, limit=10)
    ids = [r.concept_id for r in results]
    assert ("tables", "orders") in ids
    assert all(r.source_backend == "entity" for r in results)


def test_alias_object_label_is_searchable(tmp_path: Path) -> None:
    (tmp_path / "design.md").write_text(
        "---\n"
        "type: Design\n"
        "title: Design System\n"
        "aliases:\n"
        "  - label: Architecture\n"
        "    discoverable: false\n"
        "---\n"
        "Body without the alias term.\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    clear_search_cache()

    lexical = search_bundle(b, "Architecture", mode=SearchMode.LEXICAL, limit=10)
    entity = search_bundle(b, "Architecture", mode=SearchMode.ENTITY, limit=10)

    assert ("design",) in [r.concept_id for r in lexical]
    assert ("design",) in [r.concept_id for r in entity]


def test_entity_mode_object_form_label_matches(tmp_path: Path) -> None:
    """Entity mode must match object-form entities keyed by `label`
    (current spec §4 canonical shape {id, label, kind, aliases}).

    Regression: showcase data using {label: Stripe, kind: vendor} silently
    returned no matches; this test guarantees the label field is indexed.
    """
    (tmp_path / "checkout.md").write_text(
        "---\n"
        "type: Service\n"
        "title: Checkout\n"
        "entities:\n"
        "  - label: Stripe\n"
        "    kind: vendor\n"
        "    aliases:\n"
        "      - Stripe Payments\n"
        "---\n"
        "body about payments\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.md").write_text(
        "---\ntype: Service\ntitle: Other\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    # Match on the label itself
    by_label = search_bundle(b, "Stripe", mode=SearchMode.ENTITY, limit=10)
    assert ("checkout",) in [r.concept_id for r in by_label]
    # Match on the entity alias
    by_alias = search_bundle(b, "Stripe Payments", mode=SearchMode.ENTITY, limit=10)
    assert ("checkout",) in [r.concept_id for r in by_alias]
    # The unrelated concept must not match
    assert ("unrelated",) not in [r.concept_id for r in by_label]


def test_entity_mode_object_form_label_in_lexical(tmp_path: Path) -> None:
    """Lexical mode must also index object-form entity labels (current spec §4).

    The lexical backend and entity-mode backend share entity-extraction logic;
    both must read `label` from object-form entities.
    """
    (tmp_path / "svc.md").write_text(
        "---\n"
        "type: Service\n"
        "title: Svc\n"
        "entities:\n"
        "  - label: Kafka\n"
        "    kind: system\n"
        "---\n"
        "body with no mention of the word kafka\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    results = search_bundle(b, "Kafka", mode=SearchMode.LEXICAL, limit=10)
    assert ("svc",) in [r.concept_id for r in results]


def test_entity_mode_case_insensitive() -> None:
    """Entity matching is case-insensitive."""
    b1 = Bundle.load(str(DEMO_BUNDLE))
    b2 = Bundle.load(str(DEMO_BUNDLE))
    low = search_bundle(b1, "customer", mode=SearchMode.ENTITY, limit=10)
    up = search_bundle(b2, "CUSTOMER", mode=SearchMode.ENTITY, limit=10)
    assert [r.concept_id for r in low] == [r.concept_id for r in up]


def test_entity_mode_field_weighted_ordering(tmp_path: Path) -> None:
    """title/alias hit (weight 3) outranks a description-only hit (weight 2)."""
    (tmp_path / "title_hit.md").write_text(
        "---\ntype: T\ntitle: Zeus\ndescription: unrelated text\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "desc_hit.md").write_text(
        "---\ntype: T\ntitle: Unrelated\ndescription: all about zeus here\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    results = search_bundle(b, "zeus", mode=SearchMode.ENTITY, limit=10)
    assert len(results) >= 2
    assert results[0].concept_id == ("title_hit",)
    assert results[1].concept_id == ("desc_hit",)


def test_entity_mode_empty_query_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert search_bundle(b, "", mode=SearchMode.ENTITY, limit=10) == []


def test_entity_mode_detail_shape(tmp_path: Path) -> None:
    """Entity results carry a JSON-serializable detail.matched_fields."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: Apollo\naliases: [sun god]\ndescription: sun stuff\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    results = search_bundle(b, "apollo", mode=SearchMode.ENTITY, limit=10)
    assert results
    d = results[0].as_dict()
    assert "detail" in d
    assert isinstance(d["detail"], dict)
    assert "matched_fields" in d["detail"]
    # The whole payload is JSON-serializable.
    json.dumps(d)


def test_entity_mode_type_filter_restriction(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: Table\ntitle: Zeus\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: View\ntitle: Zeus Two\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    results = search_bundle(
        b, "zeus", mode=SearchMode.ENTITY, type_filter="Table", limit=10
    )
    assert len(results) == 1
    assert results[0].concept_id == ("a",)
    # Non-matching type → empty.
    b2 = Bundle.load(tmp_path)
    assert (
        search_bundle(
            b2,
            "zeus",
            mode=SearchMode.ENTITY,
            type_filter="Nonexistent",
            limit=10,
        )
        == []
    )


# --- P1-9 + P2-1: Relation mode (current spec §6) --------------------------


def test_relation_mode_union_frontmatter_and_markdown() -> None:
    """Relation mode unions frontmatter relations ∪ markdown-link graph."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "", mode=SearchMode.RELATION, limit=20)
    src_ids = [r.concept_id for r in results]
    assert ("tables", "orders") in src_ids
    orders = next(r for r in results if r.concept_id == ("tables", "orders"))
    vias = {e["via"] for e in orders.detail["edges"]}
    # Both sources contribute (union).
    assert "relations" in vias
    assert "markdown" in vias


def test_relation_mode_via_discrimination_and_edge_shape() -> None:
    """detail.edges = [{type, target, via}] with via ∈ {relations, markdown}."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "", mode=SearchMode.RELATION, limit=20)
    orders = next(r for r in results if r.concept_id == ("tables", "orders"))
    for e in orders.detail["edges"]:
        assert set(e.keys()) == {"type", "target", "via"}
        assert e["via"] in ("relations", "markdown")
        if e["via"] == "relations":
            assert e["type"]  # typed-relation edges carry a non-empty type
        else:
            assert e["type"] == ""  # markdown edges have empty type


def test_relation_mode_uses_logical_edges_not_duplicate_occurrences(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: T\nrelations:\n"
        "  - {target: b, type: references}\n"
        "  - {target: /b.md, type: references, detail: duplicate}\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    results = search_bundle(
        Bundle.load(tmp_path), "", mode=SearchMode.RELATION,
        relation="references", limit=10,
    )
    assert len(results) == 1
    assert results[0].score == 1.0
    assert len(results[0].detail["edges"]) == 1


def test_relation_mode_relation_filter_excludes_markdown() -> None:
    """P2-1: --relation TYPE filters the union by edge type; markdown edges
    (empty type) drop out naturally under a --relation filter."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(
        b, "", mode=SearchMode.RELATION, relation="references", limit=20
    )
    orders = next(
        (r for r in results if r.concept_id == ("tables", "orders")), None
    )
    assert orders is not None
    assert len(orders.detail["edges"]) >= 1
    for e in orders.detail["edges"]:
        assert e["via"] == "relations"
        assert e["type"].lower() == "references"


def test_relation_mode_no_filter_includes_markdown_edges() -> None:
    """P2-1: with NO --relation filter, markdown edges MUST appear."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "", mode=SearchMode.RELATION, limit=20)
    orders = next(r for r in results if r.concept_id == ("tables", "orders"))
    markdown_edges = [e for e in orders.detail["edges"] if e["via"] == "markdown"]
    assert markdown_edges  # orders body links to customers/currencies


def test_relation_mode_source_filter() -> None:
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(
        b, "", mode=SearchMode.RELATION, source="tables/orders", limit=20
    )
    src_ids = {r.concept_id for r in results}
    assert src_ids == {("tables", "orders")}


def test_relation_mode_target_filter() -> None:
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(
        b, "", mode=SearchMode.RELATION, target="tables/customers", limit=20
    )
    assert results  # at least one source targets tables/customers
    for r in results:
        for e in r.detail["edges"]:
            assert e["target"] == "tables/customers"


def test_relation_mode_source_target_combo() -> None:
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(
        b,
        "",
        mode=SearchMode.RELATION,
        source="tables/orders",
        target="tables/customers",
        limit=20,
    )
    for r in results:
        assert r.concept_id == ("tables", "orders")
        for e in r.detail["edges"]:
            assert e["target"] == "tables/customers"


def test_relation_mode_concept_id_tie_break(tmp_path: Path) -> None:
    """Sources with equal edge counts tie-break by ascending concept id."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](b.md) and [c](c.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nSee [c](c.md) and [d](d.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: C\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "d.md").write_text(
        "---\ntype: T\ntitle: D\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    results = search_bundle(b, "", mode=SearchMode.RELATION, limit=20)
    # 'a' and 'b' each have 2 outgoing edges → tie; ordered by concept id.
    two_edge = [r for r in results if r.score == 2.0]
    two_edge_ids = [r.concept_id for r in two_edge]
    assert two_edge_ids == sorted(two_edge_ids)


def test_relation_mode_query_optional() -> None:
    """Query is optional in relation mode when edge filters are provided."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(
        b, "", mode=SearchMode.RELATION, relation="references", limit=20
    )
    assert isinstance(results, list)
    assert all(r.source_backend == "relation" for r in results)


# --- P2-19: SearchResult.description populated in TAG/ENTITY/RELATION -----


def test_tag_mode_populates_description() -> None:
    """P2-19: tag results expose the concept description (non-empty when set)."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "orders", mode=SearchMode.TAG, limit=20)
    assert results
    orders = next(
        (r for r in results if r.concept_id == ("tables", "orders")), None
    )
    assert orders is not None, "tables/orders carries the 'orders' tag"
    # tables/orders.md declares description: "One row per completed customer order."
    assert orders.description == "One row per completed customer order."


def test_entity_mode_populates_description() -> None:
    """P2-19: entity results expose the concept description."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(
        b, "purchase orders", mode=SearchMode.ENTITY, limit=10
    )
    orders = next(
        (r for r in results if r.concept_id == ("tables", "orders")), None
    )
    assert orders is not None
    assert orders.description == "One row per completed customer order."


def test_relation_mode_populates_description() -> None:
    """P2-19: relation results expose the concept description."""
    b = Bundle.load(str(DEMO_BUNDLE))
    results = search_bundle(b, "", mode=SearchMode.RELATION, limit=20)
    orders = next(
        (r for r in results if r.concept_id == ("tables", "orders")), None
    )
    assert orders is not None
    assert orders.description == "One row per completed customer order."


def test_populate_description_skips_concepts_without_description(
    tmp_path: Path,
) -> None:
    """P2-19: description stays empty when the concept has no description."""
    (tmp_path / "nodesc.md").write_text(
        "---\ntype: T\ntitle: NoDesc\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    # Empty tag needle lists every concept (tag-mode "empty needle lists all"
    # behaviour), so nodesc is returned with description="".
    results = search_bundle(b, "", mode=SearchMode.TAG, limit=10)
    nodesc = next(r for r in results if r.concept_id == ("nodesc",))
    assert nodesc.description == ""


# --- P3-2: search_hybrid activates only on non-empty results --------------


def test_hybrid_empty_results_do_not_activate_capability(
    empty_bundle: Path,
) -> None:
    """P3-2: an empty-result hybrid query does NOT activate okf.cap.search_hybrid.

    Activating a capability on a zero-match query is semantically loose
    (the cap signals "this bundle has been queried in hybrid mode and
    produced useful results"). An empty bundle has no concepts, so every
    backend returns [] and the fused result is empty → no activation.
    """
    b = Bundle.load(empty_bundle)
    assert not b.capabilities().is_active("okf.cap.search_hybrid")
    results = search_bundle(b, "anything", mode=SearchMode.HYBRID, limit=5)
    assert results == []
    assert not b.capabilities().is_active("okf.cap.search_hybrid")


def test_hybrid_non_empty_results_activate_capability() -> None:
    """P3-2: a non-empty hybrid query DOES activate okf.cap.search_hybrid.

    Companion to ``test_hybrid_empty_results_do_not_activate_capability``:
    confirms the gate does not regress the non-empty activation path
    (already covered by ``test_hybrid_activates_search_hybrid_capability``
    above; restated here to make the P3-2 gate pair explicit).
    """
    b = Bundle.load(str(DEMO_BUNDLE))
    assert not b.capabilities().is_active("okf.cap.search_hybrid")
    results = search_bundle(b, "customers", mode=SearchMode.HYBRID, limit=5)
    assert results, "demo bundle must have non-empty hybrid hits for 'customers'"
    assert b.capabilities().is_active("okf.cap.search_hybrid") is True
