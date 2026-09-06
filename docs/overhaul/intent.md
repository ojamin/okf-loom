---
type: Reference
title: Preserved product intent
description: Decision inventory and compatibility contract for the studio platform overhaul.
resource: /overhaul/intent.md
tags: [overhaul, intent, architecture, compatibility]
timestamp: "2026-09-06T00:00:00Z"
---

# Preserved product intent

This inventory is grounded in the [current specification](../../docs-bundle/reference/spec.md),
[studio rationale](../../docs-bundle/explanation/live_studio_design.md), existing implementation,
tests, and graph design notes. It separates deliberate behavior from defects.
The [implementation plan](plan.md) is subordinate to these contracts.

## Product and ownership

| ID | Intentional decision | Consequence for the overhaul |
|---|---|---|
| I01 | Knowledge is plain Markdown with YAML frontmatter, one concept per file. | No migration into a proprietary database or required sidecar content schema. |
| I02 | OKF v0.1 compatibility; only nonempty `type` is required. | Keep optional metadata optional; do not silently tighten conformance. |
| I03 | Unknown keys, authored prose, and formatting survive edits. | Retain round-trip parsing and semantic validation of rewritten frontmatter. |
| I04 | Repository checkout is the distribution unit and a loadable skill. | Retain helper CLI and agent resources; no mandatory package installation or frontend build. |
| I05 | Python 3.11+ standard-library runtime, optional PyYAML with conservative fallback. | Development dependencies remain separate; test fallback explicitly. |
| I06 | Human directs, external agent authors. | Browser reading modes remain read-only; comments carry open-ended intent. |
| I07 | No embedded LLM, provider account, GPU, or model network requirement. | Integrations consume contracts; adapters do not own reasoning. |
| I08 | Single-user local-admin trust model. | Do not introduce accounts, billing, roles, multi-tenant storage, CRDTs, or mandatory approval gates. |
| I09 | Visibility, attribution, validation, and undo are the safety net. | Retain direct agent writes and user-selected constraints. |

## Content, discovery, and retrieval

| ID | Intentional decision | Consequence for the overhaul |
|---|---|---|
| I10 | One `ContentIndex` feeds graph, search, listings, rendering, and exports. | Avoid divergent integration-only models. |
| I11 | Reserved root/directory indexes and append-only logs. | Preserve generated markers and authored regions. |
| I12 | Absolute bundle-relative and relative Markdown links; wikilinks tolerated on import. | Mutators continue emitting standard Markdown links. |
| I13 | Broken links load permissively; mutators fail closed unless forward references are explicit. | Preserve warnings versus errors and profile behavior. |
| I14 | `spec`, `producer`, `loose`, strict, and broken-link validation policies. | Keep stable diagnostic codes and machine-readable results. |
| I15 | Governed relations, entities, aliases, provenance, citations; opaque unknown metadata. | Preserve metadata shapes and raw relation occurrences; logical graph de-duplicates by source/type/target. |
| I16 | Capability tiers: core, recommended, optional. | Advertise actual supported surfaces separately from reserved hooks. |
| I17 | Deterministic scanning and rankings. | Stable tie-breaking and no symlink-directory traversal. |
| I18 | Scan exclusions, nested gitignore semantics, explicit include precedence. | HTTP, imports, exports, watcher, and assets use the same visibility contract. |
| I19 | Six search modes: lexical, semantic-lite, hybrid, tag, entity, relation. | Preserve CLI names; explain fuzzy retrieval honestly; expose available modes consistently. |
| I20 | Weighted BM25, token/trigram cosine, reciprocal-rank fusion. | Keep opt-in score gates and component evidence; no claims of embedding-based semantics. |
| I21 | Discovery → reviewable plan → idempotent apply; scoped neighbor work. | Preserve dry runs, advisory graph health, and safe-to-apply distinctions. |
| I22 | Partial updates fail on missing or ambiguous targets. | Preserve exact replacement and section update semantics. |
| I23 | Bootstrap/import/upgrade/index/repair operations remain available. | Verify complete CLI parity rather than replacing the tool with a viewer. |

## Reading and graph experience

| ID | Intentional decision | Consequence for the overhaul |
|---|---|---|
| I24 | Rendered, Source, Split reading modes. | Maintain selection, scroll, view state, and source synchronization across live patches. |
| I25 | Progressive GFM, Mermaid, KaTeX, code, tables, media rendering. | Preserve readable fallbacks and table sort/filter/copy/resize behavior. |
| I26 | Metadata is navigable context: type, entities, relations, provenance, citations. | Improve hierarchy without hiding or flattening authored meaning. |
| I27 | Five token-based themes plus system preference. | Keep all palettes, contrast roles, reduced-motion, focus, print, and narrow layouts. |
| I28 | Six question-based graph lenses, not an algorithm selector. | Map/Themes/Flow/Bridges/Recent/Focus remain distinct and reachable. |
| I29 | Map uses communities and PageRank; Themes emphasizes grouping. | Retain deterministic community detection and explicit metric encoding. |
| I30 | Flow shows direction; Bridges uses betweenness; Recent uses authored timestamps. | Preserve evidence, undated state, and distinction between recency and correctness. |
| I31 | Focus neighborhood, path tracing, hover previews, keyboard node index, ranked evidence. | Keep graph useful without requiring pointer-only canvas interaction. |
| I32 | Stable positions, type icons, theme-color opt-out, advanced controls. | Avoid decorative graph redesign that discards computed meaning. |
| I33 | Live, SPA, static, single-file outputs share rendering. | Preserve file:// search/graph data, relative links, media, printable/no-JS reading. |

## Collaboration and safety

| ID | Intentional decision | Consequence for the overhaul |
|---|---|---|
| I34 | Comments anchor to concept, heading, text selection, or frontmatter. | Keep exact context and anchor recovery visible. |
| I35 | Open → claimed → resolved/dismissed; archive is independent. | Preserve root-only archive, whole-thread-resolved rule, and unarchive on reply. |
| I36 | Agent clarification replies do not become new work for that agent. | Preserve reply ownership, parent lifecycle, and capped displayed nesting. |
| I37 | Ask and done summaries are distinct; creation time is immutable. | Retain request_summary, summary, updated_at, linked activities, and backlog reporting. |
| I38 | Foreground block-once `wait`, continuous `watch`, headless operation. | Keep the CLI loop independent of whether a browser is open. |
| I39 | Presence and claims recover after stale TTLs. | Make disconnected/waiting/editing states truthful and recoverable. |
| I40 | REST + SSE, polling fallback, ascending cursor replay, bounded event queues. | No WebSocket requirement; maintain event identity, resync, and cross-process delivery. |
| I41 | Writes are atomic, revision-checked, attributed, snapshotted, broadcast. | Serialize cooperating writers and detect deletion conflicts; do not mistake atomic rename for a transaction. |
| I42 | Single/group undo is an explicit user action. | Preserve history limits and report unavailable snapshots without pretending success. |
| I43 | Session state is bounded, ephemeral, self-ignored. | Keep tokens secret and do not promise comments/history travel with Markdown alone. |
| I44 | Loopback default; public exposure explicit; mutation token + Origin AND Host checks. | Integrations reuse the same authorization boundary. |
| I45 | Whitelisted operations, capped JSON, containment, active-code double consent. | Never turn an integration endpoint into arbitrary shell execution. |
| I46 | Read-only kiosk retains live reads; tunnel administration is separately allowed. | Distinguish a deliberate static/read-only mode from a connection failure. |

## Ecosystem boundaries

| ID | Intentional decision | Consequence for the overhaul |
|---|---|---|
| I47 | CLI subprocess, Python library, HTTP/SSE, and viewer overrides support embedding. | Supply documented lifecycle and transport clients without owning the host process. |
| I48 | Panel and viewMode extensions work; toolbar/graphDecorator/suggestionRenderer were reserved. | Preserve current hooks and report reserved capability status honestly. |
| I49 | Host controls process lifetime, model choice, deployment, and extra policy. | Offer start/close/context-manager semantics; do not add a background agent daemon. |
| I50 | Optional extension seams must not change the base wire format. | Version API contracts separately from OKF and runtime versions. |

## Observed gaps, not intentional design

The published static demo displays a live-studio-unavailable warning despite
being a successful static build. Its long authored introduction appears before
the generated concept catalog, making direct browsing expensive. These are
presentation defects, not a reason to delete authored introductions.

The baseline implementation has a read/check/write race, ignores an expected
revision when the file disappears, and embeds lifecycle assembly inside a
blocking server function. The main browser-loop test forces a document refresh
and converts server startup errors into skips. The only checked-in CI workflow
deploys docs. These are correctness and verification gaps, not product choices.

Some historical explanations contradict the current spec about init/bootstrap,
proposal storage, and metadata requirements. Code, current spec, and executable
tests must be reconciled; historical research must not silently expand scope.

## Non-goals

This overhaul does not add multi-user identity, cloud sync, a hosted agent,
embedding models, a new document format, or mandatory publication. Those would
change the founding intent and need separate product decisions.
