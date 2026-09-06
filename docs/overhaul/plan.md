---
type: Reference
title: Studio platform overhaul plan
description: Complete staged implementation and acceptance plan for a faithful, embeddable studio redesign.
resource: /overhaul/plan.md
tags: [overhaul, plan, ux, integration]
timestamp: "2026-09-06T00:00:00Z"
---

# Studio platform overhaul plan

Branch: `overhaul/studio-platform`, based on `develop`.
The [intent inventory](intent.md) is the compatibility checklist.
Implementation follows tested replacement of boundaries; established format
algorithms are retained where changing them would damage fidelity.

## 1. Establish evidence and preserve intent

- Read the current specification, design rationale, graph decisions, runtime,
  extension contracts, and tests; record decisions I01–I50.
- Capture the published index and concept experience; inspect the graph.
- Record baseline backend results and environmental browser limitations.
- Keep a source-backed audit separate from claims of observed live behavior.

Acceptance: every major capability has an explicit preserve/improve decision;
no hypothetical functionality is advertised as shipped.

## 2. Rebuild write and session boundaries

- Add a reusable, reentrant lock shared across Studio instances and processes.
- Serialize cooperating mutations through a bundle transaction boundary.
- Compare expected revision even when the target was deleted; preserve errors
  reading existing content rather than treating them as new-file writes.
- Keep snapshots, constraints, attributed events, and atomic replacement inside
  the transaction; enforce bundle path containment for direct library callers.
- Make competing claims fail deterministically rather than silently stealing
  work; preserve reopen/dismiss and archive semantics.
- Keep external editor races documented: advisory locks coordinate Loom writers,
  not arbitrary noncooperating filesystem writers.

Acceptance: deletion conflict, competing writers and claims, constraint caps,
undo, containment, and legacy mutation tests pass.

## 3. Separate server lifecycle and supported integration contracts

- Extract an embeddable server factory from the blocking CLI entrypoint.
- Expose start, close, context-manager, address, and deterministic cleanup.
- Keep run_server and existing imports compatible.
- Add a versioned discovery contract and machine-readable operation schemas.
- Provide small standard-library Python and dependency-free JavaScript clients
  with explicit credentials, timeouts/errors, cancellation where supported,
  and caller-controlled lifetime.
- Use existing REST and SSE routes; preserve auth, no-edit, and active-code gates.
- Add realistic recipes for CLI, Python, browser/Node, and host adapters.
- Expose actual capability support, including reserved extension kinds.

Acceptance: embedded start/stop leaves no listener or watcher, clients exercise
real HTTP routes, errors remain structured, and no new runtime dependency exists.

## 4. Redesign the information architecture and reading shell

- Keep the established teal identity, system serif display face, compact tags,
  five palettes, and semantic color roles.
- Introduce a consistent workspace shell: clear bundle identity, primary
  navigation, readable content measure, and contextual tools.
- Make the index a usable catalog immediately: compact overview, directory
  shortcuts, locally filterable concept groups, authored introduction in an
  accessible disclosure with full content preserved.
- Give concept pages a calmer header and separate reading/context hierarchy.
- Make static, read-only, live, reconnecting, and unavailable states truthful.
- Make controls keyboard reachable; retain native forms and links without JS.
- Improve mobile wrapping, panel widths, touch targets, focus, reduced motion,
  and print output without breaking graph geometry.

Acceptance: index, nested concept, search, graph, comments, source/split,
activity, all themes, narrow layouts, and static/no-JS modes remain usable.

## 5. Rebuild reusable frontend behavior and integration seams

- Extract reusable client/transport concerns from presentation.
- Give catalog filtering and navigation bounded, deterministic behavior.
- Keep the existing graph algorithms and incremental document patch machinery;
  redesign their surrounding controls instead of replacing evidence with art.
- Make initial loading, failed requests, connection loss, and no-result states
  explicit; preserve unsent comment drafts and retry semantics.
- Keep extension registration compatibility and document host lifecycle.

Acceptance: no frontend bundler, no mandatory framework, no new provider,
working client module usable outside the studio, and no token in exported data.

## 6. Close verification gaps

- Require backend tests on pushes/PRs across supported Python versions.
- Run a mandatory browser gate in CI with explicit installed browser tooling.
- Remove forced patchNow calls from the complete collaboration-loop proof.
- Make unexpected server boot failures fail instead of skip.
- Add focused integration and concurrency regressions rather than source-text
  tests that merely mirror the new implementation.
- Validate docs and produce every export target; check static paths and assets.
- Record representative build/search timing and compatibility outcomes.
- Inspect screenshots where the available browser can access the build; report
  unexecuted gates honestly rather than inferring visual success from tests.

Acceptance: results include commands, counts, failures/skips, limitations,
and no unsupported claim that all environments or integrations were tested.

## 7. Migration, delivery, and completion

- Keep OKF format version and current CLI compatible; version new integration
  contracts independently.
- Document intentional differences, supported embedding shapes, and boundaries.
- Commit coherent implementation batches and publish the branch without
  merging or deploying the default branch.
- Include review evidence and remaining blockers in the completion record.

Completion requires working code and verification, not only this plan. Any
unfinished acceptance gate remains explicitly unfinished in the delivery notes.

## 8. Meridian export and reading refinement

The follow-up preserves the intent inventory while addressing the actual CI
screenshots: document-first reading, accessible context disclosure and toolbar
reflow. Meridian receives a native board view, item tab and dashboard widget
through its supported plugin contracts. Full-source edits must retain unknown
fields and use mandatory revision checks. Plugin networking must use the
granted host bridge; credentials stay in frame memory. Exported directories
must match the real loader and validate against the real SDK.

Acceptance covers read/search/edit/comment/resolve/undo, mobile layout,
cleanup, conflict handling and installable package structure. Browser tests
against a host double prove Loom behavior only. Installed-host acceptance and
complete specialized-renderer parity remain separate claims requiring direct
evidence; they cannot be inferred from manifest validation.
