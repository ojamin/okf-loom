---
type: How-to
title: Build a static site
description: Render an OKF bundle to a portable static site with the build command, choosing the SPA, static, or single-file target, and optionally embedding it inside another site.
resource: /how-to/build_static_site.md
tags: [build, render, static-site, viewer]
timestamp: "2026-06-29T00:00:00Z"
---

# Build a static site

This guide takes a bundle on disk and ships it as a static site you can
host anywhere — GitHub Pages, Netlify, an intranet, or a `file://` URL.

You already have a bundle that passes validation.
If you do not, [Author your first bundle](/tutorials/first_bundle.md)
gets you there.

# Step 1: Choose a build target

The build command ships three targets, each tuned for a different shipping
context.

| Target | When to use |
|---|---|
| `spa` | Quartz-style default for a published reading experience. Client-side rendering; richest interactions. |
| `static` | Server-rendered, JS-optional, printable, SEO- and airgapped-friendly. |
| `single-file` | One self-contained HTML with everything embedded. Email it, attach to a ticket, open from `file://`. |

Pick `static` when you want a hostable site that still works without
JavaScript.
Pick `spa` when you want the smoothest reading experience and do not mind
requiring JS.
Pick `single-file` when the artifact has to travel as one file.

# Step 2: Build the site

Run the checkout-local build command with the target and an output directory.

```bash
scripts/okf-loom build path/to/bundle --target static --out dist/
```

The `dist/` directory now contains the full site: rendered HTML pages, a
copy of the graph/search data, and the viewer assets. Static graph and search
pages also carry inert embedded copies of their data so direct-file browsing
does not depend on browser-blocked `fetch()` calls.

The other targets take the same flags:

```bash
scripts/okf-loom build path/to/bundle --target spa --out dist/
scripts/okf-loom build path/to/bundle --target single-file --out dist/viz.html
```

# Step 3: Preview the build locally

Open `dist/index.html` directly to exercise the `file://` contract. Concept
pages, graph data, and static search all load from the directory without an
HTTP server. Graph/rendering enhancement libraries still follow the viewer's
CDN configuration, so use local overrides when the environment is fully
air-gapped.

For an HTTP-hosted preview, any static file server works.

```bash
python3 -m http.server --directory dist 8000
```

Open `http://127.0.0.1:8000/` and click around to confirm the render
before you ship.

For a quick single-file preview, `render` writes one self-contained
`viz.html` without invoking the full build pipeline.

```bash
scripts/okf-loom render path/to/bundle --out viz.html
```

`render` is the right tool when you want one portable artifact and do
not need the multi-file site.

# Step 4: Handle active code (overrides and plugins)

If the bundle ships viewer overrides or plugins that run JavaScript —
custom templates, a `ViewerPlugin`, or `viewer.allow_active_code: true` in
`okf-loom.config.yaml` — the build refuses to execute them unless you give
operator consent.

```bash
scripts/okf-loom build path/to/bundle --target spa --out dist/ --allow-active-code
```

The two-layer gate is intentional: the bundle declares it wants active
code, and the operator explicitly allows it at build time.
Without consent, the build still succeeds but the overrides and plugins
are inert.

Set `OKF_LOOM_ALLOW_ACTIVE_CODE=1` in the build environment to consent
non-interactively, for example in a CI job.

# Step 5: Ship the site

The `dist/` directory contains all generated pages and bundle data; copy it
anywhere. Optional CDN-configured enhancement libraries are external unless
you provide local viewer assets.

For GitHub Pages, drop it into the `gh-pages` branch or point Pages at the
build output of your CI workflow.
For Netlify or Vercel, point the deploy command at `dist/`.
For an airgapped intranet, copy the directory onto the static file server
of your choice.

Because the `static` target is JS-optional, the site renders legibly even
in a restricted browser.

# Step 6: Embed the site inside another site

The `single-file` target is the easiest embed: drop the `viz.html` into
an `<iframe>` on your existing docs site.

```html
<iframe src="/okf/viz.html" width="100%" height="800" frameborder="0"></iframe>
```

For a deeper integration — your own chrome around the viewer, a custom
build step, or running the live server inside another process — see
[Embed the viewer in an agent harness](/how-to/embed_in_harness.md).

# Step 7: Automate the build in CI

Pair the build with the validate gate so a broken bundle never ships.

```yaml
# .github/workflows/okf-build.yml
name: OKF build
on:
  push:
    branches: [main]
    paths: ['docs-bundle/**']
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: scripts/okf-loom validate docs-bundle --strict --profile producer
      - run: scripts/okf-loom build docs-bundle --target static --out dist/
      - uses: actions/upload-pages-artifact@v3
        with:
          path: dist/
```

See [Validate a bundle in CI](/how-to/validate_in_ci.md) for the full
validate workflow.

# End state

You now have a repeatable build that:

- Turns a bundle into a hostable site in one command.
- Picks the right target for the shipping context.
- Honours the active-code gate when overrides or plugins are present.

# See also

* [Validate a bundle in CI](/how-to/validate_in_ci.md) — gate the build behind strict validation.
* [Embed the viewer in an agent harness](/how-to/embed_in_harness.md) — deeper integration shapes.
* [CLI command reference](/reference/cli.md) — every flag on `build` and `render`.
* [HTTP server routes](/reference/http_routes.md) — the routes the live server and SPA share.
* [okf-loom architecture](/explanation/architecture.md) — why one content model renders to many targets.

Return to [How-to guides](/how-to/index.md).
