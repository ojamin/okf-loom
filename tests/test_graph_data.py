"""Browser-free proof that ``build_graph_data`` produces render-ready JSON.

The full render proof lives in ``tests/test_viewer_browser.py`` and is gated on
the optional browser-proof dependencies + a Chromium binary, so it is SKIPPED in the
default ``pytest`` run. That left a gap: a regression that dropped graph nodes,
left labels blank, or skipped colour assignment could ship green because the
only test that would catch it never ran without the extra.

This module closes that gap with a BROWSER-FREE data-layer proof that runs in
every suite. It asserts the invariants the Cytoscape view needs to render
anything meaningful:

  * exactly one node per concept (no drops, no duplicates);
  * every node carries a non-empty label and a colour; and
  * the set of node ids equals the set of concept ids.

These run on the always-shipped ``samples/demo_bundle`` so they never skip.
"""
from __future__ import annotations

from okf_loom.model import Bundle
from okf_loom.paths import concept_id_to_str
from okf_loom.render import build_graph_data

DEMO = "samples/demo_bundle"


def test_graph_data_node_count_equals_concept_count() -> None:
    """``build_graph_data`` emits exactly one node per concept."""
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    nodes = data["nodes"]
    assert len(nodes) == len(b.concepts), (
        f"node count {len(nodes)} != concept count {len(b.concepts)}; "
        f"a concept was dropped or duplicated in the graph payload"
    )


def test_graph_data_every_node_has_label_and_color() -> None:
    """Every node carries a non-empty label and a colour.

    These are the two fields Cytoscape needs to render a visible, labelled
    node (``background-color: data(color)`` and ``label: data(label)``); a
    missing/blank value would render an invisible or unlabelled node.
    """
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    assert data["nodes"], "graph data has no nodes"
    for node in data["nodes"]:
        d = node["data"]
        assert d.get("label"), (
            f"node {d.get('id')!r} is missing/empty label (renders unlabelled)"
        )
        assert d.get("color"), (
            f"node {d.get('id')!r} is missing/empty color (renders invisible)"
        )


def test_graph_data_node_ids_match_concept_ids() -> None:
    """The set of node ids equals the set of concept ids (no drops/dupes)."""
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    node_ids = {n["data"]["id"] for n in data["nodes"]}
    concept_ids = {concept_id_to_str(c.id) for c in b.concepts.values()}
    assert node_ids == concept_ids, (
        f"node ids != concept ids: missing={sorted(concept_ids - node_ids)} "
        f"extra={sorted(node_ids - concept_ids)}"
    )


def test_graph_data_exposes_allowlisted_grouping_metadata(tmp_path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: Runbook\n"
        "title: Deploy\n"
        "graph_cluster: ops/deploy\n"
        "source_system: local\n"
        "secret_owner: do-not-ship\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    data = build_graph_data(Bundle.load(tmp_path))
    node = data["nodes"][0]["data"]

    assert node["graph_cluster"] == "ops/deploy"
    assert node["source_system"] == "local"
    assert node["metadata"]["graph_cluster"] == "ops/deploy"
    assert "secret_owner" not in node["metadata"]


def test_graph_data_exposes_alias_object_labels(tmp_path) -> None:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: Design\n"
        "title: Design System\n"
        "aliases:\n"
        "  - label: Architecture\n"
        "    discoverable: false\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    data = build_graph_data(Bundle.load(tmp_path))
    node = data["nodes"][0]["data"]

    assert node["aliases"] == ["Architecture"]


def test_graph_data_prefers_typed_edges_but_keeps_distinct_relation_types(
    tmp_path,
) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntype: T\nrelations:\n"
        "  - {target: b, type: references}\n"
        "  - {target: b, type: depends_on}\n"
        "  - {target: /b.md, type: references, detail: duplicate}\n"
        "---\nSee [B](b.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    edges = build_graph_data(Bundle.load(tmp_path))["edges"]
    pair = [
        e["data"] for e in edges
        if e["data"]["source"] == "a" and e["data"]["target"] == "b"
    ]
    assert {e["label"] for e in pair} == {"references", "depends_on"}
    assert all(e["origin"] == "relation" for e in pair)
