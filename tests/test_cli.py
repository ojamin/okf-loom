"""Tests for ``okf_loom.cli``.

Pinned invariants:
  * ``main(["info", ...])`` returns 0 on a valid bundle.
  * ``main(["validate", bad])`` returns 1 (conformance failure).
  * ``main(["validate", good, "--strict"])`` returns 0 or 2 depending on warnings.
  * ``main(["search", ..., "--format", "json"])`` returns 0 and emits valid JSON.
  * ``main(["capabilities", "--format", "json"])`` returns 0.
  * Unknown command → non-zero.
  * ``--version`` works (exits 0 with the version string).
"""
from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from okf_loom import LOOM_VERSION
from okf_loom.cli import build_parser, main
from okf_loom.config import CONFIG_FILENAME
from okf_loom.studio import Studio


def _capture(argv: list[str]) -> tuple[int, str, str]:
    """Run main(argv) capturing stdout/stderr; return (exit_code, out, err)."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _configure_custom_session(bundle: Path) -> Path:
    """Write a custom studio.session_dir and return its absolute path."""
    rel = ".okf-loom/custom-session"
    (bundle / CONFIG_FILENAME).write_text(
        f"studio:\n  session_dir: {rel}\n",
        encoding="utf-8",
    )
    return bundle / rel


def _write_minimal_cli_bundle(bundle: Path) -> None:
    """Create a tiny mutable bundle with no pre-existing session files."""
    bundle.mkdir(parents=True)
    (bundle / "index.md").write_text(
        "---\nokf_version: '0.1'\n---\n# Test Bundle\n",
        encoding="utf-8",
    )
    (bundle / "log.md").write_text("# Log\n", encoding="utf-8")
    (bundle / "tables").mkdir()
    (bundle / "tables" / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\ndescription: Original\n"
        "tags: [orders]\ntimestamp: '2026-01-01T00:00:00Z'\n---\n# Orders\n",
        encoding="utf-8",
    )


# --- info --------------------------------------------------------------------


def test_info_returns_zero_on_valid_bundle(tiny_good_bundle: Path) -> None:
    """``main(["info", bundle])`` exits 0 on a valid bundle."""
    rc, out, _ = _capture(["info", str(tiny_good_bundle)])
    assert rc == 0
    assert "tiny_good" in out


def test_info_json_emits_valid_json(tiny_good_bundle: Path) -> None:
    """``--format json`` produces a parseable JSON document."""
    rc, out, _ = _capture(["info", str(tiny_good_bundle), "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert data["name"] == "tiny_good"
    assert data["concept_count"] == 4


def test_info_missing_bundle_returns_nonzero(tmp_path: Path) -> None:
    rc, _, err = _capture(["info", str(tmp_path / "does_not_exist")])
    assert rc != 0
    assert "error" in err.lower()


# --- validate ----------------------------------------------------------------


def test_validate_good_bundle_returns_zero(tiny_good_bundle: Path) -> None:
    """``validate`` on a conformant bundle exits 0 even with soft warnings."""
    rc, _, _ = _capture(["validate", str(tiny_good_bundle)])
    assert rc == 0


def test_validate_bad_bundle_returns_one(tiny_bad_bundle: Path) -> None:
    """``validate`` on a non-conformant bundle exits 1."""
    rc, _, _ = _capture(["validate", str(tiny_bad_bundle)])
    assert rc == 1


def test_validate_strict_good_returns_two_when_warnings_exist(
    tiny_good_bundle: Path,
) -> None:
    """``--strict`` on a bundle WITH warnings returns exit code 2.

    tiny_good has missing_recommended_keys warnings, so strict mode fails
    with exit code 2 (conformance pass, strict fail).
    """
    rc, _, _ = _capture(["validate", str(tiny_good_bundle), "--strict"])
    assert rc == 2


def test_validate_strict_clean_returns_zero(tmp_path: Path) -> None:
    """``--strict`` on a perfectly clean bundle exits 0."""
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
    rc, _, _ = _capture(["validate", str(tmp_path), "--strict"])
    assert rc == 0


def test_validate_json_emits_report(tiny_bad_bundle: Path) -> None:
    """``--format json`` returns the report dict as JSON."""
    rc, out, _ = _capture([
        "validate", str(tiny_bad_bundle), "--format", "json",
    ])
    assert rc == 1  # bad bundle still fails conformance
    data = json.loads(out)
    assert data["ok"] is False
    assert data["counts"]["error"] >= 1


def test_validate_checks_subset(tiny_bad_bundle: Path) -> None:
    """``--checks`` should enable only the named (comma-separated) checks."""
    rc, out, _ = _capture([
        "validate", str(tiny_bad_bundle), "--checks", "link_integrity",
        "--format", "json",
    ])
    assert rc == 0 or rc == 1  # validate runs, only link.broken is added
    data = json.loads(out)
    codes = {f["code"] for f in data["findings"]}
    assert "link.broken" in codes
    assert "concept.missing_recommended_keys" not in codes


# --- search ------------------------------------------------------------------


def test_search_returns_zero_and_results(tiny_good_bundle: Path) -> None:
    """``search users`` exits 0 AND returns the tables/users concept.

    Previously ``assert "users" in out.lower() or "(no matches)" in out`` was
    always true (the query term is echoed in every text result line, and the
    text mode prints nothing when there are zero matches), so a broken search
    returning zero results still passed. Now we parse the JSON and assert the
    specific concept id is present, so the test fails if search breaks.
    """
    rc, out, err = _capture([
        "search", str(tiny_good_bundle), "users", "--format", "json",
    ])
    assert rc == 0, f"search failed: {err}"
    data = json.loads(out)
    assert len(data) >= 1, "search returned no results for 'users'"
    assert data[0]["concept_id"] == "tables/users", (
        f"top result not tables/users: {data[0]['concept_id']!r}"
    )


def test_search_json_emits_valid_json(tiny_good_bundle: Path) -> None:
    """``search --format json`` returns a JSON list of results."""
    rc, out, _ = _capture([
        "search", str(tiny_good_bundle), "users", "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, list)
    if data:
        assert "concept_id" in data[0]


def test_search_empty_query_returns_zero(tiny_good_bundle: Path) -> None:
    """An empty query is legal and exits 0 (no results)."""
    rc, out, _ = _capture(["search", str(tiny_good_bundle), ""])
    assert rc == 0


def test_search_semantic_returns_one(tiny_good_bundle: Path) -> None:
    """Semantic mode returns at least one real semantic-lite result.

    Previously ``assert "semantic-lite" in out or rc == 0`` was a tautology:
    ``rc == 0`` was already asserted on the line above, so the assertion held
    for any successful run — even one that returned zero results or fell back
    silently. Now we parse the JSON and prove the hit came from the
    semantic-lite backend and is a real concept id, so the test fails if the
    semantic backend breaks or returns nothing.
    """
    rc, out, err = _capture([
        "search", str(tiny_good_bundle), "users", "--mode", "semantic",
        "--format", "json",
    ])
    assert rc == 0, f"semantic search failed: {err}"
    data = json.loads(out)
    assert len(data) >= 1, "semantic search returned no results"
    assert data[0]["source_backend"] == "semantic-lite", (
        f"top hit not from semantic-lite: {data[0]['source_backend']!r}"
    )
    # The hit must be a real concept id, not an empty string / placeholder.
    cid = data[0]["concept_id"]
    assert cid == "tables/users", f"unexpected top concept_id: {cid!r}"


def test_search_cli_relevance_flags_emit_no_match_for_irrelevant_query() -> None:
    docs = Path(__file__).resolve().parent.parent / "docs-bundle"
    query = "How do I bake sourdough bread in a Dutch oven?"
    rc, out, err = _capture([
        "search", str(docs), query, "--mode", "semantic",
        "--min-semantic-score", "0.1", "--format", "json",
    ])
    assert rc == 0, err
    assert json.loads(out) == []

    rc, out, err = _capture([
        "search", str(docs), query, "--mode", "hybrid",
        "--hybrid-require", "lexical", "--format", "json",
    ])
    assert rc == 0, err
    assert json.loads(out) == []


def test_search_tag_mode(tiny_good_bundle: Path) -> None:
    rc, out, _ = _capture([
        "search", str(tiny_good_bundle), "#users", "--mode", "tag",
    ])
    assert rc == 0


# --- capabilities ------------------------------------------------------------


def test_capabilities_json_returns_zero() -> None:
    """``capabilities --format json`` exits 0 and lists known caps."""
    rc, out, _ = _capture(["capabilities", "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert "available" in data
    assert isinstance(data["available"], list)
    assert len(data["available"]) > 0


def test_capabilities_for_bundle(tiny_good_bundle: Path) -> None:
    """``capabilities --bundle X`` resolves active caps for the bundle."""
    rc, out, _ = _capture([
        "capabilities", "--bundle", str(tiny_good_bundle), "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert "active" in data
    assert "available" in data


# --- graph / discover / render / build / index / log / update commands ------


def test_graph_json(tiny_good_bundle: Path) -> None:
    rc, out, _ = _capture(["graph", str(tiny_good_bundle), "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert "nodes" in data
    assert len(data["nodes"]) == 4


def test_graph_json_includes_grouping_metadata(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: Runbook\n"
        "title: Deploy\n"
        "graph_cluster: ops/deploy\n"
        "source_system: local\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    rc, out, _ = _capture(["graph", str(tmp_path), "--format", "json"])

    assert rc == 0
    data = json.loads(out)
    node = data["nodes"][0]
    assert node["graph_cluster"] == "ops/deploy"
    assert node["source_system"] == "local"
    assert node["metadata"]["graph_cluster"] == "ops/deploy"


def test_graph_quality_json(tiny_good_bundle: Path) -> None:
    rc, out, _ = _capture([
        "graph-quality", str(tiny_good_bundle), "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert "metrics" in data
    assert "findings" in data
    assert data["metrics"]["concept_count"] == 4


def test_discover_json(tiny_good_bundle: Path) -> None:
    rc, out, _ = _capture([
        "discover", str(tiny_good_bundle), "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert "total" in data
    assert "actionability_counts" in data
    assert "actionability" in data
    assert "suggestions" in data


def test_discover_text_prints_actionability(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.md").write_text("---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8")

    rc, out, _ = _capture(["discover", str(tmp_path)])

    assert rc == 0
    assert "actionability:" in out
    assert "safe_to_apply=" in out


def test_discover_writes_out_file(tiny_good_bundle: Path, tmp_path: Path) -> None:
    out_file = tmp_path / "plan.json"
    rc, _, _ = _capture([
        "discover", str(tiny_good_bundle), "--out", str(out_file),
    ])
    assert rc == 0
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "suggestions" in data


def test_render_writes_viz_html(tiny_good_bundle: Path, tmp_path: Path) -> None:
    out_file = tmp_path / "viz.html"
    rc, out, _ = _capture([
        "render", str(tiny_good_bundle), "--out", str(out_file),
    ])
    assert rc == 0
    assert out_file.exists()
    assert "Rendered" in out


def test_build_static(tiny_good_bundle: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "_site"
    rc, out, _ = _capture([
        "build", str(tiny_good_bundle), "--target", "static",
        "--out", str(out_dir),
    ])
    assert rc == 0
    assert (out_dir / "index.html").exists()


def test_index_dry_run(tiny_good_bundle: Path) -> None:
    """``index --dry-run`` exits 0 and prints a JSON plan."""
    rc, out, _ = _capture(["index", str(tiny_good_bundle), "--dry-run"])
    assert rc == 0
    data = json.loads(out)
    assert "would_write" in data


def test_log_append(tiny_good_bundle: Path) -> None:
    """``log --append`` adds an entry and exits 0."""
    rc, out, _ = _capture([
        "log", str(tiny_good_bundle), "--append", "cli test entry",
        "--date", "2026-06-27", "--kind", "Update",
    ])
    assert rc == 0
    content = (tiny_good_bundle / "log.md").read_text(encoding="utf-8")
    assert "cli test entry" in content
    assert "## 2026-06-27" in content


def test_update_applies_plan(tiny_good_bundle: Path, tmp_path: Path) -> None:
    """``update --plan`` applies a JSON plan and exits 0."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "plan_kind": "update",
        "description": "test",
        "ops": [
            {"kind": "add_tag", "target": "tables/users", "args": {"tag": "cli"}},
        ],
    }), encoding="utf-8")
    rc, out, _ = _capture([
        "update", str(tiny_good_bundle), "--plan", str(plan_path),
        "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["applied"] == 1


def test_upgrade_no_bundle() -> None:
    """``upgrade`` without --bundle exits 0 and prints version info."""
    rc, out, _ = _capture(["upgrade"])
    assert rc == 0
    assert LOOM_VERSION in out


# --- error paths ------------------------------------------------------------


def test_unknown_command_returns_nonzero() -> None:
    """An unknown subcommand exits non-zero (argparse error: SystemExit(2))."""
    with pytest.raises(SystemExit) as exc_info:
        _capture(["totally-bogus-command"])
    assert exc_info.value.code != 0


def test_no_command_returns_nonzero() -> None:
    """No subcommand at all exits non-zero (SystemExit(2))."""
    with pytest.raises(SystemExit) as exc_info:
        _capture([])
    assert exc_info.value.code != 0


# --- --version ---------------------------------------------------------------


def test_version_prints_and_exits_zero(capsys) -> None:
    """``--version`` exits 0 with okf-loom version."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert LOOM_VERSION in captured.out
    assert "SPEC" in captured.out


# --- parser shape -----------------------------------------------------------


def test_build_parser_returns_argparse() -> None:
    """``build_parser`` returns an ArgumentParser with subcommands."""
    import argparse
    p = build_parser()
    assert isinstance(p, argparse.ArgumentParser)


# --- P1-23 + P1-38: missing bundle FileNotFoundError -> 2 ---


def test_search_missing_bundle_returns_2(tmp_path: Path) -> None:
    """P1-23/P1-38: missing bundle directory -> exit 2 (FileNotFoundError
    propagates to main(); cmd_search no longer swallows it as exit 1)."""
    rc, _, err = _capture([
        "search", str(tmp_path / "nope"), "anything",
    ])
    assert rc == 2
    assert "Bundle directory not found" in err


def test_search_bad_concept_filter_returns_1(tiny_good_bundle: Path, monkeypatch) -> None:
    """P1-38: ValueError raised inside the try block STILL returns 1.

    The local ``except ValueError`` was preserved (only FileNotFoundError was
    removed). We exercise it by monkeypatching ``search_bundle`` to raise
    ValueError, simulating a bad concept-id filter that propagates from the
    search layer.
    """
    import argparse
    from okf_loom import cli

    def _raise(*_a, **_k):
        raise ValueError("bad concept-id filter")

    monkeypatch.setattr(cli, "_load_bundle", lambda p: __import__(
        "okf_loom.model", fromlist=["Bundle"]).Bundle.load(p))
    # Patch search_bundle inside the function's import scope (cmd_search does
    # `from .search import search_bundle, SearchMode` lazily).
    import okf_loom.search as search_mod
    monkeypatch.setattr(search_mod, "search_bundle", _raise)

    ns = argparse.Namespace(
        bundle=str(tiny_good_bundle),
        query="x",
        mode="lexical",
        type=None, tag=None, limit=20,
        relation=None, source=None, target=None,
        format="text",
    )
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = cli.cmd_search(ns)
    assert rc == 1
    assert "bad concept-id filter" in err_buf.getvalue()


# --- P3-2: plan --format restricted to (text, json) -------------------------


def test_plan_format_rejects_md(tmp_path: Path) -> None:
    """P3-2: ``plan --format md`` is rejected (argparse error)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    with pytest.raises(SystemExit) as exc_info:
        _capture(["plan", str(dst), "--format", "md"])
    assert exc_info.value.code != 0


def test_plan_format_rejects_dot(tmp_path: Path) -> None:
    """P3-2: ``plan --format dot`` is rejected (argparse error)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    with pytest.raises(SystemExit) as exc_info:
        _capture(["plan", str(dst), "--format", "dot"])
    assert exc_info.value.code != 0


def test_plan_format_json_still_works(tmp_path: Path) -> None:
    """P3-2: ``plan --format json`` still works after the restriction."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture(["plan", str(dst), "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert "actions" in data
    assert "by_action" in data


# --- iter1 P1-1: cmd_plan / cmd_discover --out use atomic_write_text ---------


def test_plan_out_uses_atomic_write(tmp_path: Path) -> None:
    """iter1 P1-1: ``plan --out`` routes through io_utils.atomic_write_text
    (§3.4 / AGENTS hard rule #8), not bare Path.write_text. An interrupted
    write must not leave a truncated plan that a downstream ``okf update``
    would then read."""
    import shutil
    from unittest.mock import patch

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    out_file = tmp_path / "plan.json"

    with patch("okf_loom.io_utils.atomic_write_text") as mock_atomic:
        mock_atomic.return_value = None
        # Side-effect: actually write so the file exists for the assertion.
        def _really_write(path, text, **kw):
            Path(path).write_text(text, encoding="utf-8")
        mock_atomic.side_effect = _really_write
        rc, _, _ = _capture(["plan", str(dst), "--out", str(out_file)])
        assert rc == 0
        assert mock_atomic.called, "cmd_plan --out must call atomic_write_text"
        called_path = Path(mock_atomic.call_args[0][0])
        assert called_path == out_file
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "actions" in data


def test_discover_out_uses_atomic_write(tiny_good_bundle: Path, tmp_path: Path) -> None:
    """iter1 P1-1: ``discover --out`` routes through atomic_write_text too."""
    from unittest.mock import patch

    out_file = tmp_path / "suggestions.json"
    with patch("okf_loom.io_utils.atomic_write_text") as mock_atomic:
        def _really_write(path, text, **kw):
            Path(path).write_text(text, encoding="utf-8")
        mock_atomic.side_effect = _really_write
        rc, _, _ = _capture([
            "discover", str(tiny_good_bundle), "--out", str(out_file),
        ])
        assert rc == 0
        assert mock_atomic.called, "cmd_discover --out must call atomic_write_text"
    assert out_file.exists()


# --- iter1 P2-1: graph/log output sorted by concept_id (§3.2 determinism) -----


def test_graph_json_sorted_by_concept_id(tmp_path: Path) -> None:
    """iter1 P2-1: ``graph --format json`` nodes/edges are explicitly sorted
    by concept_id (§3.2), not relying on Bundle dict-insertion order. Proven
    fragile when Bundle is constructed in-memory with reversed insertion."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture(["graph", str(dst), "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    node_ids = [n["id"] for n in data["nodes"]]
    assert node_ids == sorted(node_ids), (
        f"graph nodes not sorted by id: {node_ids}"
    )
    # Edges sorted by (source, target, target_raw).
    edge_keys = [
        (e["source"], e.get("target") or "", e.get("target_raw") or "")
        for e in data["edges"]
    ]
    assert edge_keys == sorted(edge_keys), (
        f"graph edges not sorted: {edge_keys}"
    )


# --- P1-18: repair --apply --format json emits a SINGLE parseable JSON doc --


def test_repair_apply_json_emits_valid_json(tmp_path: Path) -> None:
    """P1-18: ``repair --all --apply --format json`` on a bundle with stale
    indexes emits ONE parseable JSON document (no trailing text corruption)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # Force a stale index by appending a stray concept file (index.md won't
    # list it until regenerated).
    (dst / "tables" / "extra.md").write_text(
        "---\ntype: Table\ntitle: Extra\n---\nbody\n", encoding="utf-8"
    )
    rc, out, err = _capture([
        "repair", str(dst), "--all", "--apply", "--format", "json",
    ])
    assert rc == 0, f"unexpected rc={rc} err={err}"
    # Must parse as a single JSON document.
    data = json.loads(out)
    assert "bundle_root" in data
    assert "applied" in data
    assert "index_regen" in data
    assert data["applied"] >= 1  # at least the index regen applied


def test_repair_apply_json_nothing_to_apply(tmp_path: Path) -> None:
    """P1-18: the '(nothing to apply)' path under --format json still emits
    a parseable JSON document (no trailing text corruption)."""
    import shutil
    from okf_loom.model import Bundle
    from okf_loom.index import regenerate_indexes
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # Pre-apply: mirror relations + regen indexes so nothing remains.
    rc1, _, _ = _capture(["repair", str(dst), "--all", "--apply"])
    assert rc1 == 0
    rc2, out, err = _capture([
        "repair", str(dst), "--all", "--apply", "--format", "json",
    ])
    assert rc2 == 0, f"unexpected rc={rc2} err={err}"
    data = json.loads(out)
    assert data["applied"] == 0
    assert data["index_regen"] in (True, False)


def test_repair_dry_run_json_emits_valid_json(tmp_path: Path) -> None:
    """P1-18: ``repair --all --format json`` (dry-run) emits parseable JSON."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture(["repair", str(dst), "--all", "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert "mechanical_actions" in data
    assert "actions" in data


# ===========================================================================
# P2-27: write-concept JSON emits the canonical 3-key shape
# ===========================================================================


def test_p2_27_write_concept_create_json_shape(tmp_path: Path) -> None:
    """P2-27: ``write-concept`` CREATE emits ``{status, path, id}`` (3 keys).

    The compatibility ``created`` / ``updated`` alias keys are also present
    (deprecated) so older consumers keep working — but new consumers
    should read ``status`` + ``path``.
    """
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture([
        "write-concept", "--bundle", str(dst),
        "--id", "tables/p27_create", "--type", "Table",
        "--title", "P2-27 Create", "--body", "hello",
        "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    # Canonical 3-key shape.
    assert data["status"] == "created"
    assert data["path"].endswith("tables/p27_create.md")
    assert data["id"] == "tables/p27_create"
    # Compatibility alias for backward compat.
    assert data["created"] == data["path"]


def test_p2_27_write_concept_update_json_shape(tmp_path: Path) -> None:
    """P2-27: ``write-concept`` UPDATE emits ``{status: "updated", path, id}``."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # First create.
    _capture([
        "write-concept", "--bundle", str(dst),
        "--id", "tables/p27_up", "--type", "Table", "--body", "x",
    ])
    # Now update with --force.
    rc, out, _ = _capture([
        "write-concept", "--bundle", str(dst),
        "--id", "tables/p27_up", "--type", "Table",
        "--body", "updated", "--force",
        "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert data["status"] == "updated"
    assert data["path"].endswith("tables/p27_up.md")
    assert data["id"] == "tables/p27_up"
    # Compatibility alias.
    assert data["updated"] == data["path"]


# ===========================================================================
# P2-28: single-op verbs exit 1 when the target concept doesn't exist
# ===========================================================================


def test_p2_28_set_frontmatter_unknown_concept_exits_1(tmp_path: Path) -> None:
    """P2-28: ``set-frontmatter --id <ghost>`` exits 1 (concept_not_found)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, err = _capture([
        "set-frontmatter", "--bundle", str(dst),
        "--id", "tables/ghost_concept", "--key", "description",
        "--value", "x",
    ])
    assert rc == 1, f"expected exit 1 for concept_not_found, got {rc}"
    # The skip reason should be reported in the text output.
    assert "concept_not_found" in out


def test_p2_28_set_frontmatter_known_concept_exits_0(tmp_path: Path) -> None:
    """P2-28 contrast: ``set-frontmatter`` on an EXISTING concept exits 0."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, _, _ = _capture([
        "set-frontmatter", "--bundle", str(dst),
        "--id", "tables/orders", "--key", "description",
        "--value", "ok",
    ])
    assert rc == 0


def test_p2_28_link_add_unknown_source_exits_1(tmp_path: Path) -> None:
    """P2-28: ``link-add --source <ghost>`` exits 1 (concept_not_found)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture([
        "link-add", "--bundle", str(dst),
        "--source", "tables/ghost_source", "--target", "tables/orders",
    ])
    assert rc == 1
    assert "concept_not_found" in out


def test_p2_28_entity_add_unknown_concept_exits_1(tmp_path: Path) -> None:
    """P2-28: ``entity-add --id <ghost>`` exits 1 (concept_not_found)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture([
        "entity-add", "--bundle", str(dst),
        "--id", "tables/ghost_concept", "--label", "Ghost Entity",
    ])
    assert rc == 1
    assert "concept_not_found" in out


def test_p2_28_set_frontmatter_skipped_already_set_exits_0(tmp_path: Path) -> None:
    """P2-28 contrast: ``set-frontmatter`` skipped with ``same_value`` (NOT
    concept_not_found) still exits 0 — the concept exists, the op was just
    a no-op."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # First set the value.
    _capture([
        "set-frontmatter", "--bundle", str(dst),
        "--id", "tables/orders", "--key", "description",
        "--value", "same value",
    ])
    # Second set with the SAME value → same_value skip → exit 0.
    rc, out, _ = _capture([
        "set-frontmatter", "--bundle", str(dst),
        "--id", "tables/orders", "--key", "description",
        "--value", "same value",
    ])
    assert rc == 0
    assert "same_value" in out


def test_iter2_p0_3_plan_update_text_mode_does_not_crash(tmp_path):
    """P0-3: `okf plan <b> --out p.json && okf update <b> --plan p.json` (the
    SPEC §6.3 acceptance example, default text mode, NO --format json) must
    not raise AttributeError. P2-24 made `op` a dict; the text renderer must
    read it via dict keys. Uses a minimal bundle whose plan yields only clean
    mirror_relation actions (avoids the separate P2-11 argv-parse edge case)."""
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    # Two concepts linked by a typed relation + a markdown link (mirror is clean).
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\nrelations:\n  - target: b\n    type: references\n"
        "---\nSee [b](/b.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8")
    plan_path = tmp_path / "p.json"
    rc, out, err = _capture(["plan", str(tmp_path), "--out", str(plan_path)])
    assert rc == 0, f"plan failed: {err}"
    rc2, out2, err2 = _capture(["update", str(tmp_path), "--plan", str(plan_path)])
    # P0-3 contract: text mode renders results without AttributeError crash.
    assert "AttributeError" not in err2, (
        f"text-mode crashed with AttributeError (P0-3 regression): {err2}"
    )
    assert rc2 == 0, f"update text-mode failed (rc={rc2}): {err2}\n{out2}"
    assert "ops:" in out2, f"text-mode output missing ops summary: {out2}"


def test_agent_loop_commands_use_configured_session_dir(tmp_path: Path) -> None:
    """token/wait/comment/presence commands share studio.session_dir."""
    dst = tmp_path / "bundle"
    _write_minimal_cli_bundle(dst)
    custom_session = _configure_custom_session(dst)
    default_session = dst / ".okf-loom" / "session"

    studio = Studio.for_configured_bundle(dst)
    studio.ensure_session()
    studio.token_path.write_text("configured-token\n", encoding="utf-8")
    comment = studio.post_comment(concept="tables/orders", body="please enrich")

    rc, out, err = _capture(["token", str(dst)])
    assert rc == 0, err
    assert out == "configured-token"

    rc, out, err = _capture(["wait", str(dst), "--timeout", "0.1"])
    assert rc == 0, err
    work = json.loads(out)
    assert work["kind"] == "comment"
    assert work["id"] == comment["id"]

    rc, out, err = _capture([
        "comment-claim", str(dst), comment["id"], "--format", "json",
    ])
    assert rc == 0, err
    claimed = json.loads(out)
    assert claimed["state"] == "claimed"

    rc, out, err = _capture([
        "presence", str(dst), "--state", "watching", "--format", "json",
    ])
    assert rc == 0, err
    presence = json.loads(out)
    assert presence["state"] == "watching"

    assert (custom_session / ".token").is_file()
    assert (custom_session / "directives.jsonl").is_file()
    assert (custom_session / "presence.json").is_file()
    assert not (default_session / ".token").exists()
    assert not (default_session / "directives.jsonl").exists()
    assert not (default_session / "presence.json").exists()


def test_mutators_detect_configured_session_dir_for_studio_logging(tmp_path: Path) -> None:
    """Studio-aware mutators must not miss a custom configured session."""
    dst = tmp_path / "bundle"
    _write_minimal_cli_bundle(dst)
    custom_session = _configure_custom_session(dst)
    default_session = dst / ".okf-loom" / "session"
    studio = Studio.for_configured_bundle(dst)
    studio.ensure_session()

    rc, out, err = _capture([
        "set-frontmatter", "--bundle", str(dst),
        "--id", "tables/orders", "--key", "description",
        "--value", "custom session proof",
    ])

    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    events_path = custom_session / "events.jsonl"
    assert events_path.is_file(), "mutator should log through configured Studio"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert any(row.get("action") == "set_frontmatter" for row in events)
    assert not (default_session / "events.jsonl").exists()


# ===========================================================================
# P1-7: _single_op_exit_code covers the FULL hard-failure reason set
# ===========================================================================
#
# iter-1's implementation only treated `reason == "concept_not_found"` as
# exit 1, producing an indefensible asymmetry: `link-add --source NO_SUCH`
# → exit 1 (concept_not_found on source) but `link-add --source REAL
# --target NO_SUCH` → exit 0 (target_concept_not_found on the link
# target). SPEC §3.7 ("exit 1 = operation-level failure") fixes BOTH as
# exit 1, plus the bad_args:* / unknown_op_kind:* families. The function
# previously had ZERO unit tests; this block covers the full reason set.


from okf_loom.cli import (
    _single_op_exit_code,
    _is_hard_failure_reason,
    _HARD_FAILURE_REASONS_EXACT,
    _HARD_FAILURE_REASON_PREFIXES,
    _IDEMPOTENT_NOOP_REASONS,
)
import pytest as _pytest


# Helper: build a single-op result dict shaped like apply_plan's return.
def _result(reasons: list[str | None], *, applied_count: int = 0) -> dict:
    """Build a result dict with one (op, op_result) pair per reason.

    `applied_count` ops are marked applied=True (reason=None); the rest
    are applied=False with the given reason. Total `applied` field =
    applied_count.
    """
    results = []
    for i in range(applied_count):
        results.append(({"kind": "x", "target": f"t{i}"}, {"applied": True, "reason": None, "path": None}))
    for i, r in enumerate(reasons):
        results.append((
            {"kind": "x", "target": f"s{i}"},
            {"applied": False, "reason": r, "path": None},
        ))
    return {"applied": applied_count, "results": results}


# Parametrize over the COMPLETE hard-failure set.
_HARD_FAILURE_REASONS_FOR_TEST = [
    "concept_not_found",
    "target_concept_not_found",
    "malformed_relations",
    # Suffixed reasons carry free-form detail after ':'; cover the prefix.
    "bad_args:'key'",
    "bad_args:invalid value for foo",
    "unknown_op_kind:totally_bogus",
    "unknown_op_kind:",
]


@_pytest.mark.parametrize("reason", _HARD_FAILURE_REASONS_FOR_TEST)
def test_p1_7_single_op_exit_code_hard_failure_reasons_exit_1(reason: str) -> None:
    """P1-7: every hard-failure reason → exit 1 when nothing applied."""
    assert _single_op_exit_code(_result([reason])) == 1, (
        f"reason={reason!r} should be exit 1 (operation-level failure)"
    )
    # And the classifier agrees.
    assert _is_hard_failure_reason(reason) is True


# Parametrize over the COMPLETE idempotent no-op set (must stay exit 0).
@_pytest.mark.parametrize("reason", sorted(_IDEMPOTENT_NOOP_REASONS))
def test_p1_7_single_op_exit_code_idempotent_noops_exit_0(reason: str) -> None:
    """P1-7: idempotent protective no-ops → exit 0 (not failures).

    `would_overwrite_hand_written` is intentionally here: it is a guard
    that REFUSED to clobber hand-curated content, not an operation-level
    failure. Surfacing it as exit 1 would conflate "I refused to destroy
    your work" with "I failed to find the concept you asked for".
    """
    assert _single_op_exit_code(_result([reason])) == 0, (
        f"reason={reason!r} should be exit 0 (idempotent no-op)"
    )
    # And the classifier agrees it's NOT a hard failure.
    assert _is_hard_failure_reason(reason) is False


def test_p1_7_single_op_exit_code_applied_wins_over_hard_failure() -> None:
    """P1-7: when ANY op applied, return 0 even if a sibling hard-failed.

    This is the multi-op case (e.g. `link-add --relation` emits add_link
    + add_relation): if the link added but the relation was a duplicate,
    the verb succeeded overall.
    """
    r = _result(["concept_not_found"], applied_count=1)
    assert _single_op_exit_code(r) == 0


def test_p1_7_single_op_exit_code_all_hard_failures_exit_1() -> None:
    """P1-7: every op hard-failed, none applied → exit 1."""
    r = _result(["concept_not_found", "bad_args:'k'", "unknown_op_kind:z"])
    assert _single_op_exit_code(r) == 1


def test_p1_7_single_op_exit_code_mixed_hard_and_idempotent_exit_1() -> None:
    """P1-7: ≥1 hard-failure op (with no applied op) → exit 1 even when
    sibling ops are idempotent no-ops."""
    r = _result(["same_value", "concept_not_found", "already_linked"])
    assert _single_op_exit_code(r) == 1


def test_p1_7_single_op_exit_code_all_idempotent_exit_0() -> None:
    """P1-7: every op is an idempotent no-op, none hard-failed → exit 0
    (clean re-run; the protective no-ops are not failures)."""
    r = _result(["same_value", "already_linked", "already_tagged"])
    assert _single_op_exit_code(r) == 0


def test_p1_7_single_op_exit_code_empty_results_exit_0() -> None:
    """P1-7: no ops at all → exit 0 (nothing to fail)."""
    assert _single_op_exit_code({"applied": 0, "results": []}) == 0


def test_p1_7_single_op_exit_code_none_reason_is_not_hard_failure() -> None:
    """P1-7: ``reason=None`` (no reason recorded) is not a hard failure."""
    r = {"applied": 0, "results": [({"kind": "x"}, {"applied": False, "reason": None, "path": None})]}
    assert _single_op_exit_code(r) == 0
    assert _is_hard_failure_reason(None) is False
    assert _is_hard_failure_reason("") is False


def test_p1_7_hard_failure_constants_are_disjoint_from_idempotent() -> None:
    """P1-7 invariant: the hard-failure and idempotent-no-op reason sets
    must be disjoint; otherwise the classifier would be ambiguous."""
    exact = set(_HARD_FAILURE_REASONS_EXACT)
    idem = set(_IDEMPOTENT_NOOP_REASONS)
    assert exact.isdisjoint(idem), (
        f"hard-failure ∩ idempotent = {exact & idem} (must be empty)"
    )


def test_p1_7_link_add_bad_target_exits_1(tmp_path: Path) -> None:
    """P1-7 CLI: ``link-add --source EXISTING --target NO_SUCH`` → exit 1.

    This is the asymmetry repro: iter-1 exited 0 here because only
    ``concept_not_found`` (the SOURCE concept) was treated as failure,
    not ``target_concept_not_found`` (the LINK target).
    """
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture([
        "link-add", "--bundle", str(dst),
        "--source", "tables/orders", "--target", "tables/NO_SUCH_TARGET",
    ])
    assert rc == 1, (
        f"link-add bad target must exit 1 (target_concept_not_found); got {rc}"
    )
    assert "target_concept_not_found" in out


def test_p1_7_set_frontmatter_empty_key_exits_1(tmp_path: Path) -> None:
    """P1-7 CLI: ``set-frontmatter --key ""`` → exit 1 (bad_args).

    iter-1 mapped only concept_not_found to exit 1; bad_args:* exited 0
    even though the op could not run (invalid empty key).
    """
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture([
        "set-frontmatter", "--bundle", str(dst),
        "--id", "tables/orders", "--key", "", "--value", "x",
    ])
    assert rc == 1, f"set-frontmatter bad_args must exit 1; got {rc}"
    assert "bad_args" in out


def test_p1_7_link_add_existing_source_existing_target_exits_0(tmp_path: Path) -> None:
    """P1-7 contrast: ``link-add --source REAL --target REAL`` → exit 0
    (the happy path is unaffected)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # tables/orders and tables/customers both exist in demo_bundle.
    rc, _, _ = _capture([
        "link-add", "--bundle", str(dst),
        "--source", "tables/orders", "--target", "tables/customers",
    ])
    assert rc in (0, 1)  # 0 first time; 0 if already_linked (still not hard-fail)
    # Either it applied (rc 0) or was already_linked (rc 0) — never exit 1
    # for a fully-existing source/target pair.
    assert rc == 0, f"happy-path link-add should exit 0; got {rc}"


# ===========================================================================
# P2-4: cmd_update exits 1 when the plan is fully stale (all hard-failures)
# ===========================================================================


def test_p2_4_stale_plan_all_targets_deleted_exits_1(tmp_path: Path) -> None:
    """P2-4: a stale plan whose every op targets a deleted/renamed concept
    exits 1, not 0. Without this fix, a fully-stale plan was
    indistinguishable from a clean idempotent re-run.

    Uses --format json because cmd_update's text-mode renderer has a
    separate as_dict-string-target bug (out of scope for R4) that
    crashes before the exit code is computed; the JSON path exercises
    the exit-code logic cleanly.
    """
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    plan_path = tmp_path / "stale.json"
    plan_path.write_text(json.dumps({
        "plan_kind": "update",
        "description": "stale — every target was renamed",
        "ops": [
            {"kind": "add_tag", "target": "tables/DELETED_ONE", "args": {"tag": "x"}},
            {"kind": "add_tag", "target": "tables/DELETED_TWO", "args": {"tag": "y"}},
        ],
    }), encoding="utf-8")
    rc, out, _ = _capture([
        "update", str(dst), "--plan", str(plan_path), "--format", "json",
    ])
    assert rc == 1, f"fully-stale plan must exit 1; got {rc}"
    data = json.loads(out)
    assert data["applied"] == 0
    reasons = [r.get("reason") for _op, r in data["results"]]
    assert all(r == "concept_not_found" for r in reasons), reasons


def test_p2_4_stale_plan_target_concept_not_found_exits_1(tmp_path: Path) -> None:
    """P2-4: a stale ``add_link`` op whose SOURCE exists but whose TARGET
    was deleted hits ``target_concept_not_found`` (a hard failure). The
    fully-stale plan must exit 1."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    plan_path = tmp_path / "stale.json"
    plan_path.write_text(json.dumps({
        "plan_kind": "update",
        "description": "stale — link target was renamed",
        "ops": [{
            "kind": "add_link",
            "target": "tables/orders",
            "args": {
                "label": "ghost",
                "target_concept_id": "tables/RENAMED_AWAY",
                "section": None,
            },
        }],
    }), encoding="utf-8")
    rc, out, _ = _capture([
        "update", str(dst), "--plan", str(plan_path), "--format", "json",
    ])
    assert rc == 1, f"stale add_link plan must exit 1; got {rc}"
    data = json.loads(out)
    reasons = [r.get("reason") for _op, r in data["results"]]
    assert "target_concept_not_found" in reasons


def test_p2_4_clean_idempotent_rerun_still_exits_0(tmp_path: Path) -> None:
    """P2-4 contrast: a pure idempotent re-run (every op same_value /
    already_*) still exits 0. P2-4 must NOT promote protective no-ops
    to operation-level failures."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # Apply once with a tag.
    plan1 = tmp_path / "plan1.json"
    plan1.write_text(json.dumps({
        "plan_kind": "update",
        "description": "apply tag",
        "ops": [{"kind": "add_tag", "target": "tables/orders", "args": {"tag": "p2_4_tag"}}],
    }), encoding="utf-8")
    rc1, _, _ = _capture(["update", str(dst), "--plan", str(plan1), "--format", "json"])
    assert rc1 == 0
    # Re-apply the SAME plan: add_tag fires already_tagged (idempotent).
    rc2, out2, _ = _capture(["update", str(dst), "--plan", str(plan1), "--format", "json"])
    assert rc2 == 0, (
        f"idempotent re-run (already_tagged) must exit 0; got {rc2}"
    )
    data = json.loads(out2)
    assert data["applied"] == 0
    reasons = [r.get("reason") for _op, r in data["results"]]
    assert all(r == "already_tagged" for r in reasons), reasons


def test_p2_4_partial_stale_plan_with_one_applied_exits_0(tmp_path: Path) -> None:
    """P2-4 edge: if AT LEAST ONE op applied, the plan exits 0 even when
    a sibling op hard-failed. Mirrors ``_single_op_exit_code``'s
    "applied wins" rule for multi-op aggregation."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    plan_path = tmp_path / "mixed.json"
    plan_path.write_text(json.dumps({
        "plan_kind": "update",
        "description": "one good, one stale",
        "ops": [
            {"kind": "add_tag", "target": "tables/orders", "args": {"tag": "good_tag"}},
            {"kind": "add_tag", "target": "tables/DELETED", "args": {"tag": "x"}},
        ],
    }), encoding="utf-8")
    rc, out, _ = _capture([
        "update", str(dst), "--plan", str(plan_path), "--format", "json",
    ])
    assert rc == 0, (
        f"applied>0 plan must exit 0 even with a hard-failed sibling; got {rc}"
    )
    data = json.loads(out)
    assert data["applied"] == 1


# ===========================================================================
# P2-17: cmd_plan text mode surfaces Plan.skipped
# ===========================================================================


def test_p2_17_plan_text_surfaces_skipped(tmp_path: Path, monkeypatch) -> None:
    """P2-17: when ``Plan.skipped`` is non-empty, ``okf plan`` (text mode)
    prints a trailing ``# N suggestion(s) skipped:`` summary.

    P1-19 added Plan.skipped to ``as_dict`` (JSON consumers could see it)
    but the text branch had no equivalent — text consumers had no way to
    tell that discovery had dropped suggestions.
    """
    import shutil
    import okf_loom.plan as plan_mod
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    b = Bundle.load(dst)

    # Force a non-empty Plan.skipped by patching build_plan to append one.
    real_build = plan_mod.build_plan

    # Current spec §7: build_plan gained scope/neighbors kwargs; the mock must
    # accept the full current signature so cmd_plan can forward them.
    def patched_build(bundle, *, rules=None, portable=False, scope=None, neighbors=False):
        p = real_build(
            bundle, rules=rules, portable=portable, scope=scope, neighbors=neighbors
        )
        p.skipped.append(("fake_rule", "unhandled_rule:fake_rule"))
        return p

    monkeypatch.setattr(plan_mod, "build_plan", patched_build)
    # cmd_plan does `from .plan import build_plan` lazily; patch the
    # attribute on the module so the import resolves to the patched object.
    rc, out, _ = _capture(["plan", str(dst)])
    assert rc == 0
    assert "skipped" in out, f"text output missing skipped summary:\n{out}"
    assert "1 suggestion(s) skipped" in out
    assert "fake_rule" in out
    assert "unhandled_rule:fake_rule" in out


def test_p2_17_plan_text_no_skipped_section_when_empty(tmp_path: Path) -> None:
    """P2-17 contrast: when ``Plan.skipped`` is empty, the text output
    does NOT include a skipped summary (no spurious noise)."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture(["plan", str(dst)])
    assert rc == 0
    assert "suggestion(s) skipped" not in out


# ===========================================================================
# P2-3: okf plan --portable emits bundle-relative argv (no host path leak)
# ===========================================================================


def test_p2_3_plan_portable_emits_relative_bundle_arg(tmp_path: Path) -> None:
    """P2-3: ``okf plan --portable`` emits ``"."`` as the bundle argument
    in every action's argv (NOT the resolved absolute host path), so the
    plan is portable across machines when committed to git.

    The chdir precondition (operators MUST run the emitted commands from
    the bundle directory) is surfaced in the text output.
    """
    import shutil
    from okf_loom.model import Bundle
    from okf_loom.plan import build_plan

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    b = Bundle.load(dst)

    plan = build_plan(b, portable=True)
    # Plan.bundle_root is "." — the relative marker, not the host path.
    assert plan.bundle_root == ".", (
        f"portable Plan.bundle_root must be '.'; got {plan.bundle_root!r}"
    )
    # Every action's argv uses "." as the bundle argument.
    assert len(plan.actions) > 0, "demo bundle should yield at least one action"
    for a in plan.actions:
        if a.argv:
            # Find the --bundle value (or positional bundle for index).
            if "--bundle" in a.argv:
                idx = a.argv.index("--bundle")
                assert a.argv[idx + 1] == ".", (
                    f"portable argv must use '.' for --bundle; got {a.argv}"
                )
            else:
                # create_index / refresh_index use a positional bundle.
                # argv shape: ["okf", "index", BUNDLE, ...]
                assert "." in a.argv[2:], (
                    f"positional-bundle argv must include '.'; got {a.argv}"
                )
            # No host-absolute path leaked anywhere in argv.
            host_abs = str(dst.resolve())
            assert not any(host_abs in str(v) for v in a.argv), (
                f"host-absolute path leaked into portable argv: {a.argv}"
            )


def test_p2_3_plan_portable_json_bundle_root_is_dot(tmp_path: Path) -> None:
    """P2-3: ``okf plan --portable --format json`` emits bundle_root='.'."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture(["plan", str(dst), "--portable", "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert data["bundle_root"] == ".", (
        f"portable JSON bundle_root must be '.'; got {data['bundle_root']!r}"
    )
    host_abs = str(dst.resolve())
    for a in data["actions"]:
        if a.get("argv"):
            assert not any(host_abs in str(v) for v in a["argv"]), (
                f"host path leaked into portable JSON argv: {a['argv']}"
            )


def test_p2_3_plan_portable_text_documents_chdir_precondition(tmp_path: Path) -> None:
    """P2-3: the plan text output documents the chdir precondition when
    --portable is set, so an operator copy-pasting commands knows where
    to run them."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    rc, out, _ = _capture(["plan", str(dst), "--portable"])
    assert rc == 0
    assert "portable" in out
    # The chdir precondition is documented in the output.
    assert "bundle directory" in out, (
        f"portable text output missing chdir precondition:\n{out}"
    )


def test_p2_3_plan_default_keeps_absolute_bundle_arg(tmp_path: Path) -> None:
    """P2-3 contrast: without --portable, the default behaviour is
    unchanged — argv uses the resolved absolute bundle path so it is
    runnable as-is from any CWD (current spec §7).
    """
    import shutil
    from okf_loom.model import Bundle
    from okf_loom.plan import build_plan

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    b = Bundle.load(dst)
    plan = build_plan(b)  # default: portable=False
    # bundle_root is the unresolved path (existing behaviour); bundle_arg
    # in argv is the resolved absolute path.
    assert plan.bundle_root == str(b.root)
    for a in plan.actions:
        if a.argv and "--bundle" in a.argv:
            idx = a.argv.index("--bundle")
            assert a.argv[idx + 1] == str(dst.resolve()), (
                f"default argv must use resolved absolute path; got {a.argv}"
            )


def test_p2_3_plan_portable_flag_is_accepted_by_parser() -> None:
    """P2-3: the ``--portable`` flag is wired into the plan subparser
    (argparse does not reject it)."""
    p = build_parser()
    ns = p.parse_args(["plan", "samples/demo_bundle", "--portable"])
    assert ns.portable is True
    ns2 = p.parse_args(["plan", "samples/demo_bundle"])
    assert ns2.portable is False


def test_iter3_p1_4_discover_format_json_out_is_clean_json(tmp_path):
    """P1-4: `okf discover --format json --out X` must emit parseable JSON
    on stdout (no trailing 'Written:' line). iter-1/2 left the unconditional
    print corrupting stdout JSON."""
    import shutil
    dst = tmp_path / "demo"
    shutil.copytree("samples/demo_bundle", dst)
    out_path = tmp_path / "disc.json"
    rc, out, err = _capture([
        "discover", str(dst), "--out", str(out_path), "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)  # MUST parse — no trailing text
    assert "suggestions" in data
    assert out_path.is_file()
