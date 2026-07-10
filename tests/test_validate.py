"""Tests for ``okf_loom.validate``.

Pinned invariants:
  * SPEC §9 conformance: only ``type`` is required (NOT title/description/
    timestamp). Tiny_good passes; tiny_bad has errors.
  * Broken links are WARNING, not ERROR (SPEC §5.3).
  * ``--strict`` semantics: ``ok_strict`` requires zero warnings.
  * ``CheckSpec.only(...)`` enables only the named checks.
  * JSON output via ``as_dict()`` has the documented shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.validate import (
    CheckSpec,
    Finding,
    Severity,
    ValidationReport,
    apply_profile,
    validate_bundle,
    validate_bundle_with_profile,
)


# --- SPEC §9: only `type` is required ---------------------------------------


def test_tiny_good_is_conformant(tiny_good_bundle: Path) -> None:
    """tiny_good satisfies SPEC §9 — ``report.ok`` is True."""
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle(b)
    assert r.ok is True
    assert len(r.errors) == 0


def test_tiny_bad_has_errors(tiny_bad_bundle: Path) -> None:
    """tiny_bad fails SPEC §9 conformance — at least one ERROR finding."""
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle(b)
    assert r.ok is False
    assert len(r.errors) >= 1
    error_codes = {f.code for f in r.errors}
    assert "concept.missing_type" in error_codes
    assert "concept.parse_failed" in error_codes


def test_only_type_is_required_not_title(tmp_path: Path) -> None:
    """A concept with ONLY ``type`` (no title/description/timestamp) passes."""
    (tmp_path / "a.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    # Conformance: no errors (warnings are allowed; this is the upstream-
    # divergent guarantee — upstream incorrectly required title/description).
    assert r.ok is True
    assert len(r.errors) == 0


def test_governed_outer_shapes_warn_without_changing_base_conformance(
    tmp_path: Path,
) -> None:
    """Present-but-malformed governed keys activate capabilities but no longer
    pass strict validation silently. They remain soft under base OKF v0.1."""
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: T\n"
        "title: A\n"
        "description: d\n"
        "resource: r\n"
        "tags: [x]\n"
        "timestamp: '2026-01-01'\n"
        "aliases: {label: wrong-outer-shape}\n"
        "entities: {label: wrong-outer-shape}\n"
        "provenance: {source: wrong-outer-shape}\n"
        "citations: {text: wrong-outer-shape}\n"
        "relations: {target: b, type: references}\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    codes = {f.code for f in r.warnings}
    assert codes >= {
        "frontmatter.aliases_malformed",
        "frontmatter.entities_malformed",
        "frontmatter.provenance_malformed",
        "frontmatter.citations_malformed",
        "frontmatter.relations_malformed",
    }
    assert r.ok is True
    assert r.ok_strict is False


def test_governed_entry_shapes_and_duplicate_relations_have_stable_findings(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: T\n"
        "aliases: [{discoverable: false}]\n"
        "entities: [{label: X, aliases: nope}]\n"
        "provenance: [{}]\n"
        "citations: [plain-string]\n"
        "relations:\n"
        "  - {target: /b.md, type: references, custom: preserved}\n"
        "  - {target: b, type: references, detail: duplicate}\n"
        "  - {target: '../escape', type: references}\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    r = validate_bundle(Bundle.load(tmp_path))
    codes = [f.code for f in r.findings]
    for code in (
        "frontmatter.aliases_malformed",
        "frontmatter.entities_malformed",
        "frontmatter.provenance_malformed",
        "frontmatter.citations_malformed",
        "frontmatter.relations_malformed",
        "relation.duplicate",
    ):
        assert code in codes
    duplicate = next(f for f in r.findings if f.code == "relation.duplicate")
    assert duplicate.detail == {
        "key": "relations",
        "index": 1,
        "first_index": 0,
        "type": "references",
        "target": "b",
    }


def test_valid_governed_shapes_do_not_warn(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: T\n"
        "aliases: [Alt, {label: Search only, discoverable: false}]\n"
        "entities: [Thing, {id: entity/x, label: X, kind: Domain, aliases: [Ex]}]\n"
        "provenance: [{source: internal, note: curated, timestamp: '2026-01-01'}]\n"
        "citations: [{id: '1', text: Reference, url: 'https://example.com'}]\n"
        "relations: [{target: b, type: references, detail: why}]\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    r = validate_bundle(Bundle.load(tmp_path))
    governed = [
        f for f in r.findings
        if f.code.startswith("frontmatter.") or f.code == "relation.duplicate"
    ]
    assert governed == []


def test_missing_type_is_error(tmp_path: Path) -> None:
    """A concept missing ``type`` is an ERROR (SPEC §9 conformance failure)."""
    (tmp_path / "a.md").write_text(
        "---\ntitle: No Type\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    assert r.ok is False
    codes = {f.code for f in r.errors}
    assert "concept.missing_type" in codes


# --- Broken links are WARNING ------------------------------------------------


def test_broken_link_is_warning_not_error(tmp_path: Path) -> None:
    """SPEC §5.3: broken links are tolerated — flagged WARNING, not ERROR."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\n[ghost](/tables/ghost.md)\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    assert r.ok is True  # broken link alone does not fail conformance
    broken = [f for f in r.warnings if f.code == "link.broken"]
    assert len(broken) == 1
    assert broken[0].severity is Severity.WARNING


def test_broken_link_finding_in_tiny_bad(tiny_bad_bundle: Path) -> None:
    """tiny_bad's broken_link.md produces a link.broken WARNING."""
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle(b)
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert len(broken) >= 1
    assert all(f.severity is Severity.WARNING for f in broken)


# --- index.md self-link + out-of-bundle link handling -----------------------

def test_directory_index_link_not_broken(tmp_path: Path) -> None:
    """A link to ``/foo/index.md`` is NOT a broken link when the
    file exists on disk. The viewer serves it as a directory index; the
    validator used to flag it because ``index.md`` is reserved (not
    registered as a concept). Now we resolve against the filesystem.
    """
    (tmp_path / "tutorials").mkdir()
    (tmp_path / "tutorials").joinpath("index.md").write_text(
        "# Tutorials\n\n* [Install](install.md)\n", encoding="utf-8")
    (tmp_path / "tutorials").joinpath("install.md").write_text(
        "---\ntype: Tutorial\ntitle: Install\n---\n"
        "Back to [Tutorials](/tutorials/index.md).\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert len(broken) == 0, (
        f"expected no broken-link warnings for index.md reference, got: {broken}"
    )


def test_relative_directory_index_link_not_broken(tmp_path: Path) -> None:
    """A RELATIVE ``./index.md`` link from a concept in a
    directory whose ``index.md`` exists should also be recognised.
    """
    (tmp_path / "tutorials").mkdir()
    (tmp_path / "tutorials").joinpath("index.md").write_text(
        "# Tutorials\n", encoding="utf-8")
    (tmp_path / "tutorials").joinpath("install.md").write_text(
        "---\ntype: Tutorial\ntitle: Install\n---\n"
        "Back to [Tutorials](./index.md).\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert len(broken) == 0, broken


def test_missing_directory_index_link_still_broken(tmp_path: Path) -> None:
    """If the ``index.md`` does NOT exist on disk, the link is
    still flagged as broken — the fix is about suppressing false
    positives, not blanket-ignoring index.md references.
    """
    (tmp_path / "tutorials").mkdir()
    (tmp_path / "tutorials").joinpath("install.md").write_text(
        "---\ntype: Tutorial\ntitle: Install\n---\n"
        "Back to [Tutorials](/tutorials/index.md).\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert len(broken) == 1


def test_out_of_bundle_link_existing_not_broken(tmp_path: Path) -> None:
    """A relative link like ``../docs/architecture.md`` that
    resolves to a file OUTSIDE the bundle root but existing on disk is
    classified as ``external_out_of_bundle`` and NOT flagged as broken.
    """
    # Bundle lives under tmp_path/bundle/ ; the link target lives under
    # tmp_path/docs/ (sibling of bundle, outside the bundle root).
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "concept.md").write_text(
        "---\ntype: T\ntitle: Concept\n---\n"
        # One ../ escapes bundle into tmp_path; then docs/architecture.md.
        "Deep design: [architecture](../docs/architecture.md).\n",
        encoding="utf-8",
    )
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "architecture.md").write_text("# Architecture\n", encoding="utf-8")
    b = Bundle.load(bundle_root)
    r = validate_bundle(b)
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert len(broken) == 0, broken
    # The link is classified as external_out_of_bundle (not a graph edge).
    external_oor = [
        link for link in b.graph().external
        if link.form == "external_out_of_bundle"
    ]
    assert len(external_oor) == 1


def test_out_of_bundle_link_missing_still_broken(tmp_path: Path) -> None:
    """If the out-of-bundle target does NOT exist on disk, the
    link is still flagged as broken, with a clearer message and the
    ``out_of_bundle`` detail flag set so the planner can skip it.
    """
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "concept.md").write_text(
        "---\ntype: T\ntitle: Concept\n---\n"
        "Missing: [ghost](../docs/ghost.md).\n",
        encoding="utf-8",
    )
    # Note: no docs/ dir created, so the target doesn't exist.
    b = Bundle.load(bundle_root)
    r = validate_bundle(b)
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert len(broken) == 1
    assert broken[0].detail.get("out_of_bundle") is True
    assert "Out-of-bundle" in broken[0].message


# --- strict semantics --------------------------------------------------------


def test_ok_strict_requires_no_warnings(tiny_good_bundle: Path) -> None:
    """``ok_strict`` is False when any WARNING is present.

    tiny_good emits ``concept.missing_recommended_keys`` warnings for
    concepts that lack recommended frontmatter keys (events/metrics/orphan
    each miss some), so strict mode fails.
    """
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle(b)
    assert r.ok is True
    assert r.ok_strict is False
    assert len(r.warnings) >= 1


def test_ok_strict_passes_when_clean(tmp_path: Path) -> None:
    """A bundle with no errors AND no warnings passes strict."""
    (tmp_path / "index.md").write_text(
        "---\nokf_version: '0.1'\n---\n# Bundle\n", encoding="utf-8"
    )
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: T\ntitle: A\ndescription: d\nresource: r\n"
        "tags: [x]\ntimestamp: '2026-01-01'\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    assert r.ok_strict is True


# --- CheckSpec.only(...) -----------------------------------------------------


def test_check_spec_only_enables_named_checks(tiny_bad_bundle: Path) -> None:
    """``CheckSpec.only('link_integrity')`` runs only the named check."""
    b = Bundle.load(tiny_bad_bundle)
    spec = CheckSpec.only("link_integrity")
    r = validate_bundle(b, checks=spec)
    # Only link.broken findings (plus load-warning promotions are still
    # surfaced because they come from bundle.warnings before checks run).
    # Verify no recommended-keys warnings appear (that check is off).
    codes = {f.code for f in r.findings}
    assert "concept.missing_recommended_keys" not in codes
    assert "link.broken" in codes


def test_check_spec_only_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="Unknown check names"):
        CheckSpec.only("totally_made_up")


def test_p2_7_check_spec_only_accepts_dotted_finding_codes() -> None:
    """P2-7: CheckSpec.only maps user-facing dotted finding codes to their
    category check. ``link.broken`` enables ``link_integrity``; a dotted code
    unknown to the map is still rejected (fail-closed)."""
    # Dotted code → enables the responsible category, disables all others.
    spec = CheckSpec.only("link.broken")
    assert spec.link_integrity is True
    # Every other category is disabled.
    for fname in spec.__dataclass_fields__:
        if fname != "link_integrity":
            assert getattr(spec, fname) is False, (
                f"{fname} should be disabled under --checks link.broken"
            )


def test_p2_7_check_spec_only_mixed_category_and_dotted() -> None:
    """P2-7: category names and dotted codes can be mixed in one call."""
    spec = CheckSpec.only("concept_recommended_keys", "link.broken")
    assert spec.concept_recommended_keys is True
    assert spec.link_integrity is True  # link.broken → link_integrity
    assert spec.tag_normalization is False


def test_p2_7_check_spec_only_rejects_unknown_dotted_code() -> None:
    """P2-7 fail-closed: a dotted-shaped but unknown code is rejected."""
    with pytest.raises(ValueError, match="Unknown check names"):
        CheckSpec.only("link.totally_made_up")


def test_check_spec_defaults_all_true() -> None:
    """All checks are enabled by default."""
    spec = CheckSpec()
    for f in spec.__dataclass_fields__.values():
        assert getattr(spec, f.name) is True


def test_check_spec_disabling_recommended_keys(tiny_good_bundle: Path) -> None:
    """Disabling ``concept_recommended_keys`` removes those warnings."""
    b = Bundle.load(tiny_good_bundle)
    spec = CheckSpec(concept_recommended_keys=False)
    r = validate_bundle(b, checks=spec)
    assert not any(f.code == "concept.missing_recommended_keys" for f in r.findings)


# --- report shape & JSON -----------------------------------------------------


def test_report_as_dict_shape(tiny_good_bundle: Path) -> None:
    """``ValidationReport.as_dict()`` has the documented top-level keys."""
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle(b)
    d = r.as_dict()
    for key in (
        "bundle_root",
        "spec_version",
        "bundle_okf_version",
        "ok",
        "ok_strict",
        "counts",
        "findings",
    ):
        assert key in d
    assert set(d["counts"].keys()) == {"error", "warning", "info"}
    assert d["counts"]["error"] == len(r.errors)
    assert d["counts"]["warning"] == len(r.warnings)
    assert d["counts"]["info"] == len(r.infos)
    assert isinstance(d["findings"], list)


def test_finding_as_dict_shape() -> None:
    """``Finding.as_dict()`` includes code/severity/message/concept_id."""
    f = Finding(
        code="x.y",
        severity=Severity.WARNING,
        message="msg",
        concept_id=("a", "b"),
        line=3,
        detail={"k": "v"},
    )
    d = f.as_dict()
    assert d["code"] == "x.y"
    assert d["severity"] == "warning"
    assert d["concept_id"] == "a/b"
    assert d["line"] == 3
    assert d["detail"] == {"k": "v"}


def test_finding_as_dict_is_json_serializable(tiny_bad_bundle: Path) -> None:
    """Every finding's ``as_dict()`` is JSON-serializable end to end."""
    import json

    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle(b)
    blob = json.dumps(r.as_dict())
    assert isinstance(blob, str)
    parsed = json.loads(blob)
    assert parsed["ok"] is False


# --- additional check behaviours --------------------------------------------


def test_log_not_newest_first_warning(tiny_bad_bundle: Path) -> None:
    """tiny_bad's log.md (newer-after-older) produces ``log.not_newest_first``."""
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle(b)
    codes = {f.code for f in r.warnings}
    assert "log.not_newest_first" in codes


def test_spec_version_undeclared_is_info(tmp_path: Path) -> None:
    """A bundle without ``okf_version`` produces an INFO, not warning/error."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\ndescription: d\nresource: r\n"
        "tags: [x]\ntimestamp: '2026-01-01'\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    infos = [f for f in r.infos if f.code == "spec.version_undeclared"]
    assert len(infos) == 1


def test_spec_version_major_mismatch_warning(tmp_path: Path) -> None:
    """A bundle declaring a different MAJOR okf_version warns (best-effort consumption).

    The semver-aware check produces:
        - same major + same minor → no finding
        - same major + different minor → WARNING (minor mismatch)
        - different major → WARNING (major mismatch)
        - bundle older than toolkit → INFO (backward-compatible per SPEC §11)
    """
    (tmp_path / "index.md").write_text(
        "---\nokf_version: '99.0'\n---\n# Bundle\n", encoding="utf-8"
    )
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\ndescription: d\nresource: r\n"
        "tags: [x]\ntimestamp: '2026-01-01'\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    # Major mismatch (99.0 vs 0.1) → WARNING.
    major_warns = [f for f in r.warnings if f.code == "spec.version_major_mismatch"]
    assert len(major_warns) == 1


def test_spec_version_newer_minor_warning(tmp_path: Path) -> None:
    """Bundle on a newer minor of the same major warns about unrecognised features."""
    (tmp_path / "index.md").write_text(
        "---\nokf_version: '0.99'\n---\n# Bundle\n", encoding="utf-8"
    )
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    minor_warns = [f for f in r.warnings if f.code == "spec.version_newer_minor"]
    assert len(minor_warns) == 1


def test_spec_version_older_minor_info(tmp_path: Path) -> None:
    """A bundle on an older minor of the same major is INFO (backward-compat)."""
    # We can't easily test this against the current toolkit version (0.1)
    # because there is no older minor; we use okf_version "0.0" which is
    # technically older than "0.1" and should produce an INFO, not a warning.
    (tmp_path / "index.md").write_text(
        "---\nokf_version: '0.0'\n---\n# Bundle\n", encoding="utf-8"
    )
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    infos = [f for f in r.infos if f.code == "spec.version_older_minor"]
    assert len(infos) == 1
    # No warning should be present for backward-compatible minor difference.
    warns = [f for f in r.warnings if f.code.startswith("spec.version_")]
    assert warns == []


def test_tag_case_inconsistency_info(tmp_path: Path) -> None:
    """Two tags differing only by case produce an INFO (curator hint)."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\ntags: [Events]\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\ntags: [events]\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    infos = [f for f in r.infos if f.code == "tag.case_inconsistency"]
    assert len(infos) >= 1


def test_bad_timestamp_warning(tmp_path: Path) -> None:
    """A non-ISO-8601 timestamp is flagged as a WARNING."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\ntimestamp: 'not a date'\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    bad = [f for f in r.warnings if f.code == "concept.bad_timestamp"]
    assert len(bad) == 1


def test_link_form_inconsistency_info(tiny_good_bundle: Path) -> None:
    """tiny_good's `users` is referenced by both absolute & relative forms."""
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle(b)
    infos = [f for f in r.infos if f.code == "link.form_inconsistency"]
    assert len(infos) >= 1


def test_report_errors_warnings_infos_partition(tiny_bad_bundle: Path) -> None:
    """``errors`` ∪ ``warnings`` ∪ ``infos`` == ``findings`` (no overlap)."""
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle(b)
    union = r.errors + r.warnings + r.infos
    assert len(union) == len(r.findings)
    assert set(map(id, union)) == set(map(id, r.findings))


# --- current spec §5: Validation profiles -----------------------------------


def test_profile_spec_default(tiny_bad_bundle: Path) -> None:
    """spec profile = baseline behavior (no remap)."""
    from okf_loom.validate import validate_bundle_with_profile
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle_with_profile(b, profile="spec")
    r_base = validate_bundle(b)
    assert len(r.errors) == len(r_base.errors)
    assert len(r.warnings) == len(r_base.warnings)


def test_profile_loose_demotes_missing_type(tiny_bad_bundle: Path) -> None:
    """loose profile: concept.missing_type → WARNING (migration aid)."""
    from okf_loom.validate import validate_bundle_with_profile
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle_with_profile(b, profile="loose")
    # missing_type should now be WARNING, not ERROR
    missing_type_findings = [f for f in r.findings if f.code == "concept.missing_type"]
    for f in missing_type_findings:
        assert f.severity.value == "warning"
    # Should have fewer errors than spec
    r_spec = validate_bundle_with_profile(b, profile="spec")
    assert len(r.errors) < len(r_spec.errors)


def test_profile_producer_promotes_missing_recommended(tiny_good_bundle: Path) -> None:
    """producer profile: concept.missing_recommended_keys → ERROR."""
    from okf_loom.validate import validate_bundle_with_profile
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle_with_profile(b, profile="producer")
    # Any missing_recommended_keys should now be ERROR
    rec_findings = [f for f in r.findings if f.code == "concept.missing_recommended_keys"]
    for f in rec_findings:
        assert f.severity.value == "error"
    # Producer should have at least as many errors as spec
    r_spec = validate_bundle_with_profile(b, profile="spec")
    assert len(r.errors) >= len(r_spec.errors)


def test_fail_on_broken_links(tiny_bad_bundle: Path) -> None:
    """--fail-on-broken-links promotes link.broken to ERROR."""
    from okf_loom.validate import validate_bundle_with_profile
    b = Bundle.load(tiny_bad_bundle)
    r = validate_bundle_with_profile(b, fail_on_broken_links=True)
    broken = [f for f in r.findings if f.code == "link.broken"]
    for f in broken:
        assert f.severity.value == "error"


def test_profile_composes_with_strict(tiny_good_bundle: Path) -> None:
    """Profile remap + strict: strict promotes remaining warnings."""
    from okf_loom.validate import validate_bundle_with_profile
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle_with_profile(b, profile="producer", strict=True)
    # With strict, there should be no warnings
    assert len(r.warnings) == 0


def test_apply_profile_does_not_mutate_original(tiny_bad_bundle: Path) -> None:
    """apply_profile returns a new report; original is unchanged."""
    from okf_loom.validate import apply_profile
    b = Bundle.load(tiny_bad_bundle)
    original = validate_bundle(b)
    original_error_count = len(original.errors)
    _ = apply_profile(original, profile="loose")
    assert len(original.errors) == original_error_count


# --- SPEC §10: wikilink reporting -------------------------------------------


def test_wikilink_present_is_info_not_warning(tmp_path: Path) -> None:
    """``[[target]]`` in a body yields an INFO ``link.wikilink_present``
    finding, never a warning (wikilinks are a supported migration
    convenience per SPEC §10)."""
    (tmp_path / "tables").mkdir()
    (tmp_path / "references").mkdir()
    (tmp_path / "tables" / "users.md").write_text(
        "---\ntype: Table\ntitle: Users\n---\n"
        "See [[references/metrics]] and [[tables/events|Events]].\n",
        encoding="utf-8",
    )
    (tmp_path / "references" / "metrics.md").write_text(
        "---\ntype: Reference\n---\nbody\n", encoding="utf-8"
    )
    (tmp_path / "tables" / "events.md").write_text(
        "---\ntype: Table\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle(b)
    wl = [f for f in r.findings if f.code == "link.wikilink_present"]
    assert len(wl) == 2
    # SPEC §10: never warn about wikilinks themselves.
    assert all(f.severity is Severity.INFO for f in wl)
    targets = sorted(f.detail["target_raw"] for f in wl)
    assert targets == ["references/metrics", "tables/events"]
    # The label is captured for the piped form.
    labels = {f.detail["label"] for f in wl}
    assert "Events" in labels
    # And the bundle still validates OK (INFO does not affect `ok`).
    assert r.ok


# --- iter-10 P1-12: ValidationReport.profile field + as_dict ---------------


def test_as_dict_contains_profile_key(tiny_good_bundle: Path) -> None:
    """``as_dict()`` emits the mandated ``"profile"`` key (§5 L343).

    The report itself is now authoritative — library callers see the
    profile without the CLI post-hoc injecting it.
    """
    b = Bundle.load(tiny_good_bundle)
    # Bare validate_bundle defaults to the "spec" profile.
    d_spec = validate_bundle(b).as_dict()
    assert "profile" in d_spec
    assert d_spec["profile"] == "spec"
    # The profile-aware wrapper stamps the requested profile.
    d_prod = validate_bundle_with_profile(b, profile="producer").as_dict()
    assert d_prod["profile"] == "producer"
    d_loose = validate_bundle_with_profile(b, profile="loose").as_dict()
    assert d_loose["profile"] == "loose"


def test_report_profile_default_is_spec(tmp_path: Path) -> None:
    """A freshly-built ValidationReport defaults to profile='spec'."""
    r = validate_bundle(Bundle.load(tmp_path))
    assert r.profile == "spec"


def test_strict_path_preserves_profile(tiny_good_bundle: Path) -> None:
    """The strict-promotion rebuild carries the profile forward."""
    b = Bundle.load(tiny_good_bundle)
    r = validate_bundle_with_profile(b, profile="producer", strict=True)
    assert r.profile == "producer"


# --- iter-10 P1-13b: apply_profile rejects unknown profile -----------------


def test_apply_profile_rejects_unknown_profile(tiny_good_bundle: Path) -> None:
    """An unknown profile name raises ValueError (fail-closed, §5).

    Previously ``_PROFILE_REMAPS.get(profile, {})`` silently fell back to
    the empty ``spec`` remap, so a typo behaved as ``spec`` while the
    report claimed the typo name.
    """
    b = Bundle.load(tiny_good_bundle)
    base = validate_bundle(b)
    with pytest.raises(ValueError, match="Unknown validation profile"):
        apply_profile(base, profile="bogus")


def test_validate_bundle_with_profile_rejects_unknown_profile(
    tiny_good_bundle: Path,
) -> None:
    """The public wrapper also fails closed on an unknown profile."""
    b = Bundle.load(tiny_good_bundle)
    with pytest.raises(ValueError, match="Unknown validation profile"):
        validate_bundle_with_profile(b, profile="prodcer")


# --- iter-10 P2-5: module docstring documents the profile table ------------


def test_module_docstring_documents_profiles() -> None:
    """§5 L335-336: the code→severity profile table lives in the MODULE
    docstring, not only inside ``apply_profile``'s docstring."""
    import okf_loom.validate as V

    md = V.__doc__ or ""
    # The three profile names must appear.
    assert "producer" in md
    assert "loose" in md
    assert "spec" in md
    # The remapped finding codes must appear.
    assert "missing_recommended_keys" in md
    assert "missing_type" in md
    # The orthogonal modifiers must be documented.
    assert "fail-on-broken-links" in md or "broken" in md.lower()
    assert "strict" in md


# --- iter-10 P2-7: library-level loose-pass + composition ACs --------------


def test_loose_profile_passes_typeless_concept(tmp_path: Path) -> None:
    """§5 acceptance: ``--profile loose`` passes a typeless concept.

    A bundle whose ONLY defect is a missing ``type`` fails under ``spec``
    (ERROR) but passes under ``loose`` (demoted to WARNING). This is the
    spec §5 loose-pass acceptance criterion, previously untested at the
    library level.
    """
    (tmp_path / "a.md").write_text(
        "---\ntitle: No Type\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    # spec: missing_type is an ERROR → not ok.
    r_spec = validate_bundle_with_profile(b, profile="spec")
    assert r_spec.ok is False
    # loose: missing_type demoted to WARNING → ok.
    r_loose = validate_bundle_with_profile(b, profile="loose")
    assert r_loose.ok is True
    mt = [f for f in r_loose.findings if f.code == "concept.missing_type"]
    assert mt, "expected a concept.missing_type finding"
    assert all(f.severity.value == "warning" for f in mt)


def test_loose_composes_with_fail_on_broken_links(tmp_path: Path) -> None:
    """``loose`` remap + ``fail_on_broken_links`` promotion both apply.

    The bundle has a missing-type (loose → WARNING) AND a broken link
    (fail_on_broken_links → ERROR). Both remaps must compose: the broken
    link makes the bundle fail while missing-type stays a warning.
    """
    (tmp_path / "a.md").write_text(
        "---\ntitle: No Type\n---\n[ghost](/tables/ghost.md)\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    r = validate_bundle_with_profile(
        b, profile="loose", fail_on_broken_links=True
    )
    # Broken link promoted to ERROR → bundle fails.
    assert r.ok is False
    broken = [f for f in r.findings if f.code == "link.broken"]
    assert broken and all(f.severity.value == "error" for f in broken)
    # Missing type still demoted to WARNING by the loose remap.
    mt = [f for f in r.findings if f.code == "concept.missing_type"]
    assert mt and all(f.severity.value == "warning" for f in mt)
    # The profile is recorded on the composed report.
    assert r.profile == "loose"
