---
name: okf-loom
description: Load this repo skill when working with Open Knowledge Format (OKF) bundles: validate, search, discover, update, render, serve the live studio, author concepts, or use the bundled OKF docs. The repo is the distribution unit; run scripts from the checkout with scripts/okf-loom.
---

# okf-loom — repo-local skill

This repository is the skill. Clone it, load this `SKILL.md`, then use the
small resource files and checkout-local scripts referenced below. There is no
package-publishing workflow to learn; the durable artifact is this git repo
layout.

## Default behaviours (do these without being asked)

These are the standing expectations for any agent using this skill:

1. **Docs are OKF bundles.** When the user asks for documentation, notes, a
   knowledge base, or "write this up", author it as OKF concepts — one
   markdown file per concept with full frontmatter (`type`, `title`,
   `description`, `tags`, `timestamp`, plus `aliases`/`entities`/`relations`/
   `provenance`/`citations` where they carry real information). Start a new
   bundle with `scripts/okf-loom bootstrap <dir>` or add to an existing one.
   [`docs-bundle/demo/showcase.md`](docs-bundle/demo/showcase.md) is the
   canonical example of a fully-dressed concept (open it rendered to see why
   each key earns its place).
2. **Validate after writing.** Run `scripts/okf-loom validate <bundle>` after any
   batch of writes and fix findings; run `scripts/okf-loom discover <bundle>`
   periodically to catch missing links/indexes/descriptions. In JSON discovery
   reports, act on `actionability.safe_to_apply` first, review
   `needs_review`, and treat suppressed buckets as explanations unless the
   user asks for a broad audit. For imports or larger bundles, also run
   `scripts/okf-loom graph-quality <bundle>` as an
   advisory graph-health report (not a conformance gate). If `index.md` files
   are stale, `scripts/okf-loom index <bundle>` regenerates the generated blocks
   (marker-safe — it only rewrites between `okf:generated` markers; hand-authored
   index prose without markers will be REPLACED, so check `git diff` after).
3. **Serve proactively.** The rendered wiki is the product, not a debug view.
   Whenever the user wants to *see*, *read*, *review*, or *comment on* docs —
   or you have just built/changed a bundle they'd plausibly want to look at —
   start the studio without waiting to be asked and hand them the URL:

   ```bash
   scripts/okf-loom serve path/to/bundle --no-open          # local:  http://127.0.0.1:8787/
   scripts/okf-loom serve path/to/bundle --no-open --tunnel # + public https URL (needs cloudflared)
   ```

   `--tunnel` prints a `https://…trycloudflare.com` link for users who are
   not on this machine and wires the tunnel host into the studio's
   cross-origin allowlist automatically; `scripts/okf-loom tunnel <bundle>`
   attaches the same link to an already-running session without a restart.
   Tell the user they can comment by selecting any text; the graph lives
   at `/__graph`.
4. **Run the collaboration loop.** While a studio is up, user comments are
   work for you: `scripts/okf-loom wait <bundle>` blocks until there is a comment
   or change, then claim → edit → resolve
   (`comment-claim` / mutators / `comment-resolve`). When an ask is
   ambiguous, ask in the thread with `comment-reply` and `wait` for the
   answer instead of guessing. During comment triage, commit frequently after
   coherent batches or resolved threads so the work stays traceable and easy
   to review or revert. Read
   [`resources/studio-agent-loop.md`](resources/studio-agent-loop.md) before
   your first loop.
5. **Prefer mutators for content changes while a studio session is live**
   (`write-concept`, `set-frontmatter`, `link-add`, `entity-add`,
   `update-section`, `replace-text`, `update`): they are attributed,
   undoable, and broadcast live to open tabs. For edits inside an existing
   body, reach for `update-section`/`replace-text` (swap one block, keep
   the rest) rather than a whole-body `write-concept --force`. Direct file
   edits are fine otherwise — the watcher picks them up.

## Fast start

From inside `okf-loom/`:

```bash
scripts/okf-loom --help
scripts/okf-loom validate docs-bundle --strict
```

No primary runtime install step is required for normal docs validation.
PyYAML is used when present; otherwise okf-loom uses its conservative built-in YAML fallback.

From a parent workspace where the checkout is named `okf-loom/`:

```bash
okf-loom/scripts/okf-loom validate okf-loom/docs-bundle --strict
```

Examples use the checked-in helper directly rather than a local shell wrapper or installed console script.

## What to read, when

| When | Read |
|---|---|
| Always, first (60-second orientation + default paths) | [`resources/overview.md`](resources/overview.md) |
| Before your first write to any bundle | [`resources/gotchas.md`](resources/gotchas.md) |
| Running any CLI verb / unsure of a flag | [`resources/command-reference.md`](resources/command-reference.md) |
| Writing or editing concept files by hand | [`resources/format-basics.md`](resources/format-basics.md) then [`resources/authoring.md`](resources/authoring.md) |
| Improving graph usefulness / after imports | [`resources/graph-health.md`](resources/graph-health.md) |
| Deciding what "good" frontmatter looks like | [`docs-bundle/demo/showcase.md`](docs-bundle/demo/showcase.md) — the worked example |
| A validate run fails or you need CI gating | [`resources/validation.md`](resources/validation.md) |
| Search modes, discovery, plans, static builds | [`resources/advanced-operations.md`](resources/advanced-operations.md) |
| A studio is up and users are commenting | [`resources/studio-agent-loop.md`](resources/studio-agent-loop.md) |
| Changing the runtime under `scripts/okf_loom/` | [`resources/architecture-map.md`](resources/architecture-map.md) |
| Anything ambiguous — the binding contract | [`docs-bundle/reference/spec.md`](docs-bundle/reference/spec.md) |
| End-user documentation to point humans at | [`docs-bundle/`](docs-bundle/) |

## Repo layout

```text
okf-loom/
├── SKILL.md                 # this loadable skill entrypoint
├── resources/               # small agent-readable guidance files
├── scripts/
│   ├── okf-loom              # checked-in helper command
│   ├── okf_loom/             # checkout-local runtime package
│   ├── build_skill_archive.py
│   ├── capture_readme_media.py
│   ├── capture_viewer_proof.py
│   ├── capture_signal_controls.py
│   └── lint-js.sh
├── docs-bundle/             # OKF documentation bundle
├── samples/                 # example OKF bundles
├── tests/                   # pytest proofs for checkout scripts
├── docs/                    # README media (docs/media/), screenshots, design plans
├── README.md                # human overview
├── AGENTS.md                # thin pointer for agents that read AGENTS first
└── CLAUDE.md                # thin pointer for agents that read CLAUDE first
```

## Core commands

```bash
# Validate a bundle.
scripts/okf-loom validate path/to/bundle --strict

# Inspect / search.
scripts/okf-loom info path/to/bundle
scripts/okf-loom graph-quality path/to/bundle
scripts/okf-loom search path/to/bundle "customer order" --mode hybrid

# Serve the live collaborative studio (add --tunnel for a public link).
scripts/okf-loom serve path/to/bundle --no-open

# Discover gaps and apply safe mutations.
scripts/okf-loom discover path/to/bundle --out /tmp/okf-suggestions.json
scripts/okf-loom link-add --bundle path/to/bundle --source tables/orders --target tables/customers

# Collaboration loop while a studio is up (current spec §12).
scripts/okf-loom wait path/to/bundle        # blocks until a comment/change arrives
scripts/okf-loom comment-claim path/to/bundle <comment-id> --summary "what I will do"
scripts/okf-loom comment-resolve path/to/bundle <comment-id> --summary "what I did"
```

## Non-negotiables

- Use `scripts/okf-loom`; do not assume an installed package or console script.
- PyYAML is preferred when present, but the bundled fallback is enough for normal runtime/docs validation and fails closed on unsupported advanced YAML.
- Do not add PyPI/package-publishing flow back as the primary path.
- Preserve unknown frontmatter keys and hand-authored bundle content.
- `type` is the only hard-required concept frontmatter key under OKF v0.1.
- Broken links are warnings by default for consumers; authoring mutators fail
  closed on missing targets unless `--allow-forward-reference` is explicit.
- All okf-loom writes must stay atomic and marker-safe.

For deeper rules, read [`resources/gotchas.md`](resources/gotchas.md).
