"""Unit coverage for the server-side Flow Atlas findings payload."""
from __future__ import annotations

from pathlib import Path

from okf_loom.model import Bundle
from okf_loom.render import build_graph_data, build_graph_findings


DOCS_BUNDLE = Path(__file__).resolve().parents[1] / "docs-bundle"


def test_docs_bundle_has_well_formed_flow_atlas_findings() -> None:
    bundle = Bundle.load(DOCS_BUNDLE)
    findings = build_graph_findings(bundle)

    assert isinstance(findings, list)
    assert findings
    assert build_graph_data(bundle)["findings"] == findings
    for finding in findings:
        assert finding["kind"]
        assert finding["message"]
        assert isinstance(finding["score"], (int, float))
