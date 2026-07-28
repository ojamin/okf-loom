"""Flow Atlas graph page contract tests."""

from __future__ import annotations

from pathlib import Path

from okf_loom.model import Bundle
from okf_loom.render import _render_graph_page, build_graph_data


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs-bundle"


def test_graph_page_emits_flow_atlas_shell() -> None:
    bundle = Bundle.load(DOCS)
    html = _render_graph_page(
        bundle, mode="serve", name="docs", config={"theme": "dark", "cdn": True}
    )
    assert 'data-atlas-mode="discover"' in html
    assert 'id="okf-mode-switch"' in html
    assert "data-mode=\"discover\"" in html
    assert "data-mode=\"flows\"" in html
    assert "data-mode=\"atlas\"" in html
    assert 'id="okf-discover"' in html
    assert "flow-atlas.js" in html
    assert "__data/discover.json" in html or "/__data/discover.json" in html
    assert "Flow Atlas" in html


def test_graph_data_includes_findings_for_static_inline() -> None:
    bundle = Bundle.load(DOCS)
    data = build_graph_data(bundle, name="docs")
    assert isinstance(data.get("findings"), list)
    assert data["findings"], "docs-bundle should produce at least one finding"
    html = _render_graph_page(
        bundle,
        mode="static",
        name="docs",
        config={"theme": "light", "cdn": False},
        graph_data=data,
    )
    assert "okf-graph-data" in html
    assert "findings" in html
