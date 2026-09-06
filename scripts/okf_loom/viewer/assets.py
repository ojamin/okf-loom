"""Asset loaders for the OKF viewer.

Resolves templates, static assets, viewer config, and type palettes with
bundle-level override support.

Override precedence (highest first):
    1. ``<bundle_root>/.okf-loom/viewer/templates/<name>.html``  (template)
       ``<bundle_root>/.okf-loom/viewer/static/<name>``           (static asset)
       ``<bundle_root>/.okf-loom/viewer/palette.json``            (type palette)
       ``<bundle_root>/.okf-loom/viewer/config.json``             (viewer config)
    2. Built-in viewer assets stored inside ``scripts/okf_loom/viewer/``.

Templates use ``str.replace``-style placeholders (NOT ``string.Template``),
because concept bodies frequently contain literal ``$`` characters that
would clash with ``$id`` substitution. Placeholder tokens are uppercase,
wrapped in double underscores, e.g. ``__TITLE__``, ``__BODY__``.

Security: every override mechanism (templates, static assets, type palette,
plugins) is gated on the **effective active-code gate** (current spec §14):

    effective_allow = bundle_cfg.viewer.allow_active_code AND operator_consent

The bundle's own ``allow_active_code: true`` is necessary but NOT
sufficient — the operator must ALSO grant consent via the
``OKF_LOOM_ALLOW_ACTIVE_CODE`` environment variable (values ``"1"`` / ``"true"`` /
``"yes"``) or the ``--allow-active-code`` CLI flag (which calls
:func:`set_operator_consent`). This prevents a bundle from turning on
arbitrary viewer-side code execution (template HTML, static JS, plugin
Python) on its own; a bundle shipping ``allow_active_code: true`` with a
malicious override still produces the built-in (safe) viewer unless the
operator explicitly opts in.

See ``viewer/OVERRIDES.md`` for the full override reference.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import os
import re
import zlib
from pathlib import Path
from typing import Any

from ..model import Bundle

_THIS_DIR = Path(__file__).resolve().parent
_BUILTIN_TEMPLATES = _THIS_DIR / "templates"
_BUILTIN_STATIC = _THIS_DIR / "static"
_OKF_VIEWER_SUBDIR = Path(".okf-loom") / "viewer"

# ---------------------------------------------------------------------------
# Bundle-local media the viewer displays (image-rich bundles): the live
# server serves exactly these extensions from the bundle tree, and the
# static/spa build copies exactly these into the site output. Nothing else
# ever routes to the media path — config/secrets (.yaml, .token,
# extensionless files) are structurally unservable regardless of the §5
# visibility checks. Keep in sync with ``server._EXT_CONTENT_TYPES`` (a
# type missing there ships as application/octet-stream under nosniff —
# download, never render).
# ---------------------------------------------------------------------------
BUNDLE_MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico",
    ".svg", ".mp4", ".webm", ".pdf",
})

# ---------------------------------------------------------------------------
# Operator consent (P1-40): the bundle's allow_active_code is necessary but
# not sufficient — the operator must also opt in via env or CLI flag.
# ---------------------------------------------------------------------------
#
# Operator consent is a process-wide constant (env var / CLI flag), so it is
# read once and cached. The bundle-level allow_active_code is re-read per
# bundle root (and cached per root — see _OVERRIDES_CACHE below).
#
#: Environment variable name. Truthy tokens: "1", "true", "yes" (case-insensitive).
OPERATOR_CONSENT_ENV: str = "OKF_LOOM_ALLOW_ACTIVE_CODE"
_TRUTHY_OPERATOR_TOKENS: frozenset[str] = frozenset({"1", "true", "yes"})

# Module-level override (set by the CLI ``--allow-active-code`` flag, which is
# outside this file's ownership — see cli.py REPORT). ``None`` means "not set
# via CLI; consult the env var". A CLI ``--allow-active-code`` MUST win over
# the env var either way (operator's most-recent explicit choice), so the
# setter stores the explicit bool.
_operator_consent_override: bool | None = None


_scoped_consent: ContextVar[bool | None] = ContextVar("okf_operator_consent", default=None)


@contextmanager
def operator_scope(granted: bool | None):
    """Isolate an embedding host's active-code choice from other servers."""
    token = _scoped_consent.set(granted)
    try:
        yield
    finally:
        _scoped_consent.reset(token)


def isolated_server_factory(factory):
    @wraps(factory)
    def wrapped(*args, **kwargs):
        granted = kwargs.get("allow_active_code")
        if granted is None:
            granted = operator_consent()
        with operator_scope(bool(granted)):
            server = factory(*args, **kwargs)
        server.operator_consent = bool(granted)
        return server
    return wrapped


def set_operator_consent(granted: bool) -> None:
    """Record an explicit operator-consent decision (CLI ``--allow-active-code``).

    Mirrors the env-var path but wins over it (the operator's most-recent
    explicit choice on the command line is authoritative). Resets the
    per-bundle override cache so the new decision takes effect immediately.
    """
    global _operator_consent_override
    _operator_consent_override = bool(granted)
    clear_overrides_cache()


def operator_consent() -> bool:
    """True iff the operator has consented to active code for this process.

    Precedence: explicit CLI override (:func:`set_operator_consent`) wins;
    otherwise the ``OKF_LOOM_ALLOW_ACTIVE_CODE`` env var is consulted (truthy
    tokens: ``"1"`` / ``"true"`` / ``"yes"``; anything else ⇒ no consent).

    This is fail-closed: an unset env var, an empty string, ``"0"``,
    ``"false"``, or an unrecognised token all yield ``False``.
    """
    scoped = _scoped_consent.get()
    if scoped is not None:
        return scoped
    if _operator_consent_override is not None:
        return _operator_consent_override
    raw = os.environ.get(OPERATOR_CONSENT_ENV, "")
    return raw.strip().lower() in _TRUTHY_OPERATOR_TOKENS


def bundle_cfg_allow_active_code(bundle_root: str | Path | Bundle) -> bool:
    """Read the bundle's own ``viewer.allow_active_code`` from okf-loom.config.yaml.

    Fail-closed: any config-read error ⇒ ``False`` (no active code).
    """
    try:
        from ..config import OkfConfig

        if isinstance(bundle_root, Bundle):
            root = bundle_root.root
        else:
            root = Path(bundle_root)
        return OkfConfig.load(root).viewer.allow_active_code
    except Exception:
        return False  # fail-closed


def effective_allow_active_code(bundle_root: str | Path | Bundle) -> bool:
    """The effective active-code gate (current spec §14).

    ``effective = bundle_cfg.allow_active_code AND operator_consent()``.

    A bundle cannot enable active code on its own; the operator must opt in
    via ``OKF_LOOM_ALLOW_ACTIVE_CODE`` or ``--allow-active-code``. This is the
    load-bearing security property: template/static/palette overrides and
    plugin discovery all consult this helper.
    """
    if not operator_consent():
        return False
    return bundle_cfg_allow_active_code(bundle_root)


# Per-bundle-root cache of the resolved effective gate (P2-45). Without this,
# every template / static / palette load during a render re-reads
# ``okf-loom.config.yaml`` from disk — O(N) reads for a constant per-bundle
# decision. The cache is keyed by the resolved bundle-root path string.
#
# Invalidation: operator consent is a process constant; bundle config can
# change. ``set_operator_consent`` clears the whole cache. The bundle
# watcher's reload path (server.py) calls ``clear_overrides_cache(root)``
# for the reloaded root so a config edit + .md touch is picked up on the
# next request. A pure config edit (no .md touch) requires a server
# restart, matching the existing watcher contract documented in run_server.
_OVERRIDES_CACHE: dict[tuple[str, bool], bool] = {}


def clear_overrides_cache(bundle_root: str | Path | None = None) -> None:
    """Drop the cached effective-gate decision.

    With no argument, clears every cached root (used when operator consent
    changes process-wide). With a root, clears only that root's entry.
    """
    if bundle_root is None:
        _OVERRIDES_CACHE.clear()
        return
    key = str(Path(bundle_root).resolve())
    _OVERRIDES_CACHE.pop((key, False), None)
    _OVERRIDES_CACHE.pop((key, True), None)


# ---------------------------------------------------------------------------
# Type palette
# ---------------------------------------------------------------------------

def stable_hue(s: str) -> int:
    """Deterministic hue (0..359) for a string.

    Uses ``zlib.crc32`` rather than the process-randomized ``hash`` so colours
    are stable across runs and machines.

    iter3 CRI3-004: this per-name hash is still the FALLBACK hue source
    for a type that is not in the bundle's sorted type list (e.g. a
    concept appearing in graph.json before the palette was computed, or
    a type added live by a CLI mutator that has not re-resolved yet — see
    render.py:404). For the COMMON case (palette pre-computed for the
    bundle's known types) prefer :func:`auto_palette`, which uses a
    golden-angle hue spread that maximally separates adjacent types.
    """
    return zlib.crc32(s.encode("utf-8")) % 360


# iter3 CRI3-004: golden-angle hue step (137.508°). For any set of N hues,
# multiplying by this step and taking mod 360 produces a sequence whose
# adjacent values are maximally separated on the hue wheel — the standard
# technique for generating N visually-distinct hues without collisions.
# Choosing it over a hash-based spread fixes the iter-2 residual where two
# types whose names hashed within ~20° of each other (Dataset/Playbook,
# Reference/Table) read as the same colour on the graph canvas.
_GOLDEN_ANGLE_HUE_STEP = 137.508


# ---------------------------------------------------------------------------
# Type icons (UI overhaul phase 1)
# ---------------------------------------------------------------------------
# A curated icon per COMMON knowledge-base type name, with a generic
# document fallback for everything else. Unlike colours (which stay on the
# neutral golden-angle auto-palette — see auto_palette's no-domain-bias
# note), icons key on generic type-name *shapes* (dataset→cylinder,
# table→grid, playbook→open book) and always degrade to the fallback, so
# no type is second-class — just less pictorial.
#
# Each value is the inner markup of a 24×24 stroke-based SVG (Lucide-style
# geometry, stroke=currentColor so icons inherit text/type colour).
_TYPE_ICON_PATHS: dict[str, str] = {
    "database": (
        '<ellipse cx="12" cy="5" rx="8" ry="3"/>'
        '<path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/>'
        '<path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/>'
    ),
    "table": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<path d="M3 9h18M3 15h18M12 3v18"/>'
    ),
    "server": (
        '<rect x="2" y="3" width="20" height="7" rx="2"/>'
        '<rect x="2" y="14" width="20" height="7" rx="2"/>'
        '<path d="M6 6.5h.01M6 17.5h.01"/>'
    ),
    "book-open": (
        '<path d="M2 4h6a4 4 0 0 1 4 4v12a3 3 0 0 0-3-3H2z"/>'
        '<path d="M22 4h-6a4 4 0 0 0-4 4v12a3 3 0 0 1 3-3h7z"/>'
    ),
    "bookmark": (
        '<path d="M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>'
    ),
    "trending": (
        '<path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/>'
    ),
    "branch": (
        '<circle cx="5" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/>'
        '<circle cx="19" cy="12" r="2.5"/>'
        '<path d="M5 7.5v9M7.2 6.2c5 1 9.3 3 9.3 5.8"/>'
    ),
    "book": (
        '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V2H6.5A2.5 2.5 0 0 0 4 4.5z"/>'
        '<path d="M9 7h6"/>'
    ),
    "shield": (
        '<path d="M12 22s8-3 8-10V5l-8-3-8 3v7c0 7 8 10 8 10z"/>'
    ),
    "zap": (
        '<path d="M13 2L3 14h7l-1 8 11-13h-8l1-7z"/>'
    ),
    "box": (
        '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v9"/>'
    ),
    "people": (
        '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 21a6.5 6.5 0 0 1 13 0"/>'
        '<path d="M16 3.5a3.5 3.5 0 0 1 0 7M21.5 21a6.5 6.5 0 0 0-4.5-6.2"/>'
    ),
    "file": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
        '<path d="M14 2v6h6M9 13h6M9 17h6"/>'
    ),
}

# Normalized type-name → icon key. Lookup lowercases and strips a trailing
# "s" so Dataset/datasets/DATASET all hit "dataset".
_TYPE_ICON_SYNONYMS: dict[str, str] = {
    "dataset": "database", "data": "database", "warehouse": "database",
    "table": "table", "view": "table", "sheet": "table",
    "service": "server", "api": "server", "system": "server", "app": "server",
    "playbook": "book-open", "runbook": "book-open", "guide": "book-open",
    "procedure": "book-open", "howto": "book-open", "tutorial": "book-open",
    "reference": "bookmark", "spec": "bookmark", "standard": "bookmark",
    "doc": "bookmark", "document": "bookmark",
    "metric": "trending", "kpi": "trending", "measure": "trending",
    "decision": "branch", "adr": "branch", "rfc": "branch",
    "glossary": "book", "term": "book", "definition": "book",
    "policy": "shield", "rule": "shield", "security": "shield",
    "event": "zap", "topic": "zap", "stream": "zap",
    "model": "box", "schema": "box", "entity": "box",
    "person": "people", "people": "people", "team": "people",
    "owner": "people", "user": "people",
}


def type_icon_key(type_name: str | None) -> str:
    """Resolve a concept type name to an icon key (fallback: ``file``)."""
    t = (type_name or "").strip().lower()
    if t.endswith("s") and t[:-1] in _TYPE_ICON_SYNONYMS:
        t = t[:-1]
    return _TYPE_ICON_SYNONYMS.get(t, "file")


def type_icon_paths() -> dict[str, str]:
    """The icon-key → SVG-inner-markup map (for client-side node glyphs)."""
    return dict(_TYPE_ICON_PATHS)


def type_icon_svg(type_name: str | None, *, size: int = 16,
                  cls: str = "okf-type-icon") -> str:
    """Inline SVG icon for a concept type.

    stroke=currentColor: the icon takes the colour of the surrounding
    text, so callers colour it via CSS (e.g. the type accent). Only OUR
    static path strings are interpolated — the (untrusted) type name never
    reaches the markup.
    """
    paths = _TYPE_ICON_PATHS[type_icon_key(type_name)]
    return (
        f'<svg class="{cls}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f'{paths}</svg>'
    )


def hsl_color(hue: int, *, sat: int = 62, light: int = 48) -> str:
    """Format an HSL colour string."""
    return f"hsl({hue % 360}, {sat}%, {light}%)"


def auto_palette(types: list[str]) -> dict[str, str]:
    """Generate a distinct colour for each type via golden-angle hue spread.

    iter3 CRI3-004: was a per-name crc32 hash. For the common 4-12 type
    catalog the hash approach produced clustered hues (Dataset/Playbook +
    Reference/Table pairs were near-indistinguishable on the graph canvas).
    Switching to a golden-angle spread over the SORTED type list
    guarantees maximally distinct adjacent hues for any N, while staying
    deterministic per bundle (the sorted type list is stable across runs).

    The palette deliberately avoids hardcoding any domain-specific types
    (no BigQuery / Stack Overflow bias) — every type gets a colour derived
    purely from its position in the sorted list.

    A bundle ``.okf-loom/viewer/palette.json`` override still wins per-type
    (resolve_palette applies overrides after this); the golden-angle
    spread only sets the auto-generated defaults.
    """
    out: dict[str, str] = {}
    # Sort first so the palette is stable across runs and machines (a
    # different insertion order would otherwise reshuffle hues). sorted()
    # also strips empties and dedupes implicitly when combined with the
    # enumerate-and-assign loop (a duplicate type gets the same hue).
    sorted_unique = sorted({t for t in types if t})
    for i, t in enumerate(sorted_unique):
        hue = int((i * _GOLDEN_ANGLE_HUE_STEP) % 360)
        out[t] = hsl_color(hue)
    return out


def load_palette_override(bundle: Bundle) -> dict[str, str]:
    """Read ``.okf-loom/viewer/palette.json`` if present.

    Schema: ``{"<type>": "<css-color>"}``. Invalid JSON, wrong shape, or
    values that fail the CSS-colour allowlist → that entry (or the whole
    file) is dropped.

    Every value is run through :func:`_sanitize_css_color` (P1-41) before
    being returned. Palette values are interpolated into ``style="...";``
    attributes in the rendered HTML; a naive HTML-escape alone does NOT
    stop CSS injection (``red;position:fixed;top:0`` has no HTML-unsafe
    characters), so values are validated against a strict CSS-colour
    allowlist. Anything that does not match is dropped (fail-closed).
    """
    path = bundle.root / _OKF_VIEWER_SUBDIR / "palette.json"
    if not path.is_file():
        return {}
    # P1-4 (security): refuse to follow a palette.json that escapes the bundle
    # root via symlink. Fail closed to the auto palette (return {}).
    from ..paths import path_within_bundle
    if not path_within_bundle(path, bundle.root):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        if not isinstance(v, str):
            # Non-string values (numbers, bools, lists) are dropped — they
            # cannot be a safe CSS colour and would interpolate verbatim.
            continue
        sanitized = _sanitize_css_color(v)
        if sanitized:
            out[k] = sanitized
        # else: value rejected by the allowlist; drop the key fail-closed.
    return out


# ---------------------------------------------------------------------------
# CSS-colour allowlist (P1-41). Palette values are interpolated into inline
# ``style="background:<color>"`` attributes; HTML-escaping alone does not
# stop CSS-meta-character injection (``;``, ``:``, ``url(...)``, etc.), so
# values MUST match one of these shapes to land in the rendered HTML.
# ---------------------------------------------------------------------------

# Named CSS colours (CSS3 subset; covers the common cross-browser set).
_CSS_NAMED_COLORS: frozenset[str] = frozenset({
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige",
    "bisque", "black", "blanchedalmond", "blue", "blueviolet", "brown",
    "burlywood", "cadetblue", "chartreuse", "chocolate", "coral",
    "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue", "darkcyan",
    "darkgoldenrod", "darkgray", "darkgreen", "darkgrey", "darkkhaki",
    "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred",
    "darksalmon", "darkseagreen", "darkslateblue", "darkslategray",
    "darkslategrey", "darkturquoise", "darkviolet", "deeppink",
    "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick",
    "floralwhite", "forestgreen", "fuchsia", "gainsboro", "ghostwhite",
    "gold", "goldenrod", "gray", "green", "greenyellow", "grey", "honeydew",
    "hotpink", "indianred", "indigo", "ivory", "khaki", "lavender",
    "lavenderblush", "lawngreen", "lemonchiffon", "lightblue", "lightcoral",
    "lightcyan", "lightgoldenrodyellow", "lightgray", "lightgreen",
    "lightgrey", "lightpink", "lightsalmon", "lightseagreen",
    "lightskyblue", "lightslategray", "lightslategrey", "lightsteelblue",
    "lightyellow", "lime", "limegreen", "linen", "magenta", "maroon",
    "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple",
    "mediumseagreen", "mediumslateblue", "mediumspringgreen",
    "mediumturquoise", "mediumvioletred", "midnightblue", "mintcream",
    "mistyrose", "moccasin", "navajowhite", "navy", "oldlace", "olive",
    "olivedrab", "orange", "orangered", "orchid", "palegoldenrod",
    "palegreen", "paleturquoise", "palevioletred", "papayawhip",
    "peachpuff", "peru", "pink", "plum", "powderblue", "purple", "rebeccapurple",
    "red", "rosybrown", "royalblue", "saddlebrown", "salmon",
    "sandybrown", "seagreen", "seashell", "sienna", "silver", "skyblue",
    "slateblue", "slategray", "slategrey", "snow", "springgreen",
    "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet",
    "wheat", "white", "whitesmoke", "yellow", "yellowgreen",
    "transparent", "currentcolor",
})

# Strict allowlist of CSS-colour shapes. Anchored (full-match) so a value
# like ``red;position:fixed`` is rejected (the ``;`` breaks the match).
#   * #rgb / #rgba / #rrggbb / #rrggbbaa hex
#   * rgb() / rgba() with numbers and optional commas/spaces/alpha
#   * hsl() / hsla() likewise
#   * a bare CSS named colour
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_RGB_FUNC_RE = re.compile(
    r"^rgba?\(\s*[\d.]+\s*%?\s*(,\s*[\d.]+\s*%?\s*){2,3}\)$"
)
# hsl/hsla: hue (number + optional unit) + saturation% + lightness% + an
# optional alpha (the alpha may be a bare number, unlike sat/light which
# CSS requires as percentages). Anchored full-match.
_HSL_FUNC_RE = re.compile(
    r"^hsla?\(\s*-?\d+(?:\.\d+)?\s*(?:deg|rad|turn|grad)?\s*"
    r"(?:,\s*\d+(?:\.\d+)?%\s*){2}"   # saturation + lightness (both %)
    r"(?:,\s*[\d.]+\s*%?\s*)?"        # optional alpha (number or %)
    r"\)$"
)


def _sanitize_css_color(value: str) -> str:
    """Return ``value`` iff it matches the strict CSS-colour allowlist.

    Returns ``""`` (empty) for anything that does not match — so the caller
    can drop the entry fail-closed. The check is anchored full-match;
    CSS-meta-character payloads (``;``, ``url(...)``, ``expression()``) and
    HTML/JS payloads are all rejected because they cannot satisfy any of the
    allowed shapes.

    This is a denylist-by-shape allowlist: we accept ONLY the known-safe
    shapes, not "anything without a semicolon".
    """
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""
    low = v.lower()
    if low in _CSS_NAMED_COLORS:
        return v
    if _HEX_COLOR_RE.match(v):
        return v
    if _RGB_FUNC_RE.match(v):
        return v
    if _HSL_FUNC_RE.match(v):
        return v
    return ""


def resolve_palette(bundle: Bundle) -> dict[str, str]:
    """Auto palette merged with bundle overrides (override wins).

    Bundle palette overrides are gated on the effective active-code gate
    (P1-41 defense in depth): even though palette values are CSS-validated,
    an untrusted bundle should not be able to recolour the UI at all unless
    the operator has opted into active code. When the gate is closed, only
    the auto-generated palette is returned.
    """
    types = sorted(t for t in bundle.types() if t)
    pal = auto_palette(types)
    if _overrides_allowed(bundle):
        pal.update(load_palette_override(bundle))
    return pal


# ---------------------------------------------------------------------------
# Viewer config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "name": None,
    "default_layout": "cose",
    "theme": "light",
    "cdn": True,
}


_ALLOWED_LAYOUTS = frozenset({"cose", "concentric", "breadthfirst", "circle", "grid"})
_ALLOWED_THEMES = frozenset({"light", "dark", "pastel", "sepia", "midnight"})


def load_config(bundle: Bundle) -> dict[str, Any]:
    """Read ``.okf-loom/viewer/config.json`` and merge over defaults.

    Supported keys:
        name (str|None): display name for the bundle in the viewer UI.
            Falls back to ``bundle.name``.
        default_layout (str): one of cose, concentric, breadthfirst, circle,
            grid. Used as the initial Cytoscape layout in the single-file and
            full-page graph views.
        theme (str): one of "light", "dark", "pastel", "sepia", "midnight" —
            initial theme for the single-file viewer. Page-served views
            honour ``localStorage['okf-theme']`` if set; otherwise this
            value.
        cdn (bool): if False, the single-file / graph templates omit the
            CDN ``<script>`` tag for Cytoscape.js. The graph view will then
            degrade (no rendering) but the page still loads — useful for
            fully offline packaging where the user inlines their own copy.
            Markdown bodies are always rendered server-side; no client-side
            markdown parser is used.

    All values are VALIDATED against their allowed sets before use, so a
    malicious config.json cannot inject arbitrary values into template
    contexts (defence against supply-chain XSS via config interpolation).
    """
    cfg = dict(_DEFAULT_CONFIG)
    path = bundle.root / _OKF_VIEWER_SUBDIR / "config.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if k not in _DEFAULT_CONFIG:
                        continue
                    # Validate values against allowed sets (fail-closed).
                    if k == "default_layout" and v not in _ALLOWED_LAYOUTS:
                        continue  # keep default
                    if k == "theme" and v not in _ALLOWED_THEMES:
                        continue  # keep default
                    if k == "cdn" and not isinstance(v, bool):
                        continue  # keep default
                    if k == "name" and not isinstance(v, (str, type(None))):
                        continue
                    cfg[k] = v
        except (OSError, ValueError):
            pass
    return cfg


# ---------------------------------------------------------------------------
# Template / static resolution (with bundle overrides)
# ---------------------------------------------------------------------------

def _bundle_override_path(bundle: Bundle, sub: str, name: str) -> Path:
    return bundle.root / _OKF_VIEWER_SUBDIR / sub / name


def _overrides_allowed(bundle: Bundle) -> bool:
    """Check if bundle template/static/palette overrides are allowed.

    Consults the **effective active-code gate** (P1-40):
    ``bundle_cfg.allow_active_code AND operator_consent()``. A bundle cannot
    enable its own overrides; the operator must opt in via
    ``OKF_LOOM_ALLOW_ACTIVE_CODE`` or ``--allow-active-code``.

    The per-root decision is cached (P2-45) so repeated template/static
    loads during a render do not re-read ``okf-loom.config.yaml`` from disk. Use
    :func:`clear_overrides_cache` (or :func:`set_operator_consent`) to
    invalidate.
    """
    try:
        key = str(bundle.root.resolve())
    except Exception:
        # If the root cannot be resolved (shouldn't happen for a loaded
        # bundle), fall back to the raw path string but still compute.
        key = str(bundle.root)
    key = (key, operator_consent())
    cached = _OVERRIDES_CACHE.get(key)
    if cached is not None:
        return cached
    allowed = effective_allow_active_code(bundle)
    _OVERRIDES_CACHE[key] = allowed
    return allowed


def load_template(name: str, bundle: Bundle | None = None) -> str:
    """Return the named template, honouring bundle overrides.

    Bundle overrides (``.okf-loom/viewer/templates/<name>``) are only loaded when
    ``allow_active_code`` is true — they can carry arbitrary HTML/scripts.
    """
    if bundle is not None and _overrides_allowed(bundle):
        override = _bundle_override_path(bundle, "templates", name)
        # P1-4 (security): refuse to follow a bundle template override whose
        # resolved path escapes the bundle root via symlink (e.g. an override
        # symlinked to ~/.aws/credentials). Fail CLOSED to the builtin
        # template — the override is untrusted bundle content; without this
        # check the secret would be served over HTTP (serve) and written into
        # built output (build/render).
        from ..paths import path_within_bundle
        if override.is_file() and path_within_bundle(override, bundle.root):
            return _strip_template_doc_comment(override.read_text(encoding="utf-8"))
    builtin = _BUILTIN_TEMPLATES / name
    if not builtin.is_file():
        raise FileNotFoundError(f"Template not found: {name}")
    return _strip_template_doc_comment(builtin.read_text(encoding="utf-8"))


# iter3 P2-4: templates carry a leading ``<!-- ... -->`` documentation block
# (placeholder contract for maintainers). Without stripping, this ~10 KB
# comment ships in EVERY generated HTML page across static/spa/serve/single-
# file targets (~47% of page bytes). The comment sits between
# ``<!DOCTYPE html>`` and ``<html>`` — never meaningful content — so stripping
# it at load time is safe for both builtin and override templates.
_TEMPLATE_DOC_RE = re.compile(
    r"(<!DOCTYPE html>\s*)<!--.*?-->\s*(<html)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_template_doc_comment(html: str) -> str:
    """Remove the leading template documentation comment block.

    Only strips a ``<!-- ... -->`` block that appears between ``<!DOCTYPE
    html>`` and ``<html>`` — body comments (including the
    ``<!-- okf:generated:index begin/end -->`` markers) are never touched.
    """
    return _TEMPLATE_DOC_RE.sub(r"\1\2", html, count=1)


def load_static(name: str, bundle: Bundle | None = None) -> str:
    """Return the named static asset, honouring bundle overrides.

    Bundle overrides (``.okf-loom/viewer/static/<name>``) are only loaded when
    ``allow_active_code`` is true — they can carry arbitrary JS.
    """
    if bundle is not None and _overrides_allowed(bundle):
        override = _bundle_override_path(bundle, "static", name)
        # P1-4 (security): same symlink-escape containment as load_template.
        from ..paths import path_within_bundle
        if override.is_file() and path_within_bundle(override, bundle.root):
            return override.read_text(encoding="utf-8")
    builtin = _BUILTIN_STATIC / name
    if not builtin.is_file():
        raise FileNotFoundError(f"Static asset not found: {name}")
    return builtin.read_text(encoding="utf-8")


def list_builtin_static() -> list[str]:
    """Names of all built-in static assets (used for static-site emission)."""
    if not _BUILTIN_STATIC.is_dir():
        return []
    return sorted(p.name for p in _BUILTIN_STATIC.iterdir() if p.is_file())
