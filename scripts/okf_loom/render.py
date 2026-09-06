"""Single-file + static-site rendering for OKF bundles.

Public API:
    render_single_file(bundle, out_path, *, name=None) -> dict
    build_site(bundle, out_dir, *, target, name=None) -> dict

Both functions are pure-Python (stdlib + pyyaml). The single-file viewer
embeds the bundle as JSON and uses Cytoscape.js from CDN (markdown is
rendered server-side; no client-side parser). The static/spa site is a
directory of server-rendered HTML pages sharing templates with the live
wiki server.

Hard constraints honoured: zero hard deps beyond pyyaml, no Jinja2/markdown/
watchdog, atomic writes for build output.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .aliases import alias_labels
from .viewer.workspace import navigation, workspace_assets
from .model import Bundle, Concept
from .paths import ConceptId, concept_id_to_str
from .viewer.assets import (
    auto_palette,
    list_builtin_static,
    load_config,
    load_palette_override,
    load_static,
    load_template,
    resolve_palette,
    stable_hue,
    hsl_color,
    type_icon_svg,
    type_icon_key,
    type_icon_paths,
)
from .viewer.markdown import (
    markdown_to_html,
    rewrite_internal_links,
    url_for_concept,
)


# ---------------------------------------------------------------------------
# Chip foreground color (luminance-aware contrast)
# ---------------------------------------------------------------------------

import re as _re_chip


# P2-23: small named-CSS-colour map so palette overrides using bare names
# (``red``, ``navy``, …) get the same luminance-aware fg treatment as
# ``hsl()``. assets.py:_sanitize_css_color already allows the full CSS3
# named set, so without this map ``_chip_fg`` fell through to the white
# default for every named colour (e.g. ``yellow`` chips rendered as
# white-on-yellow). The map is intentionally limited to the high-contrast
# common names; ``transparent`` / ``currentcolor`` stay on the white
# fallback (they have no computable luminance).
_NAMED_CSS_COLORS: dict[str, tuple[int, int, int]] = {
    "aliceblue": (240, 248, 255), "antiquewhite": (250, 235, 215),
    "aqua": (0, 255, 255), "aquamarine": (127, 255, 212),
    "azure": (240, 255, 255), "beige": (245, 245, 220),
    "bisque": (255, 228, 196), "black": (0, 0, 0),
    "blanchedalmond": (255, 235, 205), "blue": (0, 0, 255),
    "blueviolet": (138, 43, 226), "brown": (165, 42, 42),
    "burlywood": (222, 184, 135), "cadetblue": (95, 158, 160),
    "chartreuse": (127, 255, 0), "chocolate": (210, 105, 30),
    "coral": (255, 127, 80), "cornflowerblue": (100, 149, 237),
    "cornsilk": (255, 248, 220), "crimson": (220, 20, 60),
    "cyan": (0, 255, 255), "darkblue": (0, 0, 139),
    "darkcyan": (0, 139, 139), "darkgoldenrod": (184, 134, 11),
    "darkgray": (169, 169, 169), "darkgreen": (0, 100, 0),
    "darkgrey": (169, 169, 169), "darkkhaki": (189, 183, 107),
    "darkmagenta": (139, 0, 139), "darkolivegreen": (85, 107, 47),
    "darkorange": (255, 140, 0), "darkorchid": (153, 50, 204),
    "darkred": (139, 0, 0), "darksalmon": (233, 150, 122),
    "darkseagreen": (143, 188, 143), "darkslateblue": (72, 61, 139),
    "darkslategray": (47, 79, 79), "darkslategrey": (47, 79, 79),
    "darkturquoise": (0, 206, 209), "darkviolet": (148, 0, 211),
    "deeppink": (255, 20, 147), "deepskyblue": (0, 191, 255),
    "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105),
    "dodgerblue": (30, 144, 255), "firebrick": (178, 34, 34),
    "floralwhite": (255, 250, 240), "forestgreen": (34, 139, 34),
    "fuchsia": (255, 0, 255), "gainsboro": (220, 220, 220),
    "ghostwhite": (248, 248, 255), "gold": (255, 215, 0),
    "goldenrod": (218, 165, 32), "gray": (128, 128, 128),
    "green": (0, 128, 0), "greenyellow": (173, 255, 47),
    "grey": (128, 128, 128), "honeydew": (240, 255, 240),
    "hotpink": (255, 105, 180), "indianred": (205, 92, 92),
    "indigo": (75, 0, 130), "ivory": (255, 255, 240),
    "khaki": (240, 230, 140), "lavender": (230, 230, 250),
    "lavenderblush": (255, 240, 245), "lawngreen": (124, 252, 0),
    "lemonchiffon": (255, 250, 205), "lightblue": (173, 216, 230),
    "lightcoral": (240, 128, 128), "lightcyan": (224, 255, 255),
    "lightgoldenrodyellow": (250, 250, 210),
    "lightgray": (211, 211, 211), "lightgreen": (144, 238, 144),
    "lightgrey": (211, 211, 211), "lightpink": (255, 182, 193),
    "lightsalmon": (255, 160, 122), "lightseagreen": (32, 178, 170),
    "lightskyblue": (135, 206, 250),
    "lightslategray": (119, 136, 153),
    "lightslategrey": (119, 136, 153),
    "lightsteelblue": (176, 196, 222), "lightyellow": (255, 255, 224),
    "lime": (0, 255, 0), "limegreen": (50, 205, 50),
    "linen": (250, 240, 230), "magenta": (255, 0, 255),
    "maroon": (128, 0, 0), "mediumaquamarine": (102, 205, 170),
    "mediumblue": (0, 0, 205), "mediumorchid": (186, 85, 211),
    "mediumpurple": (147, 112, 219), "mediumseagreen": (60, 179, 113),
    "mediumslateblue": (123, 104, 238),
    "mediumspringgreen": (0, 250, 154),
    "mediumturquoise": (72, 209, 204),
    "mediumvioletred": (199, 21, 133),
    "midnightblue": (25, 25, 112), "mintcream": (245, 255, 250),
    "mistyrose": (255, 228, 225), "moccasin": (255, 228, 181),
    "navajowhite": (255, 222, 173), "navy": (0, 0, 128),
    "oldlace": (253, 245, 230), "olive": (128, 128, 0),
    "olivedrab": (107, 142, 35), "orange": (255, 165, 0),
    "orangered": (255, 69, 0), "orchid": (218, 112, 214),
    "palegoldenrod": (238, 232, 170), "palegreen": (152, 251, 152),
    "paleturquoise": (175, 238, 238),
    "palevioletred": (219, 112, 147), "papayawhip": (255, 239, 213),
    "peachpuff": (255, 218, 185), "peru": (205, 133, 63),
    "pink": (255, 192, 203), "plum": (221, 160, 221),
    "powderblue": (176, 224, 230), "purple": (128, 0, 128),
    "rebeccapurple": (102, 51, 153), "red": (255, 0, 0),
    "rosybrown": (188, 143, 143), "royalblue": (65, 105, 225),
    "saddlebrown": (139, 69, 19), "salmon": (250, 128, 114),
    "sandybrown": (244, 164, 96), "seagreen": (46, 139, 87),
    "seashell": (255, 245, 238), "sienna": (160, 82, 45),
    "silver": (192, 192, 192), "skyblue": (135, 206, 235),
    "slateblue": (106, 90, 205), "slategray": (112, 128, 144),
    "slategrey": (112, 128, 144), "snow": (255, 250, 250),
    "springgreen": (0, 255, 127), "steelblue": (70, 130, 180),
    "tan": (210, 180, 140), "teal": (0, 128, 128),
    "thistle": (216, 191, 216), "tomato": (255, 99, 71),
    "turquoise": (64, 224, 208), "violet": (238, 130, 238),
    "wheat": (245, 222, 179), "white": (255, 255, 255),
    "whitesmoke": (245, 245, 245), "yellow": (255, 255, 0),
    "yellowgreen": (154, 205, 50),
}


def _relative_luminance_rgb(r: float, g: float, b: float) -> float:
    """WCAG relative luminance for an RGB triple where each channel is in [0, 1]."""
    def _lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast_ratio(lum_fg: float, lum_bg: float) -> float:
    """WCAG contrast ratio between two relative luminances."""
    light, dark = max(lum_fg, lum_bg), min(lum_fg, lum_bg)
    return (light + 0.05) / (dark + 0.05)


def _chip_fg(bg_css: str) -> str:
    """Compute a readable foreground color for a chip with the given CSS background.

    Picks black or white based on which gives the higher WCAG contrast ratio
    against the background (P2-23: replaces the brittle luminance threshold
    of 0.18, which had no margin near the boundary). Handles ``hsl()``,
    ``rgb()``/``rgba()``, ``#hex`` (3/6/8-digit), and the common CSS named
    colours; unparseable values fall back to ``#ffffff``.

    P1-7: hex and ``rgb()``/``rgba()`` inputs are now parsed (previously
    only ``hsl()`` and named colours were, so palette overrides using hex
    like ``#ffff00`` fell through to ``#ffffff`` and produced invisible
    white-on-yellow chips). The parse order and math mirror the JS port
    ``graph.js:_chipFg`` exactly so a palette override yields the same fg
    in the server-rendered concept page and the client-rendered graph view
    (contract-runtime-parity invariant).

    The dark-foreground choice is pure black (``#000000``) rather than a
    softened ``#1a1a2e`` — pure black passes WCAG AA for every hue in the
    auto-palette (verified across 0..360 deg at ``hsl(h, 62%, 48%)``).
    """
    if not isinstance(bg_css, str):
        return "#ffffff"
    s = bg_css.strip()
    rgb: tuple[float, float, float] | None = None
    m = _re_chip.match(r"hsla?\(\s*(\d+(?:\.\d+)?)\s*(?:deg|rad|turn|grad)?\s*,\s*(\d+(?:\.\d+)?)%\s*,\s*(\d+(?:\.\d+)?)%", s)
    if m:
        h = float(m.group(1)) % 360
        sat = float(m.group(2)) / 100.0
        l = float(m.group(3)) / 100.0
        c = (1 - abs(2 * l - 1)) * sat
        x = c * (1 - abs((h / 60) % 2 - 1))
        m_ = l - c / 2
        if h < 60:
            r, g, b = c, x, 0
        elif h < 120:
            r, g, b = x, c, 0
        elif h < 180:
            r, g, b = 0, c, x
        elif h < 240:
            r, g, b = 0, x, c
        elif h < 300:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x
        rgb = (r + m_, g + m_, b + m_)
    else:
        # P1-7: parse rgb()/rgba() (alpha is ignored — luminance is RGB).
        m_rgb = _re_chip.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", s)
        m_hex = _re_chip.match(
            r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$", s, _re_chip.IGNORECASE
        )
        if m_rgb:
            rgb = (
                float(m_rgb.group(1)) / 255.0,
                float(m_rgb.group(2)) / 255.0,
                float(m_rgb.group(3)) / 255.0,
            )
        elif m_hex:
            h = m_hex.group(1)
            if len(h) == 3:
                # Expand shorthand: #abc → #aabbcc.
                h = "".join(ch * 2 for ch in h)
            elif len(h) == 8:
                # Strip alpha (#rrggbbaa) — luminance is RGB-only.
                h = h[:6]
            rgb = (
                int(h[0:2], 16) / 255.0,
                int(h[2:4], 16) / 255.0,
                int(h[4:6], 16) / 255.0,
            )
        else:
            named = _NAMED_CSS_COLORS.get(s.lower())
            if named is not None:
                rgb = (named[0] / 255.0, named[1] / 255.0, named[2] / 255.0)
    if rgb is None:
        return "#ffffff"
    lum_bg = _relative_luminance_rgb(*rgb)
    # P2-23: pick the higher-contrast foreground instead of using a brittle
    # luminance threshold. Black has luminance 0, white has luminance 1.
    contrast_black = _contrast_ratio(0.0, lum_bg)
    contrast_white = _contrast_ratio(1.0, lum_bg)
    return "#000000" if contrast_black >= contrast_white else "#ffffff"

# ---------------------------------------------------------------------------
# Bundle → graph JSON
# ---------------------------------------------------------------------------

# P1-3 (iter-1): matches RFC-3986-style scheme prefixes (e.g. "javascript:",
# "data:", "mailto:") so _graph_governed_keys can distinguish URL-like values
# (which _safe_url must vet) from plain-text values (kept for display).
_LINK_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _graph_governed_keys(concept: Concept) -> dict[str, list]:
    """Extract governed frontmatter keys (§7) for the graph view.

    Mirrors :func:`_render_governed_keys` (concept page) but returns a
    JSON-safe dict so the data ships to the client (graph.js renders it in
    the detail panel). Every key is always present, defaulting to an empty
    list when the frontmatter key is absent or malformed — the client hides
    empty sections, so absence is graceful (fail-closed).

    URLs in ``provenance[].source`` and ``citations[].url`` are run through
    :func:`_safe_url` so a ``javascript:``/``data:`` value can't XSS the
    graph view (graph.js assigns them to ``a.href``; graph.js ALSO guards
    with an http(s)-only check, so this is defense-in-depth). A value that
    looks like a URL (has a scheme) but is rejected by ``_safe_url`` is
    blanked; plain-text values with no scheme are kept for display.

    ``relations`` is read from the frontmatter (typed-relation declarations)
    so the detail panel can show the relation type + target even when the
    target concept is not in the bundle (unresolved). Resolved edges are
    already shipped separately in ``edges``.
    """
    from .viewer.markdown import _safe_url

    def _safe_list(v: Any) -> list:
        return list(v) if isinstance(v, (list, tuple)) else []

    def _sanitize_linkable(val: str) -> str:
        """Return a safe href for allowed URLs, plain text for non-URL
        values, or '' for URL-like-but-dangerous schemes."""
        if not val:
            return ""
        safe = _safe_url(val)
        if safe:
            return safe
        # Not an allowed URL. Keep it as display text only when it has no
        # scheme (e.g. "internal memo"); blank scheme-bearing values that
        # _safe_url rejected (javascript:, data:, ...).
        return "" if _LINK_SCHEME_RE.match(val) else val

    fm = concept.frontmatter

    # aliases — tolerate strings or {label, discoverable} dicts.
    aliases = alias_labels(fm.get("aliases"))

    # entities — tolerate bare strings or {id,label,kind,aliases} dicts (§7.2)
    entities_raw = _safe_list(fm.get("entities"))
    entities: list[dict[str, Any]] = []
    for ent in entities_raw:
        if isinstance(ent, str) and ent.strip():
            entities.append({"id": "", "label": str(ent).strip(), "kind": "", "aliases": []})
        elif isinstance(ent, dict):
            ent_aliases = _safe_list(ent.get("aliases"))
            entities.append({
                "id": str(ent.get("id", "") or ""),
                "label": str(ent.get("label", "") or ""),
                "kind": str(ent.get("kind", "") or ""),
                "aliases": [str(a).strip() for a in ent_aliases if isinstance(a, str) and str(a).strip()],
            })

    # provenance — [{source,note,timestamp}] with sanitized source URL
    prov_raw = _safe_list(fm.get("provenance"))
    provenance: list[dict[str, str]] = []
    for prov in prov_raw:
        if not isinstance(prov, dict):
            continue
        provenance.append({
            "source": _sanitize_linkable(str(prov.get("source", "") or "")),
            "note": str(prov.get("note", "") or ""),
            "timestamp": str(prov.get("timestamp", "") or ""),
        })

    # citations — [{id,text,url}] with sanitized url
    cit_raw = _safe_list(fm.get("citations"))
    citations: list[dict[str, str]] = []
    for cit in cit_raw:
        if not isinstance(cit, dict):
            continue
        citations.append({
            "id": str(cit.get("id", "") or ""),
            "text": str(cit.get("text", "") or ""),
            "url": _sanitize_linkable(str(cit.get("url", "") or "")),
        })

    # relations — typed-relation declarations [{type,target,via}]
    rel_raw = _safe_list(fm.get("relations"))
    relations: list[dict[str, str]] = []
    for rel in rel_raw:
        if not isinstance(rel, dict):
            continue
        relations.append({
            "type": str(rel.get("type", "related") or "related"),
            "target": str(rel.get("target", "") or ""),
            "via": str(rel.get("detail", "") or ""),
        })

    return {
        "aliases": aliases,
        "entities": entities,
        "provenance": provenance,
        "citations": citations,
        "relations": relations,
    }


_GRAPH_GROUPING_KEYS = (
    "graph_cluster",
    "source_system",
    "project",
    "section",
    "import_batch",
    "redmine_project",
)


def _graph_grouping_metadata(concept: Concept) -> dict[str, str]:
    """Return a small allowlist of graph grouping metadata.

    Unknown frontmatter is legal and preserved, but graph JSON is a rendered
    read surface. Do not expose arbitrary keys here; only ship generic grouping
    fields that are useful across imported or mixed-source bundles.
    """
    out: dict[str, str] = {}
    fm = concept.frontmatter
    for key in _GRAPH_GROUPING_KEYS:
        value = fm.get(key)
        if isinstance(value, (list, dict)):
            continue
        text = str(value or "").strip()
        if text:
            out[key] = text[:160]
    return out


def build_graph_data(bundle: Bundle, *, name: str | None = None) -> dict[str, Any]:
    """Serialise a Bundle into the JSON shape consumed by the viewer.

    Shape (consumed by ``viewer/static/graph.js``):
        {
          "name": str,
          "nodes": [{data: {id, label, type, description, resource, tags,
                            color, size,
                            aliases, entities, provenance, citations,
                            relations,
                            metadata, graph_cluster, source_system}}],
          "edges": [{data: {id, source, target}}],
          "external": [{source, target_raw, label}],
          "bodies": {id: markdown_body},
          "types": [str],
          "palette": {type: css_color},
          "backlinks": {target_id: [source_id, ...]},
        }

    The §7 governed keys (P1-3 iter-1) are always present per node,
    defaulting to empty lists when the frontmatter key is absent.
    """
    graph = bundle.graph()
    palette = resolve_palette(bundle)
    types = sorted(t for t in bundle.types() if t)

    nodes: list[dict[str, Any]] = []
    bodies: dict[str, str] = {}
    # §3.2: sort concepts by id so graph node ordering (and thus the
    # emitted static __data/graph.json) is byte-stable regardless of
    # Bundle insertion order. Bundle.load happens to sort via rglob today,
    # but in-memory Bundle construction must not silently break current spec §8
    # byte-stability or graph rendering determinism.
    for c in sorted(bundle.concepts.values(), key=lambda c: c.id):
        cid_str = concept_id_to_str(c.id)
        # Pre-render the body to HTML server-side using our escaping renderer
        # (with URL scheme allowlist). This eliminates the need for a
        # client-side markdown parser (and its XSS surface). The client
        # assigns the HTML directly to innerHTML; it is safe because our
        # renderer escapes raw HTML and neutralises dangerous URL schemes.
        # Apply heading demotion for consistent document outline across
        # all render targets (concept pages AND single-file/graph views).
        bodies[cid_str] = _demote_headings(markdown_to_html(c.body))
        # Sanitize resource URL through _safe_url to prevent javascript:/data:
        # XSS via the graph view (graph.js assigns data.resource to a.href).
        safe_resource = ""
        if c.resource:
            from .viewer.markdown import _safe_url as _su
            sr = _su(c.resource)
            safe_resource = sr if sr is not None else ""
        color = palette.get(c.type or "", hsl_color(stable_hue(c.type or "concept")))
        # P1-3 (iter-1): ship §7 governed keys per node so the graph detail
        # panel can render them (mirrors the concept page). Read safely and
        # default to empty lists when absent — the client hides empties.
        governed = _graph_governed_keys(c)
        grouping_meta = _graph_grouping_metadata(c)
        nodes.append({
            "data": {
                "id": cid_str,
                "label": c.title or cid_str,
                "type": c.type or "concept",
                "description": c.description,
                "resource": safe_resource,
                "tags": list(c.tags),
                "color": color,
                # Phase 3: icon key for the node glyph (graph.js builds a
                # data-URI SVG from the shared icon path set).
                "icon": type_icon_key(c.type),
                "size": 32 + min(48, len(c.body) // 200),
                # Recent needs the authored timestamp (ISO
                # string or ""), Attention needs the body weight to spot
                # stubs. Strings only — the client parses defensively.
                "timestamp": str(c.frontmatter.get("timestamp") or ""),
                "body_chars": len(c.body or ""),
                # §7 governed keys (P1-3 iter-1): URLs already sanitized
                # via _safe_url inside _graph_governed_keys.
                "aliases": governed["aliases"],
                "entities": governed["entities"],
                "provenance": governed["provenance"],
                "citations": governed["citations"],
                "relations": governed["relations"],
                "metadata": grouping_meta,
                "graph_cluster": grouping_meta.get("graph_cluster", ""),
                "source_system": grouping_meta.get("source_system", ""),
            }
        })

    # Consume the model's logical-edge view. Raw ``graph.edges`` retains every
    # authored occurrence; logical_edges() de-duplicates by
    # (source, relation-type, target), preserving distinct typed relations.
    edges: list[dict[str, Any]] = []
    backlinks: dict[str, list[str]] = {}
    logical_links = graph.logical_edges()
    typed_pairs = {
        (link.source, link.target)
        for link in logical_links
        if link.origin == "relation"
    }
    for link in logical_links:
        if link.target is None:
            continue
        # Preserve the viewer's established "typed relation upgrades a body
        # link" contract. The model still exposes both logical edges; this
        # projection suppresses only the generic markdown edge when one or
        # more typed edges describe the same source/target pair.
        if link.origin == "markdown" and (link.source, link.target) in typed_pairs:
            continue
        src = concept_id_to_str(link.source)
        tgt = concept_id_to_str(link.target)
        if src == tgt:
            continue
        # Preserve edge label so typed-relation types (references, written_by,
        # etc.) are visible in the graph view.
        has_label = link.label and not link.label.startswith("/")
        edge_data: dict[str, Any] = {
            "id": f"edge-{len(edges)}",
            "source": src,
            "target": tgt,
            "origin": link.origin,
        }
        if has_label:
            edge_data["label"] = link.label
        edges.append({"data": edge_data})
        backlinks.setdefault(tgt, [])
        if src not in backlinks[tgt]:
            backlinks[tgt].append(src)

    external = [
        {
            "source": concept_id_to_str(e.source),
            "target_raw": e.target_raw,
            "label": e.label,
        }
        for e in graph.external
    ]

    # iter2 P2-1: explicit sort for byte-stable output (current spec §3/§8). Iter-1
    # sorted nodes but missed these sibling lists; the dedup loop above
    # preserves graph.edges insertion order which is nondeterministic for
    # in-memory Bundle construction. Sort all output lists by stable keys.
    edges.sort(key=lambda e: (e["data"]["source"], e["data"]["target"]))
    external.sort(key=lambda e: (e["source"], e["target_raw"]))
    backlinks = {k: sorted(v) for k, v in sorted(backlinks.items())}

    return {
        "name": name or bundle.name,
        "nodes": nodes,
        "edges": edges,
        "external": external,
        "bodies": bodies,
        "types": types,
        "palette": palette,
        "backlinks": backlinks,
        # Phase 3: icon-key → SVG inner markup, so graph.js can build node
        # glyph data-URIs from the same curated set the wiki uses.
        "icon_paths": type_icon_paths(),
    }


# ---------------------------------------------------------------------------
# Theme button (P2-74) + shared topbar nav fragment (P2-61)
# ---------------------------------------------------------------------------

# Valid data-theme values. wiki.css defines a token block per theme; the
# button cycles them in this order. KEEP IN SYNC with the THEMES /
# THEME_GLYPHS copies in wiki.js, graph.js and studio.js (each JS context
# loads without the others).
_THEMES: tuple[str, ...] = ("light", "dark", "pastel", "sepia", "midnight")
_THEME_GLYPHS: dict[str, str] = {
    "light": "\u2600",     # \u2600 sun
    "dark": "\u263e",      # \u263e moon
    "pastel": "\u273f",    # \u273f flower
    "sepia": "\u2615",     # \u2615 hot beverage
    "midnight": "\u2605",  # \u2605 star
}


def _theme_button_html(initial_theme: str) -> str:
    """Server-side initial theme button to avoid FOUC (P2-74).

    Emits the glyph that matches the initial ``data-theme`` so the first
    paint is consistent. ``wiki.js`` / ``graph.js`` update both the
    ``data-theme`` attribute and the button glyph atomically when the user
    (or localStorage) overrides the initial theme. The button cycles the
    five themes, so it carries an aria-label naming the current theme
    rather than a two-state aria-pressed.
    """
    theme = initial_theme if initial_theme in _THEMES else "light"
    glyph = _THEME_GLYPHS[theme]
    return (
        '<button id="okf-theme" type="button" '
        f'aria-label="Change colour theme (current: {theme})" '
        f'title="Theme: {theme} \u2014 click to cycle">{glyph}</button>'
    )


def _nav_controls_html(
    *,
    root_prefix: str,
    graph_link: str,
    initial_theme: str,
    current_query: str = "",
    mode: str = "serve",
    search_mode: str | None = None,
) -> str:
    """Shared topbar controls fragment (P2-61).

    Renders the search form + Graph + Index + theme button. Used by the
    concept, index, and search templates so the chrome is identical
    across all three. (Graph view and single-file viewers keep their own
    topbars because they carry typeahead/filter/layout controls.)

    P1-3: in ``static`` mode the search form posts to ``__search.html``
    (the page is emitted at that path; the live server never runs to
    rewrite the extensionless URL). ``spa``/``serve`` keep the bare
    ``__search`` path because the live router handles it.
    """
    search_value = (
        f' value="{_esc_attr_qs(current_query)}"' if current_query else ""
    )
    index_href = root_prefix or "./"
    search_target = f"{root_prefix}__search{'.html' if mode == 'static' else ''}"
    mode_control = ""
    if mode == "serve" and search_mode is not None:
        choices = [("lexical", "Words"), ("semantic", "Similar wording"),
                   ("hybrid", "Combined"), ("tag", "Tags"),
                   ("entity", "Entities"), ("relation", "Relations")]
        options = "".join(f'<option value="{value}"' +
                          (' selected' if value == search_mode else '') +
                          f'>{label}</option>' for value, label in choices)
        mode_control = ('<select name="mode" aria-label="Search method">' + options +
                        '</select><button type="submit">Search</button>')
    return (
        '<div class="okf-topbar__controls">'
        f'<form action="{search_target}" method="get" role="search" class="okf-search-form">'
        '<input type="search" name="q" placeholder="Search\u2026" autocomplete="off"'
        f' aria-label="Search"{search_value}>'
        f'{mode_control}</form>'
        f'<a class="okf-btn" href="{graph_link}">Graph</a>'
        f'<a class="okf-btn" href="{index_href}">Index</a>'
        f'{_theme_button_html(initial_theme)}'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Single-file viewer
# ---------------------------------------------------------------------------

_CDN_SCRIPTS = (
    # Cytoscape.js + the fCoSE layout chain are loaded from CDN. Markdown
    # rendering happens server-side (see ``build_graph_data``) so marked.js
    # is NOT needed, which removes a client-side XSS surface and a
    # supply-chain dep.
    # Every tag is pinned to an exact version AND carries an SRI hash
    # (computed via:
    #   curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A
    # ). The browser will REFUSE to execute a script if the CDN response
    # does not match its hash, mitigating CDN-compromise / MITM risk.
    #
    # fCoSE (phase 3) is the quality force layout the graph uses by
    # default; its UMD chain is layout-base → cose-base → cytoscape-fcose
    # (each reads the previous from a browser global) and cytoscape-fcose
    # self-registers against window.cytoscape. graph.js falls back to
    # plain cose when the chain fails to load (offline / cdn:false).
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js"'
    ' integrity="sha384-J7Q85oZE4GJ/e7+n2aOQsLXfDwwfnA8S2nZAL5BpFsfpCF84zQD7LroZ/dMnLgex"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/layout-base@2.0.1/layout-base.js"'
    ' integrity="sha384-5E2lB9AIGE6LRCnOOSTnZRlYZFZ01iMeN2fw97Z1r4Z/kXALxKw2AC+ZzQqoeDsG"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/cose-base@2.2.0/cose-base.js"'
    ' integrity="sha384-RswRBkrMsPUYpJLbZ1CVA08zbNzAkykE2oGJTujBwfjWNdfxv2WVjLJNqv1LhAOp"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape-fcose@2.2.0/cytoscape-fcose.js"'
    ' integrity="sha384-uk5Wbjq1+KqUdHO30w7N7GrEGdzBhaJeW9o/ANF6v9+yx3M/cBmoX51C000JNCUf"'
    ' crossorigin="anonymous"></script>\n'
    # dagre powers the "Flow" lens (layered/Sugiyama layout for the
    # directed dependency subgraph — the evidence-backed layout for
    # direction/flow reading). cytoscape-dagre self-registers; graph.js
    # falls back to directed breadthfirst when the chain is absent.
    '<script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"'
    ' integrity="sha384-2IH3T69EIKYC4c+RXZifZRvaH5SRUdacJW7j6HtE5rQbvLhKKdawxq6vpIzJ7j9M"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.js"'
    ' integrity="sha384-u69h9ebXeSjlg6q/rb1zKTRAGu/h8deCl0409xpS/QJctMKnc4M9Fzkm01VOQdeF"'
    ' crossorigin="anonymous"></script>'
)


def _json_for_script(data: Any) -> str:
    """JSON-encode ``data`` for safe embedding inside ``<script>...</script>``.

    Escapes ``<``, ``>``, ``&`` to their ``\\u003c`` / ``\\u003e`` /
    ``\\u0026`` JSON escapes, plus the JS-only line separators
    ``\\u2028`` / ``\\u2029``. This prevents:

        1. HTML parser breakout via ``</script>`` sequences in the data
           (the HTML spec closes the script element on the first
           ``</script`` sequence, regardless of JS string context).
        2. ``<!--`` sequences from compatibility parser quirks.
        3. JS syntax errors from raw line separators (valid in JSON
           strings, invalid in JS string literals pre-ES2019).

    The browser's JS engine unescapes the sequences back to the original
    characters AT EXECUTION TIME, after the HTML parser has already
    finished, so the data round-trips losslessly.
    """
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_single_file(
    bundle: Bundle,
    out_path: Path,
    *,
    name: str | None = None,
) -> dict:
    """Render ONE self-contained HTML file at ``out_path``.

    The bundle is embedded as a JSON blob. Cytoscape.js 3.28.1 is loaded from
    CDN by default (markdown is rendered server-side; no client-side parser);
    pass ``config.cdn = false`` via the
    bundle's ``.okf-loom/viewer/config.json`` to omit the CDN tags for fully
    offline use (you would then need to ship your own copy).

    Returns ``{"concepts": N, "edges": M, "bytes": K}``.
    """
    out_path = Path(out_path)
    config = load_config(bundle)
    display_name = name or config.get("name") or bundle.name

    data = build_graph_data(bundle, name=display_name)
    template = load_template("single_file.html", bundle)
    css = load_static("wiki.css", bundle) + "\n" + load_static("graph.css", bundle)
    # Bundle renderers.js too so the single-file detail panel gets
    # the same mermaid/hljs/KaTeX treatment as the wiki + graph views
    # (graph.js dispatches okf-loom:bodyPatched after every showDetail).
    js = load_static("graph.js", bundle) + "\n" + load_static("renderers.js", bundle)

    initial_theme = "light"
    if config.get("theme") in _THEMES:
        initial_theme = config["theme"]
    initial_layout = config.get("default_layout") or "cose"
    cdn_scripts = _CDN_SCRIPTS if config.get("cdn", True) else (
        "<!-- CDN scripts omitted (config.cdn=false). Provide your own "
        "Cytoscape.js to enable the graph view. -->"
    )

    html = (
        template
        .replace("__BUNDLE_NAME__", _json_for_script(display_name))
        .replace("__BUNDLE_NAME_DISPLAY__", _esc(display_name))
        .replace("__BUNDLE_DATA__", _json_for_script(data))
        .replace("/*__VIEWER_CSS__*/", css)
        .replace("/*__VIEWER_JS__*/", js)
        .replace("__INITIAL_THEME__", initial_theme)
        .replace("__INITIAL_THEME_BUTTON__", _theme_button_html(initial_theme))
        .replace("__INITIAL_LAYOUT__", initial_layout)
        .replace("__CDN_SCRIPTS__", cdn_scripts)
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(out_path, html)
    return {
        "concepts": len(bundle.concepts),
        "edges": len(data["edges"]),
        "bytes": len(html.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# Multi-file site build (spa | static | single-file)
# ---------------------------------------------------------------------------

def build_site(
    bundle: Bundle,
    out_dir: Path,
    *,
    target: str,
    name: str | None = None,
    plugin: Any = None,
    allow_active_code: bool | None = None,
) -> dict:
    """Build a static site for the given target.

    Targets:
        "single-file" → calls :func:`render_single_file` into ``out_dir/index.html`.
        "static"      → multi-file server-rendered HTML, no JS router
                        (print-friendly, works on file://).
        "spa"         → same per-concept pages as "static" with the SPA
                        enhancements enabled (search-as-you-type, popovers).

    ``plugin`` (optional) is a viewer plugin applied to each concept page at
    render time (current spec §15). When ``None`` the bundle's
    :class:`~okf_loom.config.OkfConfig`-gated composite is built via
    :func:`okf_loom.viewer.plugins.build_viewer_plugin`.

    ``allow_active_code`` (optional, current spec §14/§15) is the
    operator-consent flag. When ``None`` it defers to the effective gate
    (bundle cfg AND operator env/CLI consent resolved via
    :func:`okf_loom.viewer.assets.effective_allow_active_code`). When the
    effective gate is open, a prominent WARNING is printed to stderr.

    Returns a stats dict with file/concept/edge counts and total bytes.
    """
    out_dir = Path(out_dir)
    if target not in ("spa", "static", "single-file"):
        raise ValueError(f"Unknown target: {target!r}")

    stats: dict[str, Any] = {"target": target, "out_dir": str(out_dir)}

    # P2-2/P2-15 (iter-5): check effective gate + warn BEFORE the single-file
    # early return so ALL build targets emit the consent warning consistently.
    from .viewer.assets import effective_allow_active_code
    effective_allow = effective_allow_active_code(bundle.root)
    if allow_active_code is not None:
        from .viewer.assets import set_operator_consent
        set_operator_consent(bool(allow_active_code))
        effective_allow = effective_allow_active_code(bundle.root)
    if effective_allow:
        import sys
        print(
            f"WARNING: active code (viewer overrides + plugins) is ENABLED for "
            f"bundle {bundle.root}. Only run this for bundles whose "
            f"override/plugin sources you trust.",
            file=sys.stderr,
        )

    if target == "single-file":
        out_dir.mkdir(parents=True, exist_ok=True)
        single = render_single_file(bundle, out_dir / "index.html", name=name)
        stats.update(single)
        stats["files"] = 1
        return stats

    # Multi-file (spa | static).
    config = load_config(bundle)
    display_name = name or config.get("name") or bundle.name
    palette = resolve_palette(bundle)
    graph_data = build_graph_data(bundle, name=display_name)
    mode = target  # passed to URL helper; spa & static share URL scheme

    # Current spec §15: load viewer plugins at render start, gated on the
    # effective allow_active_code (computed above before the single-file
    # early return). Callers may pass an explicit ``plugin``; otherwise
    # the effective-gate-driven composite is built.
    if plugin is None:
        from .viewer.plugins import build_viewer_plugin
        plugin = build_viewer_plugin(
            bundle.root, allow_active_code=effective_allow,
        )

    # Stage to a tmp dir, then atomically swap into place.
    with tempfile.TemporaryDirectory(prefix="okf-build-", dir=str(out_dir.parent) if out_dir.parent.exists() else None) as tmp:
        tmp_root = Path(tmp)
        _emit_site(bundle, tmp_root, mode=mode, name=display_name,
                   palette=palette, graph_data=graph_data, config=config,
                   plugin=plugin)

        out_dir.mkdir(parents=True, exist_ok=True)
        from .io_utils import atomic_replace_tree as _replace_tree
        _replace_tree(tmp_root, out_dir)

    # Compute stats from the final tree.
    total_bytes = 0
    file_count = 0
    for p in out_dir.rglob("*"):
        if p.is_file():
            file_count += 1
            total_bytes += p.stat().st_size
    stats.update({
        "concepts": len(bundle.concepts),
        "edges": len(graph_data["edges"]),
        "files": file_count,
        "bytes": total_bytes,
    })
    return stats


def _apply_optional_hook(plugin: Any, hook_name: str, concept: Any, html: str) -> str:
    """P1-28: call an optional plugin hook (e.g. ``on_index_render``) if the
    plugin exposes it. Mirrors the live server's parity with the composite.
    A plugin that does not expose the hook returns ``html`` unchanged. The
    ``CompositeViewerPlugin`` already wraps hook calls in try/except; for raw
    plugins we guard here too so a raising plugin never crashes the build."""
    method = getattr(plugin, hook_name, None)
    if method is None:
        return html
    try:
        out = method(concept, html) if concept is not None else method(html)
    except Exception:
        # Best-effort: a raising optional hook is logged-and-skipped, not fatal.
        import sys
        print(f"warning: viewer plugin {hook_name} raised; skipped",
              file=sys.stderr)
        return html
    return out if isinstance(out, str) else html


def _emit_site(
    bundle: Bundle,
    root: Path,
    *,
    mode: str,
    name: str,
    palette: dict[str, str],
    graph_data: dict[str, Any],
    config: dict[str, Any],
    plugin: Any = None,
) -> None:
    """Emit the full multi-file site into ``root`` (which must exist).

    ``plugin`` is an optional viewer plugin applied to each rendered concept
    page (current spec §15). It is the composite from
    :func:`~okf_loom.viewer.plugins.build_viewer_plugin` when called via
    :func:`build_site`; pass ``None`` to skip plugin processing.
    """
    static_dir = root / "__static"
    static_dir.mkdir(parents=True, exist_ok=True)
    for asset_name in list_builtin_static():
        _atomic_write_text(static_dir / asset_name, load_static(asset_name, bundle))

    data_dir = root / "__data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(data_dir / "graph.json", json.dumps(graph_data).encode("utf-8"))
    _atomic_write_bytes(data_dir / "content.json", json.dumps(_content_index_json(bundle)).encode("utf-8"))
    # Current spec §9: emit the static client-side search
    # corpus. Only emitted for static builds; spa/serve use the live
    # /__search backend. Sorted by concept_id at emit time (§3.2 determinism).
    if mode == "static":
        _atomic_write_bytes(
            data_dir / "search.json",
            json.dumps(_search_corpus_json(bundle)).encode("utf-8"),
        )
    # Per-concept pages.
    graph = bundle.graph()
    for concept in bundle.concepts.values():
        html = _render_concept_page(
            concept, bundle, graph, mode=mode, name=name,
            palette=palette, config=config,
        )
        if plugin is not None:
            # Apply viewer plugin to the concept-page render path. The
            # composite handles try/except + active-code gating internally.
            html = plugin.on_concept_render(concept, html)
        cid_str = concept_id_to_str(concept.id)
        # tables/users → tables/users.html
        out_path = root.joinpath(*cid_str.split("/")).with_suffix(".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(out_path, html)

    # Directory index pages (per directory that has any concepts).
    dirs_with_concepts: set[Path] = set()
    for c in bundle.concepts.values():
        if len(c.id) > 1:
            for d in _ancestor_dirs(c.id):
                dirs_with_concepts.add(d)
    for d in sorted(dirs_with_concepts, key=lambda p: len(p.parts)):
        # Use a hand-authored index.md if present (as a directory index page),
        # else synthesize. For static emission, always emit a page at <dir>/index.html.
        rel_index_md = d / "index.md" if d != Path() else Path("index.md")
        index_file = bundle.indexes.get(rel_index_md)
        html = _render_index_page(
            bundle, mode=mode, name=name, palette=palette, config=config,
            sub=("/".join(d.parts) if d.parts else ""),
            index_file=index_file,
        )
        if plugin is not None:
            # P1-28: apply on_index_render to directory index pages too (parity
            # with the live server, which calls on_index_render at server.py).
            html = _apply_optional_hook(plugin, "on_index_render", None, html)
        out_path = (root / d / "index.html") if d.parts else (root / "index.html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(out_path, html)

    # Root index.html (always, even if no subdir index).
    root_index = bundle.root_index()
    html = _render_index_page(bundle, mode=mode, name=name, palette=palette,
                              config=config, sub="", index_file=root_index)
    if plugin is not None:
        html = _apply_optional_hook(plugin, "on_index_render", None, html)
    _atomic_write_text(root / "index.html", html)

    # Full-page graph view + search page (so internal links to them resolve).
    graph_html = _render_graph_page(
        bundle, mode=mode, name=name, config=config, graph_data=graph_data,
    )
    _atomic_write_text(root / "__graph.html", graph_html)
    # Search results page. Static mode embeds its build-time corpus so it works
    # under both HTTP hosting and direct file:// browsing.
    search_html = _render_search_page(
        bundle, mode=mode, name=name, config=config,
        query="", results=[],
    )
    _atomic_write_text(root / "__search.html", search_html)

    # Bundle-local media: copy every §5-visible media file into the site
    # tree at its bundle-relative path, so image/video/PDF references keep
    # working in exported sites exactly as the live server serves them.
    _copy_bundle_media(bundle, root)


def _copy_bundle_media(bundle: Bundle, root: Path) -> int:
    """Copy §5-visible bundle media into the site output; returns the count.

    Same visibility semantics as the live server's media route
    (``iter_bundle_files`` over ``BUNDLE_MEDIA_EXTENSIONS`` with the
    bundle's exclude/include/gitignore config) and the same containment
    guard (a symlinked file pointing outside the bundle root is skipped,
    P2-54). Files under a top-level ``__``-prefixed directory are skipped —
    ``__static``/``__data`` are the site's own namespaces.
    """
    import posixpath as _pp
    from .config import OkfConfig, OkfConfigError
    from .ignore import iter_bundle_files, path_within
    from .viewer.assets import BUNDLE_MEDIA_EXTENSIONS
    try:
        bundle_cfg = OkfConfig.load(bundle.root).bundle
    except OkfConfigError:
        from .config import BundleConfig
        bundle_cfg = BundleConfig()
    media = iter_bundle_files(
        bundle.root,
        name_predicate=lambda n: _pp.splitext(n.lower())[1] in BUNDLE_MEDIA_EXTENSIONS,
        exclude=bundle_cfg.exclude,
        include=bundle_cfg.include,
        respect_gitignore=bundle_cfg.respect_gitignore,
    )
    copied = 0
    for src in media:
        rel = src.relative_to(bundle.root)
        if rel.parts and rel.parts[0].startswith("__"):
            continue
        if not path_within(src, bundle.root):
            continue
        dest = root.joinpath(*rel.parts)
        try:
            data = src.read_bytes()
        except OSError as e:
            import sys
            print(f"warning: skipping unreadable media file {src}: {e}",
                  file=sys.stderr)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(dest, data)
        copied += 1
    return copied


def _ancestor_dirs(cid: ConceptId) -> list[Path]:
    out: list[Path] = []
    for i in range(len(cid) - 1):
        out.append(Path(*cid[:i + 1]))
    return out


def _content_index_json(bundle: Bundle) -> dict[str, Any]:
    """Compact content index suitable for external tools to consume."""
    return {
        "name": bundle.name,
        "concepts": [
            {
                "id": concept_id_to_str(c.id),
                "type": c.type,
                "title": c.title,
                "description": c.description,
                "tags": list(c.tags),
                "resource": c.resource,
                "timestamp": c.timestamp,
                "path": str(c.rel_path),
            }
            # iter3 P2-1: sort by concept_id (§3.2 determinism).
            for c in sorted(bundle.concepts.values(), key=lambda c: c.id)
        ],
    }


def _concept_aliases(c: Concept) -> list[str]:
    """Frontmatter ``aliases`` (SPEC §7 governed key) as a list of strings.

    Tolerates missing/scalar/list shapes (mirrors :py:attr:`Concept.tags`).
    Producers MAY omit ``aliases``; consumers MUST treat it as ``[]`` then.
    """
    return alias_labels(c.frontmatter.get("aliases"))


def _search_corpus_json(bundle: Bundle) -> list[dict[str, Any]]:
    """Build-time search corpus for the static client-side searcher.

    One entry per concept, sorted by concept_id (current spec §3 determinism).
    The shape matches what ``static-search.js`` consumes: a bare JSON array
    of ``{id, title, description, type, tags, aliases, body_excerpt}``
    records. ``body_excerpt`` is the first ~500 chars of
    :func:`strip_markdown_for_search` output (stable, low-noise text).

    Used only by the ``--target static`` build path; ``serve``/``spa`` keep
    using the live ``/__search`` backend. Mirrors the lexical-backend field
    set so live and static results stay semantically close.
    """
    from .parse import strip_markdown_for_search
    out: list[dict[str, Any]] = []
    for c in bundle.concepts.values():
        body_clean = strip_markdown_for_search(c.body or "")
        out.append({
            "id": concept_id_to_str(c.id),
            "title": c.title,
            "description": c.description,
            "type": c.type or "",
            "tags": list(c.tags),
            "aliases": _concept_aliases(c),
            "body_excerpt": body_clean[:500],
        })
    out.sort(key=lambda e: e["id"])
    return out


# ---------------------------------------------------------------------------
# Page rendering (shared with server.py via _render_* helpers)
# ---------------------------------------------------------------------------

def _demote_headings(html: str) -> str:
    """Demote all HTML headings in ``html`` by one level (h1→h2, h2→h3, … h6→h6).

    Used on concept-page body HTML so the page <h1> (the concept title) is
    the only top-level heading, preserving a clean document outline. Runs
    right-to-left to avoid double-demoting (h1→h2 would then match h2→h3).
    Operates on <hN> and </hN> tags; attribute-safe because the regex
    requires the digit immediately after ``h`` and matches the closing
    slash in self-closing variants.
    """
    import re as _re
    out = html
    for level in range(6, 0, -1):
        new = min(level + 1, 6)
        out = _re.sub(
            rf"<h{level}([ >])", f"<h{new}\\1", out
        )
        out = _re.sub(
            rf"</h{level}>", f"</h{new}>", out
        )
    return out


def _esc(s: Any) -> str:
    """HTML-escape a value for safe interpolation into the page."""
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _static_prefix_for(mode: str, source_cid: ConceptId | None = None) -> str:
    """URL prefix for ``/__static`` from a given source path."""
    if mode in ("serve", "spa"):
        return "/__static"
    # static: relative path back to the root.
    if source_cid is None or len(source_cid) <= 1:
        return "__static"
    depth = len(source_cid) - 1
    return "/".join([".."] * depth) + "/__static"


def _root_prefix_for(mode: str, source_cid: ConceptId | None = None) -> str:
    """URL prefix for the bundle root from a given source path.

    P1-3 (iter-4): serve/spa must return ``"/"`` (root-absolute), not ``""``
    (directory-relative). With ``""``, nested concepts at ``/tables/orders``
    resolve nav links (search/graph/index) to ``/tables/__search`` → 404.
    The brand link already special-cases this (render.py); the nav controls
    must match.
    """
    if mode in ("serve", "spa"):
        return "/"
    if source_cid is None or len(source_cid) <= 1:
        return "./"
    depth = len(source_cid) - 1
    return "/".join([".."] * depth) + "/"


def _render_link_map(bundle: Bundle, concept: Concept, mode: str) -> dict[str, str | None]:
    """Build a target_raw → url map for the concept's outgoing links.

    Keys are ANCHOR-STRIPPED: ``rewrite_internal_links`` looks hrefs up
    with the ``#fragment`` removed (and re-appends it to the mapped URL),
    so a map keyed by the full ``cli.md#validate`` target_raw would never
    match — anchored internal links then silently stayed raw and 404'd in
    static builds (the live server resolves raw .md paths, which masked
    this).
    """
    out: dict[str, str | None] = {}
    for link in bundle.graph().out_edges.get(concept.id, []):
        key = link.target_raw.split("#", 1)[0]
        if not key:
            continue  # pure-anchor link; not a rewrite candidate
        if link.target is None:
            out.setdefault(key, None)
            continue
        out[key] = url_for_concept(
            link.target, mode, source_cid=concept.id,
        )
    return out


# <img> tags come from our own renderer (_img_tag) with a double-quoted
# src, so this match is reliable. ``(?!/)`` skips protocol-relative ``//``
# (already neutralised by _safe_url, but keep the guard local too).
_IMG_ABS_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")/(?!/)([^"]+)(")')


def _relativize_asset_srcs(html: str, root_prefix: str) -> str:
    """Rewrite absolute bundle-relative ``<img src="/...">`` to root-relative.

    Static builds are browsed from ``file://`` or under arbitrary path
    prefixes (GitHub Pages project sites), so an absolute src — the
    recommended authoring form — must become relative to the page's
    directory, mirroring what :func:`url_for_concept` does for links.
    serve/spa keep absolute srcs (both are rooted at ``/``). Relative srcs
    (``./assets/x.png``) already resolve correctly against the page path
    and pass through untouched.
    """
    return _IMG_ABS_SRC_RE.sub(
        lambda m: m.group(1) + root_prefix + m.group(2) + m.group(3), html
    )


def _static_md_href(href: str, root_prefix: str) -> str | None:
    """Map an internal ``/a/b.md`` href to its static-build page URL.

    Static builds contain only ``.html`` files and are served under an
    arbitrary path prefix (e.g. GitHub Pages project sites), so a
    bundle-absolute raw-file href would 404 twice over. Returns
    ``<root_prefix>a/b.html`` for internal bundle-absolute ``.md`` hrefs,
    None for anything else (external URLs, anchors, non-md paths).
    """
    if not (href.startswith("/") and href.endswith(".md")):
        return None
    return root_prefix + href[1:-3] + ".html"


def _index_body_link_map(bundle: Bundle, body_md: str, mode: str) -> dict[str, str | None]:
    """target_raw → url map for the root ``index.md`` body.

    The root index is a reserved file, not a concept, so it has no graph
    out-edges; extract and resolve its links directly. External /
    out-of-bundle / anchor-only links are left untouched (absent from the
    map); internal links resolve to the mode URL — or None (rendered with
    ``data-okf-broken``) — exactly like concept bodies do via
    :func:`_render_link_map`. The index page sits at the bundle root, so
    static URLs need no ``source_cid`` relativisation.
    """
    from .parse import extract_links
    out: dict[str, str | None] = {}
    for link in extract_links(body_md, source_dir=bundle.root, bundle_root=bundle.root):
        if link.form in ("external", "external_out_of_bundle", "anchor"):
            continue
        key = link.target_raw.split("#", 1)[0]  # match _render_link_map keying
        if not key:
            continue
        cid = link.concept_id
        if cid is not None and cid in bundle.concepts:
            out[key] = url_for_concept(cid, mode)
            continue
        # Links to reserved sub-index files (`explanation/index.md`) are
        # not concepts but DO have a page: map them to the sub-index URL
        # in static builds; the live server already resolves them (serve
        # renders /<sub>/index.md directly), so leave them alone there.
        if cid is not None and cid[-1] == "index" and len(cid) > 1:
            sub_dir = "/".join(cid[:-1])
            if (bundle.root / sub_dir).is_dir():
                if mode == "static":
                    out[key] = f"{sub_dir}/index.html"
                continue
        out.setdefault(key, None)
    return out


def _readtime_html(body: str) -> str:
    """Estimated reading time chip for the concept header (~220 wpm).

    Hidden for stub-length bodies (< half a minute of reading) where
    "1 min read" would be noise rather than signal.
    """
    words = len((body or "").split())
    if words < 110:
        return ""
    minutes = max(1, round(words / 220))
    return f'<span class="okf-readtime">{minutes} min read</span>'


def _render_concept_page(
    concept: Concept,
    bundle: Bundle,
    graph,
    *,
    mode: str,
    name: str,
    palette: dict[str, str],
    config: dict[str, Any],
) -> str:
    template = load_template("concept_page.html", bundle)
    cid_str = concept_id_to_str(concept.id)
    static_prefix = _static_prefix_for(mode, concept.id)
    root_prefix = _root_prefix_for(mode, concept.id)

    # Body markdown → HTML → internal-link rewrite.
    body_html = markdown_to_html(concept.body)
    link_map = _render_link_map(bundle, concept, mode)
    body_html = rewrite_internal_links(body_html, link_map)
    if mode == "static":
        # Absolute image srcs must become page-relative in static builds
        # (the copied media sits at its bundle-relative path in the output).
        body_html = _relativize_asset_srcs(body_html, root_prefix)
    # P1-39: when frontmatter ``citations:`` is present (rendered by the
    # governed-keys block below), suppress the body's ``# Citations``
    # heading + its content so the two citation lists don't render twice.
    # We strip the body's Citations section BEFORE heading demotion so the
    # regex matches the original (pre-demote) ``<h1>Citations</h1>`` the
    # markdown renderer emits for ``# Citations``.
    if _has_frontmatter_citations(concept):
        body_html = _strip_body_citations_section(body_html)
    # Demote body headings by one level so the page <h1> (concept title)
    # is the only top-level heading. This preserves a clean document
    # outline for screen-reader navigation and SEO. `# Schema` in the
    # body becomes <h2>, `## Subsection` becomes <h3>, etc. h6 stays h6.
    body_html = _demote_headings(body_html)

    palette_for = palette
    type_color = palette_for.get(concept.type or "", "#94a3b8")

    # Frontmatter table (exclude governed keys that get dedicated sections).
    _GOVERNED_KEYS = frozenset({"aliases", "entities", "provenance", "citations", "relations"})
    # P2-11 (iter-3): also exclude keys already rendered in the page header
    # (type chip / h1 / description paragraph / resource link / tags row).
    # The "All fields" details block promises completeness but mostly
    # repeated what the user just read; only timestamp + unknown/extra keys
    # are genuinely new information in the table. Forward-compat value
    # (unknown producer-added keys surfacing here) is preserved.
    _HEADER_RENDERED_KEYS = frozenset({
        "type", "title", "description", "resource", "tags",
    })
    fm_rows: list[str] = ["<table><tbody>"]
    for k, v in concept.frontmatter.items():
        if k in _GOVERNED_KEYS:
            continue  # rendered in dedicated sections below
        if k in _HEADER_RENDERED_KEYS:
            continue  # already rendered in the page header (P2-11)
        fm_rows.append(
            f'<tr><th>{_esc(k)}</th><td><code>{_esc(_fmt_fm_value(v))}</code></td></tr>'
        )
    fm_rows.append("</tbody></table>")
    frontmatter_html = "\n".join(fm_rows) if concept.frontmatter else "<p class='okf-muted'>(none)</p>"

    # Dedicated sections for governed frontmatter keys.
    governed_html = _render_governed_keys(concept, mode=mode, root_prefix=root_prefix)

    # Resource.
    resource_html = ""
    if concept.resource:
        # Route through _safe_url to prevent javascript:/data: XSS via the
        # resource frontmatter key (same defence as markdown link hrefs).
        from .viewer.markdown import _safe_url
        safe_resource = _safe_url(concept.resource)
        if safe_resource is not None:
            # Static builds have no server to render raw bundle paths, so
            # an internal /a/b.md resource must point at its built page.
            static_href = _static_md_href(safe_resource, root_prefix)
            if mode == "static" and static_href is not None:
                safe_resource = static_href
            resource_html = (
                f'<a class="okf-external" href="{_esc(safe_resource)}" '
                f'target="_blank" rel="noopener noreferrer">{_esc(concept.resource)}</a>'
            )
        else:
            resource_html = (
                f'<span class="okf-link-blocked" title="blocked: '
                f'{_esc(concept.resource)}">{_esc(concept.resource)}</span>'
            )

    # Tags.
    tags_html = "".join(
        f'<span class="okf-tag">{_esc(t)}</span>' for t in concept.tags
    )

    # Backlinks ("Cited by").
    backlinks = graph.backlinks(concept.id)
    backlinks_html = _render_link_list(
        [(b.source, b.label, mode, concept.id, b.target_raw) for b in backlinks],
        bundle, kind="back",
    )

    # Outgoing links.
    out_links = graph.outlinks(concept.id)
    outgoing_html = _render_link_list(
        [(l.target, l.label, mode, concept.id, l.target_raw) for l in out_links if l.target is not None],
        bundle, kind="out",
    )

    # Local graph widget data (1-hop neighbours + self).
    # CRITICAL: the current concept MUST be nodes[0] so the client-side SVG
    # renderer (wiki.js renderLocalGraph) draws it as the centered node.
    local_ids = {concept.id} | set(graph.neighbours(concept.id, max_depth=1))
    current_node = {
        "id": concept_id_to_str(concept.id),
        "label": concept.title,
        "type": concept.type or "concept",
        "color": palette_for.get(concept.type or "", "#94a3b8"),
        "is_center": True,
    }
    neighbour_nodes = [
        {
            "id": concept_id_to_str(c.id),
            "label": c.title,
            "type": c.type or "concept",
            "color": palette_for.get(c.type or "", "#94a3b8"),
            "is_center": False,
        }
        for c in sorted(bundle.concepts.values(), key=lambda c: c.id)
        if c.id in local_ids and c.id != concept.id
    ]
    local_nodes = [current_node] + neighbour_nodes
    local_edges = [
        {"source": concept_id_to_str(e.source), "target": concept_id_to_str(e.target)}
        # iter2 P2-1: sort for deterministic local-graph JSON (§3.2).
        for e in sorted(graph.edges, key=lambda e: (e.source, e.target if e.target is not None else ()))
        if e.source in local_ids and e.target in local_ids
    ]
    local_data = json.dumps({"nodes": local_nodes, "edges": local_edges})

    # Topbar nav (P2-61: shared fragment so chrome is identical across
    # concept/index/search pages; theme button glyph matches initial theme
    # to avoid FOUC — P2-74).
    # P1-3: in static mode the Graph link must point at __graph.html (the
    # page is emitted at that path; the extensionless URL 404s).
    initial_theme = config.get("theme") or "light"
    nav_html = _nav_controls_html(
        root_prefix=root_prefix,
        graph_link=f"{root_prefix}__graph{'.html' if mode == 'static' else ''}",
        initial_theme=initial_theme,
        mode=mode,
    )

    theme_attr = ""
    if initial_theme in _THEMES:
        theme_attr = f' data-theme="{initial_theme}"'
    data_attrs = f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}"'

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    js_link = f'<script src="{static_prefix}/wiki.js" defer></script>'
    renderers_link = f'<script src="{static_prefix}/renderers.js" defer></script>'

    # P2-22: render aliases as a subtitle paragraph immediately after the
    # page title (SPEC §7.1 wording: "show as subtitles"). The subtitle is
    # emitted as a standalone placeholder so the template can interpolate
    # it directly after ``__CONCEPT_TITLE__``.
    subtitle_html = _render_subtitle(concept)

    # P2-3 (iter-1): clickable breadcrumb trail (replaces the flat raw id).
    breadcrumb_html = _render_breadcrumb(concept, mode=mode, name=name)

    rendered = (
        template
        .replace("__WORKSPACE_NAV__", navigation(bundle, mode=mode, root_prefix=root_prefix))
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(f"{concept.title} — {name}"))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__NAV_HTML__", nav_html)
        .replace("__STATIC_PREFIX__", static_prefix)
        # P1-8: brand link must point to the bundle root for ALL modes.
        # _root_prefix_for returns "" for serve/spa (which is correct for
        # the relative nav URLs the server resolves), but the brand link
        # needs an explicit "/" there. For static we use the relative
        # root_prefix ("./" or "../../"+).
        .replace("__BRAND_HREF__", _esc_attr_qs("/" if mode in ("serve", "spa") else root_prefix))
        # P1-9: local-graph pill nav reads data-root-prefix to build the
        # target URL. Only consumed by wiki.js in static mode.
        .replace("__ROOT_PREFIX__", _esc_attr_qs(root_prefix))
        .replace("__WIKI_CSS_LINK__", css_link + workspace_assets(static_prefix))
        .replace("__WIKI_JS_LINK__", js_link)
        .replace("__RENDERERS_JS_LINK__", renderers_link)
        .replace("__CONCEPT_ID__", _esc(cid_str))
        .replace("__BREADCRUMB_HTML__", breadcrumb_html)
        .replace("__CONCEPT_TYPE__", _esc(concept.type or "concept"))
        .replace("__CONCEPT_TYPE_COLOR__", _esc(type_color))
        .replace("__CONCEPT_TYPE_FG__", _esc(_chip_fg(type_color)))
        .replace("__TYPE_ICON__", type_icon_svg(concept.type, size=18))
        .replace("__READTIME_HTML__", _readtime_html(concept.body))
        # P1-1 (iter-2): the subtitle is a SIBLING <p> after the h1, not a
        # child of it. The prior concatenation into __CONCEPT_TITLE__ produced
        # invalid HTML (<h1> content model is phrasing-only; <p> is flow) and
        # polluted the h1's accessible name with the alias list on every
        # aliased concept. subtitle_html is "" when there are no aliases, so
        # the placeholder renders nothing in that case.
        .replace("__CONCEPT_TITLE__", _esc(concept.title))
        .replace("__CONCEPT_SUBTITLE__", subtitle_html)
        .replace("__CONCEPT_DESCRIPTION__", _esc(concept.description))
        .replace("__CONCEPT_RESOURCE__", resource_html)
        .replace("__CONCEPT_TAGS__", tags_html)
        .replace("__CONCEPT_BODY__", body_html)
        .replace("__BACKLINKS_HTML__", backlinks_html)
        .replace("__OUTGOING_HTML__", outgoing_html)
        .replace("__FRONTMATTER_HTML__", frontmatter_html)
        .replace("__GOVERNED_HTML__", governed_html)
        .replace("__LOCAL_GRAPH_DATA__", _esc(local_data))
    )
    # P2-68: strip empty ``<section class="okf-relations__block">`` blocks
    # (those whose only content was an empty link list + the heading).
    rendered = _EMPTY_RELATIONS_SECTION_RE.sub("", rendered)
    return rendered


def _render_link_list(
    items: list[tuple[ConceptId | None, str, str, ConceptId, str | None]],
    bundle: Bundle,
    *,
    kind: str,
) -> str:
    """Render a list of (target_cid, label, mode, source_cid, target_raw)
    tuples as ``<li>``s.

    Returns just the inner HTML (caller wraps in <section>).

    Deduplicates by target concept id (so a concept reached via BOTH a body
    link AND a typed relation shows once, with the relation type as a chip).

    P1-36: typed-relation detection now uses the SAME predicate as the
    graph-view edge labeller (:func:`build_graph_data`) —
    ``target_raw.startswith("relation:")`` — instead of the prior
    label-content heuristic. The two consumers in render.py are now
    consistent; the model.py + search.py consumers remain a separate
    cross-file cleanup (see REPORT in the bundle closeout).

    P2-68: returns an EMPTY string when there are no items so the caller
    (:func:`_render_concept_page`) can strip the now-empty wrapping
    ``<section class="okf-relations__block">`` rather than rendering the
    heading + "(none)" placeholder.
    """
    if not items:
        return ""
    out = ['<ul class="okf-linklist">']
    seen_targets: dict[ConceptId, str | None] = {}  # target_cid → relation_label
    for target_cid, label, mode, source_cid, target_raw in items:
        if target_cid is None:
            out.append(f'<li><span class="okf-muted">{_esc(label)}</span></li>')
            continue
        # Dedup by target_cid.
        is_typed_relation = _is_typed_relation_target_raw(target_raw)
        if target_cid in seen_targets:
            # If we already have this target but this entry carries a typed
            # relation label, record it for display.
            if is_typed_relation and _is_relation_label(label):
                seen_targets[target_cid] = label
            continue
        seen_targets[target_cid] = (
            label if (is_typed_relation and _is_relation_label(label)) else None
        )

    # iter3 P2-2: sort seen_targets by concept_id so the rendered
    # backlinks/outlinks list is deterministic (§3.2), not relying on
    # items[] insertion order (which mirrors graph.edges order, masked
    # by Bundle.load's sorted rglob for disk-loaded bundles).
    # Phase 2: connection CARDS — each target renders with its type icon,
    # a type-tinted accent, title + id, and the typed-relation chip with a
    # direction arrow. The palette is resolved once per call (cached
    # override read; cheap at page-render granularity).
    try:
        palette = resolve_palette(bundle)
    except Exception:  # noqa: BLE001 — cards degrade to accentless
        palette = {}
    arrow = "→" if kind == "out" else "←"
    for target_cid, rel_label in sorted(seen_targets.items()):
        concept = bundle.concepts.get(target_cid)
        title = concept.title if concept else concept_id_to_str(target_cid)
        ctype = concept.type if concept else None
        color = palette.get(ctype or "", "#94a3b8")
        icon = type_icon_svg(ctype, size=16, cls="okf-type-icon")
        url = url_for_concept(target_cid, mode, source_cid=items[0][3])
        # P2-57: emit ``okf-rel-type`` (the chip class defined in wiki.css)
        # rather than the orphaned ``okf-rel-kind`` which had no CSS rule
        # and rendered typed-relation labels invisible in link lists.
        chip = (
            f'<span class="okf-rel-type">{arrow} {_esc(rel_label)}</span>'
            if rel_label else ""
        )
        out.append(
            f'<li class="okf-conn" style="--okf-type-accent:{_esc(color)}">'
            f'<span class="okf-conn__icon" style="color:{_esc(color)}" aria-hidden="true">{icon}</span>'
            f'<span class="okf-conn__body">'
            f'<a href="{_esc(url)}" class="okf-internal okf-conn__title">{_esc(title)}</a>'
            f' <span class="okf-muted okf-conn__id">{_esc(concept_id_to_str(target_cid))}</span>'
            f'</span>{chip}</li>'
        )
    out.append("</ul>")
    return "\n".join(out)


def _is_typed_relation_target_raw(target_raw: str | None) -> bool:
    """P1-36: the canonical typed-relation discriminator.

    The graph builder (model.py:691) tags typed-relation edges with the
    magic prefix ``relation:`` on ``target_raw``. This predicate is shared
    by both render.py consumers (:func:`build_graph_data` and
    :func:`_render_link_list`) so the discriminator is consistent within
    the renderer. The clean fix lives in model.py (giving :class:`Link`
    an explicit ``origin`` field); that is owned by another bundle.
    """
    return bool(target_raw) and target_raw.startswith("relation:")


def _is_relation_label(label: str | None) -> bool:
    """P1-36: helper that filters out labels that look like paths/URLs.

    Even when ``_is_typed_relation_target_raw`` is True, the label might
    be empty or accidentally path-like; this guard ensures we only emit a
    chip when the label is plausibly a relation-type word.
    """
    return bool(label) and not label.startswith(("/", "http", "mailto"))


# P2-68: regex to strip empty ``<section class="okf-relations__block">``
# blocks after substitution. Matches a section whose only content is
# whitespace + an optional ``<h2>...</h2>`` heading. The template wraps
# both ``__OUTGOING_HTML__`` and ``__BACKLINKS_HTML__`` in such sections;
# when the link list is empty, the section collapses to ``<section ...><h2
# ...>...</h2>\n</section>`` which this regex removes.
_EMPTY_RELATIONS_SECTION_RE = re.compile(
    r'<section class="okf-relations__block"[^>]*>\s*'
    r'(?:<h2>[^<]*</h2>\s*)?'
    r'</section>\s*',
    re.DOTALL,
)


def _fmt_fm_value(v: Any) -> str:
    """Format a frontmatter value for display without leaking Python repr."""
    if isinstance(v, (list, tuple)):
        # If any item is a dict, use JSON for clean structured display
        if any(isinstance(x, dict) for x in v):
            return json.dumps(v, default=str, ensure_ascii=False, indent=2)
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, default=str, ensure_ascii=False, indent=2)
    return str(v)


# P1-39: detect frontmatter ``citations:`` so the renderer can suppress
# the duplicate body ``# Citations`` heading.
def _has_frontmatter_citations(concept: Concept) -> bool:
    """True when the concept has a non-empty ``citations:`` frontmatter key."""
    cits = concept.frontmatter.get("citations")
    return isinstance(cits, list) and bool(cits)


def _strip_body_citations_section(body_html: str) -> str:
    """P1-39: remove the body's ``# Citations`` section from rendered HTML.

    The frontmatter ``citations:`` governed key is rendered separately by
    :func:`_render_governed_keys`. When BOTH are present (a known producer
    pattern), the citations list appears twice on the rendered page. This
    helper strips the body section so only the governed-keys block remains.

    The regex matches the ``<hN...>Citations</hN>`` heading (allowing
    arbitrary attributes between the tag name and ``>``, since the markdown
    renderer emits ``<h2 id="citations">``) + all body content up to (but
    not including) the next heading. If no match is found, the body is
    returned unchanged.
    """
    pat = re.compile(
        r"<h([1-6])(?:[^>]*)>\s*Citations\s*</h\1>\s*(.*?)(?=<h[1-6][>\s]|$)",
        re.DOTALL | re.IGNORECASE,
    )
    return pat.sub("", body_html, count=1)


def _render_subtitle(concept: Concept) -> str:
    """P2-22: render the concept's aliases as a subtitle line.

    SPEC §7.1 wording: aliases are "Consumed by §4.3 entity search and the
    viewer (show as subtitles)". This emits a single ``<p>`` subtitle line
    (joined with " · ") immediately after the page title.

    P2-8 (iter-3): this subtitle is the SINGLE canonical surface for
    aliases. The previous "Also known as:" pills block in
    :func:`_render_governed_keys` was removed because it duplicated the
    same alias values within ~80px of this subtitle. Keeping one surface
    (the subtitle) gives screen-reader / SEO context AND avoids the
    information-architecture ambiguity of two renderings.

    P2-7 (iter-1): the inline ``style=`` fallback was removed; ``.okf-subtitle``
    is now a real rule in wiki.css (margin/font-size/colour via tokens).
    """
    strs = alias_labels(concept.frontmatter.get("aliases"))
    if not strs:
        return ""
    joined = " · ".join(_esc(a) for a in strs)
    return f'<p class="okf-subtitle">{joined}</p>'


def _render_breadcrumb(concept: Concept, *, mode: str, name: str) -> str:
    """P2-3 (iter-1): render a clickable breadcrumb trail.

    Emits ``{bundle_name} / tables / orders`` where each ancestor segment
    links to its directory index (``/dir/`` for serve/spa, relative
    ``index.html`` for static). The current concept (last segment) is plain
    text with ``aria-current="page"``.

    P2-10 (iter-3): the secondary ``<small class="okf-breadcrumb__id">``
    raw concept-id was removed. It duplicated the path segments already
    shown in the trail, AND it rendered at ~11.2px (``<small>`` ×
    ``.okf-muted``'s 14px × ``<small>`` default 0.8em) — below the 12px
    typographic floor used everywhere else in the viewer. The trail itself
    is now the only breadcrumb affordance. Developers who need the raw
    concept id can read it from the page URL or from the "All fields"
    details block.

    Determinism (§3.2): iteration is over the concept's own id segments,
    which are inherently ordered — no sorting needed.
    """
    cid = concept.id
    items: list[str] = []

    # Root crumb: bundle name → root index.
    if mode in ("serve", "spa"):
        root_href = "/"
    else:
        depth = len(cid) - 1
        root_href = ("../" * depth) + "index.html"
    items.append(f'<a href="{_esc(root_href)}" class="okf-breadcrumb__root">{_esc(name)}</a>')

    # Each full ancestor segment (except the last) is a directory index.
    for d in range(1, len(cid)):
        seg_label = _esc(str(cid[d - 1]))
        if mode in ("serve", "spa"):
            href = "/" + "/".join(str(s) for s in cid[:d]) + "/"
        else:
            ups = (len(cid) - 1) - d
            href = ("../" * ups) + "index.html"
        items.append(f'<a href="{_esc(href)}">{seg_label}</a>')

    # Final segment: current concept (plain text, marks current page).
    items.append(
        f'<span class="okf-breadcrumb__current" aria-current="page">{_esc(str(cid[-1]))}</span>'
    )

    sep = '<span class="okf-breadcrumb__sep" aria-hidden="true">/</span>'
    trail = f" {sep} ".join(items)
    return (
        f'<nav class="okf-breadcrumb" aria-label="Breadcrumb">'
        f'{trail}'
        f"</nav>"
    )


def _render_governed_keys(
    concept: Concept, *, mode: str = "serve", root_prefix: str = "",
) -> str:
    """Render governed keys (aliases, entities, provenance, citations, relations).

    P2-6 (iter-1): each section now has distinct treatment sized to its
    semantic weight (the prior flat inline-run layout was too dense to scan):

      * aliases    → NOT rendered here (P2-8 iter-3): aliases are rendered
                     ONCE as the page subtitle by :func:`_render_subtitle`
                     (SPEC §7.1 wording "show as subtitles"). The prior
                     "Also known as:" pills block duplicated the subtitle
                     within ~80px of vertical space.
      * entities   → ``<dl>`` (term = label + kind chip; dd = aliases)
      * provenance → stacked list: source link / muted note / ``<time>``
      * citations  → numbered ``<ol>`` (the ``[id]`` becomes the list marker)
      * relations  → typed-relation chips (unchanged)

    URLs in provenance/citations are sanitized via :func:`_safe_url`.
    """
    parts: list[str] = []

    # P2-8 (iter-3): aliases are NO LONGER rendered here. The subtitle
    # paragraph from :func:`_render_subtitle` is the single canonical
    # surface for aliases (it carries the SPEC §7.1 wording and gives
    # screen-reader / SEO context). The previous "Also known as:" pills
    # block duplicated the same alias values within ~80px of the subtitle,
    # which became conspicuous once P1-1 (iter-2) moved the subtitle out
    # of the h1 as a sibling. Removing the pills keeps the alias count at
    # exactly one render per concept.

    # Entities — definition list (P2-6): term = label + kind chip; dd = aliases
    entities = concept.frontmatter.get("entities")
    if entities and isinstance(entities, list):
        ent_terms: list[str] = []
        for ent in entities:
            if isinstance(ent, str) and ent.strip():
                ent_terms.append(
                    f'<dt><span class="okf-entity">{_esc(ent)}</span></dt>'
                    f'<dd></dd>'
                )
            elif isinstance(ent, dict):
                label = _esc(str(ent.get("label", "") or ""))
                kind = ent.get("kind")
                kind_html = f' <span class="okf-entity-kind">{_esc(str(kind))}</span>' if kind else ""
                ent_aliases = ent.get("aliases", [])
                alias_html = ""
                if isinstance(ent_aliases, list) and ent_aliases:
                    alias_html = '<span class="okf-entity-aliases">(' + ", ".join(
                        _esc(str(a)) for a in ent_aliases if isinstance(a, str)
                    ) + ")</span>"
                ent_terms.append(
                    f'<dt><span class="okf-entity">{label}</span>{kind_html}</dt>'
                    f'<dd>{alias_html}</dd>'
                )
        if ent_terms:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-entities">'
                f'<span class="okf-governed-label">Entities:</span>'
                f'<dl class="okf-entity-list">' + "".join(ent_terms) + '</dl></div>'
            )

    # Provenance — stacked list (P2-6): source link / note / time
    provenance = concept.frontmatter.get("provenance")
    if provenance and isinstance(provenance, list):
        prov_items: list[str] = []
        for prov in provenance:
            if not isinstance(prov, dict):
                continue
            source = prov.get("source", "")
            note = prov.get("note", "")
            ts = prov.get("timestamp", "")
            line_bits: list[str] = []
            if source:
                from .viewer.markdown import _safe_url
                safe = _safe_url(str(source))
                if safe:
                    # Same static-build mapping as the resource chip: an
                    # internal /a/b.md source must point at its built page.
                    static_href = _static_md_href(safe, root_prefix)
                    if mode == "static" and static_href is not None:
                        safe = static_href
                    line_bits.append(f'<a href="{_esc(safe)}" rel="noopener">{_esc(str(source))}</a>')
                else:
                    line_bits.append(f'<span class="okf-provenance-source">{_esc(str(source))}</span>')
            if note:
                line_bits.append(f'<span class="okf-provenance-note">{_esc(str(note))}</span>')
            if ts:
                line_bits.append(f'<time class="okf-provenance-time">{_esc(str(ts))}</time>')
            if line_bits:
                prov_items.append('<li class="okf-provenance-item">' + "".join(line_bits) + "</li>")
        if prov_items:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-provenance">'
                f'<span class="okf-governed-label">Sources:</span>'
                f'<ul class="okf-provenance-list">' + "".join(prov_items) + '</ul></div>'
            )

    # Citations — numbered list (P2-6): [id] text url
    citations = concept.frontmatter.get("citations")
    if citations and isinstance(citations, list):
        cit_items: list[str] = []
        for cit in citations:
            if not isinstance(cit, dict):
                continue
            text = cit.get("text", "")
            url = cit.get("url", "")
            cid_val = cit.get("id", "")
            bits: list[str] = []
            if cid_val:
                bits.append(f'<span class="okf-cite-id">[{_esc(str(cid_val))}]</span>')
            if text:
                bits.append(f'<span class="okf-cite-text">{_esc(str(text))}</span>')
            if url:
                from .viewer.markdown import _safe_url
                safe = _safe_url(str(url))
                if safe:
                    bits.append(f'<a href="{_esc(safe)}" rel="noopener">{_esc(str(url))}</a>')
            if bits:
                cit_items.append('<li class="okf-citation">' + " ".join(bits) + "</li>")
        if cit_items:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-citations">'
                f'<span class="okf-governed-label">Citations:</span>'
                f'<ol class="okf-citation-list">' + "".join(cit_items) + '</ol></div>'
            )

    # Relations — typed relation chips
    relations = concept.frontmatter.get("relations")
    if relations and isinstance(relations, list):
        rel_parts: list[str] = []
        for rel in relations:
            if isinstance(rel, dict):
                rel_type = _esc(str(rel.get("type", "related")))
                rel_target = _esc(str(rel.get("target", "")))
                rel_detail = rel.get("detail", "")
                detail_html = f' <span class="okf-rel-detail">{_esc(str(rel_detail))}</span>' if rel_detail else ""
                rel_parts.append(
                    f'<span class="okf-relation"><span class="okf-rel-type">{rel_type}</span> → {rel_target}{detail_html}</span>'
                )
        if rel_parts:
            parts.append(
                f'<div class="okf-governed okf-governed--block okf-relations-fm"><span class="okf-governed-label">Relations:</span> '
                + ", ".join(rel_parts) + "</div>"
            )

    return "\n".join(parts)


def _render_index_page(
    bundle: Bundle,
    *,
    mode: str,
    name: str,
    palette: dict[str, str],
    config: dict[str, Any],
    sub: str,
    index_file,
) -> str:
    template = load_template("index_page.html", bundle)
    static_prefix = "/__static" if mode in ("serve", "spa") else (
        "../" * (len(sub.split("/")) if sub else 0) + "__static" if sub else "__static"
    )

    # Title and intro.
    if sub:
        title = f"{name} / {sub}"
        intro = ""
    else:
        title = name
        intro_md = ""
        root_idx = bundle.root_index()
        if root_idx and root_idx.body.strip():
            intro_md = root_idx.body
        intro = markdown_to_html(intro_md) if intro_md else f'<p>{len(bundle.concepts)} concepts across {len(bundle.types())} types.</p>'
        # iter3 P1-1: demote headings in the root-index intro body so a
        # bundle whose index.md starts with "# Bundle Name" does not
        # produce a SECOND <h1> with the same text as the template's own
        # <h1 class="okf-index__title">. Concept pages already do this at
        # render.py:1062; this matches the discipline.
        if intro_md:
            intro = _demote_headings(intro)
            # The root index.md body was the ONE rendered-markdown surface
            # that skipped rewrite_internal_links (concept bodies route
            # through _render_link_map): in static builds every
            # hand-authored `demo/showcase.md`-style link 404'd because
            # only .html files exist in the output.
            intro = rewrite_internal_links(
                intro, _index_body_link_map(bundle, intro_md, mode)
            )
            if mode == "static":
                # Root index sits at the bundle root: "./" is its root prefix.
                intro = _relativize_asset_srcs(intro, "./")

    # Hero stat chips (root index only): concepts / types / links, plus a
    # prominent graph entry point. Fail-soft on graph errors.
    hero_stats = ""
    if not sub:
        n_edges = 0
        try:
            n_edges = len([e for e in bundle.graph().edges if e.target is not None])
        except Exception:  # noqa: BLE001 — stats are decorative
            n_edges = 0
        graph_href_stat = "/__graph" if mode in ("serve", "spa") else "__graph.html"
        hero_stats = (
            f'<div class="okf-hero__stats">'
            f'<span class="okf-stat"><strong>{len(bundle.concepts)}</strong> concepts</span>'
            f'<span class="okf-stat"><strong>{len(bundle.types())}</strong> types</span>'
            f'<span class="okf-stat"><strong>{n_edges}</strong> links</span>'
            f'<a class="okf-hero__graph-link" href="{_esc(graph_href_stat)}">'
            f'{type_icon_svg("model", size=15, cls="okf-type-icon")} Explore the graph</a>'
            f'</div>'
        )

    # Determine which concepts to list. Subdirectory indexes list direct
    # children only (unchanged). The ROOT index lists EVERY concept in the
    # bundle grouped by type — phase 1 dashboard: most bundles nest all
    # concepts one level deep, which used to leave the root with nothing
    # but directory tiles (zero concepts above the fold).
    sub_parts = tuple(sub.split("/")) if sub else ()
    direct: list[Concept] = []
    for c in bundle.concepts.values():
        if not sub_parts:
            direct.append(c)
        elif len(c.id) == len(sub_parts) + 1 and c.id[:len(sub_parts)] == sub_parts:
            direct.append(c)

    # Group by type.
    by_type: dict[str, list[Concept]] = {}
    for c in direct:
        by_type.setdefault(c.type or "<untyped>", []).append(c)

    # Per-concept link counts for the card footers (cheap: bundle.graph()
    # is cached on the bundle). Fail soft — cards render without counts.
    graph = None
    try:
        graph = bundle.graph()
    except Exception:  # noqa: BLE001 — index must render even if links are broken
        graph = None

    groups_parts: list[str] = []
    for t in sorted(by_type):
        # iter2 P3-3: add c.id tiebreaker so two concepts with the same
        # lowercased title are ordered deterministically (§3.2), not by
        # Bundle dict-insertion order.
        items = sorted(by_type[t], key=lambda c: (c.title.lower(), c.id))
        color = palette.get(t, "#94a3b8")
        icon = type_icon_svg(t, size=15, cls="okf-type-icon")
        rows = []
        for c in items:
            cid_str = concept_id_to_str(c.id)
            # Static sub-index pages live at <sub>/index.html, so concept
            # URLs must be relativised against that location (a root-
            # relative "explanation/x.html" would resolve to
            # explanation/explanation/x.html from there).
            sub_source = tuple(sub.split("/")) + ("index",) if sub else None
            url = url_for_concept(c.id, mode, source_cid=sub_source)
            desc = _esc(c.description[:140] + ("…" if len(c.description) > 140 else ""))
            # Card footer: outgoing/incoming counts + first few tags.
            meta_bits: list[str] = []
            if graph is not None:
                n_out = len([l for l in graph.outlinks(c.id) if l.target is not None])
                n_in = len(graph.backlinks(c.id))
                if n_out or n_in:
                    meta_bits.append(
                        f'<span class="okf-card__links" title="{n_out} outgoing, {n_in} incoming links">'
                        f'→ {n_out} · ← {n_in}</span>'
                    )
            for tag in c.tags[:3]:
                meta_bits.append(f'<span class="okf-tag okf-tag--card">{_esc(tag)}</span>')
            meta_html = (
                f'<span class="okf-card__meta">{"".join(meta_bits)}</span>'
                if meta_bits else ""
            )
            # The li keeps the .okf-concept-list li a contract (first anchor
            # = concept link; studio.js stampConceptIds + presence rely on
            # it) — the card look is CSS on top.
            description_html = f' <span class="okf-concept-desc okf-card__desc">{desc}</span>' if desc else ""
            rows.append(
                f'<li class="okf-card" style="--okf-type-accent:{_esc(color)}">'
                f'<a href="{_esc(url)}" class="okf-internal okf-card__link">{_esc(c.title)}</a>'
                f' <span class="okf-muted okf-card__id">{_esc(cid_str)}</span>'
                f'{description_html}'
                f'{meta_html}'
                f'</li>'
            )
        groups_parts.append(
            f'<section class="okf-section" style="--okf-type-accent:{_esc(color)}">'
            f'<h2 class="okf-section__title"><span class="okf-section__icon" style="color:{_esc(color)}">{icon}</span>'
            f'{_esc(t)} <span class="okf-section__count">{len(items)}</span></h2>'
            f'<ul class="okf-concept-list okf-cardgrid">{"".join(rows)}</ul>'
            f'</section>'
        )

    # Subdirectory links: any directory strictly deeper than `sub` whose
    # immediate parent is `sub`.
    sub_dirs: set[str] = set()
    for c in bundle.concepts.values():
        if not sub:
            if len(c.id) > 1:
                sub_dirs.add(c.id[0])
        else:
            sp = sub_parts
            if len(c.id) > len(sp) + 1 and c.id[:len(sp)] == sp:
                sub_dirs.add("/".join(c.id[:len(sp) + 1]))
    subdir_parts: list[str] = []
    for d in sorted(sub_dirs):
        url = f"{('../' * (len(sub_parts))) if sub_parts else ''}{d}/" if mode == "static" else f"/{d}/"
        # For static, link to the directory's index.html.
        if mode == "static":
            rel = "../" * len(sub_parts) + d + "/index.html"
        else:
            rel = "/" + d + "/"
        subdir_parts.append(
            f'<a href="{_esc(rel)}"><strong>{_esc(d)}</strong>'
            f'<span class="okf-muted">directory</span></a>'
        )
    subdirs_html = (
        f'<div class="okf-subdirs">{"".join(subdir_parts)}</div>'
        if subdir_parts else ""
    )

    theme_attr = ""
    initial_theme = config.get("theme") or "light"
    if initial_theme in _THEMES:
        theme_attr = f' data-theme="{initial_theme}"'

    # P1-10: emit data-okf-enhance on index pages too so wiki.js detects
    # static mode and shows the static-search-note (was only on concept
    # pages, so the notice never appeared on the index/search surfaces).
    data_attrs = f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}"'

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    js_link = f'<script src="{static_prefix}/wiki.js" defer></script>'
    renderers_link = f'<script src="{static_prefix}/renderers.js" defer></script>'
    graph_link = "/__graph" if mode in ("serve", "spa") else (
        ("../" * len(sub_parts)) + "__graph.html" if sub_parts else "__graph.html"
    )

    # P2-61: shared topbar nav. Index link points to the current directory
    # root (acts as a reload when already on the index). Graph link uses
    # the page-relative path computed above.
    root_prefix_for_nav = ("../" * len(sub_parts)) if sub_parts else ""
    if mode in ("serve", "spa"):
        root_prefix_for_nav = "/"  # P1-3 (iter-5): always root-absolute for serve/spa
    nav_html = _nav_controls_html(
        root_prefix=root_prefix_for_nav,
        graph_link=graph_link,
        initial_theme=initial_theme,
        mode=mode,
    )

    # iter1 P3-11: brand consistency — the index page brand is a real link to
    # the bundle root (matching the other 3 templates). On the root index it
    # is a self-link with aria-current="page"; on subdir indexes it links back
    # to the root so a deep user can climb back up.
    if mode in ("serve", "spa"):
        brand_href = "/"
    else:
        depth = len(sub.split("/")) if sub else 0
        brand_href = ("../" * depth) + "index.html" if depth else "index.html"
    brand_aria = ' aria-current="page"' if not sub else ""

    return (
        template
        .replace("__WORKSPACE_NAV__", navigation(bundle, mode=mode, root_prefix=("../" * len(sub_parts) if mode == "static" else "/")))
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(title))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__BRAND_HREF__", _esc_attr_qs(brand_href))
        .replace("__BRAND_ARIA_CURRENT__", brand_aria)
        .replace("__STATIC_PREFIX__", static_prefix)
        .replace("__WIKI_CSS_LINK__", css_link + workspace_assets(static_prefix))
        .replace("__WIKI_JS_LINK__", js_link)
        .replace("__RENDERERS_JS_LINK__", renderers_link)
        .replace("__NAV_HTML__", nav_html)
        .replace("__INDEX_TITLE__", _esc(title))
        .replace("__INDEX_INTRO__", intro)
        .replace("__HERO_STATS__", hero_stats)
        .replace("__GROUPS_HTML__", "\n".join(groups_parts))
        .replace("__SUBDIRS_HTML__", subdirs_html)
        .replace("__GLOBAL_GRAPH_LINK__", _esc(graph_link))
    )


def _render_search_page(
    bundle: Bundle,
    *,
    mode: str,
    name: str,
    config: dict[str, Any],
    query: str,
    search_mode: str = "lexical",
    results: list[dict[str, Any]],
) -> str:
    template = load_template("search_page.html", bundle)
    static_prefix = "/__static" if mode in ("serve", "spa") else "__static"
    back_link = "/" if mode in ("serve", "spa") else "index.html"

    # Phase 2: results carry the type identity (icon + tinted accent) so a
    # mixed result list is scannable by kind at a glance.
    try:
        palette = resolve_palette(bundle)
    except Exception:  # noqa: BLE001 — results degrade to accentless
        palette = {}
    result_parts: list[str] = []
    for r in results:
        cid = r.get("concept_id") or r.get("id") or ""
        title = r.get("title") or cid
        desc = r.get("description") or (r.get("snippets") or [""])[0]
        url = ("/" + cid) if mode in ("serve", "spa") else (cid + ".html")
        ctype = r.get("type") or ""
        if not ctype:
            try:
                from .paths import concept_id_from_str as _cid_from_str
                c = bundle.concepts.get(_cid_from_str(cid)) if cid else None
                ctype = (c.type or "") if c else ""
            except Exception:  # noqa: BLE001 — type chip is decorative
                ctype = ""
        color = palette.get(ctype, "#94a3b8")
        icon = type_icon_svg(ctype, size=14, cls="okf-type-icon")
        type_chip = (
            f'<span class="okf-search-result__type" style="color:{_esc(color)}">'
            f'{icon} {_esc(ctype)}</span>' if ctype else ""
        )
        result_parts.append(
            f'<article class="okf-search-result" style="--okf-type-accent:{_esc(color)}">'
            f'<h3><a href="{_esc(url)}" class="okf-internal">{_esc(title)}</a></h3>'
            # iter1 P3-13: concept-id moved OUT of the <h3> so the heading
            # outline announces only the title (screen-reader heading-list
            # navigation no longer reads "title concept/id" as one string).
            f'<div class="okf-search-result__meta okf-muted">{type_chip} {_esc(cid)}</div>'
            f'<div class="okf-search-snippet">{_esc(str(desc)[:200])}</div>'
            f'</article>'
        )
    results_html = "\n".join(result_parts) or '<p class="okf-search-empty">No results.</p>'

    theme_attr = ""
    initial_theme = config.get("theme") or "light"
    if initial_theme in _THEMES:
        theme_attr = f' data-theme="{initial_theme}"'

    # P1-10: emit data-okf-enhance on the search page too so wiki.js
    # detects static mode and shows the static-search-note there as well.
    data_attrs = f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}"'

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    js_link = f'<script src="{static_prefix}/wiki.js" defer></script>'
    renderers_link = f'<script src="{static_prefix}/renderers.js" defer></script>'
    # Current spec §9: in static mode also load the client-side
    # searcher so the static __search.html page is functional (no /__search
    # backend available). The live `serve`/`spa` paths are unchanged — they
    # keep hitting /__search server-side. The static-search.js script is
    # emitted under __static/ by _emit_site (it iterates list_builtin_static).
    if mode == "static":
        js_link = (
            f'<script src="{static_prefix}/wiki.js" defer></script>\n'
            f'<script src="{static_prefix}/static-search.js" defer></script>'
        )
        search_data_inline = (
            '<template id="okf-search-data">'
            + _json_for_script(_search_corpus_json(bundle))
            + "</template>"
        )
    else:
        search_data_inline = ""

    # P2-61: shared topbar nav. Search page lives at the bundle root, so
    # the form posts back to itself and Graph/Index are root-relative.
    root_prefix_for_nav = "/" if mode in ("serve", "spa") else "./"
    graph_link = "/__graph" if mode in ("serve", "spa") else "__graph.html"
    nav_html = _nav_controls_html(
        root_prefix=root_prefix_for_nav,
        graph_link=graph_link,
        initial_theme=initial_theme,
        current_query=query,
        search_mode=search_mode,
        mode=mode,
    )

    # iter1 P3-13: proper pluralisation (no "result(s)" cop-out). The heading
    # also carries the query so the empty state reads naturally.
    count = len(results)
    if count == 0:
        heading = f'No results for &ldquo;{_esc(query)}&rdquo;'
    elif count == 1:
        heading = f'1 result for &ldquo;{_esc(query)}&rdquo;'
    else:
        heading = f'{count} results for &ldquo;{_esc(query)}&rdquo;'

    return (
        template
        .replace("__WORKSPACE_NAV__", navigation(bundle, mode=mode, root_prefix=root_prefix_for_nav))
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(f"Search: {query} — {name}"))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__STATIC_PREFIX__", static_prefix)
        .replace("__WIKI_CSS_LINK__", css_link + workspace_assets(static_prefix))
        .replace("__WIKI_JS_LINK__", js_link)
        .replace("__RENDERERS_JS_LINK__", renderers_link)
        .replace("__SEARCH_DATA_INLINE__", search_data_inline)
        .replace("__NAV_HTML__", nav_html)
        .replace("__QUERY_ESCAPED__", _esc_attr_qs(query))
        .replace("__QUERY_HTML__", _esc(query))
        .replace("__RESULTS_HTML__", results_html)
        .replace("__RESULTS_HEADING__", heading)
        .replace("__BACK_LINK__", back_link)
    )


def _esc_attr_qs(s: str) -> str:
    """Escape a query for safe interpolation into an HTML attribute value."""
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;"))


def _render_graph_page(
    bundle: Bundle,
    *,
    mode: str,
    name: str,
    config: dict[str, Any],
    graph_data: dict[str, Any] | None = None,
) -> str:
    template = load_template("graph_page.html", bundle)
    static_prefix = "/__static" if mode in ("serve", "spa") else "__static"
    data_url = "/__data/graph.json" if mode in ("serve", "spa") else ""
    graph_data_inline = ""
    if mode == "static":
        payload = graph_data if graph_data is not None else build_graph_data(bundle, name=name)
        graph_data_inline = (
            '<template id="okf-graph-data">'
            + _json_for_script(payload)
            + "</template>"
        )
    back_link = "/" if mode in ("serve", "spa") else "index.html"
    initial_theme = config.get("theme") or "light"
    initial_layout = config.get("default_layout") or "cose"
    theme_attr = f' data-theme="{initial_theme}"' if initial_theme in _THEMES else ""

    cdn_scripts = _CDN_SCRIPTS if config.get("cdn", True) else (
        "<!-- CDN scripts omitted (config.cdn=false). -->"
    )

    # P1-3: emit data-okf-mode on the graph page body so graph.js can
    # detect static mode and append .html to the Open-page href (the
    # concept pages are emitted as <id>.html; the extensionless URL 404s
    # in static builds). Mirrors the __DATA_ATTRS__ wiring on the
    # concept/index/search pages.
    data_attrs = f'data-okf-mode="{mode}" data-okf-enhance="{("1" if mode != "static" else "0")}"'

    css_link = f'<link rel="stylesheet" href="{static_prefix}/wiki.css">'
    graph_css_link = f'<link rel="stylesheet" href="{static_prefix}/graph.css">'
    # The graph detail panel renders the same
    # server-produced concept HTML as the wiki pages, so it needs the SAME
    # progressive renderers (mermaid/hljs/KaTeX). graph.js dispatches
    # okf-loom:bodyPatched after each showDetail() so renderers.js re-scans.
    graph_js = (
        f'<script src="{static_prefix}/graph.js" defer></script>\n'
        f'<script src="{static_prefix}/renderers.js" defer></script>'
    )

    return (
        template
        .replace("__WORKSPACE_NAV__", navigation(bundle, mode=mode, root_prefix=("./" if mode == "static" else "/")))
        .replace("__LANG__", "en")
        .replace("__THEME_ATTR__", theme_attr)
        .replace("__DATA_ATTRS__", data_attrs)
        .replace("__HEAD_TITLE__", _esc(f"Graph — {name}"))
        .replace("__BUNDLE_NAME__", _esc(name))
        .replace("__STATIC_PREFIX__", static_prefix)
        .replace("__WIKI_CSS_LINK__", css_link + workspace_assets(static_prefix))
        .replace("__GRAPH_CSS_LINK__", graph_css_link)
        .replace("__GRAPH_JS_SRC__", graph_js)
        .replace("__CDN_SCRIPTS__", cdn_scripts)
        .replace("__INITIAL_THEME__", initial_theme)
        .replace("__INITIAL_THEME_BUTTON__", _theme_button_html(initial_theme))
        .replace("__INITIAL_LAYOUT__", initial_layout)
        .replace("__GRAPH_DATA_URL__", data_url)
        .replace("__GRAPH_DATA_INLINE__", graph_data_inline)
        .replace("__BACK_LINK__", back_link)
    )


# ---------------------------------------------------------------------------
# Atomic write helpers — migrated to io_utils (shared, symlink-safe,
# mkstemp-based). The former local copies are deleted; io_utils.atomic_
# write_text/_bytes/atomic_replace_tree are the canonical implementations.
# ---------------------------------------------------------------------------

from .io_utils import atomic_write_text as _atomic_write_text
from .io_utils import atomic_write_bytes as _atomic_write_bytes
