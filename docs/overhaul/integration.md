---
type: Reference
title: Embedding and migration guide
description: Supported integration boundaries and compatibility changes in the studio platform overhaul.
resource: /overhaul/integration.md
tags: [integration, embedding, migration]
timestamp: "2026-09-06T00:00:00Z"
---

# Embedding and migration guide

Loom remains a checkout-local Python 3.11+ runtime. Markdown/YAML files are the
source of truth. There is no database migration, mandatory JavaScript build,
model provider, or package-publishing step. The new API discovery version (`1`)
is independent of OKF format v0.1 and the runtime version.

## Choose an integration boundary

| Host | Boundary | Lifetime and ownership |
|---|---|---|
| Agent running alongside a studio | Existing helper CLI; `wait`, `comment-claim`, mutators, `comment-resolve` | Host owns the agent process and reasoning. |
| Python desktop application or local service | `create_server` plus `StudioClient` | Host explicitly starts/closes the server and chooses port, token access and active-code consent. |
| Browser application | Same-origin proxy to REST/SSE; reusable `client.js` | Host provides credentials and AbortSignal; mount the studio separately or use supported viewer extensions. |
| Node or another backend | REST JSON contract and cursor-polling feed | Host owns retries, authentication storage and any protocol translation. |
| Search/retrieval pipeline | `Bundle`, `ContentIndex`, `search_bundle`, or exported content JSON | Uses the same index as the viewer. |
| Documentation hosting | Existing static, SPA, or single-file exports | No live server or token required. |
| Meridian | Exported board/item/dashboard plugin using the host network bridge | Host owns installation and network grants; Loom owns documents, tokens and agent processes. |
| MCP/IDE/other ecosystem adapter | Translate host tools to the existing CLI/client operations | Adapter supplies protocol-specific discovery and authorization. No bundled MCP server is claimed. |

## Python host

For Meridian export, run `python scripts/export_meridian.py --help` and read
the checkout's `plugins/meridian/README.md`. It documents the tested manifest
contract, deployment layout, credential lifecycle and rendering boundaries.
An export is not evidence of installation in a running Meridian deployment.

Exact-source clients may call `POST /__save` with `id`, `source` and mandatory
`expected_rev`: the revision returned by `GET /__data/doc`, or explicit JSON
`null` to create only if the file is absent. The document response now includes
`source` (the complete Markdown file) alongside the existing body-only `raw`.
The save preserves supplied bytes, requires valid frontmatter and a non-empty
type, protects reserved paths, and rejects broken references unless
`allow_forward_reference: true` is explicit. A stale revision returns 409
without changing the file. It uses the same attributed history and undo funnel
as other writes; an absent-file snapshot restores absence, not an empty file.

From a checkout, add its `scripts` directory to the host's import path:

```python
from okf_loom.server import create_server
from okf_loom.client import StudioClient, StudioError

with create_server("/absolute/path/to/bundle", port=0,
                   allow_active_code=False) as server:
    client = StudioClient(server.url, token=server.csrf_token, timeout=15)
    capabilities = client.discover()
    document = client.document("guide/topic")
    note = client.comment("guide/topic", "Clarify the example",
                          idempotency_key="host-request-42")["comment"]
    client.claim(note["id"], actor="my-agent")
    result = client.apply("replace_text", "guide/topic",
                          {"old": "exact old text", "new": "clearer text"},
                          expected_rev=document["rev"], group_id="host-pass-42")
    client.resolve(note["id"], summary="Clarified the example")
```

Replace the example concept/text with actual content. A failed apply is an error,
not evidence that the request is complete. Catch `StudioError`; inspect `status`
and `payload`. On revision conflict (409), refetch and reconsider the operation.
Do not automatically retry mutations. Comment and undo calls accept explicit
idempotency keys. `create_server` binds immediately; `.start()` starts serving,
`.close()` is idempotent, and the context manager does both. Port zero selects an
available port. The factory never opens a browser or launches an agent. Watcher,
listener and optional tunnel cleanup are owned by the server.

One live studio per bundle/session remains the supported topology: a second
server on the same bundle rotates its on-disk token/discovery state. Multiple
bundles can run in one host process with isolated active-code consent. An
explicit factory choice is scoped to that server and its watcher; it does not
change another server or the process-wide CLI setting.

## JavaScript host

Import the ES module from the checkout or serve it as a same-origin asset:

```javascript
import { createClient } from "./client.js";

const client = createClient({ baseUrl: "", token: hostProvidedToken });
const abort = new AbortController();
const results = await client.search("refund policy", "hybrid", {
  signal: abort.signal,
});
const note = await client.comment({
  concept: "guide/topic", body: "Clarify this example", anchor: {},
}, { idempotencyKey: crypto.randomUUID(), signal: abort.signal });
```

The host controls cancellation (including timeouts through `AbortSignal.timeout`
where supported). Reads do not send the mutation token; mutations and privileged
diffs do. Redirects are rejected. Errors preserve HTTP status and response data.
The module has no DOM dependency and no runtime npm dependencies. `npm ci` and
`npm test` install/run development-only DOM tests.

A browser integration should use the same origin or a host-controlled reverse
proxy. An allowed Host/Origin does not itself provide a CORS policy. Keep the
existing token, Host AND Origin checks, and double active-code gate. Non-loopback
embedding requires `allow_network=True`; the CLI retains its explicit public-bind
acknowledgment. Tokens are local session credentials, never public export data.

## Discovery and events

`GET /__api/v1` reports actual capabilities, route names/methods, and the accepted
argument schema for every apply operation. It is a Loom discovery manifest,
not a full OpenAPI document. Original routes remain supported. New additive
routes are `GET /__health`, `POST /__claim` and `POST /__resolve`; HTTP search now
accepts the same six modes as the CLI. Health reports readiness or reload failure.
Reserved extension kinds remain marked reserved.

SSE remains `GET /__events`. Use `GET /__data/events?since=...&order=asc` for cursor
polling and replay. Deduplicate by `event_id`; treat resync as a request to fetch
current document revisions. The feed is bounded session history, not a permanent
audit archive. External file changes are loaded before their notifications are
broadcast so immediate document fetches receive current content. Existing
`EventBus` imports remain compatible after extraction to `events.py`.

## Intentional behavior changes

- Expected-revision writes fail if the target has disappeared.
- Cooperating Loom writers serialize revision checking, snapshots and replacement
  through a reentrant advisory transaction. Arbitrary editors do not honor this
  lock and can still race; this is not a filesystem-wide compare-and-swap.
- A claim already owned by another actor returns conflict; terminal comments
  cannot be claimed without an explicit state transition.
- Direct library writes reject paths outside the bundle.
- Invalid JSON request shapes return 400; real server startup failures fail the
  collaboration test rather than being silently skipped.
- Static exports no longer announce a failed live studio. The catalog has
  navigation, filtering and an authored-introduction disclosure. Reading modes,
  graph lenses, themes, comments, activity and supported extension hooks remain.
- Failed comment submission preserves the draft and reuses its idempotency key
  for an identical retry. Live patches include full text, link/format changes,
  code whitespace and table semantics, with bounded large-document reconciliation.

Custom templates can opt into `__WORKSPACE_NAV__`; built-in templates already do.
The existing token palette and viewer overrides continue to work. The shared
workspace styles are additive. Custom CSS should be reviewed against the new
navigation width and responsive layout. Engineering plans live under `docs/`
so they do not displace end-user guidance in the published bundle's search index.
