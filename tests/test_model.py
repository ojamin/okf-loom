"""Tests for ``okf_loom.model``.

Pinned invariants:
  * ``Bundle.load`` populates concepts/indexes/logs and records soft
    warnings instead of raising on missing ``type``, duplicate ids, bad
    segments, and non-root index.md frontmatter.
  * ``Bundle.graph()`` produces the correct edge set on a known fixture.
  * ``Bundle.content_index()`` indexes by type and tag.
  * ``Concept`` accessors handle missing frontmatter keys gracefully.
  * SPEC §10: ``Bundle.has_wikilinks`` reflects ``[[...]]`` in concept
    bodies, is memoized, is cleared by ``Bundle.invalidate()``, and drives
    auto-activation of ``okf.cap.wikilinks`` via ``Bundle.capabilities()``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom import Bundle, Concept, LoadWarning
from okf_loom.model import ContentIndex, Graph, IndexFile, LogFile


# --- Bundle.load: happy path -------------------------------------------------


def test_load_tiny_good_populates_all(tiny_good_bundle: Path) -> None:
    """``Bundle.load`` parses concepts, indexes, and logs from tiny_good."""
    b = Bundle.load(tiny_good_bundle)
    assert len(b.concepts) == 4
    assert len(b.indexes) == 3  # root + tables/ + references/
    assert len(b.logs) == 1

    ids = sorted(b.concepts.keys())
    assert ids == [
        ("references", "metrics"),
        ("references", "orphan"),
        ("tables", "events"),
        ("tables", "users"),
    ]


def test_load_root_index_records_okf_version(tiny_good_bundle: Path) -> None:
    """The bundle-root index.md's ``okf_version`` is surfaced on Bundle."""
    b = Bundle.load(tiny_good_bundle)
    assert b.okf_version == "0.1"
    root = b.root_index()
    assert root is not None
    assert root.is_root is True


def test_load_concepts_have_correct_type(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    assert b.concept_at("tables/users").type == "BigQuery Table"
    assert b.concept_at("references/metrics").type == "Reference"


def test_load_types_and_tags(tiny_good_bundle: Path) -> None:
    """``Bundle.types()`` / ``Bundle.tags()`` aggregate from concepts."""
    b = Bundle.load(tiny_good_bundle)
    assert b.types() == {"BigQuery Table", "Reference"}
    assert "users" in b.tags()
    assert "events" in b.tags()
    assert "metrics" in b.tags()


def test_len_and_iter(tiny_good_bundle: Path) -> None:
    """``len(bundle)`` is the concept count; iterating yields Concept objects."""
    b = Bundle.load(tiny_good_bundle)
    assert len(b) == 4
    for c in b:
        assert isinstance(c, Concept)


def test_bundle_name_defaults_to_dir_name(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    assert b.name == "tiny_good"


def test_bundle_name_override(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle, name="custom")
    assert b.name == "custom"


# --- Bundle.load: soft warnings ---------------------------------------------


def test_missing_type_becomes_warning_not_exception(tmp_path: Path) -> None:
    """SPEC §9: missing ``type`` is permissive — recorded as a load warning."""
    (tmp_path / "a.md").write_text("---\ntitle: No Type\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    assert len(b.concepts) == 1  # file still loaded
    codes = [w.code for w in b.warnings]
    assert "concept.missing_type" in codes


def test_duplicate_concept_id_warning(tmp_path: Path) -> None:
    """Two files mapping to the same concept id produce a duplicate warning.

    We trigger this by using a symlink-like trick: write two .md files that
    resolve to the same relative path via case-insensitive behaviour is
    filesystem-dependent, so instead we use Bundle's in-memory constructor
    to inject the duplicate directly via _load_concept twice.
    """
    b = Bundle(tmp_path)
    # First concept at ('a',)
    b._load_concept(
        tmp_path / "a.md",
        Path("a.md"),
        "---\ntype: T\ntitle: A\n---\nbody\n",
    )
    # Second concept at the same id (a different physical file that
    # resolves to the same concept id is unusual; we emulate it by reusing
    # the load path).
    b._load_concept(
        tmp_path / "a.md",
        Path("a.md"),
        "---\ntype: T\ntitle: A2\n---\nbody2\n",
    )
    codes = [w.code for w in b.warnings]
    assert "concept.duplicate_id" in codes


def test_bad_concept_id_segment_warning(tmp_path: Path) -> None:
    """A file whose path yields an invalid concept id is skipped with a warning."""
    bad_dir = tmp_path / "bad dir"  # space -> invalid segment
    bad_dir.mkdir()
    (bad_dir / "x.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    # The bad file is skipped (no concept loaded).
    assert len(b.concepts) == 0
    codes = [w.code for w in b.warnings]
    assert "concept.bad_id" in codes


def test_concept_parse_failed_warning(tmp_path: Path) -> None:
    """Malformed YAML frontmatter records a parse_failed warning, not an exception."""
    (tmp_path / "broken.md").write_text(
        "---\nbad: yaml: with: colons\n- a\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert len(b.concepts) == 0
    codes = [w.code for w in b.warnings]
    assert "concept.parse_failed" in codes


def test_non_root_index_frontmatter_warning(tmp_path: Path) -> None:
    """A non-root ``index.md`` carrying frontmatter SHOULD warn (SPEC §6)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "index.md").write_text(
        "---\ntype: Index\ntitle: Sub Index\n---\n# Sub index\n", encoding="utf-8"
    )
    (sub / "concept.md").write_text(
        "---\ntype: T\ntitle: C\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    codes = [w.code for w in b.warnings]
    assert "index.unexpected_frontmatter" in codes


def test_load_warnings_are_loadwarning_objects(tiny_bad_bundle: Path) -> None:
    """Every warning is a ``LoadWarning`` dataclass with code+message."""
    b = Bundle.load(tiny_bad_bundle)
    assert b.warnings, "tiny_bad should produce warnings"
    for w in b.warnings:
        assert isinstance(w, LoadWarning)
        assert isinstance(w.code, str) and w.code
        assert isinstance(w.message, str) and w.message


# --- Bundle.graph: known edges on tiny_good ---------------------------------


def test_graph_edges_on_tiny_good(tiny_good_bundle: Path) -> None:
    """``Bundle.graph()`` produces the expected edges on tiny_good.

    tiny_good exercises BOTH link forms:
      - metrics -> users (absolute /tables/users.md)
      - events  -> users (relative users.md)
      - users   -> metrics (absolute /references/metrics.md)
      - users   -> events (relative events.md)
    """
    b = Bundle.load(tiny_good_bundle)
    g = b.graph()
    # The edge list includes both body-link edges AND typed-relation edges
    # (okf.cap.typed_relations auto-activates because the fixture's users.md
    # has a `relations:` frontmatter key). users.md has a typed relation
    # entry {target: references/metrics, type: references} which adds one
    # extra edge: users -> metrics (absolute, from relation).
    edge_pairs = sorted(
        (e.source, e.target, e.form) for e in g.edges if e.target is not None
    )
    expected = sorted([
        (("references", "metrics"), ("tables", "users"), "absolute"),
        (("tables", "events"), ("tables", "users"), "relative"),
        (("tables", "users"), ("references", "metrics"), "absolute"),
        # The typed-relation edge from users -> metrics:
        (("tables", "users"), ("references", "metrics"), "absolute"),
        (("tables", "users"), ("tables", "events"), "relative"),
    ])
    assert edge_pairs == expected
    assert g.unresolved == []
    # External (anchor-only, http) counts:
    assert isinstance(g.external, list)


def test_graph_typed_relation_link_form_target_resolves(tmp_path: Path) -> None:
    """A typed-relation target written in SPEC §5.1 absolute-link form
    (``/tables/subscriptions.md``) must resolve to the same graph node as
    the bare concept-id form.

    Regression: ``concept_id_from_str`` did not strip the trailing ``.md``,
    so relation targets written as link paths produced a phantom node id
    ``('tables', 'subscriptions.md')`` that never matched a real concept.
    The cytoscape graph view then crashed with "nonexistant target".
    """
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "orders.md").write_text(
        "---\n"
        "type: Table\n"
        "title: Orders\n"
        "relations:\n"
        "  - type: derived_from\n"
        "    target: /subs/active.md\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    (tmp_path / "subs").mkdir()
    (tmp_path / "subs" / "active.md").write_text(
        "---\ntype: Table\ntitle: Active Subs\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    g = b.graph()
    # The relation edge must resolve (not land in unresolved).
    rel_edges = [
        e for e in g.edges
        if e.source == ("orders",) and e.target == ("subs", "active")
    ]
    assert len(rel_edges) == 1, (
        f"expected 1 resolved relation edge, got {rel_edges}; "
        f"unresolved={g.unresolved}"
    )
    assert rel_edges[0].label == "derived_from"
    # Nothing should be unresolved for this bundle.
    assert g.unresolved == []


def test_graph_logical_edges_dedupe_by_source_type_target(tmp_path: Path) -> None:
    """Raw occurrences survive, while logical consumers get stable edges."""
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: T\n"
        "relations:\n"
        "  - {target: b, type: references, detail: first}\n"
        "  - {target: /b.md, type: references, detail: duplicate}\n"
        "  - {target: b, type: depends_on}\n"
        "---\n"
        "See [B](b.md) and [B again](b.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    graph = Bundle.load(tmp_path).graph()
    occurrences = [e for e in graph.edges if e.source == ("a",)]
    logical = [e for e in graph.logical_edges() if e.source == ("a",)]
    assert len(occurrences) == 5
    assert len(logical) == 3
    assert [(e.origin, e.logical_type) for e in logical] == [
        ("markdown", ""),
        ("relation", "references"),
        ("relation", "depends_on"),
    ]


def test_graph_unresolved_collected(tiny_bad_bundle: Path) -> None:
    """Broken-link targets land in ``graph.unresolved``."""
    b = Bundle.load(tiny_bad_bundle)
    g = b.graph()
    assert any(
        l.target == ("tables", "ghost") or "ghost" in l.target_raw
        for l in g.unresolved
    )


def test_graph_backlinks_and_outlinks(tiny_good_bundle: Path) -> None:
    """``backlinks``/``outlinks`` are consistent with the edge set.

    Note: when ``okf.cap.typed_relations`` is active (auto-activated by the
    ``relations:`` frontmatter key in the fixture), typed-relation edges are
    folded into the graph alongside body-link edges. So a concept that has
    BOTH a markdown link AND a typed relation to the same target appears
    twice in the edge list.
    """
    b = Bundle.load(tiny_good_bundle)
    g = b.graph()
    users = ("tables", "users")
    # metrics -> users (absolute) and events -> users (relative) link IN.
    # Plus users has a typed relation TO metrics, which means metrics has
    # a relation backlink FROM users — but that's an outlink from users,
    # not an inlink to users. So the inlinks to users are still just the
    # body links from metrics and events.
    back_sources = sorted(set(l.source for l in g.backlinks(users)))
    assert back_sources == [("references", "metrics"), ("tables", "events")]
    # users links OUT to metrics (body link) + events (body link) +
    # metrics (typed relation). Deduped by target.
    out_targets = sorted(set(l.target for l in g.outlinks(users)))
    assert out_targets == [("references", "metrics"), ("tables", "events")]


def test_graph_is_memoized(tiny_good_bundle: Path) -> None:
    """``Bundle.graph()`` returns the same object on subsequent calls."""
    b = Bundle.load(tiny_good_bundle)
    assert b.graph() is b.graph()


def test_graph_external_links_classified(tmp_path: Path) -> None:
    """http/https/mailto links land in ``graph.external``."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\n"
        "[ext](https://example.com) [mail](mailto:x@y) [int](/b.md)\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    g = b.graph()
    assert len(g.external) == 2
    assert len(g.edges) == 1


# --- Bundle.content_index ----------------------------------------------------


def test_content_index_indexes_by_type(tiny_good_bundle: Path) -> None:
    """``content_index().by_type`` groups concepts by their ``type``."""
    b = Bundle.load(tiny_good_bundle)
    ci = b.content_index()
    table_cids = sorted(c.id for c in ci.concepts_of_type("BigQuery Table"))
    ref_cids = sorted(c.id for c in ci.concepts_of_type("Reference"))
    assert table_cids == [("tables", "events"), ("tables", "users")]
    assert ref_cids == [("references", "metrics"), ("references", "orphan")]


def test_content_index_indexes_by_tag(tiny_good_bundle: Path) -> None:
    """``content_index().by_tag`` groups concepts by tag."""
    b = Bundle.load(tiny_good_bundle)
    ci = b.content_index()
    users_tag = ci.concepts_with_tag("users")
    assert len(users_tag) == 1
    assert users_tag[0].id == ("tables", "users")


def test_content_index_unknown_type_returns_empty(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    ci = b.content_index()
    assert ci.concepts_of_type("Nonexistent") == []
    assert ci.concepts_with_tag("nonexistent") == []


def test_content_index_is_memoized(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    assert b.content_index() is b.content_index()


def test_content_index_corpus_text(tiny_good_bundle: Path) -> None:
    """``corpus_text`` has an entry per concept."""
    b = Bundle.load(tiny_good_bundle)
    ci = b.content_index()
    assert set(ci.corpus_text.keys()) == set(b.concepts.keys())
    # Each entry is non-empty (search_text concatenates title at minimum).
    for cid, text in ci.corpus_text.items():
        assert isinstance(text, str)
        assert text.strip()


# --- Concept accessors -------------------------------------------------------


def test_concept_accessors_handle_missing_keys(tmp_path: Path) -> None:
    """A concept with minimal frontmatter still has safe accessor behaviour."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nbody without much\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    c = b.concept_at("a")
    assert c.type == "T"
    # Missing title -> falls back to last path segment.
    assert c.title == "a"
    # Missing description -> empty string.
    assert c.description == ""
    # Missing resource -> None.
    assert c.resource is None
    # Missing tags -> empty list.
    assert c.tags == []
    # Missing timestamp -> None.
    assert c.timestamp is None


def test_concept_title_falls_back_to_filename(tmp_path: Path) -> None:
    """SPEC §4.1: title may be omitted; falls back to filename with _->space."""
    (tmp_path / "user_profile.md").write_text(
        "---\ntype: T\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert b.concept_at("user_profile").title == "user profile"


def test_concept_tags_scalar_tolerated(tmp_path: Path) -> None:
    """Some producers emit a scalar tag; the accessor coerces to a list."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntags: just_one\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert b.concept_at("a").tags == ["just_one"]


def test_concept_schema_section(tiny_good_bundle: Path) -> None:
    """``schema_section`` returns the body of a ``# Schema`` heading."""
    b = Bundle.load(tiny_good_bundle)
    users = b.concept_at("tables/users")
    schema = users.schema_section
    assert schema is not None
    assert "id" in schema
    assert "FK" in schema  # FK reference text is present


def test_concept_schema_section_none_when_absent(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    orphan = b.concept_at("references/orphan")
    assert orphan.schema_section is None


def test_concept_examples_and_citations_sections(tiny_good_bundle: Path) -> None:
    """``citations_section`` returns ``# Citations`` body, etc."""
    b = Bundle.load(tiny_good_bundle)
    users = b.concept_at("tables/users")
    cites = users.citations_section
    assert cites is not None
    assert "https://example.com/citations/users" in cites
    # Examples absent in tiny_good.
    assert users.examples_section is None


def test_concept_search_text_caches(tiny_good_bundle: Path) -> None:
    """``Concept.search_text`` memoizes on the instance."""
    b = Bundle.load(tiny_good_bundle)
    c = b.concept_at("tables/users")
    t1 = c.search_text()
    t2 = c.search_text()
    assert t1 == t2
    assert getattr(c, "_search_text_cache", None) is not None


def test_concept_links_resolved_against_bundle(tiny_good_bundle: Path) -> None:
    """``Concept.links`` returns Link objects with resolved targets."""
    b = Bundle.load(tiny_good_bundle)
    users = b.concept_at("tables/users")
    links = users.links(bundle_root=b.root)
    targets = sorted(l.target for l in links if l.target is not None)
    assert ("references", "metrics") in targets
    assert ("tables", "events") in targets


# --- IndexFile / LogFile shapes ---------------------------------------------


def test_index_file_entries(tiny_good_bundle: Path) -> None:
    """``IndexFile.entries`` parses ``* [label](target) - desc`` rows."""
    b = Bundle.load(tiny_good_bundle)
    tables_idx = b.indexes[Path("tables/index.md")]
    rows = tables_idx.entries()
    labels = sorted(label for (label, _target, _desc) in rows)
    assert labels == ["Events", "Users"]


def test_log_file_entries(tiny_good_bundle: Path) -> None:
    """``LogFile.entries`` parses date-grouped blocks."""
    b = Bundle.load(tiny_good_bundle)
    log = list(b.logs.values())[0]
    assert isinstance(log, LogFile)
    assert len(log.entries) == 2
    assert log.entries[0].date == "2026-06-01"  # newest first
    assert log.entries[1].date == "2026-05-15"
    assert log.latest_date == "2026-06-01"


def test_index_file_is_root_flag(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    assert b.indexes[Path("index.md")].is_root is True
    assert b.indexes[Path("tables/index.md")].is_root is False


# --- Empty / missing directories --------------------------------------------


def test_load_empty_bundle(empty_bundle: Path) -> None:
    """An empty directory loads as a bundle with zero concepts."""
    b = Bundle.load(empty_bundle)
    assert len(b.concepts) == 0
    assert len(b.indexes) == 0
    assert len(b.logs) == 0


def test_load_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Bundle.load(tmp_path / "does_not_exist")


def test_load_file_not_directory_raises(tmp_path: Path) -> None:
    f = tmp_path / "notadir.md"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        Bundle.load(f)


# --- Bundle.has_wikilinks (SPEC §10) ----------------------------------------


def test_has_wikilinks_true_when_wikilink_present(tmp_path: Path) -> None:
    """A bundle whose body contains ``[[...]]`` reports has_wikilinks True."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [[b]] for more details.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert b.has_wikilinks is True


def test_has_wikilinks_true_for_labeled_wikilink(tmp_path: Path) -> None:
    """The ``[[target|Label]]`` form also activates has_wikilinks."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nSee [[b|the B concept]].\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    assert b.has_wikilinks is True


def test_has_wikilinks_false_when_no_wikilink(tmp_path: Path) -> None:
    """A bundle with only standard markdown links reports has_wikilinks False."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](/b.md) for more.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    assert b.has_wikilinks is False


def test_has_wikilinks_false_on_empty_bundle(empty_bundle: Path) -> None:
    """An empty bundle has no wikilinks (any() of empty iterable is False)."""
    b = Bundle.load(empty_bundle)
    assert b.has_wikilinks is False


def test_has_wikilinks_is_memoized(tmp_path: Path) -> None:
    """``has_wikilinks`` caches its result on the Bundle instance."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\n[[b]]\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    # First call computes and caches.
    assert b.has_wikilinks is True
    assert b._has_wikilinks_cache is True
    # Second call returns the cached value (same identity is fine for a bool).
    assert b.has_wikilinks is True


def test_has_wikilinks_invalidated_on_invalidate(tmp_path: Path) -> None:
    """``Bundle.invalidate()`` clears the has_wikilinks cache so mutations
    to concept bodies are reflected on the next read.

    This is the cache-invalidation contract for the new property: a stale
    True/False after a body mutation would be a silent bug, so the test
    proves both the stale-before-invalidate state and the fresh recompute.
    """
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nplain body, no wikilink\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    # Initial scan: no wikilink.
    assert b.has_wikilinks is False
    assert b._has_wikilinks_cache is False

    # Mutate the body in-place (simulates what apply_plan / direct edits do
    # without going through Bundle.load again).
    concept = b.concept_at("a")
    assert concept is not None
    concept.body = "now with [[target]] wikilink"

    # Without invalidate(), the cache is stale (this is the documented
    # contract — callers must invalidate after mutation).
    assert b.has_wikilinks is False, "cache should be stale until invalidate()"
    assert b._has_wikilinks_cache is False

    # After invalidate(), the next read recomputes from the mutated body.
    b.invalidate()
    assert b._has_wikilinks_cache is None
    assert b.has_wikilinks is True
    assert b._has_wikilinks_cache is True


def test_has_wikilinks_invalidates_in_reverse_direction(tmp_path: Path) -> None:
    """Invalidation also works when a wikilink is *removed* (True -> False)."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nbody with [[target]] wikilink\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert b.has_wikilinks is True

    concept = b.concept_at("a")
    assert concept is not None
    concept.body = "wikilink removed; just plain text now"

    # Stale until invalidated.
    assert b.has_wikilinks is True
    b.invalidate()
    assert b.has_wikilinks is False


def test_capabilities_wikilinks_active_when_wikilink_present(tmp_path: Path) -> None:
    """SPEC §10: ``okf.cap.wikilinks`` auto-activates on observed ``[[...]]``
    in any concept body, via ``Bundle.capabilities()``."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [[b]] for more.\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    resolved = b.capabilities()
    assert resolved.is_active("okf.cap.wikilinks") is True


def test_capabilities_wikilinks_inactive_when_no_wikilink(tmp_path: Path) -> None:
    """Without any ``[[...]]`` body, the wikilinks capability stays inactive."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [b](/b.md) instead.\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    assert b.capabilities().is_active("okf.cap.wikilinks") is False


def test_capabilities_wikilinks_picks_up_mutation_after_invalidate(
    tmp_path: Path,
) -> None:
    """Capability resolution is memoized too; invalidate() forces a recompute
    that picks up a newly-added wikilink (state-ownership + cache contract)."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nplain body\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert b.capabilities().is_active("okf.cap.wikilinks") is False

    concept = b.concept_at("a")
    assert concept is not None
    concept.body = "now [[target]] is linked"

    # Stale until invalidated.
    assert b.capabilities().is_active("okf.cap.wikilinks") is False
    b.invalidate()
    assert b.capabilities().is_active("okf.cap.wikilinks") is True


def test_p1_5_symlinked_concept_md_skipped_not_crashed(tmp_path):
    """P1-5: a concept .md symlinked outside the bundle is skipped with a
    warning, NOT crashing Bundle.load. iter-1's reserved-file defense caught
    (OSError, ValueError) but the concept path caught only ConceptIdError,
    so a plain ValueError from relative_to propagated and crashed every
    bundle-loading command + leaked the resolved host path."""
    import os
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "good.md").write_text("---\ntype: T\ntitle: Good\n---\nbody\n", encoding="utf-8")
    outside = tmp_path.parent / "outside_target_p15.md"
    outside.write_text("---\ntype: T\ntitle: Outside\n---\nhost secret\n", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "evil.md")
    except (OSError, NotImplementedError):
        try: outside.unlink()
        except OSError: pass
        pytest.skip("symlinks not supported")

    try:
        b = Bundle.load(tmp_path)  # MUST NOT raise
        assert ("evil",) not in b.concepts
        assert ("good",) in b.concepts
        codes = [w.code for w in b.warnings]
        assert "concept.path_escapes_bundle" in codes
        # message must NOT leak the resolved host path
        for w in b.warnings:
            if w.code == "concept.path_escapes_bundle":
                assert "outside_target_p15.md" not in w.message
                assert str(tmp_path.parent.resolve()) not in w.message
                assert "evil.md" in w.message
    finally:
        try: outside.unlink()
        except OSError: pass


def test_iter3_p1_1_symlink_loop_does_not_crash(tmp_path):
    """P1-1: a symlink loop (loop.md -> loop2.md -> loop.md) must not crash
    Bundle.load. iter-2 caught ValueError/OSError in _load_concept but the
    read_text in Bundle.load's loop was unguarded → ELOOP crashed every
    bundle-loading command."""
    import os
    (tmp_path / "index.md").write_text("# B\n", encoding="utf-8")
    (tmp_path / "good.md").write_text("---\ntype: T\ntitle: Good\n---\nbody\n", encoding="utf-8")
    try:
        os.symlink(tmp_path / "evil2.md", tmp_path / "evil.md")
        os.symlink(tmp_path / "evil.md", tmp_path / "evil2.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    b = Bundle.load(tmp_path)  # MUST NOT raise
    assert ("good",) in b.concepts
    assert ("evil",) not in b.concepts
    codes = [w.code for w in b.warnings]
    # Either path_escapes_bundle (loop resolve fails) or unreadable.
    assert any(c in codes for c in ("concept.path_escapes_bundle", "concept.unreadable")), codes
