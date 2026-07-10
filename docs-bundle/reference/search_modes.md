---
type: Reference
title: Search modes
description: The six search modes behind one `--mode` flag, their backends, and the `SearchBackend` Protocol for plugging in custom backends.
resource: /reference/search_modes.md
tags: [search, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Search modes

okf-loom's `search` command and the live studio's `/__search`
endpoint dispatch to one of six modes behind a single `--mode` flag.
**All six modes are dependency-free** out of the box: lexical (BM25),
semantic (fuzzy-semantic SemanticLite), and hybrid (RRF fusion of the
two) require no extra packages.
See [index.md](/reference/index.md) for the rest of the reference quadrant.

# Mode summary

| Mode | Backend | What it ranks by | Activate |
|---|---|---|---|
| `lexical` (default) | `LexicalBackend` | Field-weighted BM25 (title 5× > headings 3× > description 2× > body 1× > tags 1×). | Always. |
| `tag` | `LexicalBackend` | Tag membership (case-insensitive); ties broken by title. | Always. |
| `semantic` | `SemanticLiteBackend` | Fuzzy-semantic similarity over a per-concept profile. `α·cosine(tokens) + (1-α)·cosine(char-trigrams)`, `α=0.6`. | Always. |
| `hybrid` | `HybridBackend` | Reciprocal Rank Fusion (k=60) of lexical ∪ semantic-lite. | Activates `okf.cap.search_hybrid` on first successful match. |
| `entity` | (in-module) | Match against concept id, title, description, entity labels, and aliases. | Activates `okf.cap.entities` / `okf.cap.aliases` when those keys are present. |
| `relation` | (in-module) | Filter typed-relation + markdown graph edges by `--relation` / `--source` / `--target`. | Activates `okf.cap.typed_relations` when `relations:` is present. |

All modes accept the cross-cutting filters `--type TYPE` and `--tag TAG`
(applied as post-filters).

# `lexical` — BM25

Pure-Python BM25 with field weighting. Default mode. Zero deps.

```bash
scripts/okf-loom search path/to/bundle "customer order"
scripts/okf-loom search path/to/bundle "user" --type "API Endpoint"
```

## Scoring

For query terms `t` in field `f` of document `d`:

```
idf(t, f)     = ln( (N - df(t, f) + 0.5) / (df(t, f) + 0.5) + 1 )
bm25(t, d, f) = idf(t, f) * ( tf(t, d, f) * (k1 + 1) )
                                / ( tf(t, d, f) + k1 * (1 - b + b * dl(d, f) / avgdl(f)) )
bm25(q, d, f) = Σ_t bm25(t, d, f)
score(q, d)   = Σ_f weight(f) * bm25(q, d, f)
```

| Symbol | Default | Meaning |
|---|---|---|
| `k1` | `1.5` | Term-frequency saturation. |
| `b` | `0.75` | Length normalisation. |
| `N` | corpus size | Total number of concepts. |
| `weight(f)` | title 5.0, headings 3.0, description 2.0, body 1.0, tags 1.0 | Field weight. |

The `+1` inside the `idf` log keeps idf strictly positive (the
standard variant).

## Determinism

Documents are scored in sorted concept-id order and the final ranking
tie-breaks by concept id. The same `(bundle, query)` always yields
the same ordering.

## Tokenisation

Lowercase, split on Unicode word boundaries, drop a small English
stopword set, split CJK runs into single-character tokens
(so `比特币` indexes as `["比", "特", "币"]`).

# `tag` — tag membership

```bash
scripts/okf-loom search path/to/bundle "#sales" --mode tag
scripts/okf-loom search path/to/bundle "sales" --mode tag
```

Matches `Concept.tags` case-insensitively. Sorted by title. The
`#` prefix is optional. Every hit carries a constant `score=1.0`
plus `matched_tags` listing the subset of the concept's own tags
that matched.

# `semantic` — SemanticLite

```bash
scripts/okf-loom search path/to/bundle "customer order" --mode semantic
```

**Not embeddings.** Combines token-level TF cosine with
character-trigram TF cosine over a weighted per-concept profile.

| Component | Detail |
|---|---|
| Token cosine | Shares the lexical tokenizer; rewards shared domain terms. |
| Trigram cosine | Robust to typos and morphology (`orders` ≈ `order`). |
| Profile string | `title ×3 + description ×2 + type + headings + tags + entity labels + aliases` (the last two only if `okf.cap.entities` / `okf.cap.aliases` are active). |
| Score | `α * cosine(tokens) + (1-α) * cosine(trigrams)`, `α=0.6`. |

SemanticLite is the cheap fuzzy-semantic layer that helps an agent find
candidate concepts. Any positive token or trigram overlap is retained by
default for backward compatibility, so a positive score means similarity, not
an automatic relevance judgement. Use an explicit floor when false positives
must become a trustworthy no-match:

```bash
scripts/okf-loom search path/to/bundle "natural-language question" \
  --mode semantic --min-semantic-score 0.1
```

The value is a SemanticLite cosine threshold in `[0,1]`; tune it against
labelled relevant, paraphrase, and irrelevant queries for the bundle. A
downstream LLM can rerank returned candidates but cannot recover a relevant
concept that retrieval did not return. Dense embeddings are therefore a
legitimate optional `SearchBackend` when paraphrase recall is a product
requirement, though no dense provider ships in the six current modes.

# `hybrid` — RRF fusion

```bash
scripts/okf-loom search path/to/bundle "customer order" --mode hybrid
```

Reciprocal Rank Fusion of `LexicalBackend` and `SemanticLiteBackend`,
both zero-dep.

```
rrf(d) = Σ_backend 1 / (k + rank_backend(d))    with k = 60
```

Concepts missing from a backend contribute 0 from that backend.
Sort by `(-rrf, concept_id)`. A successful hybrid query (at least
one match) activates `okf.cap.search_hybrid` for the bundle.

RRF is a rank-fusion value, not calibrated relevance. Hybrid results include
the evidence needed to interpret it:

- `detail.matched_backends`: `lexical`, `semantic-lite`, or both;
- `detail.component_scores`: each matching backend's native score;
- `detail.component_ranks`: each matching backend's 1-based rank.

`--min-semantic-score N` gates SemanticLite candidates before fusion.
`--hybrid-require any` preserves the legacy union; `lexical`, `semantic`, or
`both` require that evidence before a hit can enter the RRF result set.

# `entity` — entity/alias match

```bash
scripts/okf-loom search path/to/bundle "order" --mode entity
```

Match against concept id, title, description, entity labels, and
aliases. Honours `okf.cap.entities` (the `entities:` frontmatter
key) and `okf.cap.aliases` (the top-level `aliases:` key). Useful
for "what concept represents this business term?" lookups when
synonyms and aliases matter.

# `relation` — typed-relation / graph-edge filter

```bash
scripts/okf-loom search path/to/bundle "" --mode relation --relation depends_on
scripts/okf-loom search path/to/bundle "" --mode relation --source tables/orders
scripts/okf-loom search path/to/bundle "" --mode relation --relation writes_to --target tables/orders
```

Filter typed-relation declarations (`relations:` frontmatter key)
plus markdown graph edges. The query string is optional when at
least one of `--relation`, `--source`, or `--target` is supplied.
Honours `okf.cap.typed_relations`.

| Flag | Effect |
|---|---|
| `--relation TYPE` | Keep only edges of this relation type. |
| `--source ID` | Keep only edges whose source concept id matches. |
| `--target ID` | Keep only edges whose target concept id matches. |

# Cross-cutting filters

| Flag | Applies to | Behaviour |
|---|---|---|
| `--type TYPE` | Every mode | Keep only concepts whose `type` equals this string (exact match). |
| `--tag TAG` | Every mode | Keep only concepts carrying this tag (case-insensitive). |
| `--limit N` | Every mode | Maximum number of results (default `20`). |
| `--min-semantic-score N` | `semantic`, `hybrid` | Opt-in SemanticLite cosine floor in `[0,1]`; applied before Hybrid RRF. |
| `--hybrid-require MODE` | `hybrid` | Evidence policy: `any` (default), `lexical`, `semantic`, or `both`. |

# Result shape

```json
{
  "concept_id": "tables/orders",
  "title": "Orders",
  "score": 0.0325,
  "snippets": ["…one row per completed customer order…"],
  "source_backend": "hybrid",
  "matched_tags": [],
  "description": "One row per completed customer order.",
  "detail": {
    "matched_backends": ["lexical", "semantic-lite"],
    "component_scores": {"lexical": 12.41, "semantic-lite": 0.72},
    "component_ranks": {"lexical": 1, "semantic-lite": 2},
    "fusion": "rrf"
  }
}
```

| Field | Notes |
|---|---|
| `concept_id` | The dotted path with `.md` stripped (e.g. `tables/orders`). |
| `title` | Back-filled from the content index if the backend emitted a placeholder. |
| `score` | Backend-specific. Tag-mode results carry `1.0`. |
| `snippets` | 0..N ~120-char excerpts centred on first matches, ellipsized. |
| `source_backend` | `lexical` / `semantic-lite` / `hybrid` / `tag`. |
| `matched_tags` | Populated in tag mode; empty otherwise. |
| `description` | Back-filled from the content index for richer result context. |
| `detail` | Optional backend evidence. Hybrid includes matched backends, native component scores/ranks, and `fusion: rrf`. |

# `SearchBackend` Protocol

```python
@runtime_checkable
class SearchBackend(Protocol):
    name: str
    def index(self, content_index: ContentIndex) -> None: ...
    def search(self, query: str, *, limit: int) -> list[SearchResult]: ...
```

A backend indexes the canonical `ContentIndex` once and answers
ranked queries. `name` identifies the backend on `SearchResult`.

Backends consume the canonical
[`ContentIndex`](/reference/architecture.md) (the Quartz "one model"
pattern) so search, graph, and listings all read the same object.

# Caching

Every backend (lexical, semantic-lite) is cached on the `Bundle`
instance itself. The cache is released when the bundle is GC'd and
is reset by `Bundle.invalidate()`. In a long-running server that
reloads bundles, reload the bundle (or call `bundle.invalidate()`)
to evict.

`okf_loom.search.clear_search_cache()` is retained as a
deprecated no-op for backward compatibility.

# HTTP entry point

The live studio exposes search at `GET /__search?q=…` (HTML) and
`GET /__search?q=…&format=json` (JSON). The HTTP path always uses
the lexical backend with a limit of `30`; for other modes use the
CLI. See [http_routes.md](http_routes.md).

# Defaults

The bundle-root [`okf-loom.config.yaml`](config_yaml.md) `search.default_mode`
key selects the mode used when no `--mode` is passed. Default is
`lexical`; allowed values are the six mode names (fail-closed on
unknown values).

# When to use which mode

| Goal | Use |
|---|---|
| Default keyword search | `lexical` |
| Tag browse | `tag` |
| Typo-tolerant / fuzzy match | `semantic` |
| Best of keyword + fuzzy | `hybrid` |
| Find the concept for a business term (with aliases) | `entity` |
| Find concepts connected by a typed relation | `relation` |

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md § search](cli.md#search) — the CLI command and its flags.
- [capabilities.md](capabilities.md) — `okf.cap.search_lexical`, `okf.cap.search_hybrid`, `okf.cap.entities`, `okf.cap.aliases`.
- [http_routes.md](http_routes.md) — `/__search`.
- [config_yaml.md](config_yaml.md) — `search.default_mode`.
- [/explanation/zero_dependencies.md](/explanation/zero_dependencies.md) — why no embedding backend by default.
- [/how-to/discover_and_fix_gaps.md](/how-to/discover_and_fix_gaps.md) — `entity` / `relation` modes in the discovery workflow.
- `okf-loom/scripts/okf_loom/search.py` — authoritative source.
