---
type: Explanation
title: Best-of-Breed Research
description: Historical research input — comparative analysis of Obsidian, Logseq, Notion, MkDocs Material, Hugo, Jekyll, Docusaurus, Quartz v4, Foam, Dendron, semantic search libraries, NER/relation extraction tooling, knowledge-graph stacks, Diátaxis, and consumer note apps. Informs okf-loom's design choices.
resource: /explanation/research.md
tags: [research, design, comparison, obsidian, quartz, mkdocs, docusaurus, diataxis]
timestamp: "2026-06-29T00:00:00Z"
---

# okf-loom — Best-of-Breed Research

> **Note:** this document is retained research input for the v1.0 baseline.
> Several recommendations it makes — a local dense-embedding backend
> (`sentence-transformers`), a `[semantic]` extra, an `[llm]` extra, a
> portable `vectors.jsonl` reader, and an on-disk SQLite FTS table — were
> prototyped and **later removed**. The surviving search story
> is fully zero-dependency: lexical (BM25) + fuzzy-semantic (`SemanticLite`)
> + hybrid (RRF fusion). `SemanticLite` stays as a cheap typo/morphology
> candidate layer, while dense retrieval remains a legitimate optional
> integration when broad paraphrase recall is a product requirement. Read the
> dense/`[semantic]`/`[llm]`/`vector-index`/`sqlite-fts` passages below as
> "explored, then dropped," not as the current runtime surface. The current
> surface lives in `SKILL.md`, `resources/advanced-operations.md`, and
> `docs-bundle/reference/architecture.md`.

**Status:** Research input for the architect and implementation subagents.
**Scope:** What okf-loom (validator, structure/link/content checks, wiki viewer, lexical + semantic search, auto-discovery, plugin extension points) should borrow from existing knowledge/documentation tools.
**Method:** Facts gathered from upstream READMEs/docs (Quartz, MkDocs Material, Dendron, sentence-transformers, sqlite-vec, LanceDB, GLiNER, MiniSearch, Lunr, Pagefind, the OKF SPEC v0.1) plus established, stable behavior of the commercial/hosted systems.
**Grounding note (important):** The task brief references an "upstream OKF `viz.html`" that embeds the bundle as JSON with Cytoscape.js + marked. As of the current upstream `GoogleCloudPlatform/knowledge-catalog` `okf/` tree (SPEC v0.1 — Draft), **no `viz.html` exists**; the tree is `SPEC.md`, `bundles/`, `samples/` (`crypto_bitcoin`, `ga4_merch_store`, `stackoverflow`), `src/`, `tests/`. This document therefore treats the described Cytoscape+marked, single-self-contained-HTML pattern as a *reference design to evaluate*, not an existing artifact, and Section 15 compares it on those merits.

OKF essentials assumed throughout (per SPEC §3–§11): bundle = directory tree; concept = YAML frontmatter (`type` required; `title`/`description`/`resource`/`tags`/`timestamp` recommended; arbitrary keys allowed); reserved `index.md` (progressive-disclosure listing) and `log.md` (ISO-8601 `YYYY-MM-DD` dated history, newest first); concept ID = path sans `.md`; cross-links absolute (`/tables/users.md`, RECOMMENDED) or relative (`./other.md`); consumers MUST tolerate broken links; conventional body headings `# Schema` / `# Examples` / `# Citations`; bundles MAY declare `okf_version` in a root `index.md`; minor = additive, major = breaking.

Systems are ordered by **relevance to OKF**, not alphabetically.

---

## 1. Quartz v4  *(highest relevance — published Obsidian-style vaults)*

**(a) What it is.** Quartz is a static-site generator that turns a folder of interlinked markdown ("digital garden") into a website. It is the closest existing analog to the OKF viewer, and it is explicitly built around the same primitives OKF standardizes: markdown + YAML frontmatter, `index.md` overrides, tag/folder listing pages, cross-links, broken-link tolerance.

**(b) Mechanisms worth stealing.**

- **Plugin pipeline = map / filter / reduce over content.** This is the single most transferable idea. Quartz config (`quartz.config.ts`) declares `transformers`, `filters`, `emitters`:
  - *Transformers* **map** over each content node, in order (e.g. `FrontMatter`, `CreatedModifiedDate`, `SyntaxHighlighting`, `ObsidianFlavoredMarkdown`, `GitHubFlavoredMarkdown`, `TableOfContents`, `CrawlLinks`, `Description`, `Latex`). Some are position-sensitive.
  - *Filters* **decide** which nodes emit (e.g. `RemoveDrafts`).
  - *Emitters* **reduce** across the whole set (e.g. `FolderPage`, `TagPage`, `ContentIndex`, `Assets`, `Static`, `RSS`, `AliasRedirects`).
  This is a clean, ordered, composable pipeline — an almost exact fit for OKF's "validator / structure checks / link checks / content checks / index+log generation / viewer build" stages. OKF should adopt this three-phase taxonomy directly.

- **One shared content index feeds many features.** The `ContentIndex` emitter produces a single `contentIndex.json` that **both full-text search and the graph view consume**. Don't build separate indexes per feature; emit one canonical graph+document model and let components read it. (SPEC §4.2 already nudges toward "structural markdown" — the index should preserve headings/lists/tables, not just flat text.)

- **Link resolution as a first-class, configurable transformer.** `CrawlLinks` exposes `markdownLinkResolution: "absolute" | "relative" | "shortest"` and `prettyLinks` (strip folder paths in display text). OKF's link rules (SPEC §5: absolute RECOMMENDED, relative allowed, broken links tolerated) map 1:1 onto this; the validator should implement the same resolution modes and the viewer should rewrite links for whatever serving context it's in.

- **Frontmatter parsing with alias collapsing.** Quartz's `Frontmatter` transformer (built on `gray-matter`) accepts YAML or TOML, configurable delimiters, and **collapses many source aliases to canonical fields** (`tags`/`tag`, `aliases`/`alias`, `created`/`date`, `modified`/`lastmod`/`updated`/`last-modified`, `draft`/`publish`). OKF should do the same: a small alias map per canonical field, so producer variety doesn't break consumption (and aligns with SPEC §9's "preserve unknown keys, tolerate variety").

- **Auto-generated folder/tag listings that hand-authored files override.** `FolderPage` and `TagPage` emitters synthesize listing pages, but if a directory already contains a hand-written `index.md` (with `title`), Quartz uses *that* instead. Tag hierarchies (`plugin/emitter`) render one page per tag level plus a global `/tags` index. This is exactly the OKF §6 model and gives a clean answer to "regenerate `index.md` without destroying hand-curated content" (Section C): **synthesize by default, defer to any existing hand-authored file, and only touch generated regions.**

- **Graph view.** Local graph (all notes ≤1 hop away) + global graph; node radius ∝ degree; visited nodes colored differently; force-directed with tunable `repelForce`/`centerForce`/`linkDistance`/`depth`; an Obsidian-style radial mode. Runs client-side off `contentIndex.json`. Cheap and high-impact.

- **Full-text search.** Powered by [FlexSearch](https://github.com/nextapps-de/flexsearch); "<10ms for half a million words." Strips markdown syntax before indexing, builds **separate indexes for title/content/tags with title weighted highest**, tokenizes CJK, exposes tag search via a `#`-prefix or `Ctrl+Shift+K`. Fully keyboard-navigable.

- **Other portable patterns:** SPA routing (relative URLs so it works "no matter where you deploy"); `ignorePatterns` (globs for private/templates); `enablePopovers` (Wikipedia-style hover preview using a `popover-hint` CSS class to select what to preview); composable layout via `Flex`/`MobileOnly`/`DesktopOnly`/`ConditionalRender`; a philosophy of **three customization tiers** (content-only / config knobs / full source) — directly applicable to "embeddable in any agent/meta-harness."

**(c) What does NOT translate.**

- Quartz is **TypeScript/Node**, opinionated for *publishing personal gardens*. OKF is Python-first, harness-agnostic, and must validate/transform as a library, not just render a site. Steal the *pipeline architecture*, not the toolchain.
- Quartz's wikilink/transclusion syntax (`![[...]]`, `[[x#^block]]`) is richer than OKF's standard-markdown links. OKF should support only standard markdown links + its two resolution modes, and treat wikilinks as an *opt-in transformer* (the `ObsidianFlavoredMarkdown` plugin), never core.
- `baseUrl`/analytics/CDN-font concerns are publishing concerns, not knowledge-format concerns.

**What OKF should steal:** the **transformer / filter / emitter pipeline** as the core extension model; a **single canonical content index** (graph + docs) consumed by search, graph view, and listings; **configurable link resolution** (absolute/relative/shortest) + **frontmatter alias collapsing**; **synthesize-but-don't-clobber** behavior for `index.md`/tag pages; **local + global graph** with degree-weighted nodes; **field-weighted, markdown-stripped full-text search** with tag-mode; the **three-tier customization philosophy**.

---

## 2. Obsidian  *(the reference UX consumers expect)*

**(a) What it is.** A local-first personal knowledge base: a "vault" = a folder of markdown. Closed-source desktop/mobile app with a deep plugin ecosystem. It is the de-facto UX benchmark; OKF's viewer will be judged against it.

**(b) Mechanisms worth stealing.**

- **Backlinks panel + outgoing links panel + unlinked mentions.** Obsidian shows, per note, what links *to* it, what it links *to*, and *plain-text mentions of the note's title that aren't yet links* ("unlinked mentions" with a one-click convert-to-link). The last one is **directly an auto-discovery primitive**: OKF's "discover missing links" can mine unlinked mentions of known concept titles/IDs across bodies. High leverage.
- **Graph view** (force-directed, filterable by tag/path/search, color by folder/tag/created-time, drag/zoom). Same idea as Quartz, richer filters.
- **Tags + nested tags** (`#area/data`, navigable). SPEC §4.1 makes tags first-class; nested tags are a cheap win for browsing.
- **Wikilinks `[[note]]` vs markdown links** with a *configurable default* and graceful coexistence. OKF keeps standard markdown links as canonical, but should *accept* wikilinks as a soft input.
- **Transclusion/embeds** (`![[note]]`, `![[](image.png|100x100)]]`). For OKF, an optional "embed concept" transformer that inlines another concept's `# Schema`/`# Examples` section at render time — useful, but keep it as a plugin.
- **Dataview plugin** (query your vault with a SQL-like/JS DSL over frontmatter + the link graph, render as tables/lists). This is the "relations/queries" primitive OKF should offer over its content index (see Notion §4 and Synthesis A).
- **Plugin architecture** (manifest + lifecycle hooks + settings tab; community plugins via a registry). Inspires OKF's plugin contract.

**(c) What does NOT translate.**

- **Proprietary binary cache** (`.obsidian/`) and a desktop GUI. OKF is plain files + a server/static HTML.
- **Block references** (`^id`) require stable, author-maintained anchors — heavy for a generator-written format; OKF should link to *concepts* and *headings*, not arbitrary blocks.
- **Wikilink-as-default** conflicts with SPEC §5's standard-markdown + absolute-path recommendation. Wikilinks are a convenience layer only.

**What OKF should steal:** **unlinked-mentions → link suggestions** (core discovery); backlinks/outgoing panels; **Dataview-style queries** over the content index as a first-class feature; nested tags; an optional transclusion transformer; the *feel* of a plugin registry.

---

## 3. MkDocs Material  *(reference for docs-site conventions)*

**(a) What it is.** The most popular MkDocs theme: markdown → polished, searchable static docs site. Server-rendered (Python `mkdocs build`), statically hostable anywhere.

**(b) Mechanisms worth stealing.**

- **`nav` + auto-discovery.** Explicit `nav` in `mkdocs.yml` for hand-curated order, with auto-discovery fallback. Maps to OKF's hand-authored `index.md` vs synthesized listings (Quartz §1 pattern again).
- **Built-in `tags` plugin** (tags index, tag listings, `tags.yml` metadata) — the same "synthesize tag pages" idea, mature and well-tested.
- **Built-in `search` plugin** (lexicographic, section-aware, highlighting) and optional **Algolia DocSearch** integration for large corpora. The "small built-in + optional hosted backend" split is exactly OKF's "zero-dep baseline + pluggable semantic backend."
- **`blog` and `projects`** plugins (chronological posts; monorepo of sub-docs). The blog model maps to OKF's `log.md` (date-grouped, newest-first) — a viewer could render `log.md` as a timeline.
- **Hooks** (Python event functions: `on_page_markdown`, `on_page_content`, `on_config`, …) — the canonical *server-side* plugin contract; pairs naturally with OKF's Python base.
- **Snippet includes** (`--8<-- "file.md"`) and `meta` per-page frontmatter, admonitions (`!!! note`).

**(c) What does NOT translate.**

- MkDocs is **single-page-at-a-time, server-rendered HTML** — no client-side graph view or SPA navigation. It is the *opposite* end of the spectrum from Quartz's SPA.
- `nav` is a single global YAML list; OKF is graph-shaped, not a strict tree, so a single linear nav is the wrong mental model.

**What OKF should steal:** **server-rendered static HTML as one of the build targets** (portability, no JS); **section-aware search with built-in + optional-hosted split**; **Python hook-style plugin lifecycle**; rendering `log.md` as a **timeline**; snippet-include as an optional transformer.

---

## 4. Notion  *(reference for structured/relational data)*

**(a) What it is.** A hosted block-based workspace. Its "databases" are tables whose rows are pages, with typed properties, **relations** between databases, and **rollups** (aggregates over related rows).

**(b) Mechanisms worth stealing.**

- **Typed properties + relations + rollups** as a model for *optional* structure. OKF's frontmatter is untyped free-form; an **optional typed-relation extension** (see §11 and Synthesis) could let a concept declare `depends_on: [/tables/customers.md]` or `owner: @alice`, with the viewer rendering a relations panel and computing simple rollups (count of backlinks, latest `timestamp` among children).
- **Filtered views** (table/board/calendar/gallery) over the same data. OKF's viewer could offer filterable listings over frontmatter (e.g. "all `type: BigQuery Table` tagged `sales`, sorted by `timestamp`").
- **Block model** (everything is a typed block; pages compose blocks). Too heavy for OKF, but the *idea* that a concept's body has addressable sections (`# Schema`, `# Examples`, `# Citations`) that can be embedded/queried is worth keeping.

**(c) What does NOT translate.**

- Notion is **proprietary, hosted, block-graph-in-a-database**, not plain files. It is not portable, not diffable, not git-friendly — the opposite of OKF's core values.
- Rollups require a typed schema and a database; OKF deliberately avoids a schema registry (SPEC §1 non-goals). Keep relations *optional and unvalidated beyond link-target-existence*.

**What OKF should steal:** an **optional typed-relation frontmatter layer** rendered as a relations panel + lightweight rollups; **filterable/sortable listings** over frontmatter (this is just a Dataview/MkDocs-tags hybrid — cheap).

---

## 5. Logseq  *(outliner + graph + journals)*

**(a) What it is.** Local-first open-source outliner-knowledge-base (markdown/org-mode files on disk), block-granular.

**(b) Worth stealing:** **block references** and **journals** (daily pages as a default capture surface); **Datalog queries** over the block graph (more rigorous than Obsidian's Dataview). **Doesn't translate:** the outliner model (indentation = structure) is alien to OKF's prose+headings; block-level references impose the same anchor-management cost as Obsidian's. **What OKF should steal:** the idea that **queries are a first-class reading surface**, and that a **dated "journal"-style surface** is a good home for `log.md`.

---

## 6. Foam / Dendron  *(markdown knowledge bases in the editor)*

**(a) What they are.** **Foam** = VSCode extension + conventions for a markdown knowledge base (graph, backlinks, wikilinks, templates), built on `markdown-it`. **Dendron** = VSCode extension with **note hierarchies** (`project.area.topic` dotted paths), **JSON note schemas** that validate hierarchy structure, **lookup** (fuzzy-find to traverse/create hierarchies), **multi-vault** (mix several git-backed folders), and a publish step.

**(b) Worth stealing:**
- **Dendron's schemas as an *optional* structural validator.** OKF forbids a global schema registry (SPEC §1), but a *bundle-local* schema ("concepts under `tables/` must have a `# Schema` section and a `resource` URI") is a natural plugin. This is how OKF can offer strict checks *without* breaking permissiveness: strictness is opt-in per subtree, layered atop the always-on conformance checks (SPEC §9).
- **Multi-vault** → OKF multi-bundle composition (mount several bundles, resolve `/...` links against a union root). Useful for org-wide cross-linking.
- **Foam's template snippets** (frontmatter templates for new concepts) → a `okf new` scaffolding generator per `type`.

**(c) Doesn't translate:** both are **editor-integrated** (VSCode); Dendron's dotted-path hierarchy is one opinion OKF shouldn't mandate (OKF paths are free-form).

**What OKF should steal:** **optional per-subtree structural schemas** as a plugin; **multi-bundle mounting**; **per-type frontmatter templates** for scaffolding; Dendron **lookup**-style fuzzy navigation in the viewer.

---

## 7. Lightweight markdown search  *(Pagefind, Lunr, MiniSearch, Stork, FlexSearch)*

| Tool | Stack | Model | Why it matters to OKF |
|------|-------|-------|------------------------|
| **Pagefind** | Rust indexer + WASM runtime + UI | **Post-build static**: scans built HTML, emits sharded fragment index; WASM searches in-browser; lazy-loads shards per query | Best-in-class for *static* sites: zero server, tiny bandwidth, scales to large corpora, supports `data-pagefind-filter` (facets) and `data-pagefind-weight`. The right default for OKF's static-HTML build target. |
| **FlexSearch** | JS | In-memory, prefix tokenizer | What Quartz uses; <10ms @ 500k words. Good for the live dev-server / SPA viewer. |
| **MiniSearch** | JS, **zero deps** | In-memory, prefix + fuzzy + field-boost + autosuggest + custom `tokenize`/`processTerm`/`extractField` | Best fit for a *self-contained single-HTML* viewer: tiny, offline, field-weighted, no build step. |
| **Lunr.js** | JS | In-memory, 14 stemmer languages, boost, fuzzy, wildcards | Mature classic; weaker than MiniSearch on API ergonomics. |
| **Stork** | Rust → WASM, config-driven index file | Build-time index, WASM search | Fine alternative to Pagefind for pre-built indexes. |

**The composite lesson:** OKF needs **two lexical search surfaces** — (1) a build-time shard index (Pagefind-style) for the static site, and (2) an in-memory JSON index (MiniSearch/FlexSearch-style) for the self-contained single-HTML file and the live dev server. Both should be generated from the *same* canonical content index (Quartz §1 lesson).

**What OKF should steal:** **Pagefind for the static site** (post-build, sharded, faceted); **MiniSearch for self-contained HTML** (zero-dep, field-weighted); expose a **single search plugin interface** so either (or the Python BM25 baseline) can sit behind it.

---

## 8. Hugo / Jekyll  *(content model + taxonomies + render hooks)*

**(a) Worth stealing:**
- **Taxonomies** (`tags`, `categories`, `series`, custom). Hugo auto-generates term-list pages and term pages — the same synthesize-listings pattern. OKF already makes `tags` first-class (SPEC §4.1) and explicitly says tag views are "synthesized at consumption time" (SPEC §3.1) — so this is *mandated by the spec*; okf-loom just implements it.
- **Sections / content organization** and **render hooks** (Hugo `link.render`-hook lets you rewrite every link — exactly the `CrawlLinks` idea, in Go).
- **Shortcodes** (`{{< foo >}}`) → OKF "include concept section" / "embed graph" transformers.
- **Fast incremental builds** (Hugo). OKF's watcher/dev-server should rebuild only changed concepts.

**(b) Doesn't translate:** both are **blog/docs SSGs** with a linear content model; OKF is graph-first. Shortcodes/template languages are non-portable. **What OKF should steal:** auto-generated **taxonomy pages** (mandated anyway), **link render-hooks**, **incremental rebuild** for the dev server.

---

## 9. Docusaurus  *(versioning + i18n + presets)*

**(a) Worth stealing:**
- **Versioning** of *docs* (snapshot a version, route under `/version-2.x`) via a `versioned_docs/` directory. OKF bundles already carry `okf_version` (SPEC §11); okf-loom could render bundle version-history (git tags) as a Docusaurus-style version switch.
- **Plugin/preset system** + **swizzlable theme components** (override a single React component). The "override one component without forking" idea maps to OKF template overrides.
- **Search**: Algolia DocSearch (hosted) **or** local search plugin; **i18n** with locale sub-paths.
- **MDX** (markdown + components).

**(b) Doesn't translate:** React/MDX toolchain; opinionated docs-site product. **What OKF should steal:** **version switcher** over bundle history; **swizzlable component overrides** as the *template-override* mechanism; **local-vs-hosted search split** (again).

---

## 10. Semantic search over markdown  *(embeddings + vector stores + hybrid)*

okf-loom already commits to this split: the baseline stays dependency-light with a PyYAML-preferred fallback, while richer semantic providers remain optional. The research confirms this is the right instinct and fills in the pluggable backend.

**Embedding options (offline-capable):**
- **sentence-transformers** (HuggingFace): dense bi-encoders (e.g. `all-MiniLM-L6-v2`, 384-dim), **Sparse encoders** (SPLADE — learned sparse, BM25-like but semantic), and **Cross-encoder rerankers** (`ms-marco-MiniLM`). Relevant efficiency techniques: **Matryoshka** embeddings (truncatable to smaller dims with little quality loss), **static embeddings** (no attention, CPU-friendly), **binary/scalar quantization**. Requires `torch` + `transformers` (heavy); fine as an *optional* extra.
- **API embeddings** (OpenAI, Cohere, Voyage, Nomic): higher quality, but require network + keys + money — explicitly out of scope for the "works without GPU/internet" baseline. Expose as an optional remote backend only.

**Vector stores for small/corpus-on-disk scale:**
- **sqlite-vec** (successor to sqlite-vss): pure C, **zero deps, runs anywhere SQLite runs — including WASM in the browser**, stores float/int8/binary vectors in `vec0` virtual tables, KNN queries. Mozilla-sponsored. *Ideal for OKF*: one SQLite file ships with the bundle; even the static viewer could query it via WASM. Companion `sqlite-lembed` (local GGUF embeddings) and `sqlite-rembed` (remote) exist.
- **LanceDB**: embedded (local, no server) on the Lance columnar format; **vector + full-text + SQL** hybrid in one engine; Python/JS/Rust. A heavier but self-contained option that unifies lexical + dense in a single query — attractive if okf-loom wants one store for everything.
- **FAISS / Chroma**: FAISS = library, index you manage yourself; Chroma = embedded vector DB (client/server). Both fine; heavier than sqlite-vec for OKF's scale.
- **In-memory cosine**: for very small bundles, just numpy/sklearn cosine over a 2-D matrix — no store at all. Good default for the `[semantic]` extra at small scale.

**Chunking markdown:** chunk by **heading** (so each `# Schema` / `# Examples` block is a retrievable unit), keep concept ID + heading path as metadata, and *also* keep a concept-level summary embedding (of `title`+`description`). Heading-chunking aligns with OKF's conventional body sections and gives citations that point to a section, not just a document.

**Hybrid BM25 + dense:** the robust recipe is **retrieval-time fusion** (BM25 candidate set ∪ dense candidate set, then reciprocal-rank-fusion or score normalization), optionally a **cross-encoder rerank** of the top-K. SPLADE can replace BM25 if you want learned-sparse without maintaining two systems.

**Works without GPU/internet?** Yes: TF-IDF/BM25 (pure Python or MiniSearch/Lunr) is the always-on baseline; `all-MiniLM-L6-v2` + sqlite-vec runs on CPU offline (download the model once); everything degrades gracefully to lexical-only when the extra isn't installed.

**What OKF should steal:** a **`SearchBackend` interface** with three providers — `(1)` lexical BM25/TF-IDF (always on, the `[semantic]`=sklearn default), `(2)` optional local dense (`sentence-transformers` + `sqlite-vec` or in-memory cosine), `(3)` optional remote (API embeddings) — combined via **reciprocal-rank-fusion** with optional cross-encoder rerank; **heading-aware chunking**; a concept-summary vector alongside per-section vectors.

---

## 11. Entity / relation extraction from prose  *(find missing relations)*

SPEC §5.3 says link *semantics* are conveyed by surrounding prose, and §5 says consumers MUST tolerate broken links. Auto-discovery of "missing" entities/relations means mining bodies for: (i) mentions of known concept titles/IDs that aren't linked, and (ii) novel entities/relations worth promoting to frontmatter or links.

**Tooling usable offline:**
- **GLiNER**: **zero-shot NER** ("give labels, get spans, no training"), plus **joint NER + relation extraction** via the **RelEx** architecture — *build a knowledge graph in one pass*. Optimized for **CPU**, supports INT8 quant and **ONNX export**. `gliner_small-v2.5`. This is the strongest single offline pick for "find entities/relations in markdown notes" with user-supplied label sets.
- **spaCy NER**: fast, rule-based (`EntityRuler`) + statistical; great for stable entity types and for `gliner-spacy` integration; deterministic and light.
- **REBEL** (seq2seq relation extraction): older, T5-based; superseded functionally by GLiNER-RelEx for new work.
- **LLM zero-shot extraction**: highest quality, highest cost; the `[llm]` extra's natural use — output *structured* relation suggestions (subject concept, relation type, object concept, evidence span) for human/agent review.

**Crucial design point:** extraction output must be **structured and attributable** so the managing agent can apply it reliably — an *agent-readable update plan* (a diff of proposed links/frontmatter keys + evidence + confidence). Whether that plan is applied directly by the agent (the current default — see [`/reference/spec.md`](/reference/spec.md) §10) or routed through an opt-in review step is a product choice, not a property of the extraction. This dovetails with Section C's "auto-update without destroying hand-curated content."

**Schema-aware extraction:** combine a bundle-local schema (Dendron §6) with GLiNER labels so extraction targets the relations *your bundle cares about* (e.g. for a BigQuery catalog: `joins`, `depends_on`, `owner`, `sla`).

**What OKF should steal:** a **discovery pass** = (a) unlinked-mention detection over known concept titles (Obsidian §2, pure string/fuzzy match — always on), (b) optional **GLiNER zero-shot NER + RelEx** (CPU, offline) for novel entities/relations, (c) optional **LLM** for high-quality relation hints — all emitting a **structured, attributable update plan** the managing agent applies through the mutators (directly by default in the current studio model — [`/reference/spec.md`](/reference/spec.md) §10; reviewable as an opt-in user constraint).

---

## 12. Knowledge-graph tooling  *(typed relations + interoperability)*

Neo4j/Memgraph (labeled property graphs: nodes + typed edges + properties), RDF/SPARQL (triples, open-world), n-quads/JSON-LD/schema.org (web-interop vocabularies). These represent **typed relations natively**; OKF deliberately doesn't (SPEC §5.3: link kind is conveyed by prose, graph edges are "untyped").

**Could OKF adopt an optional typed-relation frontmatter key without breaking the spec?** **Yes — and the spec already permits it.** SPEC §4.1 allows arbitrary frontmatter keys, and §9 forbids rejecting bundles over unknown keys. So a *convention* such as:

```yaml
relations:
  depends_on: [/tables/customers.md, /jobs/ingest.md]
  joins_with: [/tables/customers.md]
  owner: @data-platform
```

is **already spec-legal** as producer-defined keys. okf-loom should:
- **Recognize a small, conventional relation vocabulary** (`depends_on`, `references`, `part_of`, `derived_from`, `owner`, `see_also`) in a *capability/extension namespace* (e.g. prefix `okf.rel.*` or a `relations:` map) — *recognized, not required*.
- Render typed relations in the viewer as **labeled graph edges** (color/style per relation type) and in a relations panel.
- Optionally **export** the bundle to JSON-LD (using schema.org where types fit) or n-quads/Neo4j CSV for external graph tools — a one-way *bridge*, never a source of truth.
- Map untyped prose-links (the default) to a generic `relates_to` edge so the graph is never empty.

**Doesn't translate:** RDF open-world/IRI identity, SPARQL federation, graph DB transactions — far beyond OKF's "cat a file" philosophy. Keep KG interop as an *export transformer*.

**What OKF should steal:** an **optional, namespaced typed-relation frontmatter convention** (already spec-legal), rendered as labeled edges; a **JSON-LD / schema.org export transformer** for interop; treat the prose graph as the canonical, always-present `relates_to` baseline.

---

## 13. Diátaxis  *(a default concept-type taxonomy?)*

Diátaxis divides docs into **Tutorials** (learning by doing), **How-to guides** (achieving a goal), **Reference** (information-oriented, authoritative), **Explanation** (understanding-oriented). It's a *purpose* taxonomy, not a domain taxonomy.

**Relevance to OKF:** SPEC explicitly *declines* a fixed type taxonomy (§1 non-goals) and §4.1 lists example `type` values (`BigQuery Table`, `Playbook`, `Reference`, …). Diátaxis is therefore **not a mandate but a strong recommended default for doc-oriented bundles**: okf-loom could ship an *optional* Diátaxis preset that suggests `type: tutorial|how-to|reference|explanation`, validates each type's conventional sections (tutorials have steps; reference has schema), and renders a four-quadrant landing page. For data-catalog bundles it's irrelevant (those types are domain-specific). Keep it as an **opt-in type-vocabulary plugin**, never core.

**What OKF should steal:** ship Diátaxis as an **optional type taxonomy + section checker** for documentation bundles; never impose it on domain catalogs.

---

## 14. Roam / DEVONthink / Bear / Apple Notes  *(what consumers want)*

Surveyed for *expectations*, not mechanisms: consumers want **instant capture** (Roam's daily notes), **bi-directional links without ceremony** (Roam/Obsidian), **strong search + AI-assisted browsing** (DEVONthink's see-also/classify), **fast tag/note navigation + beautiful typography** (Bear), **ubiquity + sharing** (Apple Notes). Common thread: **low-friction linking, instant search, and "what's related?" assistance**. OKF's toolkit already targets all three via auto-discovery (§11) + hybrid search (§10) + graph/backlinks (§1/§2). Nothing here is portable as a *mechanism* (all are closed/local-DB apps); they confirm priorities.

**What OKF should steal:** *product priorities* — make linking and "see also" effortless and instant; these are the features users will reach for first.

---

## 15. Self-contained HTML viewer patterns  *(single-file vs SPA vs server-rendered)*

The brief's reference design (call it **Pattern A**: one HTML file embedding the whole bundle as JSON + Cytoscape.js + marked) sits at one end of a spectrum:

| Pattern | Example | Pros | Cons | OKF fit |
|---------|---------|------|------|---------|
| **A. Single self-contained HTML** (bundle-as-JSON inline) | OKF `viz.html` reference; **MiniSearch/Lunr** in-page | Maximally portable: email it, `file://`-open it, drop in any repo; git-diffable; no server; works offline | Re-encodes *all* content into one file → large for big bundles; no per-URL linking/bookmarking; Cytoscape.js is heavy (~hundreds of KB); search is whole-bundle-in-memory | **Great as a "share one bundle" export** and a zero-setup default; should be a *build target*, not the only surface |
| **B. SPA (multi-file static)** | **Quartz**, Obsidian-publish | Per-concept URLs (deep-linkable, bookmarkable); code-splits; rich interactivity (popovers, SPA nav, graph, search); CDN/any-static-host | Needs JS; less "single artifact"; client-side routing | **Best interactive reading experience**; default for the published site |
| **C. Server-rendered static** | **MkDocs Material**, Hugo | No-JS/SEO-friendly; clean URLs; printable | No client graph/SPA; rebuild to update | **Best for portability/SEO/print**; good default for "static docs" consumers |
| **D. Live HTTP dev server** | Quartz dev, MkDocs serve | Hot reload, interactive editing, run discovery live | Requires a process | **Essential authoring loop** |

**Recommendation (detailed in Synthesis B):** support **all four** from one content index, because they serve different deployment shapes OKF explicitly wants (portability, git-friendliness, interactivity, embedding in a harness). For Pattern A specifically: prefer **MiniSearch** (zero-dep, field-weighted) over rolling a bespoke search, and make the graph *lazy* (only the local subgraph on open) so a single-file viewer stays responsive. Avoid bundling Cytoscape.js unconditionally — offer a lighter force-directed renderer (e.g. `d3-force` + canvas/SVG) and treat the heavy Cytoscape option as an override.

**What OKF should steal:** treat single-file HTML as **one export target** (not the only viewer); default the static site to **Quartz-style SPA** for interactivity and **MkDocs-style server-rendered** for portability; always include a **live dev server**; generate **all surfaces from one canonical content index**.

---

# Synthesis

## A. Search architecture for OKF

**Layered design, all behind one interface.**

1. **Canonical content index (produced once).** A single in-memory model — `{ concepts: [{id, path, type, title, description, tags, timestamp, resource, headings, sections, body_text, frontmatter, out_links, in_links}], bundle: {okf_version, ...} }`. This is the Quartz `ContentIndex` lesson: search, graph, and listings all read the *same* object. It is also the validation graph (so link/structure checks share code with search).

2. **Layer 0 — always-on lexical (zero deps).** BM25/TF-IDF over **title + description + section bodies + tags**, with **field weights** (`title` > `heading`/`Schema`/`Examples` > body > tags-for-text), markdown stripped at index time, tokenization with CJK support, prefix/fuzzy matching. This baseline is implemented in pure Python (or, for the JS viewers, generated into a MiniSearch/FlexSearch index). Tag-mode search via `#`. This layer alone satisfies "search by term/relation/entity" for lexical needs and **works without GPU/internet**.

3. **Layer 1 — optional semantic (local, `[semantic]`/`[llm]` extras).**
   - **Embeddings:** prefer **sentence-transformers** `all-MiniLM-L6-v2` (CPU, offline, 384-dim, Matryoshka-truncatable). Chunk **by heading** + keep a concept-summary vector of `title`+`description`.
   - **Store:** default **in-memory cosine** (numpy/sklearn) for small bundles; scale to **sqlite-vec** (one `.db` file, WASM-able, float/int8/binary) when the bundle is large or the static viewer must query offline. `sqlite-vec` is the standout pick because it runs in-browser via WASM, matching OKF's "plain files, no server" ethos.
   - **API embeddings** (OpenAI/Cohere/Voyage/Nomic) as a remote provider behind the same interface.

4. **Layer 2 — hybrid fusion + rerank.** Retrieve BM25 top-N **∪** dense top-N, merge by **reciprocal rank fusion**, optionally rerank top-K with a **cross-encoder** (`ms-marco-MiniLM`). Expose a `relation`/`entity` facet (from §11 extraction) so "search by relation/entity" is a filter over typed relations + NER tags.

5. **Plugin interface (what a backend must implement):**
   ```python
   class SearchBackend(Protocol):
       def index(self, content_index: ContentIndex) -> None: ...
       def search(self, query: str, *, mode: SearchMode,
                  filters: Filters | None, limit: int) -> list[SearchResult]: ...
   ```
   with `SearchMode = LEXICAL | SEMANTIC | HYBRID | TAG` and `SearchResult` carrying `{concept_id, section_path, score, match_spans, source_backend}`. A `LexicalBackend` (always present), `DenseBackend` (sentence-transformers), `RemoteBackend` (API), and a `HybridBackend` (fusion of any two) all implement it. Registration via entry-points so downstream harnesses plug in their own.

**Justification:** the layering matches the spec's permissiveness and the checkout's explicit optional proof dependencies; the **content-index-first** design avoids N indexes; **sqlite-vec + MiniSearch** give "offline, no-server, single-artifact" search that no incumbent fully delivers together.

## B. Viewer architecture for OKF

**One content index → four build targets** (rationale in §15), with **wiki navigation and graph view coexisting in one experience**:

- **`scripts/okf-loom serve`** — live HTTP dev server. Watch bundle; on change, rebuild only affected concepts (Hugo-style incremental); run validator + discovery pass; push results over WebSocket. This is the authoring loop and the harness-embedding entrypoint (a meta-agent points a browser at `scripts/okf-loom serve`).
- **`scripts/okf-loom build --spa`** — Quartz-style multi-file static site (per-concept URLs, SPA routing, popovers, local+global graph, FlexSearch/MiniSearch). Best interactive reading experience; deep-linkable. **Default published surface.**
- **`scripts/okf-loom build --static`** — MkDocs-style server-rendered, no-JS HTML (printable, SEO, airgapped). For consumers who reject JS.
- **`scripts/okf-loom build --single-file`** — one self-contained HTML (bundle JSON + MiniSearch + a light `d3-force` graph), for sharing/emailing/`file://`. The brief's Cytoscape+marked pattern belongs here, as an *option*.

**Unifying wiki + graph navigation** (the hard requirement), following Quartz/Obsidian:
- Every concept page renders a **backlinks** panel (incoming links + unlinked mentions) and an **outgoing-links** panel, both with **popover previews** (Wikipedia-style hover, using a `popover-hint` class to select title+description+tags+first section).
- A **local graph** widget (≤N hops, degree-weighted nodes, visited-node coloring) sits beside the page; clicking a node navigates (SPA) to that concept. A header button toggles a **global graph** overlay.
- All three (backlinks, local graph, search) read the **same `contentIndex.json`** emitted by the build, so they're consistent and cheap.
- **Filterable/sortable listings** (Notion/MkDocs-style) for directories (from `FolderPage`-equivalent) and tags (from `TagPage`-equivalent), overridable by hand-authored `index.md`.

**Extension points** (mirroring Quartz's three customization tiers):
- **Transformers** (per-concept map: parse frontmatter, resolve links, extract description, run checks, embed), **filters** (decide emit), **emitters** (reduce → site files: pages, index, search index, graph data, RSS, JSON-LD). Order matters; declare in a `okf.config`.
- **Template overrides** (Docusaurus "swizzle" style): override one partial (e.g. the graph component, the concept header) without forking.
- **Theme** (colors, fonts, layout composition via a `Flex`-style layout config).
- **Link resolution mode** (`absolute`/`relative`/`shortest`) and `ignorePatterns` as first-class config.

**Justification:** Quartz proves SPA + graph + search + backlinks can coexist from one index; MkDocs proves server-rendered portability; the single-file target (Pagefind/MiniSearch) proves no-server search. OKF can offer all of these because they're *renderings* of the same model — exactly the "embeddable in any harness" requirement.

## C. Forward-compatibility for OKF

okf-loom must stay correct as the spec moves (v0.1 → 0.x → 1.0) **and** let bundles extend without breaking. Concrete mechanisms:

1. **Spec-version tracking.** A single `okf_loom.spec.SPEC_VERSION = "0.1"` plus a parser of the rules in SPEC §9 (conformance = parseable FM + non-empty `type` + reserved-filename structure). At load time, read the bundle's declared `okf_version` (SPEC §11, root `index.md` frontmatter); if okf-loom doesn't *understand* that version it does **best-effort consumption** (SPEC §11 mandate) and emits a warning, never a hard failure. Keep a `VersionedRuleSet` keyed by `(major, minor)`.

2. **Capability registry (the key forward-compat mechanism).** A manifest of named capabilities okf-loom *recognizes*, each declaring: an id (e.g. `okf.cap.typed_relations`, `okf.cap.diataxis_types`, `okf.cap.log_timeline`, `okf.cap.embeddings`), min spec version, whether it's `core`/`recommended`/`optional`, and the frontmatter keys / body sections it governs. This is the *extension namespace* from §12: typed relations live under `okf.cap.typed_relations` and use a `relations:` key — recognized-if-present, never required. Bundles **opt in** by using the keys; okf-loom activates the matching behavior. Unknown keys are always passed through (SPEC §9).

3. **Bundle-local capability declaration.** A bundle may carry its own extensions via a reserved, namespaced frontmatter block (e.g. in root `index.md`):
   ```yaml
   okf_version: "0.1"
   okf_extensions:
     - okf.cap.typed_relations   # recognized built-in
     - acme.custom_types         # user-defined, registered via plugin
   ```
   User-defined capabilities are supplied as **plugins** (transformer/filter/emitter + a JSON-schema fragment for the keys they introduce). This is how "custom body sections / typed relations" extend OKF *without* touching the spec — the spec already allows arbitrary keys; the registry just makes them *meaningful*.

4. **Migration hooks.** Between minor bumps: a registry of idempotent `migrate(old_minor -> new_minor, bundle)` functions; okf-loom runs them on `scripts/okf-loom upgrade --check` (dry-run, prints a diff) and `scripts/okf-loom upgrade --apply`. Between major bumps: explicit, opt-in, with a backup/branch recommendation. Migrations are *transformers*, reusing the same pipeline.

5. **Auto-update without destroying hand-curated content** (the hard constraint). The model is **"synthesize by default, defer to hand-authored, touch only marked regions."** Mechanism:
   - **`index.md` regeneration:** if a directory's `index.md` is **hand-authored** (no `generated: true` marker OR a `okf-managed` comment-fence region), the generator **never overwrites it** — it only *fills missing entries* or emits a warning listing what's absent (Quartz `FolderPage` behavior). If the file is **toolkit-generated** (frontmatter `generated: true` or content between markers like `<!-- okf:generated:index begin --> … <!-- okf:generated:index end -->`), the generator rewrites **only the marked region**, preserving anything outside it.
   - **`log.md` append:** *always append-only* under a today-dated `## YYYY-MM-DD` heading (SPEC §7); never edit history. New entries are derived from `git log`/diffs since the last recorded date (set via a `okf:last_log_date` marker or the newest existing heading).
   - **`relations:` / tags / frontmatter suggestions from discovery (§11)** are
     applied through **marker-safe mutators** that never destroy hand-curated
     content (hand-authored `index.md` is only filled within
     `<!-- okf:generated:index -->` fences; `log.md` is append-only). In the early design the
     default is to write these to a sidecar `okf-suggestions.json` (or
     `# Citations`-style `okf-suggestions.md`) plus an agent-readable diff for
     review; in the current design the managing agent applies them **directly** through those
      same safe mutators ([`/reference/spec.md`](/reference/spec.md) §10). Either way the SPEC §9
     permissiveness contract and hand-curated content are preserved.
   - A `--frozen` mode for CI: regeneration that *would* write a hand-authored file fails loudly instead.

**Why these mechanisms:** markers (comment-fences + `generated:` frontmatter) are the lightest weight, survive `cat`/`git diff` (spec's "readable without tooling"), and are already a proven pattern (Region markers in literate tools; Quartz's "index.md override"). A capability registry + namespaced extensions is how typed relations/custom sections can grow *without* a spec change (already legal under §4.1/§9). Migrations-as-transformers reuses the core pipeline rather than inventing a parallel system.

---

# Appendix

## A. Source grounding & discrepancies
- **OKF SPEC.md v0.1 — Draft** (fetched from `GoogleCloudPlatform/knowledge-catalog` `okf/SPEC.md`, 451 lines) — the binding rules cited as `SPEC §N`.
- **`viz.html` does not exist** in the current upstream `okf/` tree (only `SPEC.md`, `bundles/`, `samples/{crypto_bitcoin,ga4_merch_store,stackoverflow}`, `src/`, `tests/`). The "Cytoscape.js + marked, bundle-as-JSON single HTML" design in §15 is treated as a *reference pattern to evaluate*, not an existing file. If/when upstream adds a viewer, reconcile with Section B.

## B. Default recommended capability set (proposed)
`core`: `okf.cap.frontmatter` (parse + alias-collapse), `okf.cap.links` (absolute/relative resolution + broken-link tolerance), `okf.cap.index_md`, `okf.cap.log_md`, `okf.cap.listings` (folder/tag pages), `okf.cap.search_lexical`, `okf.cap.graph`, `okf.cap.backlinks`.
`recommended`: `okf.cap.search_hybrid`, `okf.cap.popovers`, `okf.cap.discovery_mentions`, `okf.cap.log_timeline`.
`optional`: `okf.cap.typed_relations`, `okf.cap.embeddings`, `okf.cap.discovery_ner` (GLiNER), `okf.cap.discovery_llm`, `okf.cap.diataxis_types`, `okf.cap.jsonld_export`, `okf.cap.multi_bundle`.

## C. Section index
Systems (§1–§15) → Synthesis (A Search, B Viewer, C Forward-compat) → Appendix (A grounding, B capability set, C this index).
