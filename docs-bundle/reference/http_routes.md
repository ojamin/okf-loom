---
type: Reference
title: HTTP server routes
description: Every route the live studio exposes — method, purpose, body shape, response shape, and status codes.
resource: /reference/http_routes.md
tags: [http, studio, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# HTTP server routes

`scripts/okf-loom serve <bundle>` runs a `ThreadingHTTPServer` with the
`OKFWikiHandler` request handler. The handler serves read routes
unconditionally and — when a studio session is live (`.okf-loom/session/`
exists) — accepts mutating POST routes behind a per-session CSRF
token and an Origin/Host allow-list.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

Default bind is `127.0.0.1:8787`. A non-loopback bind (`--public` or
a non-loopback `--host`) requires `--public-ack` or an interactive
y/N ack. See [cli.md § serve](cli.md#serve).

# Route summary

| Method | Route | Purpose |
|---|---|---|
| GET | `/__api/v1` | Versioned route/capability discovery and apply argument schemas; no secrets. |
| GET | `/__health` | Readiness and content-reload status. |
| GET | `/` | Root index page (top-level concepts + subdirs + graph link). |
| GET | `/<concept_id>` | Concept page (rendered MD, frontmatter, backlinks, outgoing, local graph). |
| GET | `/<dir>/` | Directory index page. |
| GET | `/__graph` | Full-page Cytoscape.js force-directed graph. |
| GET | `/__search` | Search results (`&format=json` for JSON; `mode=lexical|semantic|hybrid|tag|entity|relation`). |
| GET | `/__raw/<concept_id>` | Raw markdown body. |
| GET | `/__data/graph.json` | Graph JSON for external tools. |
| GET | `/__data/content.json` | Full content index JSON. |
| GET | `/__data/doc?id=` | One concept's `{rev, html, raw, source, frontmatter, backlinks, outgoing, headings}` for in-place re-render. |
| GET | `/__data/events` | Filtered change-list read over `events.jsonl`. |
| GET | `/__events` | SSE stream of change/presence/activity/comment events. |
| GET | `/__comments` | Canonical comment state from `directives.jsonl`. |
| GET | `/__diff` | Line diff between two revs of one concept. |
| GET | `/__static/<file>` | Static assets (JS/CSS; bundle overrides first when active-code is on). |
| GET | `/<path>.<media ext>` | Bundle-local media file (screenshots, diagrams, video, PDF). |
| POST | `/__comment` | User's comment / threaded reply → `directives.jsonl`. |
| POST | `/__comment-update` | User-facing state/archive transition, or a body edit of a not-yet-resolved comment. |
| POST | `/__claim` | Claim `{id, actor, summary?}`; a competing owner or terminal state returns 409. |
| POST | `/__resolve` | Resolve `{id, summary?, activity?, reply?}` using the existing comment lifecycle. |
| POST | `/__presence` | Agent presence (idle/watching/thinking/editing) + optional progress `message`. |
| POST | `/__apply` | Run a whitelisted `UpdateOp` via the studio write funnel. |
| POST | `/__save` | Save exact Markdown source with mandatory revision checks and validation. |
| POST | `/__undo` | Restore a prior snapshot (single or group). |
| POST | `/__preview` | Render arbitrary in-flight markdown. |
| POST | `/__tunnel` | Server admin: attach/detach/status a cloudflared quick tunnel at runtime. |

Routing normalises a trailing slash (except root) and a trailing
`.md` on concept routes (except `/index.md` and `<dir>/index.md`,
which are reserved). So `/tables/customers.md`,
`/tables/customers`, and `/tables/customers/` all resolve to the
same concept page.

# Authentication model

| Surface | Guard |
|---|---|
| Every GET | None (loopback bind by default). |
| `GET /__diff` | Per-session CSRF token (`X-OKF-Token`). |
| Every POST | Per-session CSRF token **AND** Origin/Host ∈ `studio.allowed_hosts`. |

The token is generated on `scripts/okf-loom serve` boot, written to
`<bundle>/.okf-loom/session/.token` (mode `0600`), embedded in every
served HTML page (CSP-safe JSON data block), and read by the
studio client which attaches it as `X-OKF-Token` on every write.
[`scripts/okf-loom token <bundle>`](cli.md#token) prints it for direct HTTP use;
the CLI mutators attach it automatically.

`allowed_hosts` defaults to `("127.0.0.1", "localhost")` and is
never empty (fail-closed). A `user` actor's presence write only
takes effect when the current agent presence is already `idle` or
`watching`, so a browser boot cannot clobber an active agent.

`--no-edit` flips the studio into a read-only kiosk: live reads
stay on, every POST returns `403` — except `/__tunnel`, which is
server admin (not a bundle write; sharing a read-only kiosk is its use
case) and stays available. It is never exempt from the token +
Origin/Host guard.

# GET routes

## `/`

Root index page. Renders the bundle-root `index.md` (hand-authored
or generated) plus the type-grouped concept listing. The studio
bootstrap (token + asset links) is injected into the `</head>`.

## `/<concept_id>`

Concept page. `concept_id` is the dotted path without `.md`
(e.g. `/tables/orders`). Returns 404 for an unknown concept unless
the path matches a directory containing concepts, in which case it
falls back to the directory index page.

The page carries: rendered body HTML, frontmatter, the "Cited by"
backlinks panel, outgoing links, and a local-graph widget.

## `/<dir>/`

Directory index page for the subdirectory `<dir>`. Renders the
`<dir>/index.md` if present, else a generated listing. Path
segments are validated; `..`, `.`, and NUL bytes return 404.

## `/__graph`

Full-page Cytoscape.js force-directed graph of the whole bundle.
The graph data comes from `/__data/graph.json`.

## `/__search`

```http
GET /__search?q=customer&format=json
```

| Query param | Effect |
|---|---|
| `q` | The query string. Capped at `MAX_SEARCH_QUERY_CHARS`; over-long queries are truncated, not rejected. |
| `format` | `html` (default) or `json`. |

Pass `mode=lexical|semantic|hybrid|tag|entity|relation` to choose a search
backend; the default is lexical. Results are limited to `30`. JSON shape matches the
`SearchResult` shape in [search_modes.md § Result shape](search_modes.md).

## `/__raw/<concept_id>`

Raw markdown body of one concept (`Content-Type: text/markdown`).
Used by popover previews and agent copy/paste. Returns 413 if the
body exceeds `MAX_RAW_RESPONSE_BYTES`.

## `/__data/graph.json`

Graph JSON: nodes (one per concept) + edges (one per internal link,
including wikilinks with `form="wikilink"`). Same shape as
[`graph --format json`](cli.md#graph).

## `/__data/content.json`

Full content index JSON: every concept's id, type, title,
description, tags, and outgoing link targets. Same shape as the
`.okf-loom/index/content.json` derived artifact emitted by
[`index --emit-json`](cli.md#index).

## `/__data/doc?id=<concept_id>`

One concept's rendered + raw + metadata JSON for in-place re-render
(the §6 no-refresh live update path).

```json
{
  "id": "tables/orders",
  "rev": "<content-hash>",
  "title": "Orders",
  "type": "Table",
  "html": "<rendered body HTML>",
  "raw": "<raw markdown body>",
  "frontmatter": { "type": "Table", "title": "Orders", "..." : "..." },
  "headings": [{"level": 1, "text": "Schema"}, "..."],
  "backlinks": ["services/checkout"],
  "outgoing": ["tables/customers", "tables/products"]
}
```

Returns 400 if `id` is absent, 404 if the id is malformed or
unknown.

## `/__data/events`

```http
GET /__data/events?since=<id>&limit=200&actor=agent&concept=tables/orders&order=desc
```

Filtered read of `events.jsonl` for the change list. Defaults to
newest-first; `?order=asc` opts into oldest-first (used by
[`scripts/okf-loom watch --since`](cli.md#watch) replay).

```json
{ "events": [ /* §7.2 schema rows */ ], "rev": "<current bundle rev>" }
```

## `/__events`

SSE stream (`text/event-stream`). One event per
change/presence/activity/comment event. 15-second heartbeat
(`type=ping`). Capped at `studio.max_sse_clients` concurrent
streams (default `32`); returns 503 when full or when the studio is
absent / `--no-watch-ui` is set.

| Frame type | Carries |
|---|---|
| `ready` | Initial frame: `rev` + per-doc rev map (`doc_revs`). |
| `changed` / `created` / `removed` | A concept mutation (debounced). |
| `graph` | A graph-affecting mutation (re-fetch the graph). |
| `presence` | Agent presence state change. |
| `comment` | A comment directive was appended or transitioned. Carries `actor`, plus `parent_id` when the comment is a reply, so a tab that did not post the reply still threads it under its parent. |
| `comment_link` | A resolve back-stamped an activity event. |
| `ping` | Heartbeat. |
| `resync` | Server instructs client to refetch (rev + per-doc rev map). |

`X-Accel-Buffering: no` is sent so buffering proxies do not stall
the stream. A HEAD request to `/__events` returns an empty body
immediately (it must not enter the SSE loop).

## `/__comments`

```http
GET /__comments?state=open&concept=tables/orders
```

Canonical comment state from `directives.jsonl` (last-write-wins
per id). The studio client consumes this directly rather than
reconstructing from the event feed. Each row carries `archived`,
`summary`, `ts`, and `updated_at`. See
[comment_lifecycle.md](comment_lifecycle.md).

```json
{ "comments": [ /* see comment_lifecycle.md § JSON shape */ ] }
```

## `/__diff`

```http
GET /__diff?concept=tables/orders&from=<rev>&to=<rev>
```

Line diff between two revs of one concept. Requires the CSRF token
(because it exposes prior snapshot bytes). Returns 400 if any of
`concept`, `from`, `to` is missing; 404 if either snapshot is
missing.

```json
{
  "ok": true,
  "concept": "tables/orders",
  "from": "<rev>",
  "to": "<rev>",
  "diff": [
    {"kind": "ctx", "num": 12, "text": "..."},
    {"kind": "del", "num": 13, "text": "..."},
    {"kind": "add", "num": 13, "text": "..."}
  ]
}
```

## `/__static/<file>`

Static assets (JS modules, CSS). Bundle overrides under
`.okf-loom/viewer/static/<file>` are served first **only** when the
active-code gate is open (bundle declares
`viewer.allow_active_code: true` AND operator consent via
`--allow-active-code` / `OKF_LOOM_ALLOW_ACTIVE_CODE`). Path traversal is
rejected (`..` and separators after normalisation; NUL bytes).

## `/<path>.<media ext>`

Bundle-local media file, so `![shot](/research/assets/shot.png)`
renders inline in the studio. Only paths whose extension is in the
media allowlist route here — everything else stays a concept path.
A media-suffixed path that names no existing file falls through to
concept routing (`shot.png.md` is a legal concept rendering at
`/shot.png`); when both exist, the file wins.

| Kind | Extensions |
|---|---|
| Images | `.png` `.jpg` `.jpeg` `.gif` `.webp` `.avif` `.bmp` `.ico` `.svg` |
| Video | `.mp4` `.webm` |
| Documents | `.pdf` |

A file is served only when **all** of these hold, otherwise `404`:

| Guard | Refuses |
|---|---|
| Clean segments | `..` / `.` / NUL / backslash anywhere in the path. |
| §5 visibility | Anything the bundle scan prunes: `.okf-loom` session state, `.git`, `node_modules`, gitignored paths. `bundle.include` revives media the same as concepts. |
| Containment | A symlinked file resolving outside the bundle root. |
| Size cap | Files over `MAX_RAW_RESPONSE_BYTES` return `413`. |

Responses carry a **sandboxed CSP** (bundle media is user content: a
bundle SVG must not run script in the studio origin when navigated
to directly; `<img>`/`<video>` embedding is unaffected) and a weak
mtime+size `ETag` — image-heavy pages revalidate with `304`s instead
of re-downloading every screenshot. Static builds copy the same
§5-visible media set into the output, so exports render identically.

# POST routes

All POST routes require the CSRF token + Origin/Host allow-list.
All bodies are JSON. A body larger than `MAX_EVENT_BODY_BYTES`
returns `413` and closes the connection (the oversized body is not
drained). Malformed JSON returns `400`. Every mutating endpoint
returns `503` when no studio session is attached.

## `/__comment`

Append a user comment or threaded reply.

```json
{
  "concept": "tables/orders",
  "body": "Add a depends_on relation to customers.",
  "anchor": {"heading": "Schema", "snippet": "..."},
  "detail": {"intent": "add_relation"},
  "actor": "user",
  "parent_id": "01JABED9K0"
}
```

| Field | Required | Notes |
|---|---|---|
| `concept` | yes | Concept id the comment is anchored to. |
| `body` | yes | The comment text. |
| `anchor` | no | Heading + snippet for inline placement. |
| `detail` | no | Free-form metadata (e.g. Quick Action intent). |
| `actor` | no | Defaults to `user`. |
| `parent_id` | no | Id of the parent comment for a threaded reply. Replying to an archived thread auto-unarchives the parent. |

Response `201 Created`:

```json
{ "ok": true, "comment": { /* see comment_lifecycle.md § JSON shape */ } }
```

An `Idempotency-Key` header deduplicates a re-POST within a
10-minute window; the body fingerprint is part of the cache key.

## `/__comment-update`

User-facing state or archive transition. **Archive is a separate
track from lifecycle state**.

```json
// Dismiss (or reopen) — state track only.
{ "id": "01JABED9K0", "state": "dismissed" }

// Archive — archive track only; state is preserved.
{ "id": "01JABED9K0", "archived": true }

// Body edit — amend the comment text in place (open/claimed only).
{ "id": "01JABED9K0", "body": "Add a depends_on relation to customers AND products." }
```

Server-side rules:

| Rule | Failure |
|---|---|
| `state == "dismissed"` on a claimed comment | `409` "cannot dismiss a claimed comment". |
| `archived: true` on a reply (not a root) | `400` "archive is only available on the top-level comment of a thread". |
| `archived: true` when any comment in the thread is not `resolved` | `409` with `unresolved_ids: [...]`. |
| `body` on a `resolved`/`dismissed` comment | `409` "cannot edit a resolved/dismissed comment (reopen it first)". |
| `body` empty | `400` "body must be non-empty". |

A `body` edit bumps `updated_at` — an agent blocked in
[`scripts/okf-loom wait`](cli.md#wait) re-receives the corrected ask —
and re-derives `request_summary` from the new text. The studio UI
shows an Edit button on user-authored, not-yet-resolved comments.

Response `200 OK`:

```json
{ "ok": true, "comment": { /* updated record */ } }
```

The agent does NOT use this endpoint for resolve; it uses
[`scripts/okf-loom comment-resolve`](cli.md#comment-resolve).
See [comment_lifecycle.md § Server-side archive rules](comment_lifecycle.md).

## `/__presence`

```json
{ "actor": "agent", "state": "editing", "focus": "tables/orders", "message": "linking 3 of 7 tables…" }
```

| Field | Allowed |
|---|---|
| `state` | `idle` / `watching` / `thinking` / `editing`. |
| `focus` | Concept id (optional). |
| `message` | Optional free-text progress line (max 200 chars; longer is truncated), rendered next to the presence chip and in the presence history. Re-POST to update it mid-pass. [`comment-claim`](cli.md#comment-claim) auto-presence carries the claim summary as the message. |
| `actor` | Defaults to `agent`. |

A `user` actor's write only takes effect when the current agent
presence is already `idle` or `watching` (so a browser boot cannot
clobber an active agent). Response `200 OK` carries `presence`
(plus `preserved: true` when an agent state was preserved).

## `/__apply`

Run a whitelisted `UpdateOp` via the studio write funnel. Never
accepts a shell/argv string — only a known op kind + args.

```json
{
  "kind": "add_link",
  "target": "tables/orders",
  "args": {
    "target_concept_id": "tables/customers",
    "label": "Customers"
  },
  "group_id": "PASS1",
  "expected_rev": "<content-hash>",
  "actor": "agent"
}
```

| Field | Notes |
|---|---|
| `kind` | A whitelisted `UpdateOp` kind (see [cli.md § update](cli.md#update)). |
| `target` | The concept id to mutate. |
| `args` | Per-kind args (validated fail-closed). |
| `group_id` | Optional; groups this write with others for one-click group undo. Must be a safe path token. |
| `expected_rev` | Optional content-hash; mismatch returns `409` (§9.3 collision guard). |
| `actor` | Defaults to `agent`. |

The block-level partial-update kinds
([cli.md § update-section](cli.md#update-section) /
[cli.md § replace-text](cli.md#replace-text)) take these args:

| Kind | Required args | Optional args |
|---|---|---|
| `update_section` | `heading` (str), `body` (str) | `mode` (`"replace"` \| `"append"`, default `"replace"`), `create_if_missing` (bool) |
| `replace_text` | `old` (str), `new` (str) | `all` (bool) |

Response `200 OK`:

```json
{ "ok": true, "applied": true, "result": { /* apply_plan result */ } }
```

Conflict responses (§9.3 / §9.4): `409` with
`{ok:false, conflict:true, concept, expected_rev, current_rev}`.

## `/__undo`

Restore a prior snapshot. Single or group.

```json
// Single
{ "concept": "tables/orders", "rev": "<content-hash>" }

// Group
{ "group_id": "PASS1" }
```

| Field | Notes |
|---|---|
| `concept` + `rev` | Restore one concept to a prior rev. Both must be safe path tokens. |
| `group_id` | Restore every member of an undo group. |

Response `200 OK`:

```json
{ "ok": true, "restored": 3 }
```

Special cases:

| Case | Response |
|---|---|
| Every target already at its snapshot rev (replay) | `409` `{ok:false, conflict:true, error:"already_undone", targets, current_state}`. |
| Every snapshot pruned by the history ring cap | `404` `{ok:false, error:"snapshots_pruned", missing}`. |
| Some targets pruned, some restored | `409` `{ok:true, restored:N, partial:true, missing}`. |

## `/__preview`

Render arbitrary in-flight markdown (DoS-capped).

```json
{ "markdown": "# Hello\n\n**bold** text" }
```

```json
{ "ok": true, "html": "<h1>Hello</h1><p><strong>bold</strong> text</p>" }
```

## `/__tunnel`

Server admin: attach/detach a cloudflared quick tunnel on the RUNNING
server, so going public does not mean restarting `serve` (losing the
session token, open SSE clients, and undo history). Driven by
[`scripts/okf-loom tunnel`](cli.md#tunnel). Exempt from `--no-edit`
kiosk mode (server admin, not a bundle write) but never from the
token + Origin/Host guard.

```json
{ "action": "start" }
```

| Action | Response |
|---|---|
| `start` | `200` `{ok: true, url}`; `{ok, url, already_running: true}` when a tunnel is already up (idempotent); `502` `{ok:false, error}` when `cloudflared` is missing or times out. |
| `stop` | `200` `{ok: true, url: null, stopped: bool}` (idempotent). |
| `status` | `200` `{ok: true, url}` (`url` is `null` when no tunnel is up). |

Any other `action` returns `400`. `start` adds the tunnel hostname to
the per-request `allowed_hosts` immediately and records the URL in
`<session>/server.json`; `stop` restores the pre-tunnel allowlist.
The tunnel process belongs to the serve process and dies with it.

# Response shapes (common conventions)

| Status | Body |
|---|---|
| `200` / `201` | JSON `{ok: true, ...}` or HTML/text. |
| `400` | JSON `{ok:false, error:"…"}` with a stable reason prefix. |
| `403` | Plain text. Cross-origin guard failure or read-only kiosk. |
| `404` | Plain text `Not found` / JSON `{ok:false, error:"…"}`. |
| `409` | JSON `{ok:false, ...}` (conflict, dismiss-on-claimed, unresolved-thread archive, etc.). |
| `413` | Plain text. Oversized body; connection closed. |
| `500` | Plain text `Internal Server Error`. Full traceback logged to stderr only (no info disclosure). |
| `502` | JSON `{ok:false, error:"…"}`. Tunnel start failed (`cloudflared` missing or timed out). |
| `503` | Plain text. Studio absent, SSE full, or `--no-watch-ui`. |

# Embedding the server

```python
from http.server import ThreadingHTTPServer
from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler

bundle = Bundle.load("path/to/bundle")
httpd = ThreadingHTTPServer(("127.0.0.1", 8787), OKFWikiHandler)
httpd.bundle = bundle         # attribute the handler reads
# httpd.studio = Studio(...)  # attach for the live studio (optional)
httpd.serve_forever()
```

Run it in a daemon thread inside another agent process; the bundle
can be swapped at runtime (`httpd.bundle = Bundle.load(...)`).
See [/how-to/embed_in_harness.md](/how-to/embed_in_harness.md).

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md § serve](cli.md#serve) — flags, bind, public/kiosk modes.
- [cli.md § tunnel](cli.md#tunnel) — runtime tunnel attach/detach on a live session.
- [cli.md § token](cli.md#token) — reading the CSRF token.
- [comment_lifecycle.md](comment_lifecycle.md) — comment state model and archive rules.
- [config_yaml.md](config_yaml.md) — `studio.*` keys (`allowed_hosts`, `max_sse_clients`, `theme`, …).
- [search_modes.md](search_modes.md) — `/__search` backend.
- [/tutorials/live_studio_basics.md](/tutorials/live_studio_basics.md) — first studio session.
- [/explanation/live_studio_design.md](/explanation/live_studio_design.md) — why the studio is shaped this way.
- [`/reference/spec.md`](/reference/spec.md) — current studio and route contract.


## `/__save`

```json
{"id":"tables/orders","source":"---\ntype: Table\n---\n\nUpdated body.\n","expected_rev":"revision-from-document-read","actor":"user"}
```

Requires `id`, full Markdown `source`, and `expected_rev`. Use the revision from
`GET /__data/doc` for an update, or explicit `null` for creation only when the
file is absent. The document response's new `source` field includes frontmatter;
`raw` remains body-only for compatibility.

The server preserves the supplied source, validates frontmatter/type and link
integrity, protects reserved filenames, and writes through the canonical
attributed snapshot funnel. Broken references require explicit
`allow_forward_reference: true`. HTTP 409 means the target changed; 400 means
invalid input; 422 means validation or a write constraint refused the save.
Failures leave the document unchanged. No mutation is automatically retried.

Undoing a newly created document restores absence. Its snapshot revision is
`absent`, distinct from the hash of an existing empty file. Undoing that removal
restores the saved document, preserving redo through the ordinary undo API.
