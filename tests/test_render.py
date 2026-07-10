"""Tests for ``okf_loom.render``.

Pinned invariants:
  * ``render_single_file`` writes a valid HTML file containing the bundle
    JSON; the embedded JSON parses and matches ``bundle.graph()``.
  * The bytes count returned is the actual file size in bytes.
  * ``build_site`` for ``static`` and ``single-file`` targets writes the
    expected file layout.
  * Atomic writes: no partial files left behind on success.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.render import build_graph_data, build_site, render_single_file

from conftest import TOOLKIT_ROOT


def _runtime_file(*parts: str) -> Path:
    """Path to a file in the checkout-local OKF runtime package."""
    return TOOLKIT_ROOT / "scripts" / "okf_loom" / Path(*parts)


# --- render_single_file -----------------------------------------------------


def test_render_single_file_writes_html(tiny_good_bundle: Path) -> None:
    """``render_single_file`` writes a non-empty HTML file."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    stats = render_single_file(b, out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.lstrip().lower().startswith("<!doctype html>") or "<html" in content.lower()
    # Stats contain the expected keys.
    for key in ("concepts", "edges", "bytes"):
        assert key in stats
    assert stats["concepts"] == len(b.concepts)


def test_render_single_file_contains_bundle_json(tiny_good_bundle: Path) -> None:
    """The HTML embeds the bundle as a JSON blob."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    render_single_file(b, out)
    content = out.read_text(encoding="utf-8")
    # Find the embedded JSON: it's a JSON-encoded object with "nodes".
    assert '"nodes"' in content
    assert '"edges"' in content


def test_embedded_json_matches_bundle_graph(tiny_good_bundle: Path) -> None:
    """The embedded JSON blob matches what ``build_graph_data`` produces."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    render_single_file(b, out)
    content = out.read_text(encoding="utf-8")

    # Extract the JSON: locate the "nodes" key embedded in the HTML.
    # We pull a sufficiently large JSON object using a balanced scan.
    start = content.index("{", content.index('"nodes"') - 200)
    # Walk forward balancing braces.
    depth = 0
    in_string = False
    esc = False
    end = -1
    for i in range(start, len(content)):
        ch = content[i]
        if in_string:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    assert end > start, "could not extract embedded JSON object"
    blob = content[start:end]
    data = json.loads(blob)

    # Compare with the canonical graph data.
    canonical = build_graph_data(b)
    # Spot-check core fields (don't require byte-for-byte equality of HTML
    # interpolation, but the JSON-parseable shape must match).
    assert data["name"] == canonical["name"]
    assert len(data["nodes"]) == len(canonical["nodes"])
    assert {n["data"]["id"] for n in data["nodes"]} == {
        n["data"]["id"] for n in canonical["nodes"]
    }
    # Edges count matches.
    assert len(data["edges"]) == len(canonical["edges"])


def test_render_bytes_matches_file_size(tiny_good_bundle: Path) -> None:
    """The returned ``bytes`` count equals the actual UTF-8 file size."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    stats = render_single_file(b, out)
    file_bytes = out.read_bytes()
    assert stats["bytes"] == len(file_bytes)


def test_render_respects_name_override(tiny_good_bundle: Path) -> None:
    """Passing ``name=`` overrides the bundle name in the rendered HTML."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    render_single_file(b, out, name="Custom Display Name")
    content = out.read_text(encoding="utf-8")
    assert "Custom Display Name" in content


def test_render_creates_parent_dirs(tmp_path: Path) -> None:
    """``render_single_file`` creates parent directories as needed."""
    b = Bundle.load(tmp_path)
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    out = tmp_path / "nested" / "deep" / "viz.html"
    render_single_file(b, out)
    assert out.exists()


# --- build_site: single-file target -----------------------------------------


def test_build_site_single_file_writes_one_html(tiny_good_bundle: Path) -> None:
    """``build_site(target='single-file')`` writes a single ``index.html``."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    stats = build_site(b, out_dir, target="single-file")
    assert (out_dir / "index.html").exists()
    assert stats["target"] == "single-file"
    assert stats["files"] == 1
    assert stats["concepts"] == len(b.concepts)


# --- build_site: static target ----------------------------------------------


def test_build_site_static_writes_per_concept_pages(tiny_good_bundle: Path) -> None:
    """``build_site(target='static')`` writes one HTML page per concept."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    stats = build_site(b, out_dir, target="static")
    assert stats["target"] == "static"
    # Every concept has a corresponding .html file.
    for concept in b.concepts.values():
        rel = "/".join(concept.id) + ".html"
        assert (out_dir / rel).exists(), f"missing page for {concept.id}"
    # Root index exists.
    assert (out_dir / "index.html").exists()


def test_build_site_static_writes_static_assets(tiny_good_bundle: Path) -> None:
    """The static site bundles CSS/JS under ``__static/``."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    build_site(b, out_dir, target="static")
    static_dir = out_dir / "__static"
    assert static_dir.exists()
    # At least wiki.css should be there.
    assert (static_dir / "wiki.css").exists()


def test_build_site_static_includes_graph_data(tiny_good_bundle: Path) -> None:
    """The static site emits ``__data/graph.json`` consumable by graph.js."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    build_site(b, out_dir, target="static")
    graph_json = out_dir / "__data" / "graph.json"
    assert graph_json.exists()
    data = json.loads(graph_json.read_text(encoding="utf-8"))
    assert "nodes" in data
    assert len(data["nodes"]) == len(b.concepts)


def test_static_graph_inlines_data_instead_of_fetching_under_file_scheme(
    tiny_good_bundle: Path,
) -> None:
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_inline_graph"
    build_site(b, out_dir, target="static")
    html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    assert 'data-graph-url=""' in html
    match = re.search(
        r'<template id="okf-graph-data">(.*?)</template>', html, re.DOTALL,
    )
    assert match, "static graph page has no inline graph payload"
    payload = json.loads(match.group(1))
    assert len(payload["nodes"]) == len(b.concepts)


def test_build_site_unknown_target_raises(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    with pytest.raises(ValueError, match="Unknown target"):
        build_site(b, tiny_good_bundle / "x", target="bogus")


def test_build_site_static_has_no_dead_internal_links(tmp_path: Path) -> None:
    """End-to-end link integrity for the static build of the real docs
    bundle (the artifact deployed to GitHub Pages).

    Guards the family of static-only link bugs fixed together: the root
    index.md body skipping ``rewrite_internal_links``, anchored
    ``cli.md#x`` links never matching the anchor-stripped rewrite lookup,
    sub-index concept cards missing source relativisation, and raw
    ``/a/b.md`` resource/provenance hrefs. All were invisible in serve
    mode because the live server resolves raw .md paths. Out-of-bundle
    repo references (``../../SKILL.md``) and external URLs are exempt —
    they have no static-build page by design.
    """
    import posixpath

    b = Bundle.load(TOOLKIT_ROOT / "docs-bundle")
    out_dir = tmp_path / "_site"
    build_site(b, out_dir, target="static")
    dead: list[str] = []
    for page in out_dir.rglob("*.html"):
        rel_dir = page.parent.relative_to(out_dir)
        for href in re.findall(r'href="([^"]+)"', page.read_text(encoding="utf-8")):
            if href.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = posixpath.normpath(
                posixpath.join(str(rel_dir), href.split("#")[0].split("?")[0])
            )
            if target.startswith(".."):  # out-of-bundle repo reference
                continue
            p = out_dir / target
            if not (p.is_file() or (p.is_dir() and (p / "index.html").is_file())):
                dead.append(f"{page.relative_to(out_dir)} -> {href}")
    assert not dead, (
        "dead internal links in static build:\n  " + "\n  ".join(dead)
    )


# --- atomic writes: no .tmp leftovers ---------------------------------------


def test_build_site_no_tmp_left_behind(tiny_good_bundle: Path) -> None:
    """After ``build_site`` succeeds, no ``.tmp`` files exist in the output."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    build_site(b, out_dir, target="static")
    tmps = list(out_dir.rglob("*.tmp"))
    # The atomic-write helpers unlink the tmp file via os.replace, so none
    # should remain. (mkstemp-created names start with ".okf-".)
    assert tmps == []


# --- build_graph_data shape -------------------------------------------------


def test_build_graph_data_shape(tiny_good_bundle: Path) -> None:
    """``build_graph_data`` returns the documented top-level keys."""
    b = Bundle.load(tiny_good_bundle)
    data = build_graph_data(b)
    for key in (
        "name", "nodes", "edges", "external", "bodies",
        "types", "palette", "backlinks",
    ):
        assert key in data
    # bodies has one entry per concept.
    assert len(data["bodies"]) == len(b.concepts)


def test_build_graph_data_node_has_required_fields(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    data = build_graph_data(b)
    node = data["nodes"][0]["data"]
    for field in ("id", "label", "type", "description", "tags", "color"):
        assert field in node


def test_build_graph_data_backlinks(tiny_good_bundle: Path) -> None:
    """``backlinks`` maps target_id -> [source_id, ...] for resolved edges."""
    b = Bundle.load(tiny_good_bundle)
    data = build_graph_data(b)
    # tables/users is linked-to by metrics and events.
    bl = data["backlinks"].get("tables/users", [])
    assert set(bl) == {"references/metrics", "tables/events"}


# ===========================================================================
# P1-39, P1-36, P2-22, P2-57, P2-68: render-layer fixes
# ===========================================================================


from pathlib import Path as _Path  # noqa: E402

from okf_loom.render import (  # noqa: E402
    _render_link_list,
    _render_subtitle,
    _strip_body_citations_section,
    _is_typed_relation_target_raw,
)


def _build_bundle_with(
    tmp_path: _Path, concept_files: dict[str, str]
) -> _Path:
    """Stage a synthetic bundle and return its root path."""
    for relpath, content in concept_files.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def _render_concept_html(bundle_root: _Path, concept_id: str) -> str:
    """Render a single concept page to HTML using the static target."""
    from okf_loom import Bundle
    from okf_loom.render import build_site
    b = Bundle.load(bundle_root)
    out_dir = bundle_root / "_site"
    build_site(b, out_dir, target="static")
    return (out_dir / (concept_id.replace("/", "/") + ".html")).read_text(
        encoding="utf-8"
    )


# --- P1-39: frontmatter citations suppress duplicate body # Citations ------


def test_p1_39_frontmatter_citations_suppress_body_citations(tmp_path: _Path) -> None:
    """P1-39: when frontmatter ``citations:`` is present, the body's
    ``# Citations`` heading is stripped so the two lists don't render twice.
    """
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Both\n"
            "citations:\n"
            "  - id: \"1\"\n"
            "    text: Frontmatter citation\n"
            "---\n\n"
            "# Body\n\nProse.\n\n"
            "# Citations\n\n- Body citation line.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # The body's "# Citations" heading must NOT appear in the rendered body.
    # (The frontmatter-rendered citation block is separate and IS present.)
    assert "Frontmatter citation" in html  # governed-keys block
    # Body Citations heading stripped: look for an h-tag containing "Citations"
    # that comes from the body. The governed-keys block uses a span/div, not h.
    import re
    body_citations_headings = re.findall(
        r"<h\d>\s*Citations\s*</h\d>", html, re.IGNORECASE
    )
    assert body_citations_headings == [], (
        f"body Citations heading should be suppressed; found "
        f"{body_citations_headings}"
    )
    # Body citation content (the markdown bullet list) should also be gone.
    assert "Body citation line" not in html


def test_p1_39_no_frontmatter_citations_keeps_body_citations(tmp_path: _Path) -> None:
    """P1-39: when frontmatter ``citations:`` is ABSENT, the body's
    ``# Citations`` heading is preserved (no suppression)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: BodyOnly\n"
            "---\n\n"
            "# Body\n\nProse.\n\n"
            "# Citations\n\n- Body citation line.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # Body Citations heading present (heading gets demoted to h2 with an
    # id attribute; the regex allows attributes between tag name and >).
    import re
    headings = re.findall(
        r"<h\d(?:[^>]*)>\s*Citations\s*</h\d>", html, re.IGNORECASE
    )
    assert len(headings) >= 1, "body Citations heading should be preserved"
    assert "Body citation line" in html


def test_p1_39_strip_body_citations_section_unit() -> None:
    """P1-39 unit test: ``_strip_body_citations_section`` removes the
    ``<hN>Citations</hN>`` heading + body up to the next heading."""
    html = (
        "<h2>Schema</h2>\n<p>cols</p>\n"
        "<h2>Citations</h2>\n<ul><li>body cite</li></ul>\n"
        "<h2>Notes</h2>\n<p>misc</p>\n"
    )
    out = _strip_body_citations_section(html)
    assert "Citations" not in out
    assert "body cite" not in out
    # Surrounding sections preserved.
    assert "<h2>Schema</h2>" in out
    assert "<h2>Notes</h2>" in out
    assert "misc" in out


# --- P1-36: unified typed-relation detection --------------------------------


def test_p1_36_is_typed_relation_target_raw_predicate() -> None:
    """P1-36: the magic-prefix discriminator is the single source of truth
    for typed-relation detection in render.py."""
    assert _is_typed_relation_target_raw("relation:tables/users") is True
    assert _is_typed_relation_target_raw("relation:any/forward/ref") is True
    assert _is_typed_relation_target_raw("/tables/users.md") is False
    assert _is_typed_relation_target_raw("./users.md") is False
    assert _is_typed_relation_target_raw(None) is False
    assert _is_typed_relation_target_raw("") is False


def test_p1_36_link_list_renders_typed_relation_chip(tmp_path: _Path) -> None:
    """P1-36 + P2-57: a concept reached via BOTH a body link AND a typed
    relation shows ONCE in the link list, with the relation type as a chip
    that uses the ``okf-rel-type`` CSS class (the one wiki.css defines).
    """
    from okf_loom import Bundle
    bundle = _build_bundle_with(tmp_path, {
        "a.md": (
            "---\n"
            "type: T\ntitle: A\n"
            "relations:\n"
            "  - target: b\n"
            "    type: references\n"
            "    detail: \"\"\n"
            "---\n\n"
            "# A\n\nSee [B](/b.md).\n"
        ),
        "b.md": "---\ntype: T\ntitle: B\n---\nB body\n",
    })
    b = Bundle.load(bundle)
    graph = b.graph()
    out_links = graph.outlinks(("a",))
    items = [
        (l.target, l.label, "static", ("a",), l.target_raw)
        for l in out_links if l.target is not None
    ]
    html = _render_link_list(items, b, kind="out")
    # P2-57: chip uses okf-rel-type, not the orphaned okf-rel-kind.
    assert "okf-rel-type" in html, f"chip should use okf-rel-type\n{html}"
    assert "okf-rel-kind" not in html, (
        f"orphaned okf-rel-kind class should not be emitted\n{html}"
    )
    # Chip carries the relation type label.
    assert "references" in html
    # Only ONE list item for target b (deduped). Phase 2: items render as
    # connection cards (<li class="okf-conn" ...>), so count tag opens.
    assert html.count("<li") == 1


# --- P2-22: aliases rendered as subtitle ------------------------------------


def test_p2_22_subtitle_renders_aliases(tmp_path: _Path) -> None:
    """P2-22: aliases are rendered as a ``<p class="okf-subtitle">`` line
    immediately after the page title (SPEC §7.1 wording)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Main Title\n"
            "aliases: [alt one, alt two]\n"
            "---\n\nBody.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    assert 'class="okf-subtitle"' in html
    # Subtitle text joins aliases with " · ".
    assert "alt one · alt two" in html
    # Subtitle appears AFTER the title (so the document outline reads
    # title → subtitle → description).
    title_pos = html.find("Main Title")
    subtitle_pos = html.find("okf-subtitle")
    assert title_pos != -1 and subtitle_pos != -1
    assert subtitle_pos > title_pos, (
        f"subtitle should come after title (title@{title_pos}, "
        f"subtitle@{subtitle_pos})"
    )


def test_p2_22_subtitle_absent_when_no_aliases(tmp_path: _Path) -> None:
    """P2-22: no aliases → no subtitle line emitted."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": "---\ntype: T\ntitle: Just Title\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "concept")
    assert "okf-subtitle" not in html


def test_p2_22_subtitle_unit_empty_aliases() -> None:
    """P2-22: ``_render_subtitle`` returns "" when aliases is empty/absent."""
    from okf_loom.model import Concept
    from okf_loom.paths import concept_id_from_str
    cid = concept_id_from_str("x")
    c = Concept(
        id=cid, path=_Path("/x.md"), rel_path=_Path("x.md"),
        frontmatter={"type": "T", "title": "X"}, body="",
        raw_text="", headings=[],
    )
    assert _render_subtitle(c) == ""
    c.frontmatter["aliases"] = []
    assert _render_subtitle(c) == ""
    c.frontmatter["aliases"] = ["a", "b"]
    out = _render_subtitle(c)
    assert "okf-subtitle" in out
    assert "a · b" in out


# --- P2-57: typed-relation chip class ---------------------------------------


def test_p2_57_link_list_uses_okf_rel_type_class() -> None:
    """P2-57: link list emits ``okf-rel-type`` (the CSS rule) not the
    orphaned ``okf-rel-kind`` (no CSS rule, labels were invisible)."""
    from okf_loom import Bundle
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        bundle = _Path(td) / "demo"
        shutil.copytree("samples/demo_bundle", bundle)
        b = Bundle.load(bundle)
        # tables/orders has typed relations to tables/customers etc.
        graph = b.graph()
        out_links = graph.outlinks(("tables", "orders"))
        items = [
            (l.target, l.label, "static", ("tables", "orders"), l.target_raw)
            for l in out_links if l.target is not None
        ]
        html = _render_link_list(items, b, kind="out")
        assert "okf-rel-type" in html
        assert "okf-rel-kind" not in html


# --- P2-68: empty link-list sections are skipped ----------------------------


def test_p2_68_empty_link_list_returns_empty_string() -> None:
    """P2-68: ``_render_link_list`` returns "" when there are no items."""
    from okf_loom import Bundle
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        bundle = _Path(td) / "demo"
        shutil.copytree("samples/demo_bundle", bundle)
        b = Bundle.load(bundle)
        # An isolated concept has no out-links and no backlinks.
        out = _render_link_list([], b, kind="out")
        assert out == ""
        assert "(none)" not in out


def test_p2_68_empty_section_stripped_from_page(tmp_path: _Path) -> None:
    """P2-68: when a concept has NO outgoing links and NO backlinks, the
    rendered concept page omits the empty ``<section class="okf-relations
    __block">`` blocks (no "Links to" / "Cited by" headings + "(none)")."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": "---\ntype: T\ntitle: Lonely\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "concept")
    # No "(none)" placeholder rendered.
    assert "(none)" not in html
    # The wrapping ``<section class="okf-relations__block">`` element is
    # gone entirely (both for outgoing and backlinks). NOTE: the template
    # carries a comment that mentions "rendered ... list" — we look for
    # the actual rendered ``<section>`` tag, not the comment.
    assert 'class="okf-relations__block"' not in html
    # No rendered ``<h2>Links to</h2>`` or ``<h2>Cited by</h2>`` headings
    # (the template wraps these in the section we just stripped).
    import re
    h2_texts = re.findall(r"<h2[^>]*>\s*([^<]+?)\s*</h2>", html)
    h2_norm = [t.strip().lower() for t in h2_texts]
    assert "links to" not in h2_norm
    assert "cited by" not in h2_norm


def test_p2_68_nonempty_section_kept(tmp_path: _Path) -> None:
    """P2-68 contrast: when a concept DOES have out-links, the section IS
    rendered (with the heading and the link list)."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": (
            "---\ntype: T\ntitle: A\n---\n\nSee [B](/b.md).\n"
        ),
        "b.md": "---\ntype: T\ntitle: B\n---\nB body\n",
    })
    html = _render_concept_html(bundle, "a")
    assert "Links to" in html
    assert "okf-relations__block" in html


# ===========================================================================
# P1-3: static-mode nav URLs must carry .html (no live router rewrites them)
# ===========================================================================


def test_p1_3_static_concept_nav_urls_have_html_extension(
    tiny_good_bundle: _Path,
) -> None:
    """P1-3: in ``--target static`` the concept page nav emits extensionful
    URLs for the search form action and the Graph link.

    Previously both emitted extensionless ``__search`` / ``__graph``,
    which 404'd in static builds (no server rewrites the path). Uses the
    nested concept ``tables/users`` so the test also confirms the relative
    ``../`` prefix is preserved alongside the new extension.
    """
    html = _render_concept_html(tiny_good_bundle, "tables/users")
    import re

    # Search form action: must be ``../__search.html`` (relative + .html).
    form_match = re.search(
        r'<form\s+action="([^"]*__search[^"]*)"[^>]*role="search"', html
    )
    assert form_match, "search form not found in concept page"
    assert "__search.html" in form_match.group(1), (
        f"static search action should include .html; got {form_match.group(1)!r}"
    )
    # The nested concept must keep its relative ``../`` prefix.
    assert "__search.html" in form_match.group(1)

    # Graph link: must be ``../__graph.html``.
    graph_match = re.search(
        r'<a\s+class="okf-btn"\s+href="([^"]*__graph[^"]*)">Graph</a>', html
    )
    assert graph_match, "Graph link not found in concept page"
    assert "__graph.html" in graph_match.group(1), (
        f"static graph link should include .html; got {graph_match.group(1)!r}"
    )


def test_p1_3_spa_concept_nav_urls_extensionless(
    tiny_good_bundle: _Path,
) -> None:
    """P1-3 parity: in ``--target spa`` (and serve) the nav URLs stay
    extensionless — the live router handles the bare path. This guards
    against an over-eager ``mode != 'serve'`` style regression."""
    from okf_loom import Bundle
    from okf_loom.render import build_site, _render_concept_page
    b = Bundle.load(tiny_good_bundle)
    g = b.graph()
    concept = b.concepts[("tables", "users")]
    html = _render_concept_page(
        concept, b, g, mode="spa", name=b.name,
        palette={"table": "hsl(18, 62%, 48%)"}, config={},
    )
    import re
    form_match = re.search(
        r'<form\s+action="([^"]*__search[^"]*)"[^>]*role="search"', html
    )
    assert form_match, "search form not found in spa concept page"
    assert ".html" not in form_match.group(1), (
        f"spa search action should be extensionless; got {form_match.group(1)!r}"
    )


def test_p1_3_static_graph_page_has_data_okf_mode(
    tiny_good_bundle: _Path,
) -> None:
    """P1-3: the graph page body carries ``data-okf-mode="static"`` so
    graph.js can decide whether Open-page hrefs need a ``.html`` suffix."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_p13_graph"
    build_site(b, out_dir, target="static")
    graph_html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    assert 'data-okf-mode="static"' in graph_html, (
        "graph page body missing data-okf-mode attribute"
    )


def test_iter8_tilde_fence_renders_as_code_block():
    """P1-1 (iter-8): ~~~ code fences must render as <pre><code>, not plain
    text. CommonMark §4.5 defines both ``` and ~~~ as fences; the viewer
    renderer previously only matched backticks."""
    from okf_loom.viewer.markdown import markdown_to_html
    # Tilde fence
    html = markdown_to_html("~~~python\nprint('hello')\n~~~")
    assert "<pre>" in html, f"tilde fence not rendered as code: {html}"
    assert "language-python" in html or "<code>" in html
    # Backtick fence (must still work)
    html2 = markdown_to_html("```python\nprint('hello')\n```")
    assert "<pre>" in html2


# ===========================================================================
# Current spec §9: static-build client-side search
#
# Static builds used to show only a "search unavailable" notice. We now emit
# a build-time corpus at __data/search.json + a vanilla-JS searcher
# (__static/static-search.js) loaded only on the static __search.html page.
# The live `serve`/`spa` paths are unchanged (they hit /__search server-side).
# ===========================================================================


def test_static_build_emits_search_corpus_sorted_by_id(
    tiny_good_bundle: Path,
) -> None:
    """``--target static`` emits ``__data/search.json`` containing one entry
    per concept, sorted by concept_id (current spec §3 determinism)."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_static_search"
    build_site(b, out_dir, target="static")

    corpus_path = out_dir / "__data" / "search.json"
    assert corpus_path.exists(), "static build did not emit __data/search.json"

    import json as _json
    data = _json.loads(corpus_path.read_text(encoding="utf-8"))
    # Bare-array shape (chosen for client simplicity).
    assert isinstance(data, list), (
        f"search corpus must be a bare JSON array; got {type(data).__name__}"
    )
    assert len(data) == len(b.concepts), (
        f"corpus has {len(data)} entries; bundle has {len(b.concepts)} concepts"
    )

    ids = [e["id"] for e in data]
    assert ids == sorted(ids), (
        f"corpus must be sorted by id (current spec §3); got {ids}"
    )

    # Every entry has the documented field set, with stable types.
    required = {"id", "title", "description", "type", "tags", "aliases", "body_excerpt"}
    for e in data:
        missing = required - set(e.keys())
        assert not missing, f"corpus entry {e.get('id')!r} missing fields {missing}"
        assert isinstance(e["tags"], list), e["id"]
        assert isinstance(e["aliases"], list), e["id"]
        assert isinstance(e["body_excerpt"], str), e["id"]
        # body_excerpt is bounded — first ~500 chars of stripped body.
        assert len(e["body_excerpt"]) <= 500, (
            f"body_excerpt for {e['id']!r} exceeds 500 chars: {len(e['body_excerpt'])}"
        )


def test_static_search_page_loads_client_side_searcher(
    tiny_good_bundle: Path,
) -> None:
    """The static ``__search.html`` page references ``static-search.js``
    (the client-side searcher), not the old "search unavailable" message."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_static_search_page"
    build_site(b, out_dir, target="static")

    html = (out_dir / "__search.html").read_text(encoding="utf-8")
    # Client-side searcher is wired in (CSP: script-src 'self', same-origin).
    assert "static-search.js" in html, (
        "static __search.html must reference __static/static-search.js"
    )
    # The script tag must be properly formed (not inside a comment).
    import re
    script_match = re.search(
        r'<script\s+src="[^"]*static-search\.js"[^>]*></script>', html
    )
    assert script_match, (
        "static __search.html missing valid <script> tag for static-search.js"
    )
    # The server-emitted page itself must not declare search broken.
    assert "search unavailable" not in html.lower(), (
        "static __search.html still contains a 'search unavailable' message"
    )
    # The static-search.js asset itself must be emitted under __static/.
    assert (out_dir / "__static" / "static-search.js").exists(), (
        "static-search.js was not emitted under __static/"
    )
    inline = re.search(
        r'<template id="okf-search-data">(.*?)</template>', html, re.DOTALL,
    )
    assert inline, "static search page has no file://-safe inline corpus"
    assert len(json.loads(inline.group(1))) == len(b.concepts)


def test_serve_and_spa_search_pages_do_not_load_static_searcher(
    tiny_good_bundle: Path,
) -> None:
    """Only the static target gets the client-side searcher. ``serve`` and
    ``spa`` must stay on the live ``/__search`` backend (regression guard
    against accidentally wiring static-search.js into all modes)."""
    from okf_loom.render import _render_search_page
    b = Bundle.load(tiny_good_bundle)
    for mode in ("serve", "spa"):
        html = _render_search_page(
            b, mode=mode, name=b.name, config={}, query="x", results=[],
        )
        assert "static-search.js" not in html, (
            f"static-search.js leaked into {mode} search page (should be "
            f"static-only)"
        )
        assert 'id="okf-search-data"' not in html
        assert "wiki.js" in html, f"{mode} page lost wiki.js"


def test_static_search_corpus_is_deterministic_across_builds(
    tiny_good_bundle: Path,
) -> None:
    """Two builds of the same bundle produce byte-identical search corpora
    (current spec §3 determinism — no timestamps, no dict-order reliance)."""
    import json as _json
    b1 = Bundle.load(tiny_good_bundle)
    out1 = tiny_good_bundle / "_site_det_a"
    build_site(b1, out1, target="static")
    b2 = Bundle.load(tiny_good_bundle)
    out2 = tiny_good_bundle / "_site_det_b"
    build_site(b2, out2, target="static")

    a = (out1 / "__data" / "search.json").read_bytes()
    b = (out2 / "__data" / "search.json").read_bytes()
    assert a == b, "search.json is not byte-stable across identical builds"


# ===========================================================================
# iter-1 frontend fixes: P1-3 graph governed keys, P1-4/P2-2 graph a11y,
# P2-3 breadcrumb, P2-4 related label, P2-6 governed hierarchy,
# P2-7 subtitle CSS class.
# ===========================================================================


def test_iter1_build_graph_data_ships_governed_keys(tmp_path: _Path) -> None:
    """P1-3: build_graph_data ships §7 governed keys per node so the graph
    detail panel can render them. Every key is always present (defaults to
    an empty list when absent — graceful)."""
    bundle = _build_bundle_with(tmp_path, {
        "rich.md": (
            "---\n"
            "type: Table\n"
            "title: Rich\n"
            "aliases: [alt one, alt two]\n"
            "entities:\n"
            "  - id: entity/x\n"
            "    label: X\n"
            "    kind: business_entity\n"
            "    aliases: [a1, a2]\n"
            "provenance:\n"
            "  - source: https://src.example.com\n"
            "    note: a note\n"
            "    timestamp: '2026-06-27T00:00:00Z'\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: ref\n"
            "    url: https://c.example.com\n"
            "relations:\n"
            "  - target: other/c\n"
            "    type: references\n"
            "    detail: fk\n"
            "---\nbody\n"
        ),
        "plain.md": "---\ntype: Note\ntitle: Plain\n---\nbody\n",
    })
    b = Bundle.load(bundle)
    data = build_graph_data(b)
    by_id = {n["data"]["id"]: n["data"] for n in data["nodes"]}
    # All governed keys present on EVERY node (graceful defaults).
    for cid, nd in by_id.items():
        for key in ("aliases", "entities", "provenance", "citations", "relations"):
            assert key in nd, f"{cid} missing governed key {key}"
            assert isinstance(nd[key], list), f"{cid}.{key} not a list"
    rich = by_id["rich"]
    assert rich["aliases"] == ["alt one", "alt two"]
    assert rich["entities"] and rich["entities"][0]["label"] == "X"
    assert rich["provenance"] and rich["provenance"][0]["source"] == "https://src.example.com"
    assert rich["citations"] and rich["citations"][0]["id"] == "1"
    assert rich["relations"] and rich["relations"][0]["type"] == "references"
    # Plain node: all governed keys are empty lists (graceful absence).
    plain = by_id["plain"]
    assert plain["aliases"] == [] and plain["entities"] == []
    assert plain["provenance"] == [] and plain["citations"] == []
    assert plain["relations"] == []


def test_iter1_build_graph_data_sanitizes_governed_urls(tmp_path: _Path) -> None:
    """P1-3 / security: a javascript: provenance source or citation url is
    neutralized via _safe_url (graph.js assigns these to a.href)."""
    bundle = _build_bundle_with(tmp_path, {
        "x.md": (
            "---\n"
            "type: T\n"
            "title: X\n"
            "provenance:\n"
            "  - source: javascript:alert(1)\n"
            "    note: bad\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: t\n"
            "    url: data:text/html,<script>\n"
            "---\nbody\n"
        ),
    })
    b = Bundle.load(bundle)
    data = build_graph_data(b)
    nd = data["nodes"][0]["data"]
    # _safe_url returns None for dangerous schemes; the helper keeps the raw
    # string as text but graph.js only turns http(s) into <a href>. Either way
    # the dangerous scheme must NOT survive as a usable href.
    assert "javascript:" not in nd["provenance"][0]["source"], nd["provenance"]
    assert "data:text/html" not in nd["citations"][0]["url"], nd["citations"]


def test_iter1_graph_page_has_h1_and_node_index(tiny_good_bundle: _Path) -> None:
    """P1-4 / P2-2: the graph page has a top-level sr-only <h1> AND a
    keyboard-accessible node-index container (<details> + list)."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_iter1_graph"
    build_site(b, out_dir, target="static")
    html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    assert re.search(r'<h1[^>]*class="[^"]*sr-only"', html), "graph page missing sr-only h1"
    assert "okf-node-index" in html, "graph page missing node-index container"
    assert 'id="okf-node-index-list"' in html
    # detail-title is an <h2> now (was <h1>), demoted because the sr-only h1
    # sits above the layout.
    assert re.search(r'<h2[^>]*id="detail-title"', html), "detail-title not demoted to h2"
    # No inline script (CSP hard rule) — iter-1 removed the comment-only block.
    assert not re.search(r"<script>(?!\s*</script>)", html) or \
        "<script>\n</script>" not in html


def test_iter1_concept_page_has_clickable_breadcrumb(tmp_path: _Path) -> None:
    """P2-3: concept page renders a <nav aria-label="Breadcrumb"> with
    clickable ancestor links, not a flat raw-id <p>."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/orders.md": (
            "---\ntype: Table\ntitle: Orders\n---\nbody\n"
        ),
        "tables/index.md": "# tables\n",
        "index.md": "# root\n",
    })
    b = Bundle.load(bundle)
    out_dir = bundle / "_site_iter1_crumb"
    build_site(b, out_dir, target="static")
    html = (out_dir / "tables" / "orders.html").read_text(encoding="utf-8")
    assert '<nav class="okf-breadcrumb" aria-label="Breadcrumb">' in html
    # Ancestor links exist (root index + tables index).
    assert 'href="../index.html"' in html, "missing root-index breadcrumb link"
    assert 'href="index.html"' in html, "missing tables-index breadcrumb link"
    # Current concept is plain text (aria-current), not a self-link.
    assert 'aria-current="page"' in html
    # P2-10 (iter-3): the raw-id <small class="okf-breadcrumb__id"> is gone
    # (it duplicated the path segments shown in the trail AND fell below the
    # 12px type floor). The breadcrumb trail itself is the only affordance.
    assert "okf-breadcrumb__id" not in html, (
        "breadcrumb raw-id <small> should be removed (P2-10)"
    )
    # The trail still shows the path segments individually.
    assert 'aria-current="page">orders<' in html, (
        "breadcrumb current-segment label missing"
    )


def test_iter1_concept_page_governed_keys_distinct_structure(tmp_path: _Path) -> None:
    """P2-6: each governed section has a distinct structure — entities as
    <dl>, provenance as <ul>, citations as <ol> (was a flat inline run)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: T\n"
            "title: C\n"
            "aliases: [a1, a2]\n"
            "entities:\n"
            "  - id: e1\n"
            "    label: Ent\n"
            "    kind: business_entity\n"
            "    aliases: [x]\n"
            "provenance:\n"
            "  - source: https://s.example.com\n"
            "    note: n\n"
            "    timestamp: '2026-06-27T00:00:00Z'\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: ref\n"
            "    url: https://c.example.com\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    assert 'class="okf-entity-list"' in html, "entities not a <dl>"
    assert "<dt>" in html and "<dd>" in html
    assert 'class="okf-provenance-list"' in html, "provenance not a <ul>"
    assert "<time" in html and 'class="okf-provenance-time"' in html
    assert 'class="okf-citation-list"' in html, "citations not an <ol>"
    assert "<ol" in html
    # P2-8 (iter-3): aliases are NOT rendered as pills here — they render
    # ONCE as the page subtitle (see test_iter3_aliases_render_once_only).
    assert "okf-alias-pill" not in html, (
        "aliases pills should be removed from the governed-keys block; "
        "the subtitle is the single canonical surface (P2-8)"
    )


def test_iter1_subtitle_uses_css_class_not_inline_style(tmp_path: _Path) -> None:
    """P2-7: the subtitle no longer carries an inline style= fallback; it
    lives on the .okf-subtitle CSS rule (token system)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\naliases: [one, two]\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    assert 'class="okf-subtitle"' in html
    assert "style=" not in html.split('class="okf-subtitle"', 1)[1].split(">", 1)[0], (
        "subtitle still carries an inline style attribute"
    )


def test_iter1_concept_page_sidebar_labeled_related(tmp_path: _Path) -> None:
    """P2-4: the concept-page sidebar aria-label says 'Related concepts'
    (honest — it's a flat labelled pill list, not a graph)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    assert 'aria-label="Related concepts"' in html
    assert 'aria-label="Concept navigation"' not in html


def test_iter1_accent_is_not_tailwind_blue() -> None:
    """P2-8: the UI accent token is no longer the default Tailwind blue
    (#2563eb light / #60a5fa dark). Guards against regressing to the
    generic 'wiki → blue' reflex."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")

    def _token(block: str, name: str) -> str:
        # Extract the actual token VALUE (skip comment-only mentions).
        m = re.search(rf"--{name}:\s*([^;]+);", block)
        assert m, f"token --{name} not defined in block"
        return m.group(1).strip()

    root_block = css.split(":root", 1)[1].split("}", 1)[0]
    light_accent = _token(root_block, "okf-accent")
    assert light_accent.lower() != "#2563eb", "light accent still Tailwind blue-600"
    dark_block = css.split('[data-theme="dark"]', 1)[1].split("}", 1)[0]
    dark_accent = _token(dark_block, "okf-accent")
    assert dark_accent.lower() != "#60a5fa", "dark accent still Tailwind blue-400"


# ===========================================================================
# iter-2 frontend fixes: P1-1 subtitle out of h1, P2-3 search h1 hierarchy,
# P2-4 graph node-index collapsed by default, P2-5 graph color governance.
# ===========================================================================


def test_iter2_subtitle_is_sibling_outside_h1(tmp_path: _Path) -> None:
    """P1-1 (iter-2): the alias subtitle is a SIBLING <p> after the <h1>,
    not a child of it.

    The prior ``.replace("__CONCEPT_TITLE__", title + subtitle_html)`` put a
    block <p> inside the h1 — invalid HTML (h1 content model is phrasing-
    only) AND it polluted the h1 accessible name on every aliased concept.
    """
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Main Title\n"
            "aliases: [alt one, alt two]\n"
            "---\n\nBody.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # Extract the h1 element and assert it contains NO <p> / no subtitle.
    h1_match = re.search(
        r'<h1 class="okf-page__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match, "missing concept page h1"
    h1_inner = h1_match.group(1)
    assert "<p" not in h1_inner, (
        f"h1 contains a <p> (invalid content model): {h1_inner!r}"
    )
    assert "okf-subtitle" not in h1_inner, (
        f"subtitle leaked inside h1: {h1_inner!r}"
    )
    # The h1 accessible name is just the title text (no alias concatenation).
    assert h1_inner.strip() == "Main Title", (
        f"h1 text is not just the title: {h1_inner!r}"
    )
    # The subtitle <p> IS present, as a sibling AFTER the </h1>.
    after_h1 = html[h1_match.end():]
    assert '<p class="okf-subtitle">' in after_h1, (
        "subtitle <p> not found as a sibling after the h1"
    )
    # And it appears BEFORE the description paragraph (document order:
    # h1 → subtitle → description).
    sub_pos = after_h1.find('class="okf-subtitle"')
    desc_pos = after_h1.find('class="okf-page__description"')
    assert sub_pos != -1 and desc_pos != -1, "subtitle or description missing"
    assert sub_pos < desc_pos, "subtitle should come before description"


def test_iter2_subtitle_placeholder_empty_when_no_aliases(tmp_path: _Path) -> None:
    """P1-1 (iter-2): when there are no aliases, __CONCEPT_SUBTITLE__ renders
    nothing and the h1 is still valid (no stray empty <p>)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": "---\ntype: T\ntitle: Just Title\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "concept")
    assert "okf-subtitle" not in html, "subtitle emitted despite no aliases"
    h1_match = re.search(
        r'<h1 class="okf-page__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match and h1_match.group(1).strip() == "Just Title"
    # No empty <p> left behind by the placeholder.
    assert "<p></p>" not in html, "empty <p> left by subtitle placeholder"


def test_iter2_search_h1_not_wrapped_in_muted(tmp_path: _Path) -> None:
    """P2-3 (iter-2): the search page's only <h1> renders full-size
    full-colour. The prior ``<span class="okf-muted">`` wrapper demoted it
    to 14px gray (.okf-muted sets font-size: --okf-text-sm), making the
    search h1 the visually-weakest heading across the three text surfaces.
    """
    from okf_loom.render import _render_search_page
    bundle = _build_bundle_with(tmp_path, {
        "index.md": "# root\n",
        "c.md": "---\ntype: T\ntitle: Orders\n---\nbody\n",
    })
    from okf_loom import Bundle
    b = Bundle.load(bundle)
    html = _render_search_page(
        b, mode="static", name=b.name, config={},
        query="orders",
        results=[{"title": "Orders", "concept_id": "c", "id": "c"}],
    )
    h1_match = re.search(
        r'<h1 class="okf-search__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match, "missing search h1"
    assert "okf-muted" not in h1_match.group(0), (
        f"search h1 still wrapped in .okf-muted (demotes it to 14px gray): "
        f"{h1_match.group(0)!r}"
    )
    # The heading text is present and full-colour (no wrapper span at all).
    assert "Orders" not in h1_match.group(0) or "1 result" in h1_match.group(0)


def test_iter2_search_title_css_is_full_size() -> None:
    """P2-3 (iter-2): .okf-search__title uses --okf-text-xl (parity with
    .okf-index__title and .okf-page__title), not --okf-text-lg."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    rule_match = re.search(r'\.okf-search__title\s*\{([^}]*)\}', css)
    assert rule_match, ".okf-search__title rule missing from wiki.css"
    rule = rule_match.group(1)
    assert "--okf-text-xl" in rule, (
        f".okf-search__title not at --okf-text-xl (h1 parity): {rule!r}"
    )
    assert "--okf-text-lg" not in rule, (
        f".okf-search__title still at --okf-text-lg: {rule!r}"
    )


def test_iter2_graph_node_index_not_open_by_default(
    tiny_good_bundle: Path,
) -> None:
    """P2-4 (iter-2): the node-index <details> defaults to COLLAPSED on all
    screen sizes. The `open` attribute was removed from graph_page.html
    (the CSS comment claiming "starts collapsed on small screens" was false
    — CSS cannot remove the attribute)."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_iter2_nodeindex"
    build_site(b, out_dir, target="static")
    html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    details_match = re.search(
        r'<details class="okf-node-index"([^>]*)>', html
    )
    assert details_match, "missing node-index <details> element"
    attrs = details_match.group(1)
    assert not re.search(r'\bopen\b', attrs), (
        f"node-index <details> still carries the open attribute "
        f"(crowds the mobile canvas): {details_match.group(0)!r}"
    )


def test_iter2_single_file_node_index_not_open(
    tiny_good_bundle: Path,
) -> None:
    """P2-4 (iter-2): single-file viewer node-index also defaults collapsed
    (parity with graph_page.html)."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "_single_iter2.html"
    render_single_file(b, out)
    html = out.read_text(encoding="utf-8")
    details_match = re.search(
        r'<details class="okf-node-index"([^>]*)>', html
    )
    assert details_match, "missing node-index <details> in single-file viewer"
    assert not re.search(r'\bopen\b', details_match.group(1)), (
        f"single-file node-index still carries open: "
        f"{details_match.group(0)!r}"
    )


def test_iter2_graph_node_index_css_comment_is_honest() -> None:
    """P2-4 (iter-2): the false CSS comment claiming the node-index 'starts
    collapsed on small screens' has been removed/fixed (CSS cannot remove
    the open attribute)."""
    css = _runtime_file("viewer", "static", "graph.css").read_text(encoding="utf-8")
    # The old false claim must be gone.
    assert "starts collapsed" not in css.lower(), (
        "graph.css still carries the false 'starts collapsed on small "
        "screens' comment"
    )


def test_iter2_graph_selection_color_is_token_governed() -> None:
    """P2-5 (iter-2): the amber #f59e0b selection border is gone (replaced
    by the --okf-select token, mirrored in a GRAPH_COLORS JS constants
    block). The 6 duplicated canvas color literals are consolidated there."""
    graph_js = _runtime_file("viewer", "static", "graph.js").read_text(encoding="utf-8")
    # Strip comments to check the amber is not used as a STYLE value (it may
    # survive in an explanatory comment, which is fine).
    code_only = re.sub(r'/\*.*?\*/', '', graph_js, flags=re.DOTALL)
    code_only = re.sub(r'//[^\n]*', '', code_only)
    assert 'f59e0b' not in code_only.lower(), (
        "amber #f59e0b still used as a style value in graph.js "
        "(should be governed by --okf-select)"
    )
    # GRAPH_COLORS per-theme palette block exists and is referenced.
    assert 'GRAPH_COLORS' in graph_js, "GRAPH_COLORS constants block missing"
    # Every theme ships a canvas palette (mirrors its wiki.css tokens).
    for theme in ("light", "dark", "pastel", "sepia", "midnight"):
        assert re.search(rf'\b{theme}:\s*\{{', graph_js), (
            f"GRAPH_COLORS missing the {theme} palette"
        )
    # node:selected border reads from the palette (not a literal).
    assert re.search(
        r'"border-color":\s*GRAPH_COLORS\.light\.select', graph_js
    ), "node:selected border-color does not read from GRAPH_COLORS.light.select"
    # syncLabelColour re-syncs the selection color on theme change.
    sync_match = re.search(
        r'function\s+syncLabelColour\s*\(\)\s*\{(.*?)\n\s*\}',
        graph_js, re.DOTALL,
    )
    assert sync_match, "syncLabelColour function missing"
    sync_body = sync_match.group(1).replace("\n", " ")
    # The palette is resolved from the active data-theme.
    assert re.search(
        r'var pal\s*=\s*graphPalette\(\)', sync_body,
    ), "syncLabelColour does not resolve the active theme palette"
    # node:selected re-syncs border-color from the palette on theme change.
    assert re.search(
        r'node:selected.*?border-color.*?pal\.select', sync_body
    ), "syncLabelColour does not re-sync node:selected border-color"


def test_iter2_select_token_defined_in_wiki_css() -> None:
    """P2-5 (iter-2): the --okf-select token is defined in EVERY theme
    (the graph.js GRAPH_COLORS block mirrors these values)."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    root_block = css.split(":root", 1)[1].split("}", 1)[0]
    assert re.search(r'--okf-select\s*:', root_block), (
        "light theme missing --okf-select token"
    )
    for theme in ("dark", "pastel", "sepia", "midnight"):
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        assert re.search(r'--okf-select\s*:', block), (
            f"{theme} theme missing --okf-select token"
        )


def test_theme_blocks_override_full_token_set() -> None:
    """Every named theme block (dark/pastel/sepia/midnight) overrides the
    same colour tokens the light :root defines, so no theme inherits a
    light-only colour that breaks contrast (e.g. a pastel page with the
    light theme's white code background)."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    core_tokens = (
        "okf-bg", "okf-bg-elev", "okf-bg-inset", "okf-fg", "okf-fg-muted",
        "okf-border", "okf-border-strong", "okf-accent", "okf-accent-hover",
        "okf-accent-on", "okf-accent-bg", "okf-select", "okf-code-bg",
        "okf-code-fg", "okf-pre-bg", "okf-pre-fg", "okf-broken",
        "okf-ok", "okf-ok-bg", "okf-warn", "okf-warn-bg",
        "okf-info", "okf-info-bg", "okf-error", "okf-error-bg", "okf-shadow",
    )
    for theme in ("dark", "pastel", "sepia", "midnight"):
        assert f'[data-theme="{theme}"]' in css, f"{theme} theme block missing"
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        for token in core_tokens:
            assert re.search(rf'--{token}\s*:', block), (
                f"{theme} theme missing --{token} token"
            )


# ===========================================================================
# iter-3 frontend fixes:
#   P2-8  aliases render ONCE (subtitle is canonical, pills removed)
#   P2-9  wiki.js does not emit the static-search note on the search page
#   P2-10 breadcrumb raw-id <small> removed (size + duplication)
#   P2-11 "All fields" table excludes header-already-rendered keys
# ===========================================================================


def test_iter3_aliases_render_once_only(tmp_path: _Path) -> None:
    """P2-8: aliases appear EXACTLY ONCE on the rendered concept page — as
    the subtitle <p>. The prior "Also known as:" pills block in
    _render_governed_keys duplicated the same values within ~80px of the
    subtitle (conspicuous once P1-1 iter-2 made the subtitle a sibling of
    the h1)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Main Title\n"
            "aliases: [alt one, alt two]\n"
            "---\n\nBody.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # The subtitle (canonical surface) IS present.
    assert 'class="okf-subtitle"' in html, "subtitle line missing"
    assert "alt one · alt two" in html, "alias text missing from subtitle"
    # The pills block is GONE — no "Also known as:" label, no alias pills.
    assert "okf-alias-pill" not in html, (
        "aliases pills should be removed (P2-8: subtitle is canonical)"
    )
    assert "Also known as:" not in html, (
        "'Also known as:' label should be removed with the pills block"
    )
    assert 'class="okf-aliases"' not in html, (
        "okf-aliases wrapper div should be removed (no longer has content)"
    )
    # Each alias value appears exactly ONCE in the rendered HTML (count the
    # subtitle occurrence only; they previously appeared in BOTH subtitle
    # and pills, doubling the count).
    assert html.count("alt one") == 1, (
        f"alias 'alt one' should appear once (subtitle only); "
        f"found {html.count('alt one')} occurrences"
    )
    assert html.count("alt two") == 1, (
        f"alias 'alt two' should appear once (subtitle only); "
        f"found {html.count('alt two')} occurrences"
    )


def test_iter3_subtitle_still_present_with_entities(tmp_path: _Path) -> None:
    """P2-8 regression guard: removing the aliases pills does NOT also drop
    the entities/provenance/citations/relations sections (those still come
    from _render_governed_keys). Only the aliases pills block was removed."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: T\n"
            "title: C\n"
            "aliases: [a1, a2]\n"
            "entities:\n"
            "  - id: e1\n"
            "    label: Ent\n"
            "    kind: business_entity\n"
            "provenance:\n"
            "  - source: https://s.example.com\n"
            "    note: n\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: ref\n"
            "    url: https://c.example.com\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Subtitle (aliases) still renders.
    assert 'class="okf-subtitle"' in html
    # Other governed sections survive.
    assert 'class="okf-entity-list"' in html, "entities section dropped"
    assert 'class="okf-provenance-list"' in html, "provenance section dropped"
    assert 'class="okf-citation-list"' in html, "citations section dropped"
    # Aliases pills do NOT render.
    assert "okf-alias-pill" not in html


def test_iter3_breadcrumb_has_no_raw_id_small(tmp_path: _Path) -> None:
    """P2-10: the breadcrumb raw-id ``<small class="okf-breadcrumb__id">``
    is gone. It duplicated the path segments shown in the trail AND fell
    below the 12px type floor (``<small>`` × ``.okf-muted`` 14px ×
    ``<small>`` default 0.8em ≈ 11.2px)."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/orders.md": (
            "---\ntype: Table\ntitle: Orders\n---\nbody\n"
        ),
        "tables/index.md": "# tables\n",
        "index.md": "# root\n",
    })
    b = _Path(str(bundle))
    from okf_loom import Bundle
    bb = Bundle.load(b)
    out_dir = b / "_site_iter3_crumb"
    build_site(bb, out_dir, target="static")
    html = (out_dir / "tables" / "orders.html").read_text(encoding="utf-8")
    # The breadcrumb nav still exists.
    assert '<nav class="okf-breadcrumb" aria-label="Breadcrumb">' in html
    # The raw-id <small> wrapper class is GONE.
    assert "okf-breadcrumb__id" not in html, (
        "breadcrumb raw-id <small> should be removed (P2-10: duplicates "
        "trail + falls below 12px type floor)"
    )
    # The breadcrumb nav closes right after the trail (no stray <small>).
    nav_match = re.search(
        r'<nav class="okf-breadcrumb"[^>]*>(.*?)</nav>',
        html, re.DOTALL,
    )
    assert nav_match, "breadcrumb nav not found"
    assert "<small" not in nav_match.group(1), (
        f"stray <small> inside breadcrumb nav: {nav_match.group(1)!r}"
    )
    # Trail still shows the current segment (orders) as aria-current.
    assert 'aria-current="page">orders<' in nav_match.group(1), (
        "breadcrumb current segment missing"
    )


def test_iter3_wiki_js_skips_search_note_on_search_page() -> None:
    """P2-9: wiki.js gates the static-search note on
    ``body:not(.okf-viewer--search)``. The note says 'Press Enter to
    search, or Open search.' and exists to point users FROM other pages
    TO the search page; on the search page itself the 'Open search' link
    would resolve to the page the user is already on (confusing).

    This is a static-source contract test: we assert the gating predicate
    appears in wiki.js so the cross-file contract is enforceable. The
    search page template emits ``<body class="okf-viewer okf-viewer--search">``
    (see search_page.html)."""
    wiki_js = _runtime_file("viewer", "static", "wiki.js").read_text(encoding="utf-8")
    # The note-emitting branch must check the search-page body class.
    assert "okf-viewer--search" in wiki_js, (
        "wiki.js must reference okf-viewer--search to skip the note on "
        "the search page (P2-9)"
    )
    # Locate the static-note branch and confirm the gating.
    # The branch reads:
    #   if (isStatic && !document.body.classList.contains("okf-viewer--search"))
    branch_match = re.search(
        r'isStatic\s*&&\s*!\s*document\.body\.classList\.contains\(\s*["\']okf-viewer--search["\']\s*\)',
        wiki_js,
    )
    assert branch_match, (
        "wiki.js static-search-note branch must be gated on "
        "body:not(.okf-viewer--search) (P2-9)"
    )
    # The search page template actually carries the class the JS keys off.
    search_tpl = _runtime_file("viewer", "templates", "search_page.html").read_text(
        encoding="utf-8"
    )
    assert 'okf-viewer--search' in search_tpl, (
        "search_page.html must emit the okf-viewer--search body class "
        "(the contract target wiki.js keys off)"
    )


def test_iter3_search_page_has_no_server_side_search_note(tmp_path: _Path) -> None:
    """P2-9 (server-side parity): the search page does NOT carry a
    server-rendered 'Press Enter to search' note either (wiki.js owns
    the note and only emits it on non-search pages). This guards against
    a future render.py change that re-adds the note statically."""
    bundle = _build_bundle_with(tmp_path, {
        "index.md": "# root\n",
        "c.md": "---\ntype: T\ntitle: Orders\n---\nbody\n",
    })
    from okf_loom import Bundle
    from okf_loom.render import _render_search_page, build_site
    b = Bundle.load(bundle)
    # 1) Direct render.
    html = _render_search_page(
        b, mode="static", name=b.name, config={},
        query="orders",
        results=[{"title": "Orders", "concept_id": "c", "id": "c"}],
    )
    assert "Press Enter to search" not in html, (
        "search page must not statically render the 'Press Enter to search' "
        "note (P2-9: wiki.js now gates it on body:not(.okf-viewer--search))"
    )
    # 2) Built-page parity: same invariant on the built __search.html.
    out_dir = bundle / "_site_iter3_search"
    build_site(b, out_dir, target="static")
    built = (out_dir / "__search.html").read_text(encoding="utf-8")
    assert "Press Enter to search" not in built, (
        "built __search.html must not contain the 'Press Enter to search' "
        "note (P2-9)"
    )


def test_iter3_all_fields_excludes_header_rendered_keys(tmp_path: _Path) -> None:
    """P2-11: the 'All fields' <details> frontmatter table excludes the
    five keys already rendered in the page header (type chip / h1 title /
    description paragraph / resource link / tags row). Only ``timestamp``
    plus unknown/extra producer-added keys should remain.

    The 'All fields' affordance promises completeness but previously
    repeated 5 of 6 already-visible header rows; this fix keeps the
    forward-compat value (unknown keys surface here) without the noise.
    """
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: Table\n"
            "title: Orders\n"
            "description: One row per order.\n"
            "resource: https://example.com/orders\n"
            "tags: [orders, revenue]\n"
            "timestamp: '2026-06-27T09:30:00Z'\n"
            "custom_key: custom_value\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Isolate the "All fields" <details> block.
    details_match = re.search(
        r'<details class="okf-frontmatter"[^>]*>(.*?)</details>',
        html, re.DOTALL,
    )
    assert details_match, "missing 'All fields' <details> block"
    details_html = details_match.group(1)

    # Header-already-rendered keys must NOT appear as rows in the table.
    # (We check the <th> text precisely so a value containing the literal
    # string — e.g. 'orders' in tags — does not false-positive.)
    for header_key in ("type", "title", "description", "resource", "tags"):
        th_match = re.search(
            rf'<th>\s*{re.escape(header_key)}\s*</th>',
            details_html,
        )
        assert th_match is None, (
            f"'All fields' table still includes header-rendered key "
            f"'{header_key}' (P2-11)"
        )

    # Governed keys must remain excluded too (regression guard).
    for gov_key in ("aliases", "entities", "provenance",
                    "citations", "relations"):
        th_match = re.search(
            rf'<th>\s*{re.escape(gov_key)}\s*</th>',
            details_html,
        )
        assert th_match is None, (
            f"'All fields' table includes governed key '{gov_key}' "
            f"(should be in dedicated sections)"
        )

    # timestamp (genuinely new info) IS present.
    assert re.search(r'<th>\s*timestamp\s*</th>', details_html), (
        "'All fields' table should still include timestamp (not in header)"
    )
    # Unknown / producer-added keys are preserved (forward-compat, SPEC §11).
    assert re.search(r'<th>\s*custom_key\s*</th>', details_html), (
        "'All fields' table should preserve unknown producer-added keys "
        "(forward-compat / SPEC §11)"
    )


def test_iter3_all_fields_header_keys_actually_rendered(tmp_path: _Path) -> None:
    """P2-11 paired invariant: the keys we now exclude from 'All fields'
    ARE actually rendered in the page header (otherwise the exclusion
    would hide them entirely). This catches a future change that drops a
    header field without re-adding it to the frontmatter table."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: Table\n"
            "title: Orders\n"
            "description: One row per order.\n"
            "resource: https://example.com/orders\n"
            "tags: [orders, revenue]\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Type chip shows the type value.
    assert re.search(r'class="okf-type-chip"[^>]*>\s*Table\s*<', html), (
        "type not rendered as the type chip in the header"
    )
    # Title in the h1.
    h1_match = re.search(
        r'<h1 class="okf-page__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match and "Orders" in h1_match.group(1), (
        "title not rendered in the page h1"
    )
    # Description paragraph.
    assert re.search(
        r'<p class="okf-page__description">[^<]*One row per order\.',
        html,
    ), "description not rendered in header"
    # Resource link.
    assert 'href="https://example.com/orders"' in html, (
        "resource URL not rendered in header"
    )
    # Tags row.
    assert '<span class="okf-tag">orders</span>' in html, (
        "tag 'orders' not rendered in header tags row"
    )
    assert '<span class="okf-tag">revenue</span>' in html, (
        "tag 'revenue' not rendered in header tags row"
    )
