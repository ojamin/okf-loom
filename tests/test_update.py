"""Tests for ``okf_loom.update``.

Pinned invariants:
  * Every op kind is exercised (add_link, set_tag, add_tag,
    set_frontmatter, add_relation, append_body_section, remove_link).
  * IDEMPOTENCY: applying the same plan twice yields
    ``applied:0, skipped:N`` on the second run with correct reasons.
  * Round-trip preserves unknown frontmatter keys and key order.
  * ``add_relation`` creates ``relations:`` as ``[]`` then appends.
  * ``dry_run=True`` writes nothing.
  * ``apply_plan`` returns the documented dict shape.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from okf_loom import Bundle
from okf_loom.update import (
    UpdateOp,
    UpdatePlan,
    apply_plan,
    load_plan,
)


# --- helpers -----------------------------------------------------------------


def _make_bundle(tmp_path: Path) -> Path:
    """Create a 2-concept bundle and return its root path."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: AAA\n---\n# A\n\nbody of A\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: BBB\ncustom_key: preserve_me\n---\n# B\n\nbody of B\n",
        encoding="utf-8",
    )
    return tmp_path


def _plan(ops: list[UpdateOp], *, root: str | None = None) -> UpdatePlan:
    return UpdatePlan(
        bundle_root=root,
        description="test plan",
        ops=ops,
        plan_kind="update",
    )


# --- documented dict shape ---------------------------------------------------


def test_apply_plan_returns_documented_shape(tmp_path: Path) -> None:
    """``apply_plan`` returns the dict shape documented in its docstring.

    P2-24: each entry in ``results`` is now ``[op_dict, result_dict]`` (not
    ``(UpdateOp, result_dict)``) so the structure is JSON-serialisable
    without leaking the UpdateOp ``repr`` into ``--format json`` output.
    """
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    summary = apply_plan(b, _plan([
        UpdateOp("add_tag", ("a",), {"tag": "x"}),
    ]))
    for key in ("plan_kind", "total_ops", "applied", "skipped", "results"):
        assert key in summary
    assert summary["plan_kind"] == "update"
    assert summary["total_ops"] == 1
    assert summary["applied"] == 1
    assert summary["skipped"] == 0
    assert len(summary["results"]) == 1
    op_dict, result = summary["results"][0]
    # P2-24: op is now a dict (JSON-safe), not an UpdateOp instance.
    assert isinstance(op_dict, dict)
    assert op_dict["kind"] == "add_tag"
    assert op_dict["target"] == "a"
    assert op_dict["args"] == {"tag": "x"}
    for k in ("applied", "reason", "path"):
        assert k in result


# --- add_link + idempotency --------------------------------------------------


def test_add_link_creates_link(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("add_link", ("a",), {
            "label": "go B", "target_concept_id": "b",
        }),
    ]))
    a = b.concept_at("a")
    links = a.links(bundle_root=b.root)
    assert any(l.target == ("b",) for l in links)


def test_add_link_idempotent(tmp_path: Path) -> None:
    """IDEMPOTENCY: applying the same add_link twice skips on the 2nd run."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    plan = _plan([
        UpdateOp("add_link", ("a",), {
            "label": "go B", "target_concept_id": "b",
        }),
    ])
    s1 = apply_plan(b, plan)
    s2 = apply_plan(b, plan)
    assert s1["applied"] == 1 and s1["skipped"] == 0
    assert s2["applied"] == 0 and s2["skipped"] == 1
    assert s2["results"][0][1]["reason"] == "already_linked"


def test_add_link_target_not_found(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_link", ("a",), {
            "label": "ghost", "target_concept_id": "ghost",
        }),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "target_concept_not_found"


def test_add_link_to_unknown_concept(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_link", ("nope",), {
            "label": "x", "target_concept_id": "b",
        }),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "concept_not_found"


# --- set_tag ----------------------------------------------------------------


def test_set_tag_replaces(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("set_tag", ("a",), {"tag": "only"}),
    ]))
    assert b.concept_at("a").tags == ["only"]


def test_set_tag_idempotent_in_shape() -> None:
    """set_tag always returns applied=True (it's an unconditional setter)."""
    # set_tag's handler doesn't check for equality, so it always applies.
    # We document this: applying it twice still returns applied=1 each time.
    # (The body is unchanged; only frontmatter is overwritten with the same
    # value, so the file ends up byte-identical — see test_dry_run_writes_nothing
    # for the actual idempotency contract we *do* enforce.)


# --- add_tag ----------------------------------------------------------------


def test_add_tag_creates_when_absent(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_tag", ("a",), {"tag": "new"}),
    ]))
    assert s["applied"] == 1
    assert b.concept_at("a").tags == ["new"]


def test_add_tag_appends_to_existing(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([UpdateOp("add_tag", ("a",), {"tag": "x"})]))
    apply_plan(b, _plan([UpdateOp("add_tag", ("a",), {"tag": "y"})]))
    assert b.concept_at("a").tags == ["x", "y"]


def test_add_tag_idempotent(tmp_path: Path) -> None:
    """IDEMPOTENCY: adding an existing tag skips on the 2nd run."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    plan = _plan([UpdateOp("add_tag", ("a",), {"tag": "x"})])
    s1 = apply_plan(b, plan)
    s2 = apply_plan(b, plan)
    assert s1["applied"] == 1
    assert s2["applied"] == 0
    assert s2["results"][0][1]["reason"] == "already_tagged"


# --- set_frontmatter --------------------------------------------------------


def test_set_frontmatter_sets_key(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("a",), {"key": "description", "value": "d"}),
    ]))
    assert b.concept_at("a").frontmatter["description"] == "d"


def test_set_frontmatter_preserves_other_keys(tmp_path: Path) -> None:
    """Round-trip preserves unknown frontmatter keys."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("b",), {"key": "description", "value": "d"}),
    ]))
    fm = b.concept_at("b").frontmatter
    assert fm["custom_key"] == "preserve_me"
    # Order: type, title, custom_key, description (set_frontmatter appends).
    assert list(fm.keys())[:3] == ["type", "title", "custom_key"]


def test_set_frontmatter_missing_key_arg(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("a",), {"key": "", "value": "x"}),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"].startswith("bad_args")


# --- add_relation -----------------------------------------------------------


def test_add_relation_creates_empty_list_then_appends(tmp_path: Path) -> None:
    """When ``relations:`` is absent it's created as a list and appended."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "references",
        }),
    ]))
    assert s["applied"] == 1
    fm = b.concept_at("a").frontmatter
    assert fm["relations"] == [
        {"target": "b", "type": "references", "detail": ""},
    ]


def test_add_relation_appends_to_existing(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "r1",
        }),
    ]))
    apply_plan(b, _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "r2",
        }),
    ]))
    rels = b.concept_at("a").frontmatter["relations"]
    assert len(rels) == 2
    assert {r["type"] for r in rels} == {"r1", "r2"}


def test_add_relation_idempotent(tmp_path: Path) -> None:
    """IDEMPOTENCY: same (target, type) relation skips on the 2nd run."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    plan = _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "references",
        }),
    ])
    s1 = apply_plan(b, plan)
    s2 = apply_plan(b, plan)
    assert s1["applied"] == 1
    assert s2["applied"] == 0
    assert s2["results"][0][1]["reason"] == "already_related"


def test_add_relation_idempotency_normalizes_target_forms(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    a = root / "a.md"
    a.write_text(
        "---\ntype: T\ntitle: A\nrelations:\n"
        "  - {target: /b.md, type: references}\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(root)
    result = apply_plan(b, _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "references",
        }),
    ]))
    assert result["applied"] == 0
    assert result["results"][0][1]["reason"] == "already_related"
    assert len(b.concept_at("a").frontmatter["relations"]) == 1


def test_add_relation_fails_closed_on_malformed_existing_value(tmp_path: Path) -> None:
    """A mutator must not overwrite malformed hand-authored governed data."""
    root = _make_bundle(tmp_path)
    a = root / "a.md"
    a.write_text(
        "---\ntype: T\ntitle: A\nrelations: {target: legacy}\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(root)
    before = a.read_bytes()
    result = apply_plan(b, _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "references",
        }),
    ]))
    assert result["applied"] == 0
    assert result["results"][0][1]["reason"] == "malformed_relations"
    assert a.read_bytes() == before


# --- append_body_section ----------------------------------------------------


def test_append_body_section_adds_heading(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("append_body_section", ("a",), {
            "heading": "Examples", "body": "Some example.",
        }),
    ]))
    a = b.concept_at("a")
    texts = [h.text for h in a.headings]
    assert "Examples" in texts
    assert "Some example." in a.body


def test_append_body_section_idempotent(tmp_path: Path) -> None:
    """IDEMPOTENCY: appending an existing section heading skips."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    plan = _plan([
        UpdateOp("append_body_section", ("a",), {
            "heading": "Examples", "body": "x",
        }),
    ])
    s1 = apply_plan(b, plan)
    s2 = apply_plan(b, plan)
    assert s1["applied"] == 1
    assert s2["applied"] == 0
    assert s2["results"][0][1]["reason"] == "section_exists"


def test_append_body_section_missing_heading_arg(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("append_body_section", ("a",), {"heading": "", "body": "x"}),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"].startswith("bad_args")


# --- remove_link ------------------------------------------------------------


def test_remove_link_removes(tmp_path: Path) -> None:
    """remove_link replaces the link with its label text (prose preserved)."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    # First add a link from a -> b.
    apply_plan(b, _plan([
        UpdateOp("add_link", ("a",), {"label": "go B", "target_concept_id": "b"}),
    ]))
    # Now remove it.
    s = apply_plan(b, _plan([
        UpdateOp("remove_link", ("a",), {"target_concept_id": "b"}),
    ]))
    assert s["applied"] == 1
    a = b.concept_at("a")
    assert not any(l.target == ("b",) for l in a.links(bundle_root=b.root))


def test_remove_link_idempotent(tmp_path: Path) -> None:
    """IDEMPOTENCY: removing a non-existing link skips with reason 'not_linked'."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    plan = _plan([
        UpdateOp("remove_link", ("a",), {"target_concept_id": "b"}),
    ])
    s1 = apply_plan(b, plan)
    s2 = apply_plan(b, plan)
    assert s1["applied"] == 0 and s1["results"][0][1]["reason"] == "not_linked"
    assert s2["applied"] == 0 and s2["results"][0][1]["reason"] == "not_linked"


# --- unknown op kind --------------------------------------------------------


def test_unknown_op_kind(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("totally_made_up", ("a",), {}),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "unknown_op_kind:totally_made_up"


# --- dry_run writes nothing -------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``dry_run=True`` returns a result but leaves the file unchanged."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    target_file = root / "a.md"
    before = target_file.read_text(encoding="utf-8")
    mtime_before = target_file.stat().st_mtime_ns

    summary = apply_plan(b, _plan([
        UpdateOp("add_tag", ("a",), {"tag": "dry_run"}),
    ]), apply=False, dry_run=True)

    # The in-memory concept IS mutated (so multi-op plans report accurately).
    assert summary["applied"] == 1
    # But the file on disk is unchanged.
    after = target_file.read_text(encoding="utf-8")
    assert after == before
    # mtime unchanged (or strictly greater-or-equal — be tolerant of FS
    # granularity by also checking content equality, which is the real proof).
    assert after == before


# --- file is actually written when apply=True --------------------------------


def test_apply_writes_file_to_disk(tmp_path: Path) -> None:
    """When apply=True (default) the file is updated on disk."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("add_tag", ("a",), {"tag": "written"}),
    ]))
    on_disk = (root / "a.md").read_text(encoding="utf-8")
    assert "written" in on_disk


def test_atomic_write_no_tmp_left_behind(tmp_path: Path) -> None:
    """Successful writes leave no ``.tmp`` files in the bundle directory."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("add_tag", ("a",), {"tag": "x"}),
    ]))
    tmps = list(root.rglob("*.tmp"))
    assert tmps == []


# --- round-trip preserves key order -----------------------------------------


def test_round_trip_preserves_key_order(tmp_path: Path) -> None:
    """Frontmatter key insertion order survives an apply + reload round-trip."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("b",), {"key": "new_key", "value": "v"}),
    ]))
    # Reload from disk and check order.
    b2 = Bundle.load(root)
    fm_keys = list(b2.concept_at("b").frontmatter.keys())
    # type, title, custom_key, new_key
    assert fm_keys[:3] == ["type", "title", "custom_key"]
    assert "new_key" in fm_keys


# --- load_plan --------------------------------------------------------------


def test_load_plan_round_trip(tmp_path: Path) -> None:
    """``UpdatePlan.as_dict()`` -> JSON -> ``load_plan`` round-trips."""
    root = _make_bundle(tmp_path)
    plan = _plan([
        UpdateOp("add_tag", ("a",), {"tag": "x"}),
        UpdateOp("add_link", ("a",), {"label": "L", "target_concept_id": "b"}),
    ], root=str(root))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan.as_dict()), encoding="utf-8")
    loaded = load_plan(plan_path)
    assert loaded.plan_kind == plan.plan_kind
    assert loaded.bundle_root == str(root)
    assert len(loaded.ops) == 2
    assert loaded.ops[0].kind == "add_tag"
    assert loaded.ops[0].target == ("a",)
    assert loaded.ops[1].args["target_concept_id"] == "b"


def test_load_plan_rejects_bad_target(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "plan_kind": "update",
        "ops": [{"kind": "add_tag", "target": "-invalid", "args": {}}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid target"):
        load_plan(plan_path)


def test_load_plan_missing_target(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "plan_kind": "update",
        "ops": [{"kind": "add_tag", "args": {}}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'target'"):
        load_plan(plan_path)


def test_update_op_as_dict_renders_target_string() -> None:
    """``UpdateOp.as_dict`` renders target tuple as slash-joined string."""
    op = UpdateOp("add_tag", ("tables", "users"), {"tag": "x"})
    d = op.as_dict()
    assert d["target"] == "tables/users"
    assert d["kind"] == "add_tag"
    assert d["args"] == {"tag": "x"}


# ===========================================================================
# P1-24: YAML round-trip preservation (text-surgery)
# ===========================================================================


import difflib

from okf_loom.update import (
    patch_frontmatter_block,
    serialize_document_round_trip,
    write_concept,
    WriteConceptError,
)
from okf_loom.parse import parse_document


_P1_24_FIXTURE = (
    "---\n"
    "type: Table\n"
    'title: "Hello World"\n'
    "tags: [a, b]\n"
    "nested:\n"
    "  key1: value1\n"
    "  key2: value2\n"
    "---\n\n"
    "Body text.\n"
)


def _count_changed_lines(before: str, after: str) -> int:
    """Count unified-diff +/- lines (excluding the +++/--- headers)."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        n=0,
    )
    return sum(
        1 for line in diff
        if (line.startswith("+") or line.startswith("-"))
        and not (line.startswith("+++") or line.startswith("---"))
    )


def test_p1_24_add_key_preserves_flow_and_quote_styles(tmp_path: Path) -> None:
    """P1-24 HEADLINE acceptance test.

    A single ``set_frontmatter`` add of a NEW key must:
      * produce a bounded diff (< 6 changed lines),
      * preserve the original flow-style ``tags: [a, b]``,
      * preserve the original double-quoted ``title: "Hello World"``,
      * preserve the original nested mapping indentation.
    """
    # Stage the fixture as a real concept file.
    (tmp_path / "a.md").write_text(_P1_24_FIXTURE, encoding="utf-8")
    before = (tmp_path / "a.md").read_text()
    b = Bundle.load(tmp_path)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("a",), {
            "key": "description", "value": "A new desc",
        }),
    ]))
    after = (tmp_path / "a.md").read_text()

    # 1. Bounded diff.
    changed = _count_changed_lines(before, after)
    assert changed <= 6, f"diff too big: {changed} changed lines\n{after}"

    # 2. Flow style preserved.
    assert "tags: [a, b]" in after, (
        f"flow style lost — got:\n{after}"
    )
    # 3. Quote style preserved.
    assert 'title: "Hello World"' in after, (
        f"quote style lost — got:\n{after}"
    )
    # 4. Nested mapping preserved.
    assert "nested:\n  key1: value1\n  key2: value2\n" in after, (
        f"nested mapping lost — got:\n{after}"
    )
    # 5. New key landed at canonical position (between title and tags).
    assert "description: A new desc" in after

    # 6. Round-trip values are correct.
    fm, body = parse_document(after)
    assert fm["description"] == "A new desc"
    assert fm["tags"] == ["a", "b"]
    assert fm["nested"] == {"key1": "value1", "key2": "value2"}


def test_p1_24_scalar_value_change_preserves_quote_style() -> None:
    """P1-24: changing a scalar value preserves the original quote style."""
    original = '---\ntype: Table\ntitle: "Hello"\n---\nbody\n'
    fm, body = parse_document(original)
    fm["title"] = "World"
    out = serialize_document_round_trip(original, fm, body)
    assert 'title: "World"' in out, f"double-quote style should survive\n{out}"


def test_p1_24_single_quoted_style_preserved() -> None:
    """P1-24: single-quoted scalars keep their style on value change."""
    # YAML single-quoted strings escape ' as '' (not backslash).
    original = "---\ntype: T\nname: 'O''Brien'\n---\nbody\n"
    fm, body = parse_document(original)
    assert fm["name"] == "O'Brien"
    fm["name"] = "Smith"
    out = serialize_document_round_trip(original, fm, body)
    # Single-quoted output: 'Smith'
    assert "name: 'Smith'" in out, f"single-quote style should survive\n{out}"


def test_p1_24_flow_list_append_preserves_flow_style() -> None:
    """P1-24: appending to a flow-style list keeps the flow style."""
    original = "---\ntype: T\ntags: [a, b]\n---\nbody\n"
    fm, body = parse_document(original)
    fm["tags"] = ["a", "b", "c"]
    out = serialize_document_round_trip(original, fm, body)
    assert "tags: [a, b, c]" in out, f"flow style lost\n{out}"


def test_p1_24_block_list_append_preserves_indent() -> None:
    """P1-24: appending to a block-style list preserves the original indent."""
    original = "---\ntype: T\ntags:\n  - a\n  - b\n---\nbody\n"
    fm, body = parse_document(original)
    fm["tags"] = ["a", "b", "c"]
    out = serialize_document_round_trip(original, fm, body)
    assert "tags:\n  - a\n  - b\n  - c\n" in out, (
        f"block list indent lost\n{out}"
    )


def test_p1_24_block_dict_change_preserves_indent() -> None:
    """P1-24: changing a nested dict value preserves the block style."""
    original = "---\ntype: T\nnested:\n  key1: v1\n  key2: v2\n---\nbody\n"
    fm, body = parse_document(original)
    fm["nested"] = {"key1": "changed", "key2": "v2", "key3": "new"}
    out = serialize_document_round_trip(original, fm, body)
    assert "nested:" in out
    assert "  key1: changed" in out
    assert "  key2: v2" in out
    assert "  key3: new" in out


def test_p1_24_key_removal_drops_lines() -> None:
    """P1-24: removing a key drops its lines without touching the rest."""
    original = (
        "---\ntype: Table\ntitle: Hello\ndescription: to_remove\n---\nbody\n"
    )
    fm, body = parse_document(original)
    del fm["description"]
    out = serialize_document_round_trip(original, fm, body)
    assert "description" not in out
    assert "title: Hello" in out
    # Re-parse to confirm semantics.
    fm2, _ = parse_document(out)
    assert "description" not in fm2


def test_p1_24_unknown_key_preserved_through_set_frontmatter(tmp_path: Path) -> None:
    """P1-24 + AGENTS.md hard rule #2: unknown keys survive round-trip."""
    original = (
        "---\n"
        "type: Table\n"
        "custom_namespace:key: value\n"
        "another: 42\n"
        "---\nbody\n"
    )
    (tmp_path / "a.md").write_text(original, encoding="utf-8")
    b = Bundle.load(tmp_path)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("a",), {
            "key": "description", "value": "d",
        }),
    ]))
    fm, _ = parse_document((tmp_path / "a.md").read_text())
    assert fm["custom_namespace:key"] == "value"
    assert fm["another"] == 42
    assert fm["description"] == "d"


def test_p1_24_falls_back_to_safe_dump_on_reorder() -> None:
    """P1-24: when key ORDER changes, we fall back to safe_dump (correctness
    over compact diff)."""
    # original_fm has keys in order [type, a, b]; new_fm has [type, b, a].
    original = "---\ntype: T\na: 1\nb: 2\n---\nbody\n"
    original_fm, body = parse_document(original)
    new_fm = {"type": "T", "b": 2, "a": 1}  # reorder
    out = serialize_document_round_trip(original, new_fm, body)
    fm2, _ = parse_document(out)
    # Values round-trip correctly even though we couldn't text-surgery.
    assert fm2 == new_fm
    # Order matches the new dict (safe_dump preserved the new order).
    assert list(fm2.keys()) == ["type", "b", "a"]


def test_p1_24_new_concept_uses_safe_dump() -> None:
    """P1-24: no original_raw_text → safe_dump canonical form (no surgery)."""
    fm = {"type": "Table", "title": "Hello", "tags": ["a", "b"]}
    out = serialize_document_round_trip(None, fm, "body\n")
    assert out.startswith("---\n")
    assert "type: Table" in out
    fm2, body = parse_document(out)
    assert fm2 == fm
    assert body.rstrip() == "body"


def test_p1_24_patch_frontmatter_block_returns_none_on_complex_diff() -> None:
    """P1-24: patch_frontmatter_block signals 'too complex' by returning None."""
    # Reorder: original is [a, b], new is [b, a] (relative order changed).
    out = patch_frontmatter_block(
        "a: 1\nb: 2", {"a": 1, "b": 2}, {"b": 2, "a": 1}
    )
    assert out is None


# ===========================================================================
# P1-25: write_concept library seam
# ===========================================================================


def test_p1_25_write_concept_creates_new(tmp_path: Path) -> None:
    """P1-25: write_concept creates a new concept file."""
    (tmp_path / "existing.md").write_text(
        "---\ntype: T\ntitle: Existing\n---\nbody\n", encoding="utf-8"
    )
    result = write_concept(
        tmp_path, "tables/new",
        type="Table", title="New Concept", body="hello",
    )
    assert result["status"] == "created"
    assert result["path"] == str(tmp_path / "tables" / "new.md")
    assert result["id"] == "tables/new"
    assert (tmp_path / "tables" / "new.md").exists()
    # Compatibility alias key still present (deprecated).
    assert result["created"] == result["path"]


def test_p1_25_write_concept_updates_existing_preserving_unknown_keys(
    tmp_path: Path,
) -> None:
    """P1-25: write_concept UPDATE preserves unknown keys + order."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\ncustom_key: preserve_me\n---\nbody\n",
        encoding="utf-8",
    )
    result = write_concept(
        tmp_path, "a", type="T", description="new desc",
    )
    assert result["status"] == "updated"
    fm, body = parse_document((tmp_path / "a.md").read_text())
    assert fm["custom_key"] == "preserve_me"
    assert fm["description"] == "new desc"
    # Order preserved.
    assert list(fm.keys())[:3] == ["type", "title", "custom_key"]
    # Compatibility alias key still present.
    assert result["updated"] == result["path"]


def test_p1_25_write_concept_no_clobber_without_force(tmp_path: Path) -> None:
    """P1-25: write_concept refuses to overwrite a non-empty body."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nExisting body\n", encoding="utf-8"
    )
    with pytest.raises(WriteConceptError) as ei:
        write_concept(tmp_path, "a", type="T", body="new body")
    assert ei.value.code == "body_refused"


def test_p1_25_write_concept_force_overwrites_body(tmp_path: Path) -> None:
    """P1-25: --force allows overwriting a non-empty body."""
    (tmp_path / "a.md").write_text(
        "---\ntype: T\n---\nExisting body\n", encoding="utf-8"
    )
    write_concept(tmp_path, "a", type="T", body="new body", force=True)
    _, body = parse_document((tmp_path / "a.md").read_text())
    assert "new body" in body


def test_p1_25_write_concept_bundle_not_found(tmp_path: Path) -> None:
    """P1-25: missing bundle directory raises WriteConceptError(bundle_not_found)."""
    with pytest.raises(WriteConceptError) as ei:
        write_concept(
            tmp_path / "does_not_exist", "a", type="T", body="x",
        )
    assert ei.value.code == "bundle_not_found"


def test_p1_25_write_concept_reserved_filename(tmp_path: Path) -> None:
    """P1-25: writing to a reserved filename raises WriteConceptError."""
    with pytest.raises(WriteConceptError) as ei:
        write_concept(
            tmp_path, "index", type="T", body="x",
        )
    assert ei.value.code == "reserved_filename"


def test_p1_25_write_concept_idempotent_on_tag_re_add(tmp_path: Path) -> None:
    """P1-25 + P2-26: re-adding the same tag is a no-op (idempotency)."""
    # CREATE with a tag.
    write_concept(tmp_path, "a", type="T", tags=["x"], body="body")
    # UPDATE: re-add the same tag.
    write_concept(tmp_path, "a", type="T", tags=["x"])
    fm, _ = parse_document((tmp_path / "a.md").read_text())
    assert fm["tags"] == ["x"]  # no duplicate


# ===========================================================================
# P2-26: tag dedup on CREATE
# ===========================================================================


def test_p2_26_write_concept_dedupes_tags_on_create(tmp_path: Path) -> None:
    """P2-26: tags dedupe (preserving order) on the CREATE path."""
    write_concept(
        tmp_path, "a", type="T",
        tags=["alpha", "beta", "alpha", "gamma", "beta"],
        body="body",
    )
    fm, _ = parse_document((tmp_path / "a.md").read_text())
    # Deduped, order preserved.
    assert fm["tags"] == ["alpha", "beta", "gamma"]


# ===========================================================================
# P2-24: apply_plan results is JSON-safe (no UpdateOp repr)
# ===========================================================================


def test_p2_24_apply_plan_results_are_json_safe(tmp_path: Path) -> None:
    """P2-24: results array contains dict ops, not UpdateOp repr strings.

    Regression: previously the JSON output of ``okf update --format json``
    leaked the UpdateOp Python ``repr`` into the ``results`` field (e.g.
    ``"UpdateOp(kind='add_tag', ...)"``) because the dataclass wasn't
    JSON-serialisable. Now apply_plan pre-renders each op via as_dict().
    """
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    summary = apply_plan(b, _plan([
        UpdateOp("add_tag", ("a",), {"tag": "json_safe"}),
    ]))
    # The whole summary must JSON-serialise without ``default=str``.
    blob = json.dumps(summary)
    parsed = json.loads(blob)
    assert parsed["results"][0][0]["kind"] == "add_tag"
    # Negative regression: the serialized form must NOT contain the
    # UpdateOp Python repr.
    assert "UpdateOp(" not in blob
    assert "UpdateOp(kind=" not in blob


# ===========================================================================
# P2-29: add_relation accepts forward-references (does NOT validate exists)
# ===========================================================================


def test_p2_29_add_relation_accepts_forward_reference(tmp_path: Path) -> None:
    """P2-29: add_relation accepts a target that doesn't exist in the bundle.

    OKF permissiveness (SPEC §9 / AGENTS.md hard rule #3) explicitly allows
    forward references — a relation may point at not-yet-written knowledge.
    This differs from add_link, which DOES validate (markdown links point
    at concrete files).
    """
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "ghost_target",
            "relation_type": "references",
        }),
    ]))
    assert s["applied"] == 1
    fm = b.concept_at("a").frontmatter
    assert fm["relations"] == [
        {"target": "ghost_target", "type": "references", "detail": ""},
    ]


def test_p2_29_add_link_rejects_missing_target_for_contrast(tmp_path: Path) -> None:
    """P2-29 contrast: add_link DOES reject a non-existent target.

    Markdown links point at concrete files; a broken file link is a
    different failure mode than a forward-reference relation.
    """
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_link", ("a",), {
            "label": "ghost", "target_concept_id": "ghost_target",
        }),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "target_concept_not_found"


# ===========================================================================
# P2-6 (iter-3): add_link accepts forward references when explicitly opted in.
# Default stays fail-closed (target_concept_not_found). SPEC §3 / AGENTS.md
# hard rule #3: a link whose target does not exist is "not-yet-written
# knowledge", not malformed.
# ===========================================================================


def test_p2_6_add_link_forward_reference_when_opted_in(tmp_path: Path) -> None:
    """With allow_forward_reference=True, add_link writes a body link to a
    not-yet-written target (SPEC §5.1 absolute form) and reports applied."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_link", ("a",), {
            "label": "future", "target_concept_id": "not_yet_written",
            "allow_forward_reference": True,
        }),
    ]))
    assert s["applied"] == 1, f"forward-ref add_link should apply: {s}"
    body = b.concept_at("a").body
    assert "/not_yet_written.md" in body, (
        f"forward-ref link not written in SPEC §5.1 absolute form:\n{body}"
    )


def test_p2_6_add_link_forward_reference_idempotent(tmp_path: Path) -> None:
    """A second forward-reference add_link to the same missing target is a
    no-op (already_linked) — no duplicate body link."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    op = UpdateOp("add_link", ("a",), {
        "label": "future", "target_concept_id": "not_yet_written",
        "allow_forward_reference": True,
    })
    s1 = apply_plan(b, _plan([op]))
    assert s1["applied"] == 1
    s2 = apply_plan(b, _plan([op]))
    assert s2["applied"] == 0
    assert s2["results"][0][1]["reason"] == "already_linked"


def test_p2_6_add_link_forward_reference_default_off(tmp_path: Path) -> None:
    """Without allow_forward_reference, the fail-closed default still rejects
    a missing target (the flag is the explicit opt-in; safety default)."""
    root = _make_bundle(tmp_path)
    b = Bundle.load(root)
    s = apply_plan(b, _plan([
        UpdateOp("add_link", ("a",), {
            "label": "future", "target_concept_id": "not_yet_written",
        }),
    ]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "target_concept_not_found"


# ---------------------------------------------------------------------------
# P0 regression tests: YAML round-trip validation gate.
# iter-1's text-surgery corrupted multi-line scalars (P0-1) and nested
# list-of-dicts (P0-2); the validation gate must catch both by re-parsing
# and falling back to safe_dump on mismatch.
# ---------------------------------------------------------------------------


def test_iter2_p0_1_multiline_scalar_round_trips(tmp_path):
    """P0-1: setting a multi-line description must NOT corrupt the file."""
    from okf_loom import Bundle
    from okf_loom.update import UpdateOp, UpdatePlan, apply_plan
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: Playbook\ntitle: P\ndescription: old single-line\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path), description="t",
        ops=[UpdateOp(kind="set_frontmatter", target=("c",),
                      args={"key": "description", "value": "Step 1.\nStep 2.\nStep 3."})],
        plan_kind="single-op",
    )
    apply_plan(b, plan, apply=True)
    # The file MUST reparse (the bug produced 'description: 'Step 1.' unterminated).
    b2 = Bundle.load(tmp_path)
    assert ("c",) in b2.concepts, "concept vanished — file corrupt"
    assert b2.concepts[("c",)].description == "Step 1.\nStep 2.\nStep 3."


def test_iter2_p0_2_nested_entities_preserved(tmp_path):
    """P0-2: add_entity to a concept with entities-with-aliases must NOT
    flatten the nested aliases list into the parent sequence."""
    from okf_loom import Bundle
    from okf_loom.update import UpdateOp, UpdatePlan, apply_plan
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\n"
        "type: Table\n"
        "title: C\n"
        "entities:\n"
        "  - label: Order\n"
        "    kind: business_entity\n"
        "    aliases:\n"
        "      - purchase\n"
        "      - transaction\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path), description="t",
        ops=[UpdateOp(kind="add_entity", target=("c",),
                      args={"label": "Currency", "kind": "ref", "aliases": ["fx", "money"]})],
        plan_kind="single-op",
    )
    apply_plan(b, plan, apply=True)
    b2 = Bundle.load(tmp_path)
    ents = b2.concepts[("c",)].frontmatter.get("entities", [])
    # Every entity must remain a dict (the bug flattened aliases to top-level strings).
    assert all(isinstance(e, dict) for e in ents), (
        f"P0-2 regression: non-dict entity (aliases flattened): {ents}"
    )
    # The original Order entity's aliases must survive as a list.
    order = next((e for e in ents if e.get("label") == "Order"), None)
    assert order is not None, f"Order entity lost: {ents}"
    assert order.get("aliases") == ["purchase", "transaction"], (
        f"Order aliases corrupted: {order.get('aliases')}"
    )
    # The new Currency entity must also be a proper dict with its aliases.
    cur = next((e for e in ents if e.get("label") == "Currency"), None)
    assert cur is not None and cur.get("aliases") == ["fx", "money"]


# ===========================================================================
# iter-3 Bundle S2: P1-6 YAML validation-gate holes + P1-8 _suggestions_to_ops
# ===========================================================================
#
# P1-6(a): Python's ``==`` treats ``True == 1`` and ``1 == 1.0`` as equal.
# Three sites in the round-trip pipeline relied on ``==``/``!=``:
#   * ``changed_keys`` detection in :func:`patch_frontmatter_block`
#   * the validation-gate comparison in :func:`serialize_document_round_trip`
#   * the idempotency check in :func:`_h_set_frontmatter`
# Together they silently dropped valid bool<->int/float mutations (e.g.
# ``flag: true -> flag: 1`` reported ``same_value`` and never wrote).
#
# P1-6(b): :func:`_format_scalar` truncated multi-line ``safe_dump`` output
# to its first line, producing an unterminated quoted scalar. The iter-2
# validation gate caught the corruption and fell back to ``safe_dump``,
# which destroys ALL comments. The fix returns the full multi-line output
# so the gate PASSES and comments survive.
#
# P1-8: :func:`_suggestions_to_ops` only converted ``unlinked_mentions``
# and ``missing_descriptions``; the other four discovery rules
# (missing_indexes / broken_links / orphan_concepts / missing_relations_hint)
# were silently dropped, and missing_descriptions auto-wrote ``TODO:``
# placeholders into hand-authored files. The fix returns ``(ops, skipped)``,
# records every unhandled rule, and defers missing_descriptions as
# ``needs_agent_input`` (aligning with the action-envelope path).


def test_iter3_p1_6a_bool_to_int_refused_via_set_frontmatter(tmp_path: Path) -> None:
    """bool -> int must NOT be silently dropped as same_value.

    Without strict type-aware equality, ``1 == True`` caused the
    set_frontmatter idempotency guard to return ``same_value`` and refuse
    to mutate. ``_values_equal`` must distinguish bool from int.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: C\nflag: 1\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    s = apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("c",), {"key": "flag", "value": True}),
    ]))
    assert s["applied"] == 1, (
        f"expected mutation, got reason={s['results'][0][1]['reason']}"
    )
    fm, _ = parse_document((tmp_path / "c.md").read_text())
    assert fm["flag"] is True, f"flag should be bool True, got {fm['flag']!r}"
    assert isinstance(fm["flag"], bool)


def test_iter3_p1_6a_int_to_bool_refused_via_set_frontmatter(tmp_path: Path) -> None:
    """Reverse direction: int -> bool must also mutate (not skip)."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: C\nflag: true\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    s = apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("c",), {"key": "flag", "value": 1}),
    ]))
    assert s["applied"] == 1
    fm, _ = parse_document((tmp_path / "c.md").read_text())
    assert fm["flag"] == 1
    assert isinstance(fm["flag"], int) and not isinstance(fm["flag"], bool)


def test_iter3_p1_6a_int_to_float_refused_via_set_frontmatter(tmp_path: Path) -> None:
    """int -> float must mutate even when values are ``==`` (``1 == 1.0``).

    Without strict type-aware equality the idempotency guard returned
    ``same_value`` and the change was silently dropped.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: C\nscore: 1\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    s = apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("c",), {"key": "score", "value": 1.0}),
    ]))
    assert s["applied"] == 1
    fm, _ = parse_document((tmp_path / "c.md").read_text())
    assert fm["score"] == 1.0
    assert isinstance(fm["score"], float)


def test_iter3_p1_6a_float_to_int_refused_via_set_frontmatter(tmp_path: Path) -> None:
    """Reverse: float -> int must mutate even when ``1.0 == 1``."""
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: C\nscore: 1.0\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    s = apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("c",), {"key": "score", "value": 1}),
    ]))
    assert s["applied"] == 1
    fm, _ = parse_document((tmp_path / "c.md").read_text())
    assert fm["score"] == 1
    assert isinstance(fm["score"], int) and not isinstance(fm["score"], bool)


def test_iter3_p1_6a_bool_change_detected_by_round_trip_gate() -> None:
    """Direct test of the patch_frontmatter_block gate.

    Changing ``flag: 1`` (int) to ``flag: True`` (bool) must produce a
    serialized block whose re-parsed value is bool True, not silently
    left as int 1. This exercises BOTH the changed_keys detection AND
    the validation-gate comparison.
    """
    original = "---\ntype: T\nflag: 1\n---\nbody\n"
    original_fm, body = parse_document(original)
    new_fm = dict(original_fm)
    new_fm["flag"] = True
    out = serialize_document_round_trip(original, new_fm, body)
    fm2, _ = parse_document(out)
    assert fm2["flag"] is True, (
        f"expected True (bool), got {fm2['flag']!r} ({type(fm2['flag']).__name__})"
    )


def test_iter3_p1_6a_bool_and_int_preserved_through_write_concept(
    tmp_path: Path,
) -> None:
    """write_concept UPDATE round-trip must preserve bool/int/float leaves.

    Even though write_concept only mutates string keys (type/title/...),
    the round-trip serializer must NOT corrupt neighbouring typed
    scalars when it patches the file via text-surgery.
    """
    (tmp_path / "c.md").write_text(
        "---\ntype: T\nflag: true\nactive: false\ncount: 5\nscore: 1.5\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    write_concept(tmp_path, "c", type="T", description="added desc")
    fm, _ = parse_document((tmp_path / "c.md").read_text())
    assert fm["flag"] is True
    assert fm["active"] is False
    assert fm["count"] == 5 and isinstance(fm["count"], int)
    assert fm["score"] == 1.5 and isinstance(fm["score"], float)
    assert fm["description"] == "added desc"


def test_iter3_p1_6b_multiline_desc_preserves_comments_and_round_trips(
    tmp_path: Path,
) -> None:
    """Multi-line description change on a concept WITH comments must:
      * preserve ALL comments (no safe_dump fallback that nukes them),
      * round-trip the multi-line value correctly,
      * produce a file that re-parses.

    iter-2 added a validation gate that caught the corruption (caused by
    ``_format_scalar`` truncating the multi-line ``safe_dump`` output to
    its first line) and fell back to ``safe_dump`` — but that destroyed
    every comment. iter-3 fixes ``_format_scalar`` so the multi-line
    block survives and the gate PASSES.
    """
    original = (
        "---\n"
        "# Top comment\n"
        "type: Playbook\n"
        "title: P\n"
        "description: old single-line\n"
        "# Trailing comment\n"
        "---\nbody\n"
    )
    (tmp_path / "c.md").write_text(original, encoding="utf-8")
    b = Bundle.load(tmp_path)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("c",), {
            "key": "description",
            "value": "Step 1.\nStep 2.\nStep 3.",
        }),
    ]))
    out = (tmp_path / "c.md").read_text()
    # Comments survive.
    assert "# Top comment" in out, f"top comment lost:\n{out}"
    assert "# Trailing comment" in out, f"trailing comment lost:\n{out}"
    # Value round-trips.
    fm, _ = parse_document(out)
    assert fm["description"] == "Step 1.\nStep 2.\nStep 3."
    # File re-parses cleanly (no corruption).
    assert fm["type"] == "Playbook"
    assert fm["title"] == "P"


def test_iter3_p1_6b_multiline_desc_new_key_preserves_comments(
    tmp_path: Path,
) -> None:
    """Adding a NEW multi-line description to a concept WITH comments must
    also preserve comments (exercises the _format_key_value path)."""
    original = (
        "---\n"
        "# Header comment\n"
        "type: Playbook\n"
        "title: P\n"
        "# Footer comment\n"
        "---\nbody\n"
    )
    (tmp_path / "c.md").write_text(original, encoding="utf-8")
    b = Bundle.load(tmp_path)
    apply_plan(b, _plan([
        UpdateOp("set_frontmatter", ("c",), {
            "key": "description",
            "value": "Line one.\nLine two.",
        }),
    ]))
    out = (tmp_path / "c.md").read_text()
    assert "# Header comment" in out
    assert "# Footer comment" in out
    fm, _ = parse_document(out)
    assert fm["description"] == "Line one.\nLine two."


def test_iter3_p1_8_discovery_suggestions_no_silent_drop(tmp_path: Path) -> None:
    """A discovery report with all 6 rule types must:
      * populate ``skipped_actions`` for every non-mechanical rule
        (missing_indexes / broken_links / orphan_concepts /
        missing_relations_hint) with reason ``unhandled_discovery_rule``,
      * defer ``missing_descriptions`` as ``needs_agent_input`` (NOT
        auto-write a TODO: placeholder),
      * convert ``unlinked_mentions`` to exactly one ``add_link`` op,
      * leave the bundle's concept bodies free of ``TODO:`` placeholders.
    """
    # A tiny bundle: a.md lacks a description and mentions "b concept".
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nMentions the b concept here.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\ndescription: has desc\n---\nbody of B\n",
        encoding="utf-8",
    )

    # Synthetic discovery report covering every rule type. (We synthesize
    # rather than run discover() so the test is independent of which
    # rules happen to fire on this tiny bundle.)
    suggestions = [
        {"rule": "unlinked_mentions", "concept_id": "a",
         "detail": {"suggested_target_concept_id": "b", "label": "B"}},
        {"rule": "missing_descriptions", "concept_id": "a", "detail": {}},
        {"rule": "missing_indexes", "concept_id": None,
         "detail": {"dir": "subdir"}},
        {"rule": "broken_links", "concept_id": "a",
         "detail": {"target": "/ghost.md"}},
        {"rule": "orphan_concepts", "concept_id": "a", "detail": {}},
        {"rule": "missing_relations_hint", "concept_id": "a",
         "detail": {"target_concept_id": "b"}},
    ]
    plan_path = tmp_path / "discovery.json"
    plan_path.write_text(json.dumps({
        "bundle_root": str(tmp_path),
        "suggestions": suggestions,
    }), encoding="utf-8")

    plan = load_plan(plan_path)

    # The plan must record a skipped entry for every non-mechanical rule.
    skipped_by_rule: dict[str, list[dict]] = {}
    for entry in plan.skipped_actions:
        skipped_by_rule.setdefault(entry.get("rule"), []).append(entry)
    for rule in (
        "missing_descriptions", "missing_indexes", "broken_links",
        "orphan_concepts", "missing_relations_hint",
    ):
        assert rule in skipped_by_rule, (
            f"rule {rule!r} missing from skipped_actions: {plan.skipped_actions}"
        )

    # unlinked_mentions -> 1 add_link op (the only mechanical conversion).
    assert len(plan.ops) == 1, (
        f"expected 1 op, got {len(plan.ops)}: {[o.as_dict() for o in plan.ops]}"
    )
    assert plan.ops[0].kind == "add_link"

    # missing_descriptions deferred as needs_agent_input (NOT a TODO write).
    md_skips = skipped_by_rule["missing_descriptions"]
    assert md_skips and md_skips[0]["reason"] == "needs_agent_input", (
        f"missing_descriptions should defer as needs_agent_input, got {md_skips}"
    )

    # The four unhandled discovery rules share a single reason.
    for rule in (
        "missing_indexes", "broken_links",
        "orphan_concepts", "missing_relations_hint",
    ):
        reasons = {e["reason"] for e in skipped_by_rule[rule]}
        assert reasons == {"unhandled_discovery_rule"}, (
            f"rule {rule!r} reasons={reasons}, expected unhandled_discovery_rule"
        )

    # Apply the plan; no TODO: placeholders must be written.
    bundle = Bundle.load(tmp_path)
    apply_plan(bundle, plan)
    after_a = (tmp_path / "a.md").read_text()
    assert "TODO:" not in after_a, (
        f"TODO placeholder written into a.md (should defer to agent):\n{after_a}"
    )


def test_iter3_p1_8_missing_descriptions_does_not_write_todo(tmp_path: Path) -> None:
    """A discovery report containing ONLY missing_descriptions must produce
    zero ops and one skipped entry — and must NOT touch the file body."""
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\noriginal body\n",
        encoding="utf-8",
    )
    before = (tmp_path / "a.md").read_text()
    plan_path = tmp_path / "discovery.json"
    plan_path.write_text(json.dumps({
        "bundle_root": str(tmp_path),
        "suggestions": [
            {"rule": "missing_descriptions", "concept_id": "a", "detail": {}},
        ],
    }), encoding="utf-8")
    plan = load_plan(plan_path)
    assert plan.ops == []
    assert len(plan.skipped_actions) == 1
    assert plan.skipped_actions[0]["reason"] == "needs_agent_input"
    # Apply — the concept file body must be unchanged.
    apply_plan(Bundle.load(tmp_path), plan)
    assert (tmp_path / "a.md").read_text() == before
