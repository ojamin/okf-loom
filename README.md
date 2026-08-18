<div align="center">

# okf-loom

**A better agent + human experience for OKF knowledge trees. Serve any
folder of plain Markdown as a live studio: you read, explore, and drop
comments on any sentence — your agent picks them up and edits the files.
The files stay plain Markdown throughout.**

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](docs-bundle/tutorials/install.md)
[![Zero runtime deps](https://img.shields.io/badge/runtime%20deps-zero-brightgreen.svg)](docs-bundle/explanation/zero_dependencies.md)
[![Tests](https://img.shields.io/badge/tests-1200%2B%20passing-brightgreen.svg)](tests/)
[![OKF BundleDex](https://bundledex.net/static-badge.svg)](https://bundledex.net)

**[▶ Live demo](https://ojamin.github.io/okf-loom/)** — this repo's docs
bundle as a static build (the live studio adds commenting on top).

</div>

![The okf-loom graph view: 30 documentation concepts grouped into six colour-coded themes, with a ranked theme summary panel on the right](docs/media/graph-map.png)

okf-loom is a toolkit and loadable agent skill for
[Open Knowledge Format (OKF)](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
trees — knowledge kept as plain Markdown files with YAML frontmatter,
cross-linked into a graph. It adds the experience layer on top and
changes nothing underneath:

- **Your files stay plain OKF/Markdown — always.** No database, no
  sidecar schema, no proprietary state. Every page you see is one `.md`
  file that stays readable on GitHub, greppable, and diffable; every
  edit okf-loom makes is an atomic, attributed write to that file.
- **You get a studio, not a file browser.** A rendered wiki (Mermaid,
  KaTeX, code), an evidence-ranked graph with six lenses, and six search
  modes — live over your working tree, with zero install.
- **Your agent gets a directable work queue.** Select any sentence and
  drop a note; your agent claims it, edits the files, and resolves it —
  and the page patches live in your browser while you keep reading.

## Quickstart

From the folder you want documented, give your agent one prompt:

```text
Clone https://github.com/ojamin/okf-loom into ./okf-loom and load
okf-loom/SKILL.md. Start the live studio on ./docs (import or bootstrap
it as an OKF bundle first if it isn't one), give me the URL, then wait
for my comments and work each one to resolution.
```

Then the whole workflow is:

1. **Agent** serves the studio and blocks on the comment feed.
2. **You** open the URL and read your docs.
3. **You** select any sentence and type a note.
4. **Agent** claims it, edits the Markdown, resolves.
5. **You** watch the edit patch into the page live — keep reading, keep
   commenting.

Requirements: Python 3.11+, nothing to install. Add `--tunnel` to `serve`
for a shareable `https://…trycloudflare.com` link (needs `cloudflared`;
see [Sharing & security](#sharing--security)).





https://github.com/user-attachments/assets/76629b4f-ade1-4d60-9ac3-6e3394839282





## Tour

### Select text, leave a comment, let your agent do the work

Commenting is the studio's native editing gesture. Select any sentence
and a *Comment* button appears; the comment is anchored to that exact
text and lands in a queue your agent consumes:

![The comment composer opened from a text selection, with the selected sentence as the anchor and an instruction typed for the agent](docs/media/comment-composer.png)

While a studio is up, an agent runs the loop from the CLI — no plugins,
no webhooks:

```bash
scripts/okf-loom wait docs-bundle                     # blocks until a comment arrives
scripts/okf-loom comment-claim docs-bundle <id> --summary "what I will do"
# … edit via attributed, undoable mutators (write-concept, link-add, update…) …
scripts/okf-loom comment-resolve docs-bundle <id> --summary "what I did"
```

Edits broadcast live to every open tab over SSE — the page patches in
place, no reload. Agent activity, pending changes, and group-undo are all
first-class panels in the top bar.

### A wiki, not a file browser

Every concept page renders full GitHub-flavoured Markdown plus Mermaid
diagrams, KaTeX math, and syntax-highlighted code. Frontmatter is not a
grey metadata dump: types become badges, entities become chips, typed
relations become navigable links, and citations and provenance are laid
out as a dressed header band.

![A concept page rendering Mermaid flowchart and sequence diagrams, with a section outline and quick actions in the sidebar](docs/media/showcase-rendering.png)

The dashboard index shows bundle-level stats and per-area entry points;
[`docs-bundle/demo/showcase.md`](docs-bundle/demo/showcase.md) is one page
that exercises every renderer if you want to see the whole palette at once.

Bundle-local media renders inline too: keep screenshots, diagrams, video,
or PDFs in the bundle (e.g. an `assets/` dir) and `![shot](/path/shot.png)`
displays in the studio and in every build target — served under the same
visibility rules as concepts, sandboxed as user content, with `validate`
flagging any image whose file is missing.

### Six graph lenses, each answering one question

The graph is not a hairball generator. Each lens reshapes layout, colour,
and ranking to answer one question a reader actually has — **Map** (how
does this fit together?), **Themes** (what topic areas exist?), **Flow**
(what depends on what?), **Bridges** (which concepts hold the areas
together?), **Recent** (what is going stale?), and **Focus** (what is in
this node's neighbourhood?). The side panel ranks the answers as clickable
evidence, not just pixels:

![Animated cycle through the Map, Themes, Flow, Bridges, and Recent graph lenses](docs/media/graph-lenses.gif)

### Five colour themes

Light, dark, pastel, sepia, and midnight — all token-governed, all
WCAG-AA-checked (the test suite enforces that every theme overrides the
full token set, and the graph canvas follows along):

![Animated cycle through the light, dark, pastel, sepia, and midnight themes on the showcase page](docs/media/themes.gif)

### Search that understands the bundle

Six search modes behind one flag — BM25 lexical, fuzzy-semantic, hybrid
(rank fusion of the two), tag, entity, and relation — all dependency-free,
served live with search-as-you-type from the top bar:

![Search results for “studio” showing typed, described results with their bundle paths](docs/media/search.png)

### Take it anywhere

The same bundle renders four ways: the live studio, a **static site**
(`build --target static` — the [live demo](https://ojamin.github.io/okf-loom/)
is exactly that, deployed by [a small workflow](.github/workflows/pages.yml)),
a **SPA** build, and a **single self-contained `viz.html`** (`render`) you
can attach to an email. And because a bundle is just Markdown, it still
reads fine as plain files on GitHub.

## Built for agents from day one

This repo *is* a loadable skill. [`CLAUDE.md`](CLAUDE.md) (Claude Code),
[`AGENTS.md`](AGENTS.md) (opencode & friends), and [`SKILL.md`](SKILL.md)
(skill loaders) route any agent to the same standing contract:

1. **Author docs as OKF concepts** with fully-dressed frontmatter.
2. **Validate after writing** (`validate`, `discover`).
3. **Serve the studio proactively** whenever a human wants to read or
   comment.
4. **Work the comment loop** (`wait` → `comment-claim` → mutators →
   `comment-resolve`).

So the setup for a team is: clone the repo, tell your agent to work in
it, open the studio, and start selecting text. The agent authors, links,
validates, and resolves; you read and direct.

## Everything is plain Markdown

A concept is one file. Frontmatter carries the metadata; Markdown links
are the graph edges. This is the entire storage format:

```markdown
---
type: reference
title: HTTP server routes
description: Every route the live studio exposes.
tags: [studio, http]
---

# HTTP server routes

The comment lifecycle is documented in
[Comment lifecycle](/reference/comment_lifecycle.md).
```

`type` is the only required key (OKF v0.1). Unknown keys are preserved
round-trip, hand-authored content is never destroyed, and every write
okf-loom makes is atomic and marker-safe. The binding contract for all of
this is [`docs-bundle/reference/spec.md`](docs-bundle/reference/spec.md).

## Common commands

```bash
# Validate and inspect.
scripts/okf-loom validate docs-bundle --strict
scripts/okf-loom info docs-bundle

# Search and discover gaps.
scripts/okf-loom search docs-bundle "current spec" --mode hybrid
scripts/okf-loom discover docs-bundle --out /tmp/okf-suggestions.json

# Serve live, or build artifacts.
scripts/okf-loom serve docs-bundle --no-open            # live studio
scripts/okf-loom build docs-bundle --target static --out /tmp/site
scripts/okf-loom render docs-bundle --out /tmp/viz.html # single file

# Author safely (attributed, undoable, broadcast live).
scripts/okf-loom bootstrap path/to/new-bundle
scripts/okf-loom write-concept --bundle path/to/bundle --id notes/decision \
    --type decision --title "Use SQLite" --body "## Context ..."
scripts/okf-loom link-add --bundle path/to/bundle --source tables/orders --target tables/customers
scripts/okf-loom update-section --bundle path/to/bundle --id notes/decision \
    --heading "## Context" --body-file /tmp/new-context.md   # swap one block, keep the rest
```

Run `scripts/okf-loom --help` for the full verb list (25+ commands), or
read the [CLI reference](docs-bundle/reference/cli.md).

## Sharing & security

`serve` binds `127.0.0.1:8787` — loopback-only — by default. Going public
is an explicit choice: `--tunnel` starts a Cloudflare quick tunnel
(anyone with the link can read; mutating routes still require the
per-session `X-OKF-Token` plus Origin/Host allow-list checks), and
`--public --public-ack` does a raw network bind. Already serving? Attach
a tunnel to the running session with `scripts/okf-loom tunnel <bundle>`
(`--stop` detaches) — no restart needed. Use `--no-edit` for
read-only kiosk sharing. Details: [SECURITY.md](SECURITY.md) and the
[HTTP routes reference](docs-bundle/reference/http_routes.md).

## Skill resources

| File | Purpose |
|---|---|
| [`resources/overview.md`](resources/overview.md) | OKF purpose and trigger list |
| [`resources/command-reference.md`](resources/command-reference.md) | Invocation and CLI flows |
| [`resources/format-basics.md`](resources/format-basics.md) | Bundles, concepts, frontmatter, links |
| [`resources/authoring.md`](resources/authoring.md) | Curation conventions and mutators |
| [`resources/validation.md`](resources/validation.md) | Validation profiles and fail-closed behavior |
| [`resources/advanced-operations.md`](resources/advanced-operations.md) | Search, discovery, build, capabilities |
| [`resources/studio-agent-loop.md`](resources/studio-agent-loop.md) | Live studio comment loop |
| [`resources/architecture-map.md`](resources/architecture-map.md) | Runtime/source map |
| [`resources/gotchas.md`](resources/gotchas.md) | Hard rules and traps |

The full documentation lives as an OKF bundle in
[`docs-bundle/`](docs-bundle/) — okf-loom is documented in okf-loom.

## Repository layout

```text
okf-loom/
├── SKILL.md                  # primary skill entrypoint for agents
├── CLAUDE.md / AGENTS.md     # thin pointers that route agents to SKILL.md
├── README.md                 # this file
├── resources/                # small agent-readable guidance files
├── scripts/
│   ├── okf-loom              # the checked-in helper command
│   ├── okf_loom/             # checkout-local runtime (stdlib-only)
│   ├── capture_readme_media.py   # regenerates docs/media/
│   ├── capture_viewer_proof.py
│   ├── capture_signal_controls.py
│   ├── build_skill_archive.py
│   └── lint-js.sh
├── docs-bundle/              # okf-loom's own docs, as an OKF bundle
├── samples/                  # example bundles (demo_bundle, showcase)
├── docs/                     # screenshots, README media, design notes
└── tests/                    # 1,200+ test pytest suite
```

The distribution unit is this git repo layout; `pyproject.toml` exists
only for pytest/coverage configuration.

## Regenerating media & browser proof

The screenshots and GIFs on this page live in [`docs/media/`](docs/media/)
and are reproducible from a live studio; browser-marked tests skip cleanly
when Playwright is absent:

```bash
python -m pip install pytest playwright pillow
python -m playwright install chromium

PYTHONPATH=scripts python scripts/capture_readme_media.py   # this page's media
PYTHONPATH=scripts python scripts/capture_viewer_proof.py   # dated spec §17 proofs
PYTHONPATH=scripts pytest tests/test_viewer_browser.py      # browser tests
```

## License

okf-loom is licensed under the Apache License, Version 2.0 — commercial
and non-commercial use are both allowed. See [LICENSE](LICENSE) and
[CONTRIBUTING.md](CONTRIBUTING.md).

okf-loom is compatible with and complementary to the upstream OKF
specification (also Apache-2.0). This implementation is independent; it
is not affiliated with or endorsed by Google.
