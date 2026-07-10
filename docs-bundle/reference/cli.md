---
type: Reference
title: CLI command reference
description: Every `scripts/okf-loom` subcommand — purpose, key flags, exit codes, and output formats — for the okf-loom v1.0 baseline.
resource: /reference/cli.md
tags: [cli, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# CLI command reference

Run commands from this checkout as `scripts/okf-loom ...`.
From a parent workspace where this checkout is named `okf-loom`, use `okf-loom/scripts/okf-loom ...`.
Do not assume an installed console script.
Bundle and output-format arguments are command-specific; check each synopsis before generating commands.

okf-loom v1.0 implements upstream OKF SPEC `v0.1` as its wire-format baseline.
`scripts/okf-loom --version` prints `okf-loom 1.0.0 (SPEC v0.1)`.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

# Command summary

| Command | Purpose |
|---|---|
| [`info`](#info) | Bundle summary: concept/type/tag/link counts, version, extensions. |
| [`validate`](#validate) | SPEC §9 conformance + soft warnings; profiles, `--strict`, `--fail-on-broken-links`. |
| [`graph`](#graph) | Print the link graph (text / JSON / DOT). |
| [`graph-quality`](#graph-quality) | Advisory graph usefulness report; does not affect OKF conformance. |
| [`search`](#search) | Six modes behind one `--mode` flag. |
| [`discover`](#discover) | Find missing links/indexes/descriptions/relations/orphans. |
| [`plan`](#plan) | Build a reviewable, executable action plan. |
| [`capabilities`](#capabilities) | List or resolve the capability registry. |
| [`serve`](#serve) | Run the live collaborative studio (HTTP server). |
| [`watch`](#watch) | Headless change feed; no HTTP server. |
| [`wait`](#wait) | Foreground block-once for the next comment/change. |
| [`token`](#token) | Print the per-session CSRF token. |
| [`tunnel`](#tunnel) | Attach/detach a cloudflared quick tunnel on a RUNNING studio (no restart). |
| [`comment-claim`](#comment-claim) | Mark a comment `claimed` by the agent, with optional `--summary`. |
| [`comment-resolve`](#comment-resolve) | Mark a comment `resolved` with reply/activity links and optional `--summary`. |
| [`comment-reply`](#comment-reply) | Post a threaded reply under the thread root WITHOUT resolving the parent. |
| [`comment-list`](#comment-list) | List comments/directives, optionally filtered. |
| [`presence`](#presence) | Set agent presence (mirror of `POST /__presence`). |
| [`render`](#render) | Render a self-contained `viz.html`. |
| [`build`](#build) | Build a multi-file static site (spa / static / single-file). |
| [`index`](#index) | Regenerate SPEC §6 `index.md` files (marker-safe). |
| [`log`](#log) | Append to SPEC §7 `log.md` (append-only). |
| [`update`](#update) | Apply an update plan or action envelope (idempotent). |
| [`upgrade`](#upgrade) | Check or apply spec-version migrations. |
| [`bootstrap`](#bootstrap) | Scaffold an empty bundle (no config file). |
| [`import`](#import) | Import existing `.md` files into a bundle. |
| [`init`](#init) | Scaffold a bundle with `index.md`, `log.md`, `okf-loom.config.yaml`. |
| [`write-concept`](#write-concept) | Create or update a single concept file. |
| [`set-frontmatter`](#set-frontmatter) | Set one frontmatter key on a concept. |
| [`update-section`](#update-section) | Replace (or append to) ONE section of a concept body. |
| [`replace-text`](#replace-text) | Exact textual patch on a concept body (fail-closed). |
| [`link-add`](#link-add) | Add a markdown link (and optionally a typed relation). |
| [`entity-add`](#entity-add) | Add an entity to a concept's `entities:` list. |
| [`repair`](#repair) | Apply mechanical fixes (index regen + relation mirroring). |

# Common conventions

| Command shape | Commands |
|---|---|
| Positional `<bundle>` + `--format {text,json,md,dot}` | `info`, `validate`, `graph`, `search`, `discover`, `repair` |
| Positional `<bundle>` + `--format {text,json}` | `graph-quality`, `plan`, `tunnel`, `comment-claim`, `comment-resolve`, `comment-reply`, `comment-list`, `presence`, `index`, `update` |
| Optional/no positional bundle + `--format {text,json}` | `capabilities` uses optional `--bundle`; `init` requires `--bundle` |
| Positional or bundle option, no `--format` | `serve`, `watch`, `wait`, `token`, `render`, `build`, `log`, `upgrade` (`upgrade` accepts optional positional `<bundle>` or `--bundle`) |
| Non-bundle source/destination, no `--format` | `bootstrap <dest>`, `import <src> <dest>` |
| Authoring verbs | `write-concept`, `set-frontmatter`, `update-section`, `replace-text`, `link-add`, `entity-add` use `--bundle` and support `--format {text,json}` |

| Convention | Detail |
|---|---|
| `--group-id` / `--actor` | Mutators accept these for one-click group undo and change-list attribution. Active only when a live studio session exists; otherwise the non-studio atomic write path is used. |
| `--dry-run` | Preview the change without writing (where applicable). |
| Atomic writes | Every file mutation goes through tmp + rename (AGENTS.md hard rule #8). |
| Studio detection | Mutators detect the configured studio session directory (`.okf-loom/session/` by default) and route through `Studio.save_concept` (attribution + undo + events + SSE). Without a session they take the non-studio atomic write path. |
| Exit `0` | Success (or `scripts/okf-loom wait` returned work). |
| Exit `1` | Conformance failure (`validate`), `scripts/okf-loom wait --timeout` expired with no work, or a fail-closed mutator refusal (`section_not_found`, `text_ambiguous:N`, missing link target, …). |
| Exit `2` | Strict-mode failure (`validate --strict`); `scripts/okf-loom init` on a non-empty destination; `scripts/okf-loom serve --public` on a non-TTY without `--public-ack`; `scripts/okf-loom tunnel` with no live `server.json`. |

# info

```bash
scripts/okf-loom info <bundle> [--format {text,json,md,dot}]
```

Bundle summary: concept/type/tag/link counts, declared `okf_version`,
declared `okf_extensions`, and any load warnings.
First command to run against an unfamiliar bundle.

See [/tutorials/first_bundle.md](/tutorials/first_bundle.md).

# validate

```bash
scripts/okf-loom validate <bundle> [--strict] [--profile {spec,producer,loose}]
            [--fail-on-broken-links] [--checks CHECKS]
            [--format {text,json,md,dot}]
```

| Flag | Effect |
|---|---|
| `--strict` | Treat warnings as errors (exit `2` instead of `0`). |
| `--profile spec` | Default; only `type` is hard-required (SPEC §9). |
| `--profile producer` | Promote `concept.missing_recommended_keys` to ERROR. |
| `--profile loose` | Downgrade `concept.missing_type` to WARNING (migration aid). |
| `--fail-on-broken-links` | Promote `link.broken` to ERROR; composes with any profile. |
| `--checks a,b` | Run a subset. Category names (`link_integrity`, `concept_required_keys`, …) or dotted codes (`link.broken`, `concept.missing_type`). |

Exit `0` ok, `1` conformance fail, `2` strict fail.
See [/how-to/validate_in_ci.md](/how-to/validate_in_ci.md).

# graph

```bash
scripts/okf-loom graph <bundle> [--format {text,json,md,dot}]
```

Print the link graph. Wikilinks (`[[…]]`) are emitted as edges with
`form="wikilink"`. `dot` output renders with Graphviz; `json` is the
machine-readable shape consumed by the viewer. JSON nodes include allowlisted
grouping metadata such as `graph_cluster` and `source_system` when present.

# graph-quality

```bash
scripts/okf-loom graph-quality <bundle> [--format {text,json}]
```

Emit an advisory report about whether the bundle is likely to be useful as a
graph. This is not `validate`: valid OKF stays valid. Findings include generic
type overuse, duplicate or near-duplicate titles, sparse optional metadata,
weak hierarchy signals, disconnected components, and orphan concepts.

# search

```bash
scripts/okf-loom search <bundle> [query] [--mode {lexical,semantic,hybrid,tag,entity,relation}]
            [--type TYPE] [--tag TAG] [--limit N]
            [--min-semantic-score N]
            [--hybrid-require {any,lexical,semantic,both}]
            [--relation RELATION] [--source SOURCE] [--target TARGET]
            [--format {text,json,md,dot}]
```

`query` is optional in `relation` mode (edge filters suffice).
`--min-semantic-score` is an opt-in cosine floor for SemanticLite and is
applied before RRF in Hybrid. `--hybrid-require` selects which component
backend(s) must match; `any` preserves the legacy union. JSON Hybrid results
include matched backends plus native component scores and ranks.
Full mode-by-mode detail lives in [search_modes.md](/reference/search_modes.md).

# discover

```bash
scripts/okf-loom discover <bundle> [--rules RULES] [--out OUT]
            [--scope SCOPE] [--neighbors] [--min-confidence N]
            [--include-low-confidence] [--format {text,json,md,dot}]
```

Emit a structured report of gaps the managing agent may fix.
Suggestions carry a rule, severity, message, target concept, and an
`action` verb phrase. JSON output includes suppressed suggestions and
`suppressed_reason_counts` so filtered data remains inspectable. It also
includes `actionability_counts` and an `actionability` object that groups
items into buckets such as `safe_to_apply`, `needs_review`,
`suppressed_existing_relation`, `suppressed_generic_label`,
`suppressed_cross_cluster`, and `low_confidence`.

| Flag | Effect |
|---|---|
| `--rules a,b` | Subset of rules: `unlinked_mentions`, `missing_indexes`, `missing_descriptions`, `broken_links`, `missing_relations_hint`, `orphan_concepts`. |
| `--out plan.json` | Write the JSON report to this path. |
| `--scope ID,ID` | Restrict rules to named concept ids (current spec §7). |
| `--neighbors` | Expand `--scope` to 1-hop graph neighbours (in + out edges). |
| `--min-confidence N` | Minimum confidence for unlinked-mention suggestions (default `0.5`). |
| `--include-low-confidence` | Keep low-confidence suggestions in the main list for broad audits. |

`unlinked_mentions` suppresses suggestions already covered by structured
`relations:` metadata (`already_structurally_related`) and lowers confidence
for generic labels such as `Architecture`, `README`, or `Implementation Plan`
unless source and target share strong context such as `graph_cluster` or
folder. Mention details include `location_counts` and `occurrence_locations`
with locations `frontmatter`, `h1`, `heading`, `table`, and `body`; matches
seen only in frontmatter, headings, or tables are lower confidence than body
prose.
Bundle-specific editorial suppressions live in `okf-loom.config.yaml` under
`discover.suppress_phrases` and `discover.suppress_pairs`; configured
suppression reasons are emitted as `configured_phrase` and `configured_pair`.

The agent applies fixes directly via the mutators; the reviewable-plan
workflow (`discover` → `plan` → `update`) is still available.
See [/how-to/discover_and_fix_gaps.md](/how-to/discover_and_fix_gaps.md).

# plan

```bash
scripts/okf-loom plan <bundle> [--rules RULES] [--out OUT]
            [--scope SCOPE] [--neighbors] [--portable] [--format {text,json}]
```

Build a reviewable, executable action plan from discovery + index
staleness + typed-relation mirroring.

| Flag | Effect |
|---|---|
| `--portable` | Emit bundle-relative argv (`--bundle .`) so the plan is portable across machines. Replay from the bundle directory. |
| `--scope ID,ID` / `--neighbors` | Scope the plan (fast per-change enrichment). |

Mechanical actions carry a runnable `argv`; agent-decision actions
carry an `argv_template` with `<PLACEHOLDER>` slots, a `confidence`
score, and an `agent_instruction`.

# capabilities

```bash
scripts/okf-loom capabilities [--bundle BUNDLE] [--format {text,json}]
```

Without `--bundle`, list every capability okf-loom knows.
With `--bundle`, resolve which are active for that bundle (declared
under `okf_extensions:` plus auto-activated by observed keys).
See [capabilities.md](/reference/capabilities.md).

# serve

```bash
scripts/okf-loom serve <bundle> [--host HOST] [--port PORT] [--no-watch] [--no-open]
            [--allow-active-code] [--no-edit] [--tunnel] [--public]
            [--public-ack] [--no-watch-ui]
```

Run the live collaborative studio. Default bind is `127.0.0.1:8787`.

| Flag | Effect |
|---|---|
| `--host HOST` | Bind host. A non-loopback host implies `--public` semantics. |
| `--port PORT` | Bind port (default `8787`). |
| `--no-watch` | Disable the file watcher (no live reload on disk change). |
| `--no-open` | Do not open a browser window on start. |
| `--allow-active-code` | Operator consent enabling viewer overrides + plugins for bundles that declare `viewer.allow_active_code: true`. Default defers to `OKF_LOOM_ALLOW_ACTIVE_CODE`. |
| `--no-edit` | Read-only kiosk: live reads stay on; all POST endpoints are off (current spec §9 and §14). |
| `--tunnel` | Also start a Cloudflare quick tunnel (requires `cloudflared` on `PATH`) and print a public `https://…trycloudflare.com` URL. The tunnel host is added to `allowed_hosts` at runtime so comments/edits work through the link; the server itself stays on loopback. Anyone with the link can read the bundle. |
| `--public` | Bind `0.0.0.0`. Requires `--public-ack` or interactive y/N ack; on a non-TTY without ack, exit `2`. |
| `--public-ack` | Non-interactive acknowledgement of the trust-boundary change implied by `--public`. |
| `--no-watch-ui` | Disable SSE live push server-side (`/__events` returns 503). |

Writes `<bundle>/<studio.session_dir>/.token` (`.okf-loom/session/.token` by default, mode `0600`) on boot.
Also writes `<session>/server.json` (`{host, port, pid, started, url, tunnel_url}`) at
startup — refreshed on tunnel attach/detach, removed on clean shutdown —
which [`tunnel`](#tunnel) reads to find the running server.
See [http_routes.md](/reference/http_routes.md),
[comment_lifecycle.md](/reference/comment_lifecycle.md),
[/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md),
and [/explanation/live_studio_design.md](/explanation/live_studio_design.md).

# watch

```bash
scripts/okf-loom watch <bundle> [--emit {jsonl,text}] [--since SINCE]
            [--auto-repair] [--debounce-ms DEBOUNCE_MS]
```

Headless change feed for the agent loop. Prints one event per line;
no HTTP server. Reuses the same `events.jsonl` append feed `serve` uses.

| Flag | Effect |
|---|---|
| `--emit jsonl` | Default; the §7.2 event schema, one JSON object per line. |
| `--emit text` | Human-readable one-line summaries. |
| `--since <id>` | Replay from `events.jsonl` after this id, then tail live (reconnect-safe). |
| `--auto-repair` | Run mechanical `scripts/okf-loom repair` (index regen + relation mirroring) on change, debounced. |
| `--debounce-ms N` | Coalesce auto-repair runs by this many ms (default `600`). |

# wait

```bash
scripts/okf-loom wait <bundle> [--for {comment,change} ...] [--since SINCE]
            [--timeout TIMEOUT] [--interval INTERVAL]
```

Foreground block-once primitive (current spec §12). Blocks until a new OPEN
comment and/or change event arrives, prints it as JSON, exits.
Run this in the agent's foreground; never background it.

| Flag | Effect |
|---|---|
| `--for comment` | Wait for a new OPEN user comment (default). |
| `--for change` | Wait for a new `changed`/`created`/`removed` event (proactive enrichment). |
| `--for comment change` | Whichever arrives first. |
| `--since <id>` | Skip the backlog; wait only for items whose id sorts after this. |
| `--timeout <s>` | Max seconds to wait; exit `1` on timeout with no work. Default: block indefinitely. |
| `--interval <s>` | Poll interval (default `0.5`). |

Without `--since`, the first call drains the oldest pre-existing open
comment; subsequent calls drain the rest, then settle into wait-for-new.

A returned comment carries a `queue: {pending: N, ids: [...]}` field —
the other open comments still pending — so the agent sees backlog depth
without diffing `comment-list` between loop turns. The one-work-item
contract (block once, print one item, exit) is unchanged.

# token

```bash
scripts/okf-loom token <bundle>
```

Print the per-session CSRF token the studio wrote to the configured session
directory (`<bundle>/.okf-loom/session/.token` by default, mode `0600`). Use only when POSTing
to `/__apply` / `/__undo` / `/__presence` / `/__comment` over HTTP
directly; the CLI mutators attach the token automatically.
Exits non-zero with a clear message if no session exists.

# tunnel

```bash
scripts/okf-loom tunnel <bundle> [--stop] [--status] [--format {text,json}]
```

Attach a cloudflared quick tunnel to an ALREADY RUNNING studio session
— no restart, no lost session token, no dropped SSE clients. Reads the
live server's address from `<session>/server.json` plus the session
`.token`, then drives the token-guarded `POST /__tunnel` admin route
(see [http_routes.md](/reference/http_routes.md)). Startup-time
alternative: [`serve --tunnel`](#serve).

| Flag | Effect |
|---|---|
| `--stop` | Detach the running tunnel and restore the pre-tunnel `allowed_hosts`. |
| `--status` | Print the current tunnel URL (or none) and exit. |

On start, the tunnel hostname joins `allowed_hosts` immediately
(checked per request), so comments/edits work through the link without
a restart. The tunnel process belongs to the serve process and dies
with it. Anyone with the link can READ the bundle; studio writes still
require the per-session token.

No `<session>/server.json` (no live server): exit `2`. Connection
refused (stale session state; server crashed): diagnostic + exit `1`.
`cloudflared` missing or slow to come up: the server answers `502` and
the CLI exits `1`.

# comment-claim

```bash
scripts/okf-loom comment-claim <bundle> <comment_id> [--actor ACTOR]
            [--summary SUMMARY] [--no-presence] [--format {text,json}]
```

Mark a comment `claimed` by the agent. Sets `claimed_by`; auto-flips
presence to `editing <concept>` unless `--no-presence` is given. The
auto-presence carries the claim summary as the presence `message`, so
the user sees WHAT is being edited, not just that editing happens.

| Flag | Effect |
|---|---|
| `--actor ACTOR` | Who is claiming (default `agent`). |
| `--summary SUMMARY` | Short agent-authored blurb shown in the collapsed thread preview. |
| `--no-presence` | Skip the automatic presence flip to `editing`. |

Canonical loop: `scripts/okf-loom wait` → `scripts/okf-loom comment-claim` → mutators →
`scripts/okf-loom comment-resolve`.

# comment-resolve

```bash
scripts/okf-loom comment-resolve <bundle> <comment_id> [--reply REPLY] [--summary SUMMARY]
            [--activity ACTIVITY] [--actor ACTOR] [--format {text,json}]
```

Mark a comment `resolved` with an optional reply and activity links.

| Flag | Effect |
|---|---|
| `--reply REPLY` | Long-form resolution message shown in the change list. |
| `--summary SUMMARY` | Short agent-authored blurb shown in the collapsed preview (separate from `--reply`). |
| `--activity ID,ID` | Comma-separated change-list entry ids the resolution produced (drives one-click group undo). |
| `--actor ACTOR` | Who is resolving (default `agent`). |

# comment-reply

```bash
scripts/okf-loom comment-reply <bundle> <comment_id> [--body BODY] [--body-file BODY_FILE]
            [--actor ACTOR] [--format {text,json}]
```

Post a threaded reply WITHOUT resolving the comment — the
clarifying-question channel for an ambiguous ask. `--body` (or
`--body-file`) is required and must be non-empty.

| Flag | Effect |
|---|---|
| `--body BODY` | Reply text shown in the thread. |
| `--body-file FILE` | Read the reply text from this file. |
| `--actor ACTOR` | Who is replying (default `agent`). |

The reply lands under the thread **root** (replying to a reply hoists
to the root, matching the 2-level display cap) and never touches the
parent's lifecycle state. It is created with `state=resolved` and
`claimed_by=<actor>` (a statement, not an ask), so
[`wait --for comment`](#wait) never returns the agent its own reply
and thread archive (all-resolved) stays satisfiable. Unknown comment
id: exit `1`. The user's answer — their reply — wakes `wait` as new
work.

# comment-list

```bash
scripts/okf-loom comment-list <bundle> [--state {open,claimed,resolved,dismissed,archived}]
            [--concept CONCEPT] [--format {text,json}]
```

List directives from the configured session directory (`<bundle>/.okf-loom/session/directives.jsonl` by default),
optionally filtered. Each row carries `archived: bool` (separate
track), `summary: str|null`, `ts` (immutable creation time), and
`updated_at` (bumped on every transition).
See [comment_lifecycle.md](/reference/comment_lifecycle.md) and
[/how-to/archive_threads.md](/how-to/archive_threads.md).

# presence

```bash
scripts/okf-loom presence <bundle> [--state STATE] [--focus FOCUS] [--message MESSAGE]
            [--actor ACTOR] [--format {text,json}]
```

CLI mirror of `POST /__presence`.

| Flag | Effect |
|---|---|
| `--state STATE` | One of `idle`, `watching`, `thinking`, `editing` (default `idle`). |
| `--focus FOCUS` | Concept id the agent is focused on. |
| `--message MESSAGE` | Free-text progress line (max 200 chars; longer is truncated) rendered next to the presence chip and in the presence history. Re-post to update it mid-pass (`"linking 3 of 7 tables…"`). |
| `--actor ACTOR` | Who is setting presence (default `agent`). |

Stale presence is auto-recovered: a watcher sweep reverts to `idle`
after `studio.presence_ttl_seconds` (default `300`).

# render

```bash
scripts/okf-loom render <bundle> [--out OUT] [--name NAME] [--allow-active-code]
```

Render one self-contained `viz.html` (Cytoscape.js + stdlib markdown
renderer; embeds the bundle as JSON). Suitable for `file://`,
email, or attaching to a ticket.

# build

```bash
scripts/okf-loom build <bundle> [--target {spa,static,single-file}] [--out OUT]
            [--name NAME] [--allow-active-code]
```

| Target | Output |
|---|---|
| `spa` | Quartz-style default for published reading. |
| `static` | Server-rendered, JS-optional, printable, SEO/airgapped-friendly. |
| `single-file` | One portable HTML with everything embedded (same shape as `render`). |

`--allow-active-code` is the operator-consent gate for build-time
viewer overrides + plugins.
See [/how-to/build_static_site.md](/how-to/build_static_site.md).

# index

```bash
scripts/okf-loom index <bundle> [--dry-run] [--frozen] [--emit-json] [--format {text,json}]
```

Regenerate SPEC §6 `index.md` files (marker-safe).

| Flag | Effect |
|---|---|
| `--dry-run` | Return a plan: `would_write`, `would_skip_hand_authored`, `would_skip_unchanged`. |
| `--frozen` | CI mode: raise instead of writing to hand-authored files. |
| `--emit-json` | Write portable `content.json` + `graph.json` under `<bundle>/.okf-loom/index/`. |

Safety: hand-authored `index.md` (no `generated: true` marker and no
`<!-- okf:generated:index begin/end -->` markers) is never overwritten.
Generated indexes rewrite only the marked region.

# log

```bash
scripts/okf-loom log <bundle> --append "…" [--kind KIND] [--date DATE]
            [--log-path LOG_PATH] [--dry-run]
```

Append to SPEC §7 `log.md` (always append-only).

| Flag | Effect |
|---|---|
| `--append TEXT` | Entry text to append under today's date. |
| `--kind KIND` | Bold-tag prefix: `Update`, `Creation`, `Deprecation`, `Initialization`. |
| `--date DATE` | Override the date heading (YYYY-MM-DD; default today). |
| `--log-path PATH` | Path to `log.md` relative to the bundle. |

# update

```bash
scripts/okf-loom update <bundle> --plan PLAN [--dry-run] [--group-id GROUP_ID]
            [--actor ACTOR] [--format {text,json}]
```

Apply an update plan or action envelope (idempotent). Accepts both
`{"ops":[...]}` (the original plan shape) and `{"actions":[...]}`
(the envelope emitted by `plan`). Template actions with unfilled
`<...>` slots are skipped with reason `needs_agent_input`.

# upgrade

```bash
scripts/okf-loom upgrade [<bundle>] [--bundle BUNDLE_FLAG] [--check] [--apply] [--to-spec TO_SPEC]
```

Spec-version migration tooling. `--check` previews pending migrations;
`--apply` runs them. Migrations are idempotent transformers reusing
the same parse → modify → serialize pipeline. No migrations are
defined for SPEC `v0.1` (it is the baseline).

See [/how-to/migrate_spec_version.md](/how-to/migrate_spec_version.md).

# bootstrap

```bash
scripts/okf-loom bootstrap <dest> [--name NAME]
```

Scaffold an empty bundle at `<dest>`. Creates the directory and a
minimal root `index.md`. Does NOT write `okf-loom.config.yaml`
(use [`init`](#init) for that).

# import

```bash
scripts/okf-loom import <src> <dest> [--default-type DEFAULT_TYPE] [--overwrite]
```

Import existing `.md` files from `<src>` into a new bundle at `<dest>`,
injecting `type:` frontmatter where missing.

| Flag | Effect |
|---|---|
| `--default-type TYPE` | Default concept type for files without a type guess (default `Reference`). |
| `--overwrite` | Overwrite existing destination files (default: skip). |

# init

```bash
scripts/okf-loom init --bundle BUNDLE [--name NAME] [--format {text,json}]
```

Scaffold a new bundle at `<bundle>` (must not exist or be empty; exit
`2` otherwise). Writes `index.md`, `log.md`, AND a commented-defaults
`okf-loom.config.yaml`.

# write-concept

```bash
scripts/okf-loom write-concept --bundle BUNDLE --id ID --type TYPE
            [--title TITLE] [--description DESCRIPTION] [--resource RESOURCE]
            [--timestamp TIMESTAMP] [--tag TAG] [--body BODY] [--body-file BODY_FILE]
            [--force] [--no-defaults] [--format {text,json}]
            [--group-id GROUP_ID] [--actor ACTOR]
```

Create or update a single concept (current spec §7). `--id` is the concept
id (e.g. `tables/orders`); the file path is derived. `--type` is
required. Updating merges frontmatter (only the passed keys are
touched; unknown keys are preserved). The body is replaced only if
`--body`/`--body-file` is given AND the existing body is empty;
otherwise `--force` is required to overwrite prose.

On CREATE, the mechanically derivable recommended keys are defaulted so
the new concept passes `validate --strict` without follow-up
`set-frontmatter` calls: `resource` defaults to the bundle-absolute
concept path (e.g. `/topics/x.md`) and `timestamp` to now UTC
(ISO-8601 `Z`). Explicit `--resource`/`--timestamp` win; UPDATE never
injects defaults; `--no-defaults` opts out. The JSON result carries
`defaults_applied: ["resource", "timestamp"]` when defaults fired.
Content keys (title, description, tags) are never auto-invented.

Refuses reserved filenames (`index.md`, `log.md`) case-insensitively.
See [/how-to/author_with_verbs.md](/how-to/author_with_verbs.md) and
[frontmatter.md](/reference/frontmatter.md).

# set-frontmatter

```bash
scripts/okf-loom set-frontmatter --bundle BUNDLE --id ID --key KEY --value VALUE
            [--json-value] [--dry-run] [--format {text,json}]
            [--group-id GROUP_ID] [--actor ACTOR]
```

Set one frontmatter key on a concept. `--json-value` parses the value
as JSON (lists/objects). Thin wrapper over the `set_frontmatter`
update op.

# update-section

```bash
scripts/okf-loom update-section --bundle BUNDLE --id ID --heading HEADING
            [--body BODY] [--body-file BODY_FILE] [--append] [--create-if-missing]
            [--dry-run] [--format {text,json}] [--group-id GROUP_ID] [--actor ACTOR]
```

Block-level partial update: replace (or append to) ONE section of a
concept body, keeping the rest of the document — and all frontmatter —
untouched. Exactly one of `--body`/`--body-file` is required. Thin
wrapper over the `update_section` update op.

| Flag | Effect |
|---|---|
| `--heading HEADING` | Section to target. Level-pinned (`"## Banking"`) or bare (`"Banking"`, matches any level); case-insensitive. |
| `--append` | Append to the end of the section instead of replacing it. |
| `--create-if-missing` | Create the section at the end of the body when the heading is absent (success reason `section_created`). |

The section span includes its subsections (up to the next heading of
the same or higher level). A leading copy of the target heading in the
fragment is stripped, so a fragment file may repeat the heading.
Headings inside fenced code blocks are ignored.

| Outcome | Reason | Exit |
|---|---|---|
| Heading absent (without `--create-if-missing`). | `section_not_found` | `1` |
| Duplicate matches — pin the level to disambiguate. | `section_ambiguous:N` | `1` |
| Content already as requested (idempotent no-op). | `section_unchanged` | `0` |

# replace-text

```bash
scripts/okf-loom replace-text --bundle BUNDLE --id ID
            [--old OLD] [--old-file OLD_FILE] [--new NEW] [--new-file NEW_FILE]
            [--all] [--dry-run] [--format {text,json}]
            [--group-id GROUP_ID] [--actor ACTOR]
```

Exact textual patch on a concept body: swap `--old` for `--new`.
Exactly one of `--old`/`--old-file` and exactly one of
`--new`/`--new-file` is required. Thin wrapper over the `replace_text`
update op. On success the reason string is `replaced N occurrence(s)`.

| Flag | Effect |
|---|---|
| `--all` | Replace every occurrence (default: more than one match fails closed). |

| Outcome | Reason | Exit |
|---|---|---|
| Zero matches. | `text_not_found` | `1` |
| More than one match without `--all` — add surrounding context to `--old` to pin one occurrence. | `text_ambiguous:N` | `1` |
| `--old` equals `--new` (idempotent no-op). | `same_value` | `0` |

# link-add

```bash
scripts/okf-loom link-add --bundle BUNDLE --source SOURCE --target TARGET
            [--label LABEL] [--section SECTION] [--relation RELATION]
            [--evidence EVIDENCE] [--allow-forward-reference] [--dry-run]
            [--format {text,json}] [--group-id GROUP_ID] [--actor ACTOR]
```

Append a markdown link in `--section` (or end of body). Always emits
the SPEC §5.1 absolute form (`/tables/customers.md`).

| Flag | Effect |
|---|---|
| `--relation TYPE` | Also append a typed relation to the source's `relations:` list (activates `okf.cap.typed_relations`). |
| `--evidence TEXT` | Becomes the relation `detail`. |
| `--allow-forward-reference` | Permit a target that does not yet exist (SPEC §3 forward reference). Default: fail (exit `1`) on a missing target. |

See [links.md](/reference/links.md).

# entity-add

```bash
scripts/okf-loom entity-add --bundle BUNDLE --id ID --label LABEL [--kind KIND]
            [--entity-id ENTITY_ID] [--alias ALIAS] [--dry-run]
            [--format {text,json}] [--group-id GROUP_ID] [--actor ACTOR]
```

Append an entity object to a concept's `entities:` frontmatter list
(activates `okf.cap.entities`). Idempotent on `(label, kind)`:
re-running will not duplicate. `--alias` is repeatable; aliases feed
entity/semantic search.

# repair

```bash
scripts/okf-loom repair <bundle> [--apply] [--indexes] [--mirror-relations] [--all]
            [--group-id GROUP_ID] [--actor ACTOR] [--format {text,json,md,dot}]
```

Mechanical fixes only (current spec §7). Dry-run by default; pass `--apply`
to write. `--all` is the default scope when no filter is given.
Relation mirroring runs **before** index regen so regenerated
indexes reflect the new links.

# Output formats

When supported, `--format` accepts `text` (default) and `json` (the
agent/harness integration path).
Only `info`, `validate`, `graph`, `search`, `discover`, and `repair`
also accept `md` and `dot`.
Commands without `--format` either emit their command-specific text/JSON
shape (`wait` prints one JSON work item; `watch` uses `--emit`) or write
files as their primary output.

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [frontmatter.md](/reference/frontmatter.md) — the frontmatter contract.
- [links.md](/reference/links.md) — link forms and graph edges.
- [capabilities.md](/reference/capabilities.md) — capability registry.
- [search_modes.md](/reference/search_modes.md) — search backends.
- [http_routes.md](/reference/http_routes.md) — HTTP server routes.
- [comment_lifecycle.md](/reference/comment_lifecycle.md) — comment state model.
- [config_yaml.md](/reference/config_yaml.md) — `okf-loom.config.yaml` keys.
- `scripts/okf-loom <command> --help` — authoritative per-command help.
- [`/reference/spec.md`](/reference/spec.md) — current build-from-docs specification.
