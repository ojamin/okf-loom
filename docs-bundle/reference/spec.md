---
type: Reference
title: okf-loom Current Specification
description: The current canonical specification for okf-loom v1.0, covering the upstream OKF SPEC v0.1 wire-format baseline, source-checkout and agent-skills distribution model, bundle contracts, CLI workflows, zero-dependency search, live studio, comment loop, session storage, trust gates, and extension API.
resource: /reference/spec.md
tags: [spec, current, agent-skills, reference]
timestamp: "2026-07-02T00:00:00Z"
---

# okf-loom Current Specification

This is the canonical current specification for the okf-loom v1.0 baseline.
It describes current behavior directly rather than as a release-history delta.
The authoritative runtime version is whatever `scripts/okf-loom --version`
prints; the v1.0 baseline reports `okf-loom 1.0.0 (SPEC v0.1)`.

The base OKF wire format remains upstream **OKF SPEC v0.1**.
okf-loom constant remains `SPEC_VERSION = "0.1"`; the product/runtime version
lives in `okf_loom.LOOM_VERSION`.
okf-loom features are compatible optional capabilities layered on top of that v0.1 Markdown-plus-frontmatter format.

## 1. Primary distribution and use model

okf-loom is currently distributed primarily as a GitHub-hosted, cloneable source repository.
The primary use case is for an agent or harness to clone the repository, load the root `SKILL.md`, read focused resources under `resources/`, and run from the checkout.

The agent-facing skill resources are:

| Resource | Use |
|---|---|
| `SKILL.md` | Primary loadable skill entrypoint. |
| `resources/format-basics.md` | Read, validate, and edit one concept or learn the base format. |
| `resources/authoring.md` | Author good concepts, links, indexes, logs, and frontmatter. |
| `resources/advanced-operations.md` | Operate search, discover/plan/update, capabilities, and builds. |
| `resources/studio-agent-loop.md` | Operate the live studio and comment loop. |

The portable command form is source-checkout invocation:

```bash
okf-loom/scripts/okf-loom <command> ...
```

When already inside the repository root, use:

```bash
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom serve docs-bundle --no-open
```

Use the checked-in helper directly; do not assume an installed console script or package.
The primary distribution assumption is this git repo skill layout plus the checked-in `scripts/okf-loom` helper.

## 2. OKF bundle and concept contract

An OKF bundle is a directory tree of Markdown files.
Each concept is one non-reserved `.md` file with YAML frontmatter plus a Markdown body.
Plain tools can read it, git can diff it, and agents can parse it without an SDK.

Only `type` is required by OKF v0.1.
okf-loom recommends but does not require `title`, `description`, `resource`, `tags`, and `timestamp`.
Unknown frontmatter keys are legal and must be preserved round-trip.

```markdown
---
type: API Endpoint
title: Create Order
description: POST a new order to the checkout service.
resource: https://api.example.com/v2/orders
tags: [checkout, write-api]
timestamp: "2026-06-15T10:00:00Z"
relations:
  - target: tables/orders
    type: writes_to
    detail: idempotent on client_request_id
entities:
  - id: entity/order
    label: Order
    kind: business_entity
aliases: [purchase, transaction]
citations:
  - id: "1"
    text: Internal data dictionary
---

# Overview

This endpoint writes [orders](/tables/orders.md).
```

Reserved files keep their upstream meanings.
`index.md` is a directory listing for progressive disclosure.
`log.md` is an optional append-only update history.
Only the bundle-root `index.md` may carry frontmatter, and only for `okf_version`, `okf_extensions`, and okf-loom `generated` marker.
Non-root `index.md` files are pure listings or marker-managed generated regions.

Concept links may be absolute bundle-relative or relative.
Prefer absolute bundle-relative links such as `[orders](/tables/orders.md)` because they remain stable when files move inside subdirectories.
Wikilinks such as `[[tables/orders|Orders]]` are accepted as migration/convenience body syntax and resolved into graph edges, but mutators write standard Markdown links.

See [/reference/frontmatter.md](/reference/frontmatter.md), [/reference/links.md](/reference/links.md), and [/reference/architecture.md](/reference/architecture.md) for the detailed reference contracts.

## 3. Core runtime invariants

okf-loom is a Python library and CLI over the same model objects: `Bundle`, `Concept`, `Link`, `Graph`, and `ContentIndex`.
The `ContentIndex` is the shared model for graph, search, listings, viewer, and build outputs.

Hard invariants:

- Do not fork the OKF v0.1 wire format.
- Do not introduce hard runtime dependencies beyond okf-loom baseline.
- Load permissively and preserve unknown data.
- Write through parse-modify-serialize paths.
- Use atomic writes through temporary file plus rename.
- Keep outputs deterministic; tie-break by concept id where ranking or ordering is otherwise equal.
- Keep machine-readable output JSON-serializable.
- Never overwrite hand-curated `index.md` or `log.md` content silently.

When a live studio session is attached, concept writes route through the studio write funnel so they are attributed, undoable, logged to the change feed, and broadcast live.
Without a live session, mutators use the same data-safety principles through the non-studio atomic write path.

## 4. Capability registry and governed extension keys

The capability registry gives meaning to optional keys without changing the base spec.
Bundles opt in by declaring `okf_extensions:` in the bundle-root `index.md` or by using governed keys that auto-activate the corresponding capability.

The active tier model is:

| Tier | Meaning |
|---|---|
| `core` | Always active format primitives such as frontmatter, links, indexes, logs, lexical search, graph, backlinks, listings, and mention discovery. |
| `recommended` | Ships with okf-loom and requires no plugin or extra. |
| `optional` | Recognised extension surface that may need external provider, plugin, or explicit opt-in. |

Current governed top-level keys include `relations`, `entities`, `aliases`, `provenance`, and `citations`.
okf-loom intentionally uses top-level governed keys rather than a single nested vendor container.
Unknown keys are opaque and preserved.

Governed keys activate by presence, but activation is not proof that their
value is usable. Producers use the documented list/object shapes; validation
emits stable warnings for malformed outer values or entries. These warnings do
not widen OKF v0.1 hard conformance, but `validate --strict` promotes them.
Unknown keys and unknown fields inside otherwise valid entries remain legal and
round-trip unchanged.

Typed relation occurrences are retained as authored. The graph also exposes a
logical view de-duplicated by `(source, type, target)` for ranking and viewer
consumers. Exact duplicates produce `relation.duplicate`; distinct relation
types between the same concepts remain distinct logical edges. Relation
mutators no-op on an existing `(source, type, target)` and fail closed rather
than overwriting a malformed existing `relations` value.

Example root declaration:

```yaml
---
okf_version: "0.1"
okf_extensions:
  - okf.cap.typed_relations
  - okf.cap.entities
  - okf.cap.aliases
generated: true
---
```

Run capability inspection with:

```bash
scripts/okf-loom capabilities
scripts/okf-loom capabilities --bundle docs-bundle --format json
```

See [/reference/capabilities.md](/reference/capabilities.md).

## 5. Validation, profiles, and configuration

Default OKF conformance is intentionally small.
A bundle is conformant when non-reserved concept files have parseable YAML frontmatter with non-empty `type`, and reserved `index.md` / `log.md` files follow their structural rules.
Broken links, missing recommended keys, unknown types, and undeclared capabilities are guidance by default.

Validation profiles:

| Profile | Behaviour |
|---|---|
| `spec` | Default; only hard v0.1 conformance errors fail. |
| `producer` | `spec` plus missing recommended concept keys promoted to errors. |
| `loose` | Permissive migration mode; missing `type` becomes a warning. |

Useful validation commands:

```bash
scripts/okf-loom validate docs-bundle
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom validate docs-bundle --profile producer
scripts/okf-loom validate docs-bundle --fail-on-broken-links
scripts/okf-loom validate docs-bundle --format json
```

`okf-loom.config.yaml` is an optional bundle-root config file.
It is additive; absence means defaults.
Recognised sections are `bundle`, `viewer`, `search`, `validate`, and `studio`.
Unknown config keys are preserved in `OkfConfig.raw` for forward compatibility.
Path-bearing fields must be bundle-relative and must not escape the bundle root.
Boolean and enum values fail closed rather than guessing.

### Bundle scanning and exclusions

Markdown discovery (`Bundle.load`, the serve watcher, and `okf import`) walks
the bundle tree top-down and prunes excluded directories rather than
rglob-scanning everything, so a bundle root that is also a real workspace
root does not sweep `node_modules`, nested cloned repos, or virtualenv trees
in as thousands of missing-`type` pseudo-concepts.

Three exclusion rule groups apply; a match in a later group overrides
earlier ones, and within a group the last matching rule wins (git
semantics):

1. Built-in defaults: hidden directories (`.git`, `.okf-loom`, `.venv`, …),
   `node_modules/`, `__pycache__/`, `venv/`, `bower_components/`. Hidden
   *files* still load — only hidden directories are pruned. Additionally,
   any non-root directory containing a `.git` entry (a nested cloned repo,
   submodule, or worktree) is pruned — a structural rule, overridable only
   by `bundle.include`.
2. `.gitignore` rules — the bundle root's file and nested ones, each scoped
   to its own directory — unless `bundle.respect_gitignore: false`.
3. `bundle.exclude` patterns from `okf-loom.config.yaml` (a negation such
   as `"!.docs/"` re-includes a path the defaults or a `.gitignore`
   excluded, with git's limitation: not from under a pruned directory).

Above all exclusions sit `bundle.include` patterns — the explicit add-back
lever. Include beats every exclusion source, including the structural
nested-repo rule, and CAN reach inside pruned directories. Two forms:

* Directory form (`"vendor-repo/"`, or a wildcard-free path like
  `"node_modules/my-pkg/docs"`): the directory is revived — the scanner
  descends its ancestors just far enough to reach it, then scans the
  subtree as normal bundle content. Exclusion rules that were overridden at
  the revived directory (an anchored `node_modules/**` in a `.gitignore`, a
  `bundle.exclude` that killed it) stay overridden for its contents, while
  unrelated rules — the hidden-dir default, basename rules like
  `*.tmp.md`, excludes targeting deeper paths — keep applying inside. This
  is the recommended form.
* Glob form (`"vendor-repo/**"`): force-includes exactly what it matches,
  everything beneath — defaults included. A broad `**` on a huge tree is a
  deliberate, paid-for traversal.

Include patterns without a `/` (basename form) apply wherever the scan
already reaches but never open pruned directories — pulling something out
of a pruned tree requires naming its path. Include patterns are positive
only; a `!` there is rejected fail-closed.

Patterns use the practical gitignore subset: `*`, `?`, and character
classes never cross `/`; `**` spans path segments; a trailing `/` makes a
rule directory-only; a `/` anywhere else anchors it to the rule's base
directory; leading `!` negates (exclusion groups only). Scanning stays
deterministic (name-sorted per directory) and never follows symlinked
directories. An unparseable config keeps the bundle loadable: loading warns
(`config.unparseable`) and scans with the built-in defaults.

See [/reference/config_yaml.md](/reference/config_yaml.md) and [/how-to/validate_in_ci.md](/how-to/validate_in_ci.md).

## 6. Search modes and zero-dependency retrieval

The current shipped search story is dependency-free.
Do not describe dense embeddings, vector indexes, SQLite FTS provider flags, provider config keys, or semantic/LLM package extras as shipped current behavior.

The six current modes are:

| Mode | Backend / source | Notes |
|---|---|---|
| `lexical` | `LexicalBackend` | Default BM25-style field-weighted keyword search. |
| `tag` | `LexicalBackend` | Case-insensitive tag membership. |
| `semantic` | `SemanticLiteBackend` | Fuzzy-semantic token + character-trigram cosine; not embeddings. |
| `hybrid` | `HybridBackend` | Reciprocal Rank Fusion of lexical and SemanticLite. |
| `entity` | In-module entity matcher | Matches concept ids, titles, descriptions, entity labels, and aliases. |
| `relation` | In-module relation/edge filter | Filters `relations:` and Markdown graph edges by relation/source/target. |

Examples:

```bash
scripts/okf-loom search docs-bundle "comment lifecycle"
scripts/okf-loom search docs-bundle "agent loop" --mode semantic
scripts/okf-loom search docs-bundle "studio" --mode hybrid
scripts/okf-loom search docs-bundle "unrelated prose" --mode semantic --min-semantic-score 0.1
scripts/okf-loom search docs-bundle "studio" --mode hybrid --hybrid-require both
scripts/okf-loom search docs-bundle "#reference" --mode tag
scripts/okf-loom search docs-bundle "" --mode relation --relation depends_on
```

The HTTP `/__search` route uses lexical search with a server-side limit.
Use the CLI for the other modes.
SemanticLite keeps every positive overlap by default for compatibility. Its
cosine score is query-dependent, and Hybrid's RRF score describes rank rather
than calibrated relevance. `--min-semantic-score N` supplies an opt-in cosine
floor for semantic results (and gates the semantic component before Hybrid
fusion). `--hybrid-require {any,lexical,semantic,both}` selects which backend
evidence a fused hit must carry. Hybrid JSON results expose
`detail.matched_backends`, `component_scores`, and `component_ranks`.

SemanticLite is typo/morphology retrieval, not a substitute for dense
embeddings. A downstream LLM can rerank returned candidates but cannot recover
a relevant concept that retrieval omitted. Dense retrieval remains an optional
future `SearchBackend` integration rather than shipped current behavior.
See [/reference/search_modes.md](/reference/search_modes.md).

## 7. Discovery, planning, update, repair, and authoring verbs

okf-loom supports both report-oriented and action-oriented workflows.
`discover` finds gaps and suggestions.
`plan` builds a reviewable action envelope.
`update` applies an update plan or action envelope idempotently.
`repair` applies mechanical fixes such as marker-safe index regeneration and typed-relation mirroring.

```bash
scripts/okf-loom discover docs-bundle --out /tmp/okf-suggestions.json
scripts/okf-loom plan docs-bundle --out /tmp/okf-plan.json
scripts/okf-loom update docs-bundle --plan /tmp/okf-plan.json --dry-run
scripts/okf-loom repair docs-bundle --indexes --dry-run
```

Scoped work is supported for fast agent loops:

```bash
scripts/okf-loom plan docs-bundle --scope reference/spec --neighbors --out /tmp/scoped-plan.json
scripts/okf-loom update docs-bundle --plan /tmp/scoped-plan.json
```

Authoring verbs are direct, safe mutators:

```bash
scripts/okf-loom write-concept --bundle docs-bundle --id reference/new_topic --type Reference --title "New Topic"
scripts/okf-loom set-frontmatter --bundle docs-bundle --id reference/new_topic --key tags --json-value '["reference"]'
scripts/okf-loom link-add --bundle docs-bundle --source reference/new_topic --target reference/spec --label "current spec"
scripts/okf-loom entity-add --bundle docs-bundle --id reference/new_topic --label "okf-loom" --kind Product
scripts/okf-loom update-section --bundle docs-bundle --id reference/new_topic --heading "## Usage" --body-file /tmp/frag.md
scripts/okf-loom replace-text --bundle docs-bundle --id reference/new_topic --old "exact old text" --new "exact new text"
```

`link-add` is fail-closed on missing targets unless `--allow-forward-reference` is passed intentionally.
Repeated actions should converge to no-op or already-present results rather than duplicate content.

Partial body updates are first-class: `update-section` replaces (or, with
`--append`, appends to) exactly one section — matched by heading text,
optionally level-pinned (`"## Banking"`), including its subsections — and
`replace-text` applies an exact textual patch. Both mutate only the body,
fail closed on a missing target (`section_not_found` / `text_not_found`)
and on ambiguity (`section_ambiguous:N` / `text_ambiguous:N`), and are
available as `/__apply` op kinds `update_section` / `replace_text`. An
agent should never need to reconstruct a whole document (or stage a copy)
to change one block.

`write-concept` CREATE fills the mechanically derivable recommended keys
by default so a fresh concept passes `validate --strict` without follow-up
`set-frontmatter` calls: `resource` defaults to the bundle-absolute
concept path and `timestamp` to the current UTC instant. Explicit values
win; UPDATE never injects defaults; `--no-defaults` opts out. Content
keys (title, description, tags) are never auto-invented.

## 8. Indexes, logs, scaffolding, and derived artifacts

`index` regenerates directory listings without clobbering hand-authored content.
It rewrites only tool-managed root indexes or marked regions delimited by `<!-- okf:generated:index begin -->` and `<!-- okf:generated:index end -->`.

`log` appends to `log.md` newest-first and preserves existing entries.

`scripts/okf-loom init` scaffolds a new bundle with `index.md`, `log.md`, and `okf-loom.config.yaml`.
`scripts/okf-loom bootstrap` scaffolds a simpler bundle without config.
Both refuse unsafe destructive assumptions.

Derived JSON indexes are optional and disposable:

```bash
scripts/okf-loom index docs-bundle --emit-json
```

This writes generated artifacts under `<bundle>/.okf-loom/index/` such as `content.json` and `graph.json`.
They contain no absolute host paths, carry `generated_by: "okf-loom"` and `format_version: 1`, and should be safe to delete and regenerate.
Bundles should normally gitignore `.okf-loom/index/` and `.okf-loom/session/`.

## 9. Viewer, build, and static outputs

All viewer targets share the same `ContentIndex`.

| Target | Command | Use |
|---|---|---|
| Live studio / server | `serve` | Interactive browsing, comments, agent loop, SSE updates. |
| SPA build | `build --target spa` | Published rich reading experience. |
| Static build | `build --target static` | JS-optional, printable, SEO/air-gapped output. |
| Single-file build | `build --target single-file` | Portable artifact for tickets, email, or local sharing. |
| One-file render | `render` | Self-contained viewer artifact. |

Examples:

```bash
scripts/okf-loom serve docs-bundle --no-open
scripts/okf-loom serve docs-bundle --no-open --tunnel   # + public https URL (cloudflared)
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
scripts/okf-loom render docs-bundle --out /tmp/viz.html
```

Static concept and index pages are ordinary server-rendered HTML. The graph and
client-side search payloads are embedded as inert JSON in `__graph.html` and
`__search.html`, so both features work when the directory is opened directly
through `file://`; they do not fetch adjacent JSON files. Generated
`__data/graph.json` and `__data/search.json` remain available to HTTP-hosted or
external consumers. Optional graph/rendering libraries may still use the
configured CDN unless the operator supplies local assets.

The current viewer surface (binding for rebuilds): the root index renders as a dashboard — hero stat chips
(concepts/types/links), an Explore-the-graph entry point, and every
concept as a type-grouped card with link counts and tags. Concept pages
carry a type-tinted header band (icon, reading time, entity chips, typed
relation chips), a full-width rendered body, connection cards for
outgoing/incoming links, and a scroll-spy section rail. The graph view
uses fCoSE with per-type node icons; its controls are a lens bar
pinned at the top of the right-hand detail pane. Each lens answers one
question with a real algorithm (see `docs/design/graph-lenses.md` for
the evidence base): Map (deterministic Louvain communities as colour +
PageRank as size), Themes (strong community grouping + ranked theme
digest), Flow (dagre layered layout over directed/typed edges, falling
back to directed breadthfirst), Bridges (betweenness centrality as
colour/size + ranked bridgers), Recent (frontmatter-timestamp recency
ramp), and Focus (neighbourhood isolation with a depth control). Every
lens renders a clickable ranked summary with a one-line verdict in the
detail pane; an Advanced disclosure holds spacing sliders, the type
filter, a relationship-label toggle, and a 'Colour by theme' toggle
(the Saket et al. 2014 escape hatch back to type colours). The selected
concept's page renders beneath the bar with the same progressive
renderers (mermaid/KaTeX/highlight.js) as the wiki pages. Hover
previews, shift-click path tracing, keyboard navigation, clickable
legend type filters, deterministic layouts, and live SSE canvas updates
are part of the contract.
`serve --tunnel` starts a Cloudflare quick tunnel, prints the public
URL, and injects the tunnel hostname into the studio's runtime
`allowed_hosts`.
`scripts/okf-loom tunnel <bundle>` attaches the same quick tunnel to an
ALREADY RUNNING session (no restart, no lost token/undo history) by
driving the token-guarded `POST /__tunnel` admin route; `--stop`
detaches it and restores the pre-tunnel allowlist, `--status` reports
the current URL. The tunnel process always belongs to the serve
process and dies with it.

Active viewer code has a two-layer gate.
The bundle must declare `viewer.allow_active_code: true`, and the operator must also consent with `--allow-active-code` or a truthy `OKF_LOOM_ALLOW_ACTIVE_CODE`.
Default is off.

## 10. Live collaborative studio model

`scripts/okf-loom serve <bundle>` opens the live collaborative studio by default.
The studio is a local single-user admin tool.
The user directs; the external agent authors.

Core posture:

- The bundle on disk is the source of truth and the bus.
- The browser is a rendered/source/split reading and directing surface.
- The user does not directly edit Markdown in the browser by default.
- The user's studio writes are comments and asks.
- The server embeds no LLM and runs no agent reasoning.
- The external agent watches comments/events and writes through CLI mutators or whitelisted apply paths.
- There is no per-write approval gate by default.
- Transparency, change list, validation, atomic writes, and undo are the safety net.
- Opt-in constraints belong to the user under `studio.constraints`; empty constraints mean a fully free agent.

The canonical bring-up loop is:

```bash
scripts/okf-loom serve path/to/bundle --no-open &
until scripts/okf-loom token path/to/bundle >/dev/null 2>&1; do sleep 0.2; done
scripts/okf-loom wait path/to/bundle --for comment
```

The agent then claims the comment, writes via mutators, resolves the comment, and waits again.
After starting a background server, always wait for token readiness before the first mutating POST or studio-aware CLI mutator.

See [/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md), [/tutorials/author_with_agent.md](/tutorials/author_with_agent.md), and [/explanation/live_studio_design.md](/explanation/live_studio_design.md).

## 11. Live protocols and event schema

The live server is stdlib HTTP plus REST and Server-Sent Events.
There are no WebSockets.

Key routes:

| Route | Purpose |
|---|---|
| `GET /__data/doc?id=<concept_id>` | One concept's `{rev, html, raw, frontmatter, backlinks, outgoing, headings}` for in-place patching. |
| `GET /__data/events?since=&limit=&actor=&concept=&order=` | Filtered change-list read over `events.jsonl`. |
| `GET /__events` | SSE stream of live frames. |
| `GET /__comments` | Canonical comment state from `directives.jsonl`. |
| `GET /__diff` | Token-protected diff between saved revisions. |
| `GET /<path>.<media ext>` | Bundle-local media file (image/video/PDF allowlist). |
| `POST /__comment` | User comment or threaded reply. |
| `POST /__comment-update` | User-facing lifecycle/archive transition, or a body edit of a not-yet-resolved comment. |
| `POST /__presence` | Agent presence update (`state`, `focus`, optional free-text `message` ≤200 chars). |
| `POST /__apply` | Whitelisted update operation. |
| `POST /__undo` | Single or group undo. |
| `POST /__preview` | Render capped in-flight Markdown. |
| `POST /__tunnel` | Server admin: attach/detach/status a quick tunnel at runtime (`action: start\|stop\|status`). Exempt from `--no-edit` (sharing a kiosk is the use case) but never from the token + Origin/Host guard. |

Bundle-local media serving is contract-bound: only an allowlisted media
extension routes to the file handler; the file must be reachable by the §5
scan (the same exclude/include/gitignore semantics as concept discovery)
and its resolved path must stay inside the bundle root. Responses are
sandboxed (bundle media is user content) and carry an ETag for
revalidation. Static builds copy the same §5-visible media set so exports
render identically to the live studio.

Event rows are JSON objects with server-stamped `id`, `ts`, `seq`, and `rev` when persisted.
Common frame types include `ready`, `changed`, `created`, `removed`, `graph`, `presence`, `comment`, `comment_link`, `activity`, `ping`, and `resync`.

`comment_link` is current behavior.
When resolving a comment with activity ids, the studio appends a back-link event carrying `activity_id`, `comment_id`, and `concept` so the change list can show which comment produced a change.

Polling fallback and replay ordering are part of the contract.
`/__data/events` defaults to newest-first for the change list.
When a client supplies `since`, replay must use `order=asc` so missed events are consumed oldest-to-newest after the cursor.
Newest-first reads are for initial history and activity panels, not cursor replay.

See [/reference/http_routes.md](/reference/http_routes.md).

## 12. Comment lifecycle and agent loop

Comments are directives stored in `<bundle>/.okf-loom/session/directives.jsonl`.
They drive the user-directs / agent-authors loop.

The lifecycle state is independent from archive visibility:

| Field | Values |
|---|---|
| `state` | `open`, `claimed`, `resolved`, `dismissed` |
| `archived` | `true`, `false` |

State ownership:

- Users create comments with `POST /__comment`.
- Agents claim comments with `scripts/okf-loom comment-claim`.
- Agents reply mid-thread with `scripts/okf-loom comment-reply` — the
  clarifying-question channel. The reply is threaded under the root,
  posted with `state=resolved` (a statement, not an ask) so `wait` never
  returns the agent its own reply, and the parent's lifecycle state is
  untouched: reply-without-resolve is the point.
- Agents resolve comments with `scripts/okf-loom comment-resolve`.
- Users dismiss or reopen comments with `/__comment-update`, and may edit
  the body of a not-yet-resolved comment (`/__comment-update` with
  `body`); the edit bumps `updated_at` (so a waiting agent re-receives
  the corrected ask) and re-derives `request_summary`.
- Archive is a soft hide and never replaces lifecycle state.

Current comment records include `id`, `ts`, `updated_at`, `concept`, `anchor`, `actor`, `body`, `state`, `claimed_by`, `resolved_activity`, `reply`, `summary`, `request_summary`, `detail`, `archived`, and optional `parent_id`.
`ts` is immutable creation time.
`updated_at` bumps on claim, resolve, dismiss, reopen, archive, unarchive, reply, and summary updates.
`summary` is the agent-authored done/status blurb; `request_summary` is the ask summary.

Threading is capped at two displayed levels: parent, reply, reply-to-reply.
Deeper nesting is hoisted to root.
Archive is root-only and server-enforced: every comment in the thread must be `resolved`, otherwise archive fails.
Replying to an archived thread auto-unarchives the parent.
Compatibility records with `state == "archived"` are normalised on read to `archived=true, state="open"`.

Canonical foreground agent loop:

```bash
scripts/okf-loom wait path/to/bundle --for comment
scripts/okf-loom comment-claim path/to/bundle 01JABED9K0 --summary "adding dependency link"
scripts/okf-loom comment-reply path/to/bundle 01JABED9K0 --body "Did you mean orders or order_items?"  # only when the ask is ambiguous
scripts/okf-loom presence path/to/bundle --state editing --focus tables/orders --message "linking 3 of 7 tables"
scripts/okf-loom link-add --bundle path/to/bundle --source tables/orders --target tables/customers --group-id PASS1
scripts/okf-loom comment-resolve path/to/bundle 01JABED9K0 \
  --activity 01JABED5K7 \
  --reply "Done - linked orders to customers." \
  --summary "done: link added"
```

`scripts/okf-loom wait` is block-once and foreground.
Do not background it; the caller is the consumer.
The returned comment carries a `queue` field — `{pending, ids}` for the
OTHER open comments still waiting — so the agent sees backlog depth
without diffing `comment-list` between loop turns; the one-work-item
contract itself is unchanged.
`scripts/okf-loom watch` is the continuous feed variant for a long-running consumer and can replay from `--since` before tailing live.

See [/reference/comment_lifecycle.md](/reference/comment_lifecycle.md) and [/reference/cli.md](/reference/cli.md).

## 13. Session storage, undo, and caps

Live studio session state is derived/ephemeral and lives under `<bundle>/.okf-loom/session/` by default.
Recommended project hygiene is to keep `.okf-loom/session/` out of git unless a harness intentionally snapshots it for debugging.

Current session shape:

| Path | Purpose | Cap / invariant |
|---|---|---|
| `.token` | Per-session CSRF token, mode `0600`. | Regenerated by `scripts/okf-loom serve`; treat as secret. |
| `server.json` | Running server address: `{host, port, pid, started, url, tunnel_url}`. | Written by `serve` at startup, refreshed on tunnel attach/detach, removed on clean shutdown. Lets `scripts/okf-loom tunnel` find the live port; stale after a crash (detected by the connect failing). |
| `events.jsonl` | Activity/change/comment/presence event feed. | Active file cap from `studio.events_max_bytes` default 8 MiB; `studio.events_keep` default 7 rotated files. |
| `directives.jsonl` | Append-only comment/directive state. | Active file cap 2 MiB; 3 files kept. |
| `presence.json` | Current presence snapshot. | Stale presence recovers to idle after configured/default TTL. |
| `history/<hex>/<rev>.md` | Per-concept undo snapshots. | Ring-capped; pruned snapshots make undo return not-found/pruned status. |
| `history/_groups/<group_id>/manifest.json` | Group undo manifest. | Safe tokenised group ids only. |
| `.idempotency/<key>.json` | POST retry cache for selected endpoints. | 10-minute TTL; max 10,000 files; unsafe keys ignored for caching. |
| `.last-logged.json` | Cross-process write/event dedup. | Internal session bookkeeping. |

`POST /__comment` and `POST /__undo` accept an optional `Idempotency-Key` header.
Within the TTL, the same key and same body replay the original response and avoid duplicate directive or undo effects.
The idempotency cache is opportunistically swept for TTL and file-count caps.

Undo is based on snapshots written before concept mutation.
Mutators support `--group-id` so several writes can be undone as one pass.
Every group id is path-token validated before it reaches history paths.

## 14. Trust and security model

The default trust boundary is loopback local use.
`scripts/okf-loom serve` binds `127.0.0.1` by default.
`--public` or a non-loopback host requires `--public-ack` or an interactive acknowledgement.

Guards:

- Every GET is public to the local server, except sensitive diff reads require the session token.
- Every POST requires `X-OKF-Token` and Origin/Host allow-list acceptance.
- `studio.allowed_hosts` defaults to `127.0.0.1` and `localhost` and is never empty.
- `--no-edit` makes the studio read-only; live reads stay on and mutating POST routes return `403`. `POST /__tunnel` is the one exemption (server admin, not a bundle write — sharing a read-only kiosk is its use case); it still requires the token and Origin/Host acceptance.
- Mutating bodies are JSON, size-capped, and schema-checked.
- `/__apply` accepts whitelisted update operations, not shell or arbitrary argv.
- Path-bearing config values must remain within the bundle root.
- Active viewer override/plugin code requires both bundle declaration and operator consent.
- The server embeds no LLM and does not talk to remote model providers.

These gates protect against other browser origins, DNS-rebinding-style abuse, malicious bundle code, accidental exposure, and data loss.
They are not a per-action leash on the user's own agent.

## 15. Extension API

There are three extension mechanisms:

1. Viewer file/template/static overrides under the configured viewer override directory.
2. Server-side `ViewerPlugin` hooks for render/build integration.
3. Client-side studio extensions via `window.okfLoomStudio.register(kind, config)`.

The current shipped `okfLoomStudio.register()` kinds are exactly:

| Kind | Current status | Contract |
|---|---|---|
| `panel` | Ships, wired | Adds a slide-over panel and studio-bar toggle. |
| `viewMode` | Ships, wired | Adds a read-only mode button to the Rendered/Source/Split switch. |
| `toolbar` | Reserved forward-compat | Accepted and warned, not wired. |
| `graphDecorator` | Reserved forward-compat | Accepted and warned, not wired. |
| `suggestionRenderer` | Reserved forward-compat | Accepted and warned, not wired. |

Reserved kinds must not be documented as usable current extension points.
They are accepted so future-targeted extensions degrade gracefully today, but the studio logs a warning stating the kind is reserved and not wired.

Example shipped panel extension:

```js
window.okfLoomStudio.register("panel", {
  id: "coverage",
  label: "Coverage",
  render(container, ctx) {
    container.textContent = "Open comments: " + ctx.state.comments.filter(c => c.state === "open").length;
  },
});
```

See `scripts/okf_loom/viewer/OVERRIDES.md` for the shipped extension reference.

## 16. Embedding in another harness

The repo is designed to be embedded by agents and harnesses without making okf-loom own the whole process.
Preferred embedding pattern:

1. Clone or vendor the repository.
2. Load root `SKILL.md` and focused resources from `resources/`.
3. Run CLI commands via source-checkout invocation.
4. Start `serve` only when an interactive studio is useful.
5. Let the external agent consume `scripts/okf-loom wait`, `scripts/okf-loom watch`, `/__events`, and `/__comments`.
6. Write through mutators or whitelisted update/apply paths.
7. Keep `.okf-loom/session/` ephemeral and out of normal commits.

Minimal Python read-only server embedding is possible with `ThreadingHTTPServer` and `OKFWikiHandler`, but mutating studio behavior requires deliberately attaching the same studio/session/token wiring that `scripts/okf-loom serve` creates.
Embedders that bind outside loopback must make the exposure decision explicitly.

See [/reference/embedding_guide.md](/reference/embedding_guide.md).

## 17. Verification expectations

A build-from-docs implementation must satisfy these acceptance checks:

- `okf_loom.SPEC_VERSION == "0.1"`, and `okf_loom.LOOM_VERSION` matches the v1.0 runtime version printed by `scripts/okf-loom --version`.
- `validate`, `info`, `graph`, `search`, `discover`, `plan`, `update`, `repair`, `index`, `log`, `init`, `bootstrap`, `import`, `serve`, `tunnel`, `wait`, `watch`, `token`, `comment-claim`, `comment-reply`, `comment-resolve`, `comment-list`, `presence`, `render`, `build`, `capabilities`, `upgrade`, `write-concept`, `set-frontmatter`, `link-add`, `entity-add`, `update-section`, and `replace-text` exist on the CLI surface.
- Validation profiles and `--fail-on-broken-links` match this spec.
- Governed metadata shape warnings and duplicate-relation findings are stable;
  raw relation occurrences and logical edges remain separately available.
- Search modes are dependency-free and restricted to the six current modes.
- Semantic/Hybrid relevance gates are opt-in, and Hybrid results expose their
  component evidence rather than presenting RRF as calibrated relevance.
- `okf-loom.config.yaml` fails closed on unknown enum, invalid bool, and path escape while preserving unknown keys.
- Mutators preserve unknown frontmatter, write atomically, and avoid duplicate/idempotency failures.
- Static graph and search work from both HTTP and direct `file://` builds.
- Live studio emits SSE/events, supports polling fallback, preserves no-refresh patching, and exposes the documented routes.
- `comment_link` events are emitted when a resolve links activity ids.
- `directives.jsonl`, `events.jsonl`, history, and idempotency caps prevent unbounded session growth.
- Comment archive is a separate boolean track, root-only, gated on whole-thread-resolved, and auto-unarchives on reply.
- `okfLoomStudio.register()` ships `panel` and `viewMode`; other documented kinds are reserved and warned.
- Public bind, active-code, no-edit, CSRF, Origin/Host, body-size, and path-containment gates are fail-closed.

Suggested repo-local verification commands:

```bash
scripts/okf-loom --version
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom info docs-bundle --format json
scripts/okf-loom search docs-bundle "current spec" --mode lexical --format json
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
```

## 18. Current source-of-truth references

This spec is intentionally self-contained for rebuilding current behavior.
Concise detailed references remain separate and should be kept aligned with this file:

- [/reference/cli.md](/reference/cli.md) for command flags and JSON output details.
- [/reference/http_routes.md](/reference/http_routes.md) for route status codes and request/response shapes.
- [/reference/comment_lifecycle.md](/reference/comment_lifecycle.md) for comment state and archive edge cases.
- [/reference/search_modes.md](/reference/search_modes.md) for ranking algorithms and result shape.
- [/reference/config_yaml.md](/reference/config_yaml.md) for config defaults and loader behavior.
- [/reference/capabilities.md](/reference/capabilities.md) for the current capability catalogue.
- [/reference/architecture.md](/reference/architecture.md) for module-level architecture.
- [/reference/embedding_guide.md](/reference/embedding_guide.md) for harness integration.
The upstream OKF SPEC v0.1 remains the base wire-format reference.
This file governs current okf-loom behavior on top of that base.
