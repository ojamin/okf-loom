"""OKF bundle-root config (current spec §5).

Reads an optional ``okf-loom.config.yaml`` at the bundle root. **Absence ⇒ all
defaults** (config is purely additive; nothing breaks without it).

Path-bearing fields (``viewer.override_dir`` / ``extension_css`` /
``extension_js``) MUST be bundle-relative. Absolute paths and
``..``-escaping paths are rejected fail-closed — reusing the §4.5 guard
(``okf_loom.log._resolve_within_bundle``), which performs a
resolve-and-containment check on the bundle root.

Unknown keys are preserved verbatim in ``OkfConfig.raw`` for forward
compatibility (producers MAY add arbitrary frontmatter/config keys;
consumers MUST tolerate them).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _yaml_compat as yaml

from .exceptions import OKFError
# Spec §11.1: "reuse the same guard as §4.5". The §4.5 path-containment
# guard lives in okf_loom.log._resolve_within_bundle (resolve candidate
# against the root and confirm containment via relative_to). Importing the
# private symbol is intentional: the spec mandates reuse, not duplication.
from .log import _resolve_within_bundle
# P2-1: import the shared YAML alias-bomb defences from parse.py rather than
# duplicating the security control. This adds only parse.py's leaf deps
# (paths/exceptions); it does NOT pull in model (config already imports
# log, which imports model transitively). A single source of truth for the
# node-counting loader + materialized budget keeps both parse boundaries
# (frontmatter and okf-loom.config.yaml) in lock-step.
from .parse import (
    _NodeLimitedSafeLoader,
    _assert_materialized_under_limit,
)
from .paths import ConceptId, ConceptIdError, concept_id_from_str

# Canonical filename (current spec §5: root placement for
# discoverability). Flip this single constant to relocate the file.
CONFIG_FILENAME: str = "okf-loom.config.yaml"


class OkfConfigError(OKFError, ValueError):
    """Raised when ``okf-loom.config.yaml`` is unparseable or violates path rules.

    Subclasses ``ValueError`` so callers that already catch ``ValueError``
    (e.g. argparse-driven CLIs) pick it up automatically.
    """


# --- spec §11.1 default values ---------------------------------------------

_DEFAULT_VIEWER_TITLE: str | None = None
_DEFAULT_VIEWER_OVERRIDE_DIR: str = ".okf-loom/viewer"
_DEFAULT_VIEWER_EXTENSION_CSS: str = ".okf-loom/viewer/extension.css"
_DEFAULT_VIEWER_EXTENSION_JS: str = ".okf-loom/viewer/extension.js"
_DEFAULT_VIEWER_ALLOW_ACTIVE_CODE: bool = False

_DEFAULT_SEARCH_DEFAULT_MODE: str = "lexical"

# --- bundle scanning defaults (current spec §5 ``bundle:``) ------------------
_DEFAULT_BUNDLE_EXCLUDE: tuple[str, ...] = ()
_DEFAULT_BUNDLE_INCLUDE: tuple[str, ...] = ()
_DEFAULT_BUNDLE_RESPECT_GITIGNORE: bool = True

_DEFAULT_VALIDATE_DEFAULT_PROFILE: str = "spec"
_DEFAULT_VALIDATE_FAIL_ON_BROKEN_LINKS: bool = False

# --- studio defaults (current spec §5 and §10) -------------------------------
# All defaults make the studio fully functional with a plain `okf serve`: live
# updates, commenting/directing, agent enrichment — no unlock flags.
_DEFAULT_STUDIO_EDIT: bool = True
_DEFAULT_STUDIO_LIVE: bool = True
_DEFAULT_STUDIO_ENRICH_OFFER: bool = True
_DEFAULT_STUDIO_AUTO_ENRICH: bool = True
_DEFAULT_STUDIO_DEBOUNCE_MS: int = 600
_DEFAULT_STUDIO_SESSION_DIR: str = ".okf-loom/session"
_DEFAULT_STUDIO_LOG_EDITS: bool = True
_DEFAULT_STUDIO_MAX_SSE_CLIENTS: int = 32
_DEFAULT_STUDIO_THEME: str = "auto"
_DEFAULT_STUDIO_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
# §10.5 events.jsonl rotation caps. 8 MiB active + 7 rotated files kept.
_DEFAULT_STUDIO_EVENTS_MAX_BYTES: int = 8 * 1024 * 1024
_DEFAULT_STUDIO_EVENTS_KEEP: int = 7

# --- validated enum domains (fail-closed on unknown values) -----------------
#
# These sets are the authoritative allow-lists for the string-enum config
# fields. They mirror the runtime enums/providers in ``search.py`` but are
# hardcoded here to keep ``config.py`` a lightweight leaf module (it would
# otherwise have to import ``search``, which pulls in ``model`` / ``parse`` /
# ``paths``). If the upstream enum/provider catalogue changes, update BOTH
# this block and the corresponding ``search.SearchMode`` members / provider
# names.
#
# ``default_mode`` mirrors ``okf_loom.search.SearchMode``:
#   {lexical, semantic, hybrid, tag, entity, relation}.
_ALLOWED_SEARCH_MODES: frozenset[str] = frozenset({
    "lexical", "semantic", "hybrid", "tag", "entity", "relation",
})
# NOTE: the provider backends (DenseBackend / VectorIndexBackend /
# SqliteFtsBackend) and their config knobs (``search.semantic_provider`` /
# ``search.lexical_provider`` / ``search.dense_model``) were removed —
# semantic search is now always zero-dep SemanticLite, lexical is always
# BM25. A dense backend is not shipped, while remaining a legitimate optional
# SearchBackend for broad paraphrase recall.
# ``default_mode`` is the only surviving ``search:`` key.
# ``validate.default_profile`` mirrors ``validate._PROFILE_REMAPS`` keys.
_ALLOWED_VALIDATE_PROFILES: frozenset[str] = frozenset({
    "spec", "producer", "loose",
})

# Known keys per section (used to split known vs unknown for .raw).
_KNOWN_BUNDLE_KEYS: frozenset[str] = frozenset({
    "exclude", "include", "respect_gitignore",
})
_KNOWN_VIEWER_KEYS: frozenset[str] = frozenset({
    "title", "override_dir", "extension_css", "extension_js",
    "allow_active_code",
})
_KNOWN_SEARCH_KEYS: frozenset[str] = frozenset({
    "default_mode",
})
_KNOWN_DISCOVER_KEYS: frozenset[str] = frozenset({
    "suppress_phrases", "suppress_pairs",
})
_KNOWN_VALIDATE_KEYS: frozenset[str] = frozenset({
    "default_profile", "fail_on_broken_links",
})
_KNOWN_STUDIO_KEYS: frozenset[str] = frozenset({
    "edit", "live", "enrich_offer", "auto_enrich", "constraints",
    "debounce_ms", "session_dir", "log_edits", "max_sse_clients",
    "allowed_hosts", "theme", "events_max_bytes", "events_keep",
})
_KNOWN_TOP_KEYS: frozenset[str] = frozenset({
    "bundle", "viewer", "search", "discover", "validate", "studio",
})

# Path-bearing viewer keys that must pass the §4.5 containment check.
_VIEWER_PATH_KEYS: tuple[str, ...] = (
    "override_dir", "extension_css", "extension_js",
)


# --- section dataclasses ----------------------------------------------------


def _coerce_patterns(name: str, value: Any) -> tuple[str, ...]:
    """Coerce a config pattern list (``bundle.exclude`` / ``bundle.include``).

    Accepts a list of non-empty strings or a single string (single-pattern
    convenience form, mirroring ``studio.allowed_hosts``). Fail-closed on
    anything else: a non-string entry (mapping, list, int…) is a config
    mistake, not a pattern — do not str()-coerce it into something that
    silently matches nothing (§11.1 philosophy).
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        patterns: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise OkfConfigError(
                    f"{name}: entries must be non-empty strings, got {item!r}"
                )
            patterns.append(item)
        return tuple(patterns)
    raise OkfConfigError(
        f"{name}: expected a list of patterns, got {type(value).__name__}"
    )


@dataclass(frozen=True)
class BundleConfig:
    """Markdown scanning excludes/includes (current spec §5 ``bundle:``).

    ``exclude`` holds gitignore-style patterns applied when discovering the
    bundle's ``*.md`` files (``okf_loom.ignore``); they sit above the
    built-in defaults and any ``.gitignore`` rules, so a negation here
    (``"!.docs/"``) can re-include a path those groups dropped (with git's
    limitation: not from under a pruned directory).

    ``include`` is the explicit add-back lever: positive patterns that beat
    every exclusion source — defaults, ``.gitignore``, ``exclude``, and the
    structural nested-repo prune — and CAN reach inside pruned directories
    (``"node_modules/my-pkg/docs/"``). Negations are rejected fail-closed
    here; removals belong in ``exclude``.

    Patterns are match rules, not filesystem paths, so the §4.5 containment
    guard does not apply (a leading ``/`` is anchor syntax and cannot escape
    the bundle: matching is always against root-relative paths).

    ``respect_gitignore`` honours ``.gitignore`` files (bundle root and
    nested) during the scan. Default True: serving a repo root skips
    ignored vendor/generated trees with zero configuration.
    """

    exclude: tuple[str, ...] = _DEFAULT_BUNDLE_EXCLUDE
    include: tuple[str, ...] = _DEFAULT_BUNDLE_INCLUDE
    respect_gitignore: bool = _DEFAULT_BUNDLE_RESPECT_GITIGNORE

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BundleConfig":
        data = data or {}
        include = _coerce_patterns("bundle.include", data.get("include"))
        for pattern in include:
            if pattern.lstrip().startswith("!"):
                raise OkfConfigError(
                    f"bundle.include: patterns are positive; negation is not "
                    f"allowed (move removals to bundle.exclude): {pattern!r}"
                )
        return cls(
            exclude=_coerce_patterns("bundle.exclude", data.get("exclude")),
            include=include,
            respect_gitignore=_coerce_bool(
                "bundle.respect_gitignore",
                data.get("respect_gitignore"),
                _DEFAULT_BUNDLE_RESPECT_GITIGNORE,
            ),
        )


@dataclass(frozen=True)
class ViewerConfig:
    """Viewer display + override settings (spec §11.1 ``viewer:``)."""

    title: str | None = _DEFAULT_VIEWER_TITLE
    override_dir: str | None = _DEFAULT_VIEWER_OVERRIDE_DIR
    extension_css: str | None = _DEFAULT_VIEWER_EXTENSION_CSS
    extension_js: str | None = _DEFAULT_VIEWER_EXTENSION_JS
    allow_active_code: bool = _DEFAULT_VIEWER_ALLOW_ACTIVE_CODE

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ViewerConfig":
        data = data or {}
        # P2-11 (iter-3): route title through _opt_str so an absent/null title
        # stays None (not the literal string "None" from str(None)). A
        # comment-only or title:null config must not produce <title>None</title>.
        title_val = data.get("title", _DEFAULT_VIEWER_TITLE)
        return cls(
            title=(str(title_val) if title_val is not None else None),
            override_dir=_opt_str(data.get("override_dir", _DEFAULT_VIEWER_OVERRIDE_DIR)),
            extension_css=_opt_str(data.get("extension_css", _DEFAULT_VIEWER_EXTENSION_CSS)),
            extension_js=_opt_str(data.get("extension_js", _DEFAULT_VIEWER_EXTENSION_JS)),
            allow_active_code=_coerce_bool(
                "viewer.allow_active_code",
                data.get("allow_active_code"),
                _DEFAULT_VIEWER_ALLOW_ACTIVE_CODE,
            ),
        )


@dataclass(frozen=True)
class SearchConfig:
    """Search mode default (spec §11.1 ``search:``).

    Only ``default_mode`` survives: it selects among the six search modes
    (lexical/semantic/hybrid/tag/entity/relation). The provider backends
    (dense / vector-index / sqlite-fts) and their knobs were removed;
    semantic is always SemanticLite, lexical is always BM25.
    """

    default_mode: str = _DEFAULT_SEARCH_DEFAULT_MODE

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SearchConfig":
        data = data or {}
        default_mode = str(data.get("default_mode", _DEFAULT_SEARCH_DEFAULT_MODE))
        # Fail-closed: reject typos so a misspelled mode does not
        # silently fall back to a different backend (§11.1).
        _validate_enum("search.default_mode", default_mode, _ALLOWED_SEARCH_MODES)
        return cls(
            default_mode=default_mode,
        )


@dataclass(frozen=True)
class DiscoverSuppressPair:
    """A configured source→target pair suppression for unlinked mentions."""

    source: ConceptId
    target: ConceptId


@dataclass(frozen=True)
class DiscoverConfig:
    """Discovery noise controls (``discover:``).

    These settings are editorial, bundle-specific suppressions. They do not
    affect validation, graph construction, search, or the OKF data model.
    """

    suppress_phrases: tuple[str, ...] = ()
    suppress_pairs: tuple[DiscoverSuppressPair, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DiscoverConfig":
        data = data or {}
        return cls(
            suppress_phrases=_coerce_phrase_list(
                "discover.suppress_phrases", data.get("suppress_phrases")
            ),
            suppress_pairs=_coerce_suppress_pairs(
                data.get("suppress_pairs")
            ),
        )


_ALLOWED_STUDIO_THEMES: frozenset[str] = frozenset(
    {"auto", "light", "dark", "pastel", "sepia", "midnight"}
)


@dataclass(frozen=True)
class StudioConfig:
    """Live collaborative studio settings (current spec §5 ``studio:``).

    Every default makes the studio fully functional with a plain ``okf
    serve``: live updates, commenting/directing, agent enrichment. The
    ``constraints`` mapping is preserved verbatim (default empty = a fully
    free agent, §1.1 / D8); a user who wants to restrain *their own* agent
    opts in here (§12.3).
    """

    edit: bool = _DEFAULT_STUDIO_EDIT
    live: bool = _DEFAULT_STUDIO_LIVE
    enrich_offer: bool = _DEFAULT_STUDIO_ENRICH_OFFER
    auto_enrich: bool = _DEFAULT_STUDIO_AUTO_ENRICH
    constraints: dict[str, Any] = field(default_factory=dict)
    debounce_ms: int = _DEFAULT_STUDIO_DEBOUNCE_MS
    session_dir: str = _DEFAULT_STUDIO_SESSION_DIR
    log_edits: bool = _DEFAULT_STUDIO_LOG_EDITS
    max_sse_clients: int = _DEFAULT_STUDIO_MAX_SSE_CLIENTS
    allowed_hosts: tuple[str, ...] = _DEFAULT_STUDIO_ALLOWED_HOSTS
    theme: str = _DEFAULT_STUDIO_THEME
    # §10.5 events.jsonl rotation (P1-3): 8 MiB active cap + 7 rotated files.
    events_max_bytes: int = _DEFAULT_STUDIO_EVENTS_MAX_BYTES
    events_keep: int = _DEFAULT_STUDIO_EVENTS_KEEP

    @property
    def session_path(self) -> str:
        """Bundle-relative session directory (normalized, no leading ./)."""
        d = self.session_dir.strip()
        if d.startswith("./"):
            d = d[2:]
        d = d.rstrip("/")
        return d or ".okf-loom/session"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StudioConfig":
        data = data or {}
        theme = str(data.get("theme", _DEFAULT_STUDIO_THEME))
        _validate_enum("studio.theme", theme, _ALLOWED_STUDIO_THEMES)
        raw_hosts = data.get("allowed_hosts", list(_DEFAULT_STUDIO_ALLOWED_HOSTS))
        if isinstance(raw_hosts, str):
            hosts: tuple[str, ...] = (raw_hosts,)
        elif isinstance(raw_hosts, (list, tuple)):
            hosts = tuple(str(h).strip() for h in raw_hosts if str(h).strip())
        else:
            hosts = _DEFAULT_STUDIO_ALLOWED_HOSTS
        if not hosts:
            hosts = _DEFAULT_STUDIO_ALLOWED_HOSTS
        constraints_raw = data.get("constraints")
        constraints = (
            dict(constraints_raw) if isinstance(constraints_raw, dict) else {}
        )
        session_dir = str(data.get("session_dir", _DEFAULT_STUDIO_SESSION_DIR))
        if Path(session_dir).is_absolute():
            raise OkfConfigError(
                f"studio.session_dir: absolute paths are not allowed: {session_dir!r}"
            )
        return cls(
            edit=_coerce_bool("studio.edit", data.get("edit"), _DEFAULT_STUDIO_EDIT),
            live=_coerce_bool("studio.live", data.get("live"), _DEFAULT_STUDIO_LIVE),
            enrich_offer=_coerce_bool(
                "studio.enrich_offer", data.get("enrich_offer"),
                _DEFAULT_STUDIO_ENRICH_OFFER,
            ),
            auto_enrich=_coerce_bool(
                "studio.auto_enrich", data.get("auto_enrich"),
                _DEFAULT_STUDIO_AUTO_ENRICH,
            ),
            constraints=constraints,
            debounce_ms=int(data.get("debounce_ms", _DEFAULT_STUDIO_DEBOUNCE_MS)),
            session_dir=session_dir,
            log_edits=_coerce_bool(
                "studio.log_edits", data.get("log_edits"), _DEFAULT_STUDIO_LOG_EDITS
            ),
            max_sse_clients=int(
                data.get("max_sse_clients", _DEFAULT_STUDIO_MAX_SSE_CLIENTS)
            ),
            allowed_hosts=hosts,
            theme=theme,
            events_max_bytes=int(
                data.get("events_max_bytes", _DEFAULT_STUDIO_EVENTS_MAX_BYTES)
            ),
            events_keep=int(
                data.get("events_keep", _DEFAULT_STUDIO_EVENTS_KEEP)
            ),
        )


@dataclass(frozen=True)
class ValidateConfig:
    """Validation profile defaults (spec §11.1 ``validate:``)."""

    default_profile: str = _DEFAULT_VALIDATE_DEFAULT_PROFILE
    fail_on_broken_links: bool = _DEFAULT_VALIDATE_FAIL_ON_BROKEN_LINKS

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ValidateConfig":
        data = data or {}
        default_profile = str(
            data.get("default_profile", _DEFAULT_VALIDATE_DEFAULT_PROFILE)
        )
        # Fail-closed: reject an unknown default_profile so a typo does not
        # silently behave as "spec" while the JSON report claims the typo
        # name (current spec §5).
        _validate_enum(
            "validate.default_profile", default_profile, _ALLOWED_VALIDATE_PROFILES
        )
        return cls(
            default_profile=default_profile,
            fail_on_broken_links=_coerce_bool(
                "validate.fail_on_broken_links",
                data.get("fail_on_broken_links"),
                _DEFAULT_VALIDATE_FAIL_ON_BROKEN_LINKS,
            ),
        )


@dataclass(frozen=True)
class OkfConfig:
    """Parsed bundle-root config (spec §11.1).

    ``raw`` preserves every unknown key okf-loom does not recognise, so
    a newer producer's config does not lose information when consumed by
    an older toolkit (forward-compat mirror of SPEC §2's frontmatter rule).
    """

    bundle: BundleConfig = field(default_factory=BundleConfig)
    viewer: ViewerConfig = field(default_factory=ViewerConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    discover: DiscoverConfig = field(default_factory=DiscoverConfig)
    validate: ValidateConfig = field(default_factory=ValidateConfig)
    studio: StudioConfig = field(default_factory=StudioConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, bundle_root: str | Path) -> "OkfConfig":
        """Load config from ``<bundle_root>/okf-loom.config.yaml``.

        Missing file ⇒ all defaults. Path-bearing fields are validated
        bundle-relative (fail-closed on absolute / ``..``-escaping values).

        Raises:
            OkfConfigError: if the file is unparseable, the top level is
                not a mapping, or a path field escapes the bundle root.
        """
        bundle_root = Path(bundle_root)
        path = bundle_root / CONFIG_FILENAME
        if not path.is_file():
            return cls()  # absence ⇒ all defaults (spec §11.1)

        try:
            with path.open("r", encoding="utf-8") as f:
                # P2-1: node-counting loader (control 1) — caps composed
                # nodes, catching non-alias depth/merge-key bombs.
                data = yaml.load(f, _NodeLimitedSafeLoader)
        except yaml.YAMLError as e:
            raise OkfConfigError(
                f"Failed to parse {CONFIG_FILENAME} at {path}: {e}"
            ) from e

        if data is None:
            # Empty / comment-only file ⇒ empty mapping ⇒ all defaults.
            data = {}
        if not isinstance(data, dict):
            raise OkfConfigError(
                f"{CONFIG_FILENAME} must be a YAML mapping at the top level, "
                f"got {type(data).__name__}"
            )

        # P2-1: reject alias bombs whose serialized form would exceed
        # MAX_NODES (control 2). PyYAML keeps the alias graph compact in
        # memory; this measures the cost a serializer would actually pay.
        try:
            _assert_materialized_under_limit(data)
        except yaml.YAMLError as e:
            raise OkfConfigError(
                f"{CONFIG_FILENAME} rejected (DoS guard): {e}"
            ) from e

        bundle_raw = data.get("bundle") or {}
        viewer_raw = data.get("viewer") or {}
        search_raw = data.get("search") or {}
        discover_raw = data.get("discover") or {}
        validate_raw = data.get("validate") or {}
        studio_raw = data.get("studio") or {}

        # Fail-closed: validate every present path-bearing field. We check
        # all sections' path keys up front so the user sees the first bad
        # one before any partial config is constructed.
        for key in _VIEWER_PATH_KEYS:
            value = viewer_raw.get(key)
            if value is not None:
                _validate_bundle_relative_path(
                    bundle_root, f"viewer.{key}", str(value)
                )
        # F5: studio.session_dir is also bundle-relative — a ``..``-escaping
        # value would write session state (events.jsonl, undo snapshots) OUT
        # of the bundle root. Route it through the same resolve-and-contain
        # guard the viewer path keys use (the absolute-only check in
        # ``StudioConfig.from_dict`` is kept as a defensive backstop for
        # direct construction, but this is the authoritative bundle-relative
        # gate because only ``OkfConfig.load`` knows ``bundle_root``).
        session_dir_raw = studio_raw.get("session_dir")
        if session_dir_raw is not None:
            _validate_bundle_relative_path(
                bundle_root, "studio.session_dir", str(session_dir_raw)
            )

        # Split known vs unknown: known keys feed the typed dataclasses;
        # unknown keys (top-level + section-internal) are preserved in raw.
        raw: dict[str, Any] = {}
        for top_key, top_val in data.items():
            if top_key not in _KNOWN_TOP_KEYS:
                raw[top_key] = top_val
        for section_name, section_data, known in (
            ("bundle", bundle_raw, _KNOWN_BUNDLE_KEYS),
            ("viewer", viewer_raw, _KNOWN_VIEWER_KEYS),
            ("search", search_raw, _KNOWN_SEARCH_KEYS),
            ("discover", discover_raw, _KNOWN_DISCOVER_KEYS),
            ("validate", validate_raw, _KNOWN_VALIDATE_KEYS),
            ("studio", studio_raw, _KNOWN_STUDIO_KEYS),
        ):
            for k, v in section_data.items():
                if k not in known:
                    raw.setdefault(section_name, {})[k] = v

        return cls(
            bundle=BundleConfig.from_dict(bundle_raw),
            viewer=ViewerConfig.from_dict(viewer_raw),
            search=SearchConfig.from_dict(search_raw),
            discover=DiscoverConfig.from_dict(discover_raw),
            validate=ValidateConfig.from_dict(validate_raw),
            studio=StudioConfig.from_dict(studio_raw),
            raw=raw,
        )


# --- path validation (§4.5 guard reuse) ------------------------------------


def _validate_bundle_relative_path(
    bundle_root: Path, field_name: str, rel: str
) -> None:
    """Reject paths that are absolute or escape the bundle root.

    Wraps ``log._resolve_within_bundle`` (the §4.5 guard) and adds a lexical
    pre-check so absolute paths produce a clearer message even when the root
    itself is not yet on disk. ``field_name`` is the FULL config label
    (e.g. ``"viewer.override_dir"`` / ``"studio.session_dir"``) used in the
    error messages, so the same guard serves every path-bearing config field.
    """
    rel_stripped = rel.strip()
    if not rel_stripped:
        raise OkfConfigError(
            f"{field_name}: empty path is not allowed"
        )
    if Path(rel_stripped).is_absolute():
        raise OkfConfigError(
            f"{field_name}: absolute paths are not allowed: {rel!r}"
        )
    try:
        _resolve_within_bundle(bundle_root, rel_stripped)
    except ValueError as e:
        raise OkfConfigError(
            f"{field_name}: path escapes bundle root: {rel!r}"
        ) from e


def _opt_str(value: Any) -> str | None:
    """Coerce a config string value: stripped str, or None if empty/null.

    ``null`` and missing both yield None (meaning "no override"); any
    non-empty string is preserved verbatim after stripping whitespace.
    """
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _coerce_phrase_list(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    raw = _coerce_patterns(name, value)
    phrases: list[str] = []
    for phrase in raw:
        normalized = " ".join(phrase.strip().lower().split())
        if normalized:
            phrases.append(normalized)
    return tuple(phrases)


def _coerce_suppress_pairs(value: Any) -> tuple[DiscoverSuppressPair, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise OkfConfigError(
            "discover.suppress_pairs: expected a list of {source, target} mappings"
        )
    pairs: list[DiscoverSuppressPair] = []
    for item in value:
        if not isinstance(item, dict):
            raise OkfConfigError(
                "discover.suppress_pairs: entries must be mappings"
            )
        source_raw = item.get("source")
        target_raw = item.get("target")
        if not isinstance(source_raw, str) or not isinstance(target_raw, str):
            raise OkfConfigError(
                "discover.suppress_pairs: each entry needs string source and target"
            )
        try:
            source = concept_id_from_str(source_raw)
            target = concept_id_from_str(target_raw)
        except (ConceptIdError, ValueError) as e:
            raise OkfConfigError(
                "discover.suppress_pairs: source and target must be concept ids "
                "or bundle-absolute concept links"
            ) from e
        pairs.append(DiscoverSuppressPair(source=source, target=target))
    return tuple(pairs)


# String tokens accepted as boolean values (case-insensitive) by _coerce_bool.
# ``bool("false")`` is True in Python, so a quoted ``"false"`` must NOT be
# allowed to enable a security-sensitive flag like ``allow_active_code``
# (§11.1 / §12 security boundary). This mapping is the fail-closed fix.
_BOOL_FALSE_TOKENS: frozenset[str] = frozenset({"", "false", "no", "off", "0"})
_BOOL_TRUE_TOKENS: frozenset[str] = frozenset({"true", "yes", "on", "1"})


def _coerce_bool(name: str, value: Any, default: bool) -> bool:
    """Coerce a config value to bool, fail-closed on ambiguity.

    Accepts:
      * ``None`` (key absent or explicit null) → ``default`` (fail-closed;
        both security-sensitive bools default to ``False``).
      * a real ``bool`` → returned unchanged.
      * a ``str`` token (case-insensitive, whitespace-trimmed) from
        ``{"", "false", "no", "off", "0"}`` → ``False`` or from
        ``{"true", "yes", "on", "1"}`` → ``True``.

    Any other value (e.g. ``"maybe"``, ``bogus``, an int, a list) raises
    :class:`OkfConfigError`. This is deliberately strict because
    ``allow_active_code`` gates arbitrary JS / full-HTML override execution
    (§11.1): a quoted ``"false"`` must NOT silently enable active code.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _BOOL_FALSE_TOKENS:
            return False
        if token in _BOOL_TRUE_TOKENS:
            return True
        raise OkfConfigError(
            f"{name}: cannot interpret {value!r} as a boolean "
            f"(expected true/false/yes/no/on/off/1/0)."
        )
    raise OkfConfigError(
        f"{name}: cannot interpret {value!r} as a boolean "
        f"(expected true/false/yes/no/on/off/1/0)."
    )


def _validate_enum(
    field_name: str, value: str, allowed: frozenset[str]
) -> None:
    """Reject unknown enum-string config values, fail-closed.

    Used for ``search.*`` provider/mode fields and ``validate.default_profile``
    so a typo does not silently fall back to a default (§11.1).
    """
    if value not in allowed:
        raise OkfConfigError(
            f"{field_name}: unknown value {value!r}; "
            f"expected one of {sorted(allowed)}."
        )


# --- commented-defaults template written by ``okf init`` -------------------

DEFAULT_CONFIG_YAML: str = """\
# okf-loom.config.yaml — OKF bundle configuration (current spec §5).
# Every key is optional. Delete this file to fall back to built-in defaults.
# Paths must be bundle-relative (absolute / ..-escaping values are rejected).

bundle:
  exclude: []                       # gitignore-style globs skipped when scanning *.md (e.g. ["drafts/**"])
  include: []                       # add-back patterns that beat every exclusion (e.g. ["vendor-docs/", "node_modules/my-pkg/docs/"])
  respect_gitignore: true           # skip .gitignore-d paths while scanning (root + nested files)

viewer:
  title: "OKF Bundle"               # viewer page <title>
  override_dir: .okf-loom/viewer         # templates / static / palette overrides
  extension_css: .okf-loom/viewer/extension.css
  extension_js: .okf-loom/viewer/extension.js
  allow_active_code: false          # gate JS / full-HTML override execution

search:
  default_mode: lexical             # lexical | semantic | hybrid | tag | entity | relation

discover:
  suppress_phrases: []              # editorial phrase suppressions for unlinked mentions
  suppress_pairs: []                # [{source: /a.md, target: /b.md}] source→target suppressions

validate:
  default_profile: spec             # spec | producer | loose
  fail_on_broken_links: false

studio:                             # Live collaborative studio (all ON by default)
  edit: true                        # interactive studio (commenting/directing + live); --no-edit = read-only kiosk
  live: true                        # SSE live updates on
  enrich_offer: true                # agent offers to watch on bring-up
  auto_enrich: true                 # while watching, the agent applies edits directly (its job, §1.1/D8)
  constraints: {}                   # empty = fully free agent; opt in restraints here (§12.3)
  debounce_ms: 600
  session_dir: .okf-loom/session
  log_edits: true                   # append a SPEC §7 log.md entry per agent write / resolved comment
  max_sse_clients: 32
  allowed_hosts: [127.0.0.1, localhost]   # invisible cross-origin guard (§15)
  theme: auto                       # auto | light | dark | pastel | sepia | midnight
"""
