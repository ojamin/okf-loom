---
type: Reference
title: Overhaul verification and delivery record
description: Executed checks, compatibility evidence, and remaining acceptance gates.
resource: /overhaul/verification.md
tags: [verification, overhaul]
timestamp: "2026-09-06T00:00:00Z"
---

# Verification and delivery record

Base: `develop` at `b9264ee4c21d7dc55177f12c5ffa5dd16afb2c73`.
Working branch: `overhaul/studio-platform`.

## Current verified state — September 6 follow-up

Implementation commit: `7af2576945514bd30c94f000a3d6e23d308a9099`.
[All five CI jobs passed](https://github.com/ojamin/okf-loom/actions/runs/34067331735).
The entries below this section preserve the earlier investigation history.

| Acceptance | Evidence |
|---|---|
| Backend compatibility | 1,273 passed and 26 optional skips on Python 3.11, 3.12 and 3.13. Strict documentation validation, all three export formats and the stdlib YAML fallback passed. |
| Shared frontend behavior | 15 JavaScript tests passed, including transport cancellation, conflict propagation, sanitizer boundaries and placement scopes. All viewer and Meridian JavaScript passed syntax checking. |
| Original agent workflow | Real browser: selection → comment → CLI wait/claim → mutation → automatic live patch → resolution → grouped undo. |
| Meridian workspace workflow | Real React and browser against the actual Loom server: connect token → source edit/save → comment/resolve → undo → mobile navigation → unmount/dispose. The Meridian host is explicitly a test double. |
| Mobile controls | Real browser: no document overflow, context disclosure, relocated source views, watching requests and command-palette Escape/focus restoration. |
| Export compatibility | Final manifest passed Meridian's actual `parsePluginManifest`, compiled from its unmodified manifest/schema dependencies at `pre-dev` commit `6697c1610245133af28e163f0490b70716bc3371`. Both loaders' manifest-plus-dist layout and the host React exports were inspected. No private Meridian implementation is distributed. |

The browser job passed all three scenarios. Screenshots were extracted directly
from that run's artifact and visually inspected without modification:

- [Studio desktop](screenshots/studio-desktop.png) and [mobile](screenshots/studio-mobile.png).
- [Plugin desktop](screenshots/meridian-desktop.png) and [mobile](screenshots/meridian-mobile.png), captured in the identified host test harness.

The revised hierarchy keeps reading primary and makes context/evidence a native
keyboard-accessible disclosure. The mobile toolbar and plugin catalogue collapse
secondary controls. All original document semantics remain available. Exact
source writes preserve unknown fields and check revisions; creation undo now
restores absence and supports redo. Browser testing additionally exposed and
fixed an Escape/focus race in the new tools disclosure.

### Boundaries still requiring deployment evidence

The export has not been installed in a running Meridian account. Installation
requires an operator's actual public HTTPS Loom origin, published privacy notice,
Meridian plugin-sandbox deployment and normal administrator consent. The exporter
accepts these explicitly rather than guessing production settings or embedding
credentials. Run the checkout's `scripts/verify-meridian.mjs` against the target
Meridian SDK, then its normal plugin installation/surface acceptance lane.

This is a native plugin for the core knowledge/editing/collaboration workflows;
it is not complete native renderer parity. Active bundle scripts, specialized
Mermaid/KaTeX rendering and arbitrary media remain available through **Open full
studio**. The plugin keeps source and media descriptions visible. It does not
launch an agent or invent a retired agent-tool extension; existing agents use
Loom's shared queue and CLI/API. These limits are explicit in
`plugins/meridian/README.md` and must remain explicit in deployment claims.

## Implemented plan

| Stage | Implementation | Acceptance status |
|---|---|---|
| Intent | 50 preserved decisions, source-backed inventory and complete staged plan | Complete; published home screenshot/DOM and showcase DOM inspected. |
| Write boundary | Cross-process reentrant transactions, revision/deletion conflicts, containment, competing claims | Behavioral and legacy tests pass. |
| Embedding | Server factory/lifecycle, extracted bus/watcher, scoped active-code consent, manifest, Python/JS clients | Real HTTP collaboration loop, shutdown and consent-isolation tests pass. |
| Reading UX | Shared responsive navigation, compact catalog, local filter, authored-intro disclosure, calmer concept/comments styling | Implemented; DOM behavior passes. Full visual acceptance remains open. |
| Frontend seams | Standalone client and bounded patcher; draft/retry recovery; exact code/table/inline semantics | Ten development-only JS tests and syntax checks pass. |
| Verification | Multi-Python backend, JS and required browser-loop CI; no forced patch fetches; startup failure is failure | Local backend/export gates pass; remote CI status recorded separately. |
| Delivery | Integration/migration guide and branch | Publication status recorded in the delivery response. |

The overhaul preserves mature parsing, indexing, graph and mutation algorithms.
These were intentionally retained where their behavior expresses the product's
contract. The implementation replaces and separates failure-prone boundaries;
it does not claim every source line or every existing component was rewritten.

## Executed checks

- Full non-browser regression suite: **1,257 passed, 26 skipped** in 78.79 s.
  The skips are three modules without Playwright and 23 optional upstream-bundle
  cases absent from this checkout. No functional failures remained in that run.
- Focused platform/server integration: **17 passed** in 6.75 s, including
  cross-process competing writers, concurrent claims, real HTTP apply/resolve/
  undo, token boundaries, shutdown, external-change ordering and isolated
  active-code settings in concurrent embedded servers.
- JavaScript: **10 tests passed**; all viewer files pass `scripts/lint-js.sh`.
  Tests exercise behavior with jsdom and mock transport; they are not browser
  layout or accessibility acceptance.
- Strict documentation validation: **PASS**, zero errors/warnings (13 existing
  informational link-form/capability notices).
- Static export: 30 concepts / 238 edges, 54 files, approximately 3.60 MB.
- SPA export: 30 concepts / 238 edges, 53 files, approximately 2.71 MB.
- Single-file render: 30 concepts / 238 edges, approximately 1.12 MB.
- Python compile and whitespace checks pass. Fallback YAML coverage is part of
  the suite and explicitly required after PyYAML removal in CI.

Commands (test dependencies installed separately from runtime):

```bash
python -m pytest -m 'not browser' -q
node --test tests/js/*.test.mjs
scripts/lint-js.sh
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom build docs-bundle --target static --out /tmp/loom-static
scripts/okf-loom build docs-bundle --target spa --out /tmp/loom-spa
scripts/okf-loom render docs-bundle --out /tmp/loom.html
```

## Open acceptance gates and practical limits

The available cloud browser explicitly blocked the local HTTP and local-file
preview. No alternate browser surface was used to bypass that restriction.
Consequently **the redesign has not passed visual acceptance** across desktop,
mobile, all five themes, source/split, graph, comments, activity and print.
Automated CI collaboration proof is a separate functional gate, not a substitute
for inspecting those surfaces. The mandatory CI browser job installs Chromium,
asserts automatic document updates and session-only claim presence, and retains
trace/screenshot evidence.

The integration target is a local single-admin studio, not a multi-tenant hosted
service. One live studio per bundle/session is supported. Advisory transactions
coordinate Loom writers; unrelated editors can still race. The discovery
manifest is not OpenAPI, and no MCP/IDE adapter or external ecosystem has been
certified. Hosts must own those adapter lifecycles and policies.

The full requested outcome remains incomplete until the open visual gate and
required CI gate pass. This record distinguishes implemented work from verified
acceptance rather than treating a test count as design approval.

## First CI findings and corrective changes

The initial published commit passed the Python 3.12 and 3.13 backend jobs and
frontend job. The new gates exposed a pre-existing Python 3.11 f-string syntax
incompatibility and a live undo regression. The undo investigation found two
independent risks: notifications emitted before the HTTP read model refresh,
and browser deduplication based on process-local revision counters. The fixes
batch HTTP event delivery until the refreshed model is available and use the
per-append `event_id` for deduplication and polling cursors. Initial descending
poll pages now replay oldest-first. Focused regressions cover all three behaviors.
The browser proof also exercises the actual Changes panel and Undo group button.

Representative local timings for the 30-concept documentation bundle: load
73.6 ms; five-query warm median lexical 2.8 ms, semantic 3.8 ms, hybrid 6.3 ms,
tag below 0.1 ms, entity 0.1 ms and relation 0.5 ms. First lexical/semantic calls
were 161.7/52.6 ms due to index construction. These are small-bundle development
measurements, not a large-scale performance guarantee.
