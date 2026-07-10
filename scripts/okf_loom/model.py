"""Core OKF data model: Bundle, Concept, Index/Log files, Link, Graph, ContentIndex.

Design goals (see docs/architecture.md):
    - Round-trip safe: every loaded object preserves the raw text and the
      frontmatter key order so serialization reproduces the original except
      for whitespace normalization.
    - Permissive: conforms to SPEC §9 — load never hard-fails on soft issues;
      it records warnings instead.
    - One canonical ContentIndex that search, graph, and listings all read
      (the Quartz pattern from docs/research.md).
    - Lazy: graph/content_index computed on demand; Bundle.load is cheap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Literal

from . import (
    GENERATED_MARKER_KEY,
    OKF_VERSION_KEY,
    RECOMMENDED_FRONTMATTER_KEYS,
    SPEC_VERSION,
)
from .exceptions import OKFParseError
from .parse import (
    ExtractedLink,
    _WIKILINK_RE,
    extract_headings,
    extract_links,
    parse_document,
    strip_markdown_for_search,
)
from .paths import (
    ConceptId,
    ConceptIdError,
    concept_id_from_path,
    concept_id_from_str,
    is_reserved_filename,
)


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    level: int
    slug: str
    text: str


# ---------------------------------------------------------------------------
# Links (re-export the parsed link with a source)
# ---------------------------------------------------------------------------


LinkForm = Literal["absolute", "relative", "external", "external_out_of_bundle", "anchor", "wikilink"]


@dataclass(frozen=True)
class Link:
    """A link extracted from a concept body, with source/target context.

    `target` is None for external and anchor-only links, or for relative
    links whose target could not be resolved against the bundle root (e.g.
    target outside the bundle). Broken absolute/relative links to a target
    that doesn't exist in the bundle still have `target` set; check
    `target_exists` separately.
    """

    source: ConceptId
    target_raw: str
    target: ConceptId | None
    form: LinkForm
    label: str
    anchor: str | None
    line: int

    @property
    def is_internal(self) -> bool:
        """True if this link points at a markdown concept inside the bundle."""
        return self.form in ("absolute", "relative", "wikilink")

    @property
    def origin(self) -> str:
        """Return ``"relation"`` for governed typed relations, else ``"markdown"``.

        Typed-relation edges remain :class:`Link` objects for backwards
        compatibility.  Centralising the discriminator avoids making every
        consumer repeat the ``target_raw`` magic-prefix check.
        """
        return "relation" if self.target_raw.startswith("relation:") else "markdown"

    @property
    def logical_type(self) -> str:
        """Relation type used by :meth:`Graph.logical_edges`.

        Markdown labels are display prose, not relation types, so markdown
        occurrences use the empty logical type.  Typed relations use their
        authored relation label.
        """
        return self.label if self.origin == "relation" else ""


# ---------------------------------------------------------------------------
# Concept
# ---------------------------------------------------------------------------


@dataclass
class Concept:
    """A single OKF concept document (one markdown file)."""

    id: ConceptId
    path: Path
    rel_path: Path
    frontmatter: dict[str, Any]
    body: str
    raw_text: str
    headings: list[Heading] = field(default_factory=list)

    # --- derived accessors (SPEC-known frontmatter keys) -------------------

    @property
    def type(self) -> str | None:
        v = self.frontmatter.get("type")
        return str(v) if v is not None else None

    @property
    def title(self) -> str:
        v = self.frontmatter.get("title")
        if v:
            return str(v)
        # Fall back to last path segment (SPEC §4.1 allows this).
        return self.id[-1].replace("_", " ")

    @property
    def description(self) -> str:
        v = self.frontmatter.get("description")
        return str(v) if v is not None else ""

    @property
    def resource(self) -> str | None:
        v = self.frontmatter.get("resource")
        return str(v) if v is not None else None

    @property
    def tags(self) -> list[str]:
        v = self.frontmatter.get("tags")
        if v is None:
            return []
        if isinstance(v, str):
            # Tolerate scalar tags (some producers emit them).
            return [v]
        if isinstance(v, list):
            return [str(t) for t in v]
        return []

    @property
    def timestamp(self) -> str | None:
        v = self.frontmatter.get("timestamp")
        # Preserve as string to avoid timezone pitfalls (SPEC just says ISO 8601).
        return str(v) if v is not None else None

    @property
    def schema_section(self) -> str | None:
        return self._section("Schema")

    @property
    def examples_section(self) -> str | None:
        return self._section("Examples")

    @property
    def citations_section(self) -> str | None:
        return self._section("Citations")

    def _section(self, name: str) -> str | None:
        """Return the body of a conventional `# <name>` section, if present."""
        pat = re.compile(
            rf"^#*\s*{re.escape(name)}\s*#*\s*$", re.MULTILINE
        )
        m = pat.search(self.body)
        if not m:
            return None
        rest = self.body[m.end():]
        # Stop at the next same-or-higher level heading.
        nxt = re.search(r"^#{1,6}\s+\S", rest, re.MULTILINE)
        return (rest[: nxt.start()] if nxt else rest).strip()

    def search_text(self) -> str:
        """Plain-text rendering suitable for lexical indexing.

        Concatenates title + description + headings + body. Memoized on the
        instance to avoid recomputing across multiple search calls.
        """
        cached = getattr(self, "_search_text_cache", None)
        if cached is not None:
            return cached
        parts = [self.title]
        if self.description:
            parts.append(self.description)
        parts.extend(h.text for h in self.headings)
        parts.append(strip_markdown_for_search(self.body))
        text = "\n".join(p for p in parts if p)
        object.__setattr__(self, "_search_text_cache", text)  # type: ignore[attr-defined]
        return text

    def links(
        self, *, bundle_root: Path
    ) -> list[Link]:
        """Extract all links from this concept's body, resolved against the
        bundle root."""
        return [
            Link(
                source=self.id,
                target_raw=el.target_raw,
                target=el.concept_id,
                form=el.form,
                label=el.label,
                anchor=el.anchor,
                line=el.line,
            )
            for el in extract_links(
                self.body, source_dir=self.path.parent, bundle_root=bundle_root
            )
        ]


# ---------------------------------------------------------------------------
# IndexFile and LogFile (SPEC §6 and §7)
# ---------------------------------------------------------------------------


@dataclass
class IndexFile:
    """A SPEC §6 ``index.md`` file."""

    rel_path: Path
    path: Path
    body: str
    raw_text: str
    # Per SPEC §11, only the bundle-root index.md may carry frontmatter (and
    # then only okf_version + okf_extensions + the generated marker).
    frontmatter: dict[str, Any] = field(default_factory=dict)
    is_root: bool = False
    okf_version: str | None = None
    okf_extensions: list[str] | None = None
    is_generated: bool = False

    def entries(self) -> list[tuple[str, str, str]]:
        """Parse `* [label](target) - description` entries from the body.

        Returns ``(label, target, description)`` triples in document order.
        Targets are kept verbatim (caller resolves them).
        """
        out: list[tuple[str, str, str]] = []
        for m in re.finditer(
            r"^\s*[*\-+]\s+\[(?P<label>[^\]]*)\]\((?P<target>[^)\s]+)\)(?:\s*-\s*(?P<desc>.*))?\s*$",
            self.body,
            re.MULTILINE,
        ):
            out.append(
                (
                    m.group("label"),
                    m.group("target"),
                    (m.group("desc") or "").strip(),
                )
            )
        return out


@dataclass
class LogEntry:
    """One date-grouped block from a SPEC §7 log.md file."""

    date: str  # YYYY-MM-DD
    raw_heading: str
    lines: list[str]


@dataclass
class LogFile:
    rel_path: Path
    path: Path
    entries: list[LogEntry]
    raw_text: str

    @property
    def latest_date(self) -> str | None:
        """The newest ISO date heading present, or None if empty."""
        return self.entries[0].date if self.entries else None


# ---------------------------------------------------------------------------
# Load warnings (collected, never fatal except for hard parse errors)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadWarning:
    code: str
    message: str
    path: Path | None = None
    line: int | None = None


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


class Bundle:
    """A loaded OKF bundle (SPEC §3).

    Use `Bundle.load(path)` to read from disk. The bundle holds:
        - `concepts`: dict[ConceptId, Concept]
        - `indexes`: dict[rel_path, IndexFile]
        - `logs`: dict[rel_path, LogFile]
        - `warnings`: list[LoadWarning] from the load pass

    Graph and ContentIndex are computed lazily and memoized.
    """

    def __init__(
        self,
        root: Path,
        *,
        name: str | None = None,
        concepts: dict[ConceptId, Concept] | None = None,
        indexes: dict[Path, IndexFile] | None = None,
        logs: dict[Path, LogFile] | None = None,
        okf_version: str | None = None,
        okf_extensions: list[str] | None = None,
        warnings: list[LoadWarning] | None = None,
    ) -> None:
        self.root = Path(root)
        self.name = name or self.root.resolve().name
        self.concepts: dict[ConceptId, Concept] = concepts or {}
        self.indexes: dict[Path, IndexFile] = indexes or {}
        self.logs: dict[Path, LogFile] = logs or {}
        self.okf_version = okf_version
        self.okf_extensions = okf_extensions or []
        self.warnings: list[LoadWarning] = warnings or []
        self._graph: Graph | None = None
        self._content_index: ContentIndex | None = None
        self._capabilities: "ResolvedCapabilities | None" = None
        self._lexical_backend: Any = None  # cached LexicalBackend (lazy)
        self._semantic_lite_backend: Any = None  # cached SemanticLiteBackend (lazy)
        # SPEC §10: cached result of `has_wikilinks`. Cleared by invalidate()
        # alongside the other derived caches so mutations to concept bodies
        # are reflected on the next read.
        self._has_wikilinks_cache: bool | None = None

    def invalidate(self) -> None:
        """Drop all derived/cached state so the next access recomputes from scratch.

        Call this after mutating ``self.concepts`` (e.g. via ``apply_plan``)
        or after swapping ``self.bundle`` in a long-running server. Without
        this, ``graph()``, ``content_index()``, ``capabilities()``,
        ``has_wikilinks`` and the lexical search backend will return STALE
        results.

        Also clears per-concept ``_search_text_cache`` so search results
        reflect mutated body/frontmatter text.

        Safe to call repeatedly (no-op if nothing is cached).
        """
        self._graph = None
        self._content_index = None
        self._capabilities = None
        self._lexical_backend = None
        self._semantic_lite_backend = None
        self._has_wikilinks_cache = None
        # Clear per-concept search-text cache (set via object.__setattr__
        # in Concept.search_text). Without this, apply_plan mutations to
        # concept.body/frontmatter produce stale search corpus text.
        for concept in self.concepts.values():
            if hasattr(concept, "_search_text_cache"):
                try:
                    object.__delattr__(concept, "_search_text_cache")
                except (AttributeError, TypeError):
                    pass

    # --- loading ------------------------------------------------------------

    @classmethod
    def load(
        cls, root: str | Path, *, name: str | None = None
    ) -> "Bundle":
        """Load a bundle from disk.

        Per SPEC §9, this is permissive: malformed YAML or missing `type`
        become warnings rather than exceptions. Only filesystem errors raise.
        """
        root = Path(root).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Bundle directory not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {root}")

        bundle = cls(root, name=name)
        root_resolved = root.resolve()
        # Scan via the pruning walker (spec §5): built-in default excludes
        # (hidden dirs, node_modules, nested git repos, …), .gitignore
        # respect, and bundle.exclude patterns from okf-loom.config.yaml —
        # so serving/validating a real workspace root does not sweep vendor
        # trees in as thousands of missing-`type` pseudo-concepts.
        # Lazy imports: config imports log, which imports model transitively.
        from .config import CONFIG_FILENAME, OkfConfig, OkfConfigError
        from .ignore import iter_markdown_files
        try:
            _bundle_cfg = OkfConfig.load(root).bundle
        except OkfConfigError as e:
            # Load stays permissive (spec §9): a broken config must not make
            # the bundle unloadable. Warn and scan with the defaults.
            bundle.warnings.append(LoadWarning(
                code="config.unparseable",
                path=root / CONFIG_FILENAME,
                message=(
                    f"{CONFIG_FILENAME} could not be loaded ({e}); "
                    f"scanning with default excludes"
                ),
            ))
            from .config import BundleConfig
            _bundle_cfg = BundleConfig()
        for md_path in sorted(iter_markdown_files(
            root,
            exclude=_bundle_cfg.exclude,
            include=_bundle_cfg.include,
            respect_gitignore=_bundle_cfg.respect_gitignore,
        )):
            try:
                rel = md_path.relative_to(root)
            except ValueError:
                continue
            # P1-1/P2-5 (iter-3 security): containment-check EVERY .md BEFORE
            # reading it. A symlinked concept/reserved file resolving outside
            # the bundle (or a symlink loop, ELOOP) would otherwise crash
            # read_text (iter-3 P1-1 DoS) or read a host secret into memory
            # before the post-read containment check rejects it (P2-5 info
            # disclosure). Fail closed → LoadWarning + skip. Uses the
            # canonical helper.
            from .paths import path_within_bundle
            if not path_within_bundle(md_path, root):
                code = (
                    "reserved_file_escapes_bundle"
                    if md_path.name in ("index.md", "log.md")
                    else "concept.path_escapes_bundle"
                )
                bundle.warnings.append(LoadWarning(
                    code=code,
                    path=md_path,
                    message=(
                        f"{rel} resolves outside the bundle root "
                        f"(symlink escape/loop); skipped"
                    ),
                ))
                continue
            # P1-1: also guard the read itself against ELOOP / unreadable files
            # (path_within_bundle catches loops via resolve(), but be defensive
            # against races and odd filesystem states).
            # P1-1 (iter-5): body-size cap — DoS guard. A 200MB concept body
            # loads in <1s, pinning memory on every CLI command + server worker.
            # MAX_BODY_BYTES = 1 MiB (generous for OKF concept docs).
            try:
                _body_size = md_path.stat().st_size
                if _body_size > 1048576:
                    bundle.warnings.append(LoadWarning(
                        code="concept.body_too_large",
                        path=md_path,
                        message=f"{rel} body exceeds 1 MiB ({_body_size} bytes); skipped",
                    ))
                    continue
                raw = md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                bundle.warnings.append(LoadWarning(
                    code="concept.unreadable",
                    path=md_path,
                    message=f"{rel} could not be read ({type(e).__name__}); skipped",
                ))
                continue
            if md_path.name == "index.md":
                bundle._load_index(md_path, rel, raw)
            elif md_path.name == "log.md":
                bundle._load_log(md_path, rel, raw)
            else:
                bundle._load_concept(md_path, rel, raw)
        return bundle

    def _load_concept(self, path: Path, rel: Path, raw: str) -> None:
        try:
            fm, body = parse_document(raw)
        except OKFParseError as e:
            self.warnings.append(
                LoadWarning(
                    code="concept.parse_failed",
                    message=str(e),
                    path=path,
                )
            )
            return
        try:
            cid = concept_id_from_path(self.root, path)
        except ConceptIdError as e:
            self.warnings.append(
                LoadWarning(
                    code="concept.bad_id",
                    message=str(e),
                    path=path,
                )
            )
            return
        except (ValueError, OSError):
            # P1-5 (security): a symlinked concept whose resolved path escapes
            # the bundle root makes ``pathlib.relative_to`` raise a PLAIN
            # ValueError (not ConceptIdError). Without this catch every
            # bundle-loading command crashes and the ValueError message embeds
            # the resolved HOST path (information disclosure). Mirror the
            # reserved-file defense (above): emit a dedicated warning code,
            # use the bundle-RELATIVE ``rel`` path in the message (never the
            # resolved host path), and skip the concept. Fail closed.
            self.warnings.append(
                LoadWarning(
                    code="concept.path_escapes_bundle",
                    path=path,
                    message=(
                        f"{rel} resolves outside the bundle root "
                        f"(symlink escape); skipped"
                    ),
                )
            )
            return
        headings = [
            Heading(level, slug, text)
            for (level, slug, text) in extract_headings(body)
        ]
        concept = Concept(
            id=cid,
            path=path,
            rel_path=rel,
            frontmatter=fm,
            body=body,
            raw_text=raw,
            headings=headings,
        )
        if not concept.type:
            # SPEC §9: type is the one required key. We still load the file
            # (permissive consumption) but record a conformance warning.
            self.warnings.append(
                LoadWarning(
                    code="concept.missing_type",
                    message="Concept frontmatter is missing the required `type` key",
                    path=path,
                )
            )
        if cid in self.concepts:
            self.warnings.append(
                LoadWarning(
                    code="concept.duplicate_id",
                    message=f"Duplicate concept id {cid!r} (also at {self.concepts[cid].path})",
                    path=path,
                )
            )
        self.concepts[cid] = concept

    def _load_index(self, path: Path, rel: Path, raw: str) -> None:
        is_root = rel.parent == Path(".")
        # Per SPEC §6, index files have no frontmatter EXCEPT the root may
        # declare okf_version (SPEC §11). We attempt to parse frontmatter on
        # every index.md so we can detect non-root frontmatter violations;
        # parse_document returns ({}, raw) when no frontmatter block is
        # present, which is the common case.
        fm, body = ({}, raw)
        try:
            fm, body = parse_document(raw)
        except OKFParseError as e:
            # Malformed frontmatter on a root index is a real problem; on a
            # non-root index we treat the whole file as body (permissive).
            if is_root:
                self.warnings.append(
                    LoadWarning(
                        code="index.parse_failed",
                        message=str(e),
                        path=path,
                    )
                )
            fm, body = {}, raw

        if not is_root and fm:
            # Non-root index.md carrying frontmatter is a soft SPEC §6 violation.
            self.warnings.append(
                LoadWarning(
                    code="index.unexpected_frontmatter",
                    message="Non-root index.md carries frontmatter (SPEC §6 reserves this for the bundle root)",
                    path=path,
                )
            )

        index_file = IndexFile(
            rel_path=rel,
            path=path,
            body=body,
            raw_text=raw,
            frontmatter=fm,
            is_root=is_root,
            okf_version=str(fm.get(OKF_VERSION_KEY)) if fm.get(OKF_VERSION_KEY) else None,
            okf_extensions=(
                [str(x) for x in fm.get("okf_extensions")]
                if isinstance(fm.get("okf_extensions"), list)
                else None
            ),
            is_generated=bool(fm.get(GENERATED_MARKER_KEY)),
        )
        self.indexes[rel] = index_file
        if is_root and index_file.okf_version:
            self.okf_version = index_file.okf_version
        if is_root and index_file.okf_extensions:
            self.okf_extensions = index_file.okf_extensions

    def _load_log(self, path: Path, rel: Path, raw: str) -> None:
        entries = _parse_log(raw)
        self.logs[rel] = LogFile(
            rel_path=rel, path=path, entries=entries, raw_text=raw
        )

    # --- accessors ----------------------------------------------------------

    def types(self) -> set[str]:
        return {c.type for c in self.concepts.values() if c.type}

    def tags(self) -> set[str]:
        out: set[str] = set()
        for c in self.concepts.values():
            out.update(c.tags)
        return out

    def concept_at(self, cid_str: str) -> Concept | None:
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return None
        return self.concepts.get(cid)

    def root_index(self) -> IndexFile | None:
        return self.indexes.get(Path("index.md"))

    def __len__(self) -> int:
        return len(self.concepts)

    def __iter__(self) -> Iterator[Concept]:
        return iter(self.concepts.values())

    # --- derived graphs -----------------------------------------------------

    def graph(self) -> "Graph":
        if self._graph is None:
            self._graph = Graph.build(self)
        return self._graph

    def content_index(self) -> "ContentIndex":
        if self._content_index is None:
            self._content_index = ContentIndex.build(self)
        return self._content_index

    @property
    def has_wikilinks(self) -> bool:
        """True iff any concept body contains a SPEC §10 ``[[...]]`` wikilink.

        Mirrors the ``okf.cap.wikilinks`` capability's auto-activation rule
        (SPEC §10 L605-607): the capability is body syntax, not a frontmatter
        key, so it activates purely on observed wikilinks. Reuses the parser's
        ``_WIKILINK_RE`` so the property agrees with what
        :func:`okf_loom.parse.extract_links` would emit as
        ``form='wikilink'`` edges.

        Memoized on the instance; cleared by :meth:`invalidate` (alongside
        ``_graph``, ``_capabilities``, etc.) so that mutations to concept
        bodies are reflected on the next read.
        """
        if self._has_wikilinks_cache is None:
            self._has_wikilinks_cache = any(
                _WIKILINK_RE.search(c.body) for c in self.concepts.values()
            )
        return self._has_wikilinks_cache

    def capabilities(self) -> "ResolvedCapabilities":
        """Resolve active capabilities for this bundle (lazy, memoized).

        Uses :func:`okf_loom.extensions.default_registry` and observes
        the bundle's ``okf_extensions`` declaration plus the frontmatter
        keys and body sections actually present in the concepts.

        SPEC §10: ``okf.cap.wikilinks`` is body syntax (no frontmatter key),
        so when :attr:`has_wikilinks` is True the capability is added to the
        ``declared`` set passed to ``resolve()`` — the smallest activation
        path that does not require widening the capability descriptor or
        adding a new ``resolve()`` rule.
        """
        if self._capabilities is None:
            from .extensions import default_registry
            observed_keys: set[str] = set()
            observed_sections: set[str] = set()
            for c in self.concepts.values():
                observed_keys.update(c.frontmatter.keys())
                observed_sections.update(h.text for h in c.headings)
            declared = list(self.okf_extensions or [])
            # SPEC §10 auto-activation: okf.cap.wikilinks fires on observed
            # [[...]] body syntax. `declared` is the right channel because
            # the capability descriptor intentionally has empty
            # frontmatter_keys/body_sections (it is not key- or section-gated).
            if self.has_wikilinks and "okf.cap.wikilinks" not in declared:
                declared.append("okf.cap.wikilinks")
            self._capabilities = default_registry().resolve(
                declared=declared,
                observed_keys=observed_keys,
                observed_sections=observed_sections,
            )
        return self._capabilities


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


@dataclass
class Graph:
    """The directed link graph of a bundle (derived from Bundle).

    All edges are Link objects. Edges to nonexistent targets are kept in
    `unresolved` so callers can report broken links (SPEC §5.3: consumers
    MUST tolerate broken links; they are not malformed but are useful to flag).
    """

    nodes: list[Concept]
    edges: list[Link]
    unresolved: list[Link]
    external: list[Link]
    out_edges: dict[ConceptId, list[Link]]
    in_edges: dict[ConceptId, list[Link]]
    concept_ids: set[ConceptId]

    @classmethod
    def build(cls, bundle: Bundle) -> "Graph":
        ids = set(bundle.concepts.keys())
        edges: list[Link] = []
        unresolved: list[Link] = []
        external: list[Link] = []
        out_edges: dict[ConceptId, list[Link]] = {cid: [] for cid in ids}
        in_edges: dict[ConceptId, list[Link]] = {cid: [] for cid in ids}

        for concept in bundle.concepts.values():
            for link in concept.links(bundle_root=bundle.root):
                if link.form == "external":
                    external.append(link)
                    continue
                if link.form == "external_out_of_bundle":
                    # Out-of-bundle links to files that exist on
                    # disk in the workspace. Not graph edges (different
                    # bundle root), but tracked separately so consumers
                    # can surface them if desired.
                    external.append(link)
                    continue
                if link.form == "anchor":
                    # Same-document anchor; not a graph edge.
                    continue
                # internal link (absolute or relative)
                edges.append(link)
                out_edges[concept.id].append(link)
                if link.target is None:
                    unresolved.append(link)
                    continue
                if link.target in ids:
                    in_edges[link.target].append(link)
                else:
                    unresolved.append(link)

        # When okf.cap.typed_relations is active, fold the `relations:`
        # frontmatter key into graph edges so typed relations appear in the
        # graph view, backlinks, and neighbour calculations. Each relation
        # entry is `{target: <concept_id_str>, type: <str>, detail: <str>}`.
        # This is the worked example of a capability actually gating behavior.
        try:
            caps = bundle.capabilities()
        except Exception:
            caps = None
        if caps is not None and caps.is_active("okf.cap.typed_relations"):
            for concept in bundle.concepts.values():
                rels = concept.frontmatter.get("relations")
                if not isinstance(rels, list):
                    continue
                for entry in rels:
                    if not isinstance(entry, dict):
                        continue
                    target_str = entry.get("target")
                    if not target_str or not isinstance(target_str, str):
                        continue
                    try:
                        target_cid = concept_id_from_str(target_str)
                    except ConceptIdError:
                        continue
                    rel_type = str(entry.get("type", "relates_to"))
                    rel_link = Link(
                        source=concept.id,
                        target_raw=f"relation:{target_str}",
                        target=target_cid,
                        form="absolute",
                        label=rel_type,
                        anchor=None,
                        line=0,
                    )
                    edges.append(rel_link)
                    out_edges[concept.id].append(rel_link)
                    if target_cid in ids:
                        in_edges[target_cid].append(rel_link)
                    else:
                        unresolved.append(rel_link)

        return cls(
            nodes=list(bundle.concepts.values()),
            edges=edges,
            unresolved=unresolved,
            external=external,
            out_edges=out_edges,
            in_edges=in_edges,
            concept_ids=ids,
        )

    def backlinks(self, cid: ConceptId) -> list[Link]:
        """Links pointing AT the given concept."""
        return list(self.in_edges.get(cid, []))

    def outlinks(self, cid: ConceptId) -> list[Link]:
        """Links FROM the given concept."""
        return list(self.out_edges.get(cid, []))

    def logical_edges(self) -> list[Link]:
        """Return a stable de-duplicated view while preserving raw occurrences.

        :attr:`edges` intentionally retains every authored occurrence for
        diagnostics and round-trip-sensitive consumers.  This view collapses
        repeats by ``(source, logical_type, target)`` while retaining distinct
        typed relation types between the same two concepts.  The first authored
        occurrence wins, preserving deterministic order.
        """
        seen: set[tuple[ConceptId, str, object]] = set()
        out: list[Link] = []
        for link in self.edges:
            target_identity: object = (
                link.target if link.target is not None else link.target_raw
            )
            key = (link.source, link.logical_type, target_identity)
            if key in seen:
                continue
            seen.add(key)
            out.append(link)
        return out

    def neighbours(self, cid: ConceptId, *, max_depth: int = 1) -> set[ConceptId]:
        """BFS over in+out edges up to max_depth hops."""
        if max_depth < 0:
            return set()
        seen: set[ConceptId] = set()
        frontier: set[ConceptId] = {cid}
        for _ in range(max_depth):
            new_frontier: set[ConceptId] = set()
            for n in frontier:
                for link in self.out_edges.get(n, []):
                    if link.target and link.target not in seen:
                        new_frontier.add(link.target)
                for link in self.in_edges.get(n, []):
                    if link.source not in seen:
                        new_frontier.add(link.source)
            seen.update(frontier)
            frontier = new_frontier - seen
            if not frontier:
                break
        seen.update(frontier)
        seen.discard(cid)
        return {n for n in seen if n in self.concept_ids}


# ---------------------------------------------------------------------------
# ContentIndex — the single canonical model (Quartz pattern)
# ---------------------------------------------------------------------------


@dataclass
class ContentIndex:
    """Canonical content model that search/graph/listings all read.

    Built from a Bundle. Cheap to build (no embeddings); the lexical backend
    precomputes plain-text per concept lazily on demand.
    """

    bundle: Bundle
    graph: Graph
    by_id: dict[ConceptId, Concept]
    by_type: dict[str, list[Concept]]
    by_tag: dict[str, list[Concept]]
    corpus_text: dict[ConceptId, str]

    @classmethod
    def build(cls, bundle: Bundle) -> "ContentIndex":
        by_type: dict[str, list[Concept]] = {}
        by_tag: dict[str, list[Concept]] = {}
        corpus: dict[ConceptId, str] = {}
        for c in bundle.concepts.values():
            by_type.setdefault(c.type or "<untyped>", []).append(c)
            for t in c.tags:
                by_tag.setdefault(t, []).append(c)
            corpus[c.id] = c.search_text()
        return cls(
            bundle=bundle,
            graph=bundle.graph(),
            by_id=dict(bundle.concepts),
            by_type=by_type,
            by_tag=by_tag,
            corpus_text=corpus,
        )

    def concepts_of_type(self, type_: str) -> list[Concept]:
        return list(self.by_type.get(type_, []))

    def concepts_with_tag(self, tag: str) -> list[Concept]:
        return list(self.by_tag.get(tag, []))


# ---------------------------------------------------------------------------
# Log parsing helpers (SPEC §7)
# ---------------------------------------------------------------------------


_DATE_HEADING_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b.*$", re.MULTILINE)
# P2-12 (iter-3): capture ANY ``## `` heading so non-ISO-date headings (e.g.
# ``## Notes``, ``## 99-99-99``) surface to the validator. Previously only
# ISO-date headings were parsed, making validate's ``log.bad_date_heading``
# check dead code (entry.date always matched by construction).
_ANY_H2_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _parse_log(raw: str) -> list[LogEntry]:
    """Parse a SPEC §7 log.md body into date-grouped entries.

    SPEC says newest first. We do NOT reorder; we trust the file's order and
    expose ``entries`` as-parsed. Callers can sort by ``entry.date`` if needed.

    P2-12 (iter-3): non-ISO ``## `` headings are also captured (with
    ``date`` set to the heading text) so :func:`validate._check_log_structure`
    can flag them via ``log.bad_date_heading`` (previously dead code).

    P1-7 (iter-4): strip fenced code blocks before matching headings so
    ``## headings`` inside `` ``` `` fences don't leak as spurious entries.
    """
    # P1-7: strip code fences before heading extraction so fenced content
    # (e.g. an example block containing ``## Notes``) doesn't surface as
    # a spurious log entry.
    from .parse import _strip_code_blocks
    raw = _strip_code_blocks(raw)

    entries: list[LogEntry] = []
    # Build a position-ordered merge of date matches and non-date h2 matches.
    date_matches = {m.start(): m for m in _DATE_HEADING_RE.finditer(raw)}
    any_matches = list(_ANY_H2_HEADING_RE.finditer(raw))
    seen_starts = set(date_matches)
    non_date = [m for m in any_matches if m.start() not in seen_starts]
    all_matches = sorted(
        list(date_matches.values()) + non_date, key=lambda m: m.start()
    )
    for i, m in enumerate(all_matches):
        if m.start() in date_matches:
            date_str = m.group(1)
            raw_heading = m.group(0).strip()
        else:
            # Non-ISO heading — date is the heading text so the validator's
            # fullmatch fails and emits log.bad_date_heading.
            date_str = m.group(1).strip()
            raw_heading = m.group(0).strip()
        start = m.end()
        end = all_matches[i + 1].start() if i + 1 < len(all_matches) else len(raw)
        block = raw[start:end].strip("\n")
        lines = [ln for ln in block.splitlines() if ln.strip()]
        entries.append(
            LogEntry(date=date_str, raw_heading=raw_heading, lines=lines)
        )
    return entries
