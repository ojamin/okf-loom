---
type: Explanation
title: Dependency-light philosophy
description: Why okf-loom runs from a checkout with no primary install step, how the PyYAML-preferred fallback works, and where optional browser proof fits.
resource: /explanation/zero_dependencies.md
tags: [dependencies, rationale, search, embedding]
timestamp: "2026-06-29T00:00:00Z"
---

# Dependency-light philosophy

This essay explains why okf-loom keeps the primary runtime path free of
install steps. PyYAML is preferred when present, and the checked-in runtime ships
a conservative built-in YAML fallback for normal docs validation when it is not.
It is not
the clone/setup guide (that is
[Use okf-loom from a clone](/tutorials/install.md)) nor the search reference
(that is [Search modes](/reference/search_modes.md)); it is the
reasoning that connects the two.

The runtime path is the checked-in `scripts/okf-loom` helper.
Everything else okf-loom does — validating, searching, discovering, rendering, and the live collaborative studio — is built on the Python standard library plus PyYAML when it is available.
Without PyYAML, the bundled fallback accepts the YAML subset used by OKF docs/config and fails closed on unsupported advanced YAML such as anchors, aliases, tags, directives, merge keys, and multi-document streams.

# Why it matters

A dependency-light baseline earns three properties that a richer
    dependency set would erode.

## Embeddability

The same checkout runtime runs in a CI runner, a meta-agent's subprocess, an
air-gapped notebook, and a long-running viewer inside another agent
harness. None of those environments is guaranteed to have a C toolchain,
network access, or an environment that can resolve a deep dependency
tree. A toolkit whose normal source-checkout path does not require a primary install step
fits everywhere; a toolkit that pulls in a markdown library, a search engine,
a web framework, and an embedding backend fits only where those resolve cleanly.

This is also what makes okf-loom **harness-agnostic**. The three
integration shapes — shell out to the CLI, import the library, or run a
long-lived server in a daemon thread (see
[Embed the viewer in an agent harness](/how-to/embed_in_harness.md)) —
all work without the host having to accommodate okf-loom's
dependencies. The embedding contract is one checkout plus `scripts/okf-loom`.

## Source-checkout usability

A dependency-light checkout is fast, deterministic, and reproducible. There
is no dependency conflict to debug, no transitive package to pin, and no
surprise platform wheel that fails to build. For a tool whose primary
audience includes agents cloning it into a sandbox and loading the local
skill resources, that friction matters: every failed setup is a failed task.

## Audit surface

Every hard dependency is code the user is trusting. A smaller dependency
tree is a smaller attack surface and a smaller review surface. For a
tool whose job includes *consuming bundles produced by arbitrary third
parties* (see [What OKF is for](what_is_okf.md)), keeping the trust
boundary narrow is a deliberate stance: okf-loom reads untrusted
markdown and YAML, using PyYAML when present or the conservative fallback otherwise, plus
its own containment guards (`paths.path_within_bundle`) rather than a
stack of third-party libraries each of which would need its own audit.

> The constraint is stated as a hard rule in the agent brief
> (`AGENTS.md`): do not reintroduce package publishing or primary dependency installs as the normal runtime path.
> New features must keep `scripts/okf-loom` usable from a checkout.

# What the constraint costs

Dependency-light design is not free. Three costs are worth naming honestly.

## No rich markdown library

okf-loom renders markdown with its own small stdlib renderer
(`viewer/markdown.py`) rather than a battle-tested library like
`markdown`, `mistune`, or `marked`. That renderer handles the common
cases — headings, tables, fenced code, links in both forms, wikilinks —
but it is not a full CommonMark implementation, and edge cases that a
mature library would handle for free have to be handled by hand.

## No native search index

There is no SQLite FTS table, no LanceDB store, no Pagefind shard. The
search corpus lives in memory and is scanned per query, which is fine
up to roughly ten thousand concepts in a single process and degrades
gracefully beyond that (see [`/reference/architecture.md`](/reference/architecture.md)
§ "Performance characteristics"). A user with a much larger corpus is
expected to swap in a custom `SearchBackend` — the seam exists by
design.

## No local embedding backend

The most consequential cost: there is no `sentence-transformers`, no
local vector store, no on-device embedding model. A dense/vector search
backend would normally be the way to get true semantic retrieval, and
okf-loom ships without one.

# How okf-loom works around each cost

Each cost is met with a workaround that preserves the constraint.

## A stdlib markdown renderer, progressively enhanced

The stdlib renderer is the source of truth for the rendered view and
the `/__preview` endpoint, which keeps a single canonical renderer (spec
§5, decision D2) rather than a second client-side engine. Rich content
— mermaid diagrams, syntax highlighting (highlight.js), and KaTeX math
— is added by **progressive enhancement from a CDN**: the scripts load
on demand only when a page contains an element of that kind, degrade
gracefully to the raw source if the CDN is unreachable, and add zero
overhead on pages that do not use them. The same renderers also run in
the graph detail panel and the single-file export (pinned CDN versions;
highlight.js via jsDelivr's `+esm` bundle).

This is a deliberate trade: the canonical render path stays
dependency-free and predictable, and the *optional richness* is layered
on at the browser where a CDN miss is non-fatal. The page is always
readable; the enhancement is a bonus, never a requirement.

## Lexical search in pure Python

The `LexicalBackend` is a field-weighted BM25 over title (5x) >
headings (3x) > description (2x) > body (1x) > tags (1x), implemented
in pure Python with no dependencies. It satisfies the common case —
"search by term" — without an index file, a build step, or a running
process. See [search_modes.md](/reference/search_modes.md).

## SemanticLite: a zero-dependency fuzzy-semantic layer

The most interesting workaround is `SemanticLiteBackend`. True semantic
search normally means embeddings; okf-loom ships a
**fuzzy-semantic** layer that needs none. It builds a per-concept
profile (title ×3, description ×2, type, headings, tags, entity labels
and aliases) and scores similarity as:

```
score = α · cosine(tokens)  +  (1 − α) · cosine(char-trigrams),   α = 0.6
```

The trigram half is what makes it robust to typos and morphology —
`orders` scores close to `order`, `customer` close to `customers`. It
is not a dense embedding, and it does not pretend to be; it is a cheap
fuzzy-semantic layer that helps the agent find candidates, and it
costs zero dependencies. The `HybridBackend` fuses lexical and
SemanticLite by Reciprocal Rank Fusion (k=60), again dependency-free.

## Retrieval and agent reasoning are separate stages

An LLM can reason well about candidates it receives, but it cannot rerank a
relevant concept that retrieval omitted. SemanticLite is intentionally a cheap
typo/morphology candidate finder, not a claim of dense semantic equivalence.
Callers can impose a SemanticLite score floor or require component evidence in
Hybrid; deployments whose product goal is broad paraphrase recall should
evaluate an optional dense `SearchBackend` against a labelled query set.

The `SearchBackend` Protocol keeps that extension point explicit: a user who
needs a local embedding backend (broad paraphrase retrieval, an air-gapped
deployment, a very large corpus, or a specific retrieval task) can implement
one behind the same interface. The constraint is on the dependency-free
default, not on the legitimacy of dense retrieval. See
[capabilities.md](/reference/capabilities.md) for the extension model.

# Where the one optional extra fits

There is exactly one optional extra, and it is narrowly scoped:
explicit browser-proof dependencies, which install Playwright. They exist for a single
purpose — capturing browser-end-to-end proof that the live wiki
actually renders: the graph mounts, concept pages load, search returns
results, and backlinks show. Without it, those tests skip cleanly so
the default suite stays dependency-light.

```bash
python -m pip install playwright
playwright install chromium
pytest tests/test_viewer_browser.py        # e2e; skips cleanly without the extra
```

This is the model okf-loom uses for anything that genuinely cannot be
done with the standard library: it goes behind an *optional extra* that
is strictly additive, degrades cleanly when absent, and never enters
the default install or the default test run. Browser proof is the only
thing that earns that treatment today; if a future feature needed a
native search index or a rich markdown library, the same pattern
(optional extra, clean skip, no default impact) is how it would land.

# Why the constraint is a feature, not a limitation

It is tempting to read "dependency-light" as "missing features."
okf-loom's stance is the opposite: the constraint is what makes the
features it *does* have portable, trustworthy, and combinable. A richer
dependency set would buy a more polished markdown render and a native
search index at the cost of an install that fails in exactly the
environments — air-gapped, sandboxed, agent-spawned — where okf-loom
is most useful.

The workaround for each cost is also a feature in disguise. The
stdlib markdown renderer plus CDN progressive enhancement gives a
single canonical render path. SemanticLite plus the agent gives a
semantic search story that needs no model download. The
`SearchBackend` Protocol gives an escape hatch for users who need more,
without imposing it on users who do not. In every case the constraint
forced a design that is simpler to reason about than the dependency-rich
alternative would have been — and that is the real reason okf-loom
keeps it.

# See also

* [Use okf-loom from a clone](/tutorials/install.md) — the clone-first
  setup and your first `scripts/okf-loom validate`.
* [Search modes](/reference/search_modes.md) — the six modes, their
  backends, and the `SearchBackend` Protocol.
* [Capability registry](/reference/capabilities.md) — how optional
  extensions land without a spec change or a new dependency.
* [Embed the viewer in an agent harness](/how-to/embed_in_harness.md) —
  the three integration shapes the constraint enables.
* [`/explanation/research.md`](/explanation/research.md) — the comparative
  survey, including the note on why the local embedding
  backend was prototyped and then removed.
* [`/reference/architecture.md`](/reference/architecture.md) — the
  module map, including the performance characteristics of the
  dependency-free search path.

Return to [Explanation](/explanation/index.md).
