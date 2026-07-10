# Advanced operations

## Search

All six modes are dependency-free (`lexical`, `semantic`, `hybrid`,
`tag`, `entity`, `relation`; the default mode comes from
`okf-loom.config.yaml search.default_mode`):

```bash
scripts/okf-loom search docs-bundle "customer order" --mode lexical
scripts/okf-loom search docs-bundle "customer order" --mode semantic
scripts/okf-loom search docs-bundle "customer order" --mode hybrid
scripts/okf-loom search docs-bundle "natural-language question" --mode semantic --min-semantic-score 0.1
scripts/okf-loom search docs-bundle "customer order" --mode hybrid --hybrid-require both
scripts/okf-loom search docs-bundle "orders" --mode tag
scripts/okf-loom search docs-bundle "Order" --mode entity
scripts/okf-loom search docs-bundle "" --mode relation --relation depends_on
```

`semantic` is SemanticLite: token + character-trigram cosine over title,
description, type, headings, tags, and entity labels. It is not a dense local
embedding backend. See
[`docs-bundle/reference/config_yaml.md`](../docs-bundle/reference/config_yaml.md)
for every config key, and [`studio-agent-loop.md`](studio-agent-loop.md)
for the serve-time collaboration loop.

SemanticLite retains every positive overlap by default. Use
`--min-semantic-score` for an explicit no-match floor. Hybrid RRF is a rank
score, not calibrated relevance; JSON results expose matched backends and
component scores/ranks, and `--hybrid-require` can require lexical, semantic,
or both forms of evidence.

## Discovery, plans, and repair

```bash
scripts/okf-loom discover docs-bundle --out /tmp/okf-suggestions.json
scripts/okf-loom graph-quality docs-bundle --format json
scripts/okf-loom plan docs-bundle --out /tmp/okf-plan.json
scripts/okf-loom update docs-bundle --plan /tmp/okf-plan.json --dry-run
scripts/okf-loom repair docs-bundle --indexes --dry-run
```

Use `--scope ID,ID --neighbors` for fast per-change enrichment.
Use `graph-quality` after imports or large curation passes to distinguish
valid-but-thin OKF from a bundle that will read well in graph view.

## Viewer/build

```bash
scripts/okf-loom serve docs-bundle --no-open
scripts/okf-loom build docs-bundle --target static --out /tmp/okf-docs-site
scripts/okf-loom render docs-bundle --out /tmp/viz.html
```

Viewer overrides are documented in
[`scripts/okf_loom/viewer/OVERRIDES.md`](../scripts/okf_loom/viewer/OVERRIDES.md).

## Capabilities and config

```bash
scripts/okf-loom capabilities
scripts/okf-loom capabilities --bundle docs-bundle --format json
```

The capability registry lives in
[`scripts/okf_loom/extensions.py`](../scripts/okf_loom/extensions.py).
