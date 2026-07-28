# Reimagine the graph — Flow Atlas

**Date:** 2026-07-28  
**Status:** Proposal (not implemented)  
**Audience:** Product + agent implementers  
**Supersedes for graph UX direction:** the “prettier atlas shell” track. Keep
[`graph-lenses.md`](graph-lenses.md) research; replace the *default product
model* of “force layout + lenses + detail pane.”

---

## 1. One-sentence north star

Stop shipping a **topology toy**. Ship a **Flow Atlas**: a place where someone
asks *what depends on this*, *what feeds that*, *what’s missing*, and *where
should I start* — and gets an answer in under three interactions without
learning Shift-click, Advanced, or cytoscape folklore.

---

## 2. Why the current graph cannot be “100×” by polishing

The atlas composition pass made the graph *look* like a place. It did not
change what the graph *is*.

| What users need | What they get today |
|---|---|
| Discoverability — find unknown-knowns, gaps, entry points | A constellation they must already understand to navigate |
| Data / knowledge **flows** — direction, stages, bottlenecks | Undirected-feeling hairballs; Flow lens is a dagre re-layout of the same soup |
| Progressive understanding | Overview of everything at once, then filters to subtract |
| Actionable next step | “Select a node” / tour tips / ranked lists that don’t change the canvas contract |
| Trust in the picture | Calm-edge heuristics hide most edges without saying *why* this spine was chosen |

Research we already agree with ([`graph-lenses.md`](graph-lenses.md)):
overview-first fails past ~100 nodes; search-first and focus techniques win;
matrix beats node-link for dense topology; layered layouts beat force for
dependency tasks; mental maps die when every lens re-rolls the world.

**Conclusion:** 100× is not denser glows or frosted rails. It is a different
primary loop:

> **Query → River → Step → Act**

not

> **Pan zoom click → maybe Focus → maybe Shift-path → read detail**

---

## 3. Jobs to be done (design against these, nothing else)

### J1 — Discover the unknown
*“I don’t know what I don’t know in this bundle.”*  
Success: land on an entry concept, a gap, or a bridge suggestion in ≤2
clicks from graph open; every suggestion cites evidence (mention, missing
relation type, orphan, stale hub).

### J2 — Follow a knowledge flow
*“How does understanding move from A to shipping B?”*  
Success: see a **directed stage diagram** (not a force graph) from sources →
explanations → how-tos → references; click a stage to expand only that cut.

### J3 — Follow a data / dependency flow
*“What feeds Orders? What breaks if Profiles changes?”*  
Success: upstream/downstream river with typed relation predicates as edge
labels; fan-in/fan-out readable; cycle and bottleneck called out in prose
beside the picture.

### J4 — Orient in a large atlas
*“Where am I, and what’s nearby that matters?”*  
Success: thematic continents with readable labels; local neighbourhood on
demand; global soup never the default past N nodes.

### J5 — Act on a finding
*“Link this orphan / open the doc / ask the agent.”*  
Success: every insight row has a primary action (Open, Trace, Propose link,
Ask agent) without leaving the flow context.

If a feature doesn’t serve one of these five jobs, it doesn’t ship in v1 of
the reimagination.

---

## 4. Product model: three modes, one shell

Replace the six co-equal lenses with **three modes**. Lenses become *recipes
inside a mode*, not the IA.

```text
┌─────────────────────────────────────────────────────────────┐
│  okf-loom · [ Discover | Flows | Atlas ] · search · Index   │
├──────────────┬──────────────────────────────┬───────────────┤
│ Context rail │         Stage / canvas        │ Insight dock │
│ (query,      │     (the answer picture)      │ (why + next) │
│  seeds,      │                               │               │
│  filters)    │                               │               │
└──────────────┴──────────────────────────────┴───────────────┘
```

### Mode A — Discover (default for cold start)

**Question:** What’s worth my attention, and what’s broken or missing?

**Canvas:** Not a full graph. A **ranked opportunity board** + mini-previews:

1. **Start here** — high PageRank / tutorial / `getting-started` tagged
2. **Gaps** — orphans, unlinked mentions, broken targets (from `discover`)
3. **Bridges to add** — cross-theme pairs with zero edges + shared tags/entities
4. **Going stale** — hubs with old timestamps
5. **Unresolved relations** — typed relations pointing at missing concepts

Each card expands into a **local 1-hop flower** (Bring & Go), not the global
map. Global topology is opt-in (“Show in Atlas”).

**Why this is 100× for discoverability:** the product leads with *findings*,
and uses graph geometry only as evidence — InfraNodus/Bloom’s insight-first
loop, not Gephi’s blank canvas.

### Mode B — Flows (the headline bet)

**Question:** How does X feed Y? Where does this knowledge/data go?

**Canvas:** A **River** — layered, left→right (or top→bottom on mobile):

```text
  Sources          Transforms         Products           Sinks
 ─────────       ─────────────      ────────────       ────────
  [Profiles] ──▶ [Orders] ──▶ [Invoices] ──▶ [Reports]
       │              │
       └─────────────▶[Fraud Agent]
```

Rules that make Rivers readable:

1. **Seed-centric.** Always start from a concept, a type, a tag, or a
   relation predicate (`derived_from`, `fulfills`, …). No seed → empty state
   with three example seeds from the bundle.
2. **Ranked layers.** Assign each reachable node a layer via longest-path /
   Coffman–Graham on the typed subgraph (not raw force). Cycles get a
   explicit “loop” badge and a back-edge channel *below* the river — never
   spaghetti through the main channel.
3. **Typed channels.** Parallel swimlanes by relation family when helpful
   (`defines` vs `references` vs `derived_from`), toggleable.
4. **Bundling.** When fan-out > K, collapse into a **bundle node**
   (“7 downstream how-tos”) that expands in place.
5. **Path as first-class.** “Trace to…” is a visible control (combobox), not
   Shift-click. The active path paints as a luminous channel; off-path layers
   stay visible but mute.
6. **Flow metrics in the dock:** depth, branching factor, bottlenecks
   (high betweenness on the river), missing expected relation types.

**Two Flow recipes (not separate top-level modes):**

| Recipe | Seed | Edge set | Layer signal |
|---|---|---|---|
| **Knowledge flow** | any concept | prose links + `explains`/`continues`/`see_also`-like types | tutorial → how-to → explanation → reference (type rank) |
| **Dependency flow** | entity/process/dataset | typed relations only | topological order of relation graph |

User can flip “Knowledge / Dependency” without losing the seed.

### Mode C — Atlas (orientation, demoted)

**Question:** How does the whole corpus cluster?

Keep today’s Map/Themes/Bridges energy, but:

- Default only when the user asks for “whole map” or bundle < ~40 nodes
- Always open **zoomed to a continent** (community), never the global soup
- Matrix alternate for “who connects to whom inside this continent”
- Orphan shelf stays; calm edges are explained (“Showing structural spine —
  N strongest links; Show density”)

Atlas is for orientation and storytelling. It is no longer the front door.

---

## 5. Interaction model (kill the secret handshake)

| Intent | Today | Reimagined |
|---|---|---|
| Start somewhere | Pan until a label is readable | Discover cards / search seed / “Continue where you left off” |
| See neighbours | Hope hover works | Flower expand on card or node; pin multiple flowers |
| Trace A→B | Select + Shift-click | “Trace path to…” control; recent targets; drag between cards |
| Focus | Focus lens + depth in Advanced | Pin / isolate toggle on any node; depth stepper always visible in Flows |
| See all edges | Advanced checkbox | “Spine / Labeled / Dense” segmented control with counts |
| Act | Open page | Open · Propose link · Ask agent · Copy path — sticky action bar |
| Undo spatial confusion | Reset view | “Restore last river” + layout stability (mental map) across recipe flips |

**Search becomes a seed composer**, not a node filter:

- typeahead concepts, types, tags, relation predicates, discover finding kinds
- query chips: `from:Orders`, `rel:derived_from`, `tag:billing`, `gap:orphan`
- Enter runs the active mode’s interpretation of that seed

---

## 6. Insight dock (right pane) — always answers “so what?”

Stop alternating between empty ranked lists and raw concept HTML as if they
were the same job. Split the dock into durable regions:

1. **Context strip** — seed, mode, active path breadcrumb  
2. **Verdict** — one sentence computed for *this* view  
3. **Evidence list** — ranked, clickable, each with why-score  
4. **Actions** — Open, Trace, Propose link, Ask agent  
5. **Reader** — concept body in a drawer / split, not permanently stealing
   the insight regions

When a node is selected in Flows, verdict examples:

- “Orders sits mid-river: 3 upstream systems, 5 downstream artifacts.”
- “No typed path from Profiles → Fraud Agent; 2 prose hops exist.”
- “This community’s only bridge to Billing is Invoices (betweenness 0.41).”

---

## 7. Data & computation we must grow

Viewer JSON today is rich on nodes, weak on *flow-ready* structure (see
current pipeline: relations exist, but multi-edges collapse; communities /
weights are client-only; discover findings are not on the canvas).

### Must add (server or shared worker)

| Capability | Why |
|---|---|
| **Multi-relation edges** (or parallel channels) | Dependency flows need `defines` and `references` between the same pair simultaneously |
| **Discover findings payload** on `/__graph` or `/__discover/graph` | Discover mode cannot scrape CLI output in the browser |
| **Layer assignment** for typed subgraphs | Rivers need stable ranks; don’t reimplement poorly per-client forever |
| **Expected-relation hints** (from discover / schema conventions) | “Orders usually `placed_by` Customers” style gaps |
| **Path enumeration** API (k-shortest, typed constraints) | Power Trace without blocking UI on huge A* in JS |
| **Comment / broken-link badges** (long-planned) | Discoverability of collaboration debt |

### Keep client-side

PageRank, betweenness, Louvain for Atlas; local flower layouts; calm-spine
selection once the spine rule is user-visible.

---

## 8. Visual language (in service of reading flows)

Do **not** chase decorative deep-space for its own sake. Visual budget:

1. **Direction is sacred** — arrows, layer columns, and path luminescence
   outrank node cosmetics  
2. **One glow meaning** — active path *or* selection, never competing halos  
3. **Bundles look like bundles** — stacked count chips, not tiny fake nodes  
4. **Gaps look like gaps** — dashed ghost nodes / pink “missing” slots in the
   river where discover expects a link  
5. **Quiet chrome** — mode switch + seed search + one action cluster; studio
   presence as a chip, not a second toolbar  

Atlas can keep the atmospheric canvas. Flows should feel closer to an
architecture diagram / Sankey hybrid: columnar, aligned, printable.

---

## 9. What we deliberately kill or demote

| Kill / demote | Replacement |
|---|---|
| Six peer lenses in a left rail | Three modes; recipes inside Flows/Atlas |
| Global force layout as default | Discover board or seeded River |
| Shift-click as the path gesture | Explicit Trace control |
| Advanced as the home of truth | Spine/Labeled/Dense + depth stepper in-canvas |
| Detail pane replacing all insight | Insight dock + reader drawer |
| “Show all edges” as the honesty valve | Named density modes with counts |
| Tour that explains failures of the model | Empty states that *run* an example seed |

---

## 10. Implementation phases (complexity, not calendar)

### Phase 0 — Contract
- Spec the mode IA, seed query language, River layout rules, discover
  finding schema for the viewer.
- Freeze success metrics (below).
- Decision record: Flows is the flagship demo surface for sales/docs.

### Phase 1 — Discover front door
- Replace `/__graph` default with Discover board wired to `discover` findings
  + graph-quality orphans/bridges.
- Card → flower expand; “Open in Flows” / “Open in Atlas” handoff with seed
  preserved.
- Insight dock verdicts for gap cards.

### Phase 2 — Rivers
- Seed composer + Dependency / Knowledge recipes.
- Layered layout with cycle channel + bundle nodes.
- Visible Trace; path metrics in dock.
- Multi-edge / channel rendering for typed relations.

### Phase 3 — Atlas as orientation
- Continent-first Map; matrix alternate per continent.
- Honest spine control; orphan shelf retained.
- Mental-map-stable transitions when arriving from Flows with a seed.

### Phase 4 — Act loop
- Propose link (mutator) from bridge cards.
- Ask agent with river/path context prefilled.
- Badges for comments / broken links on flowers and rivers.

Each phase must ship usable alone; Phase 1 already beats today’s cold start
even if Rivers slip.

---

## 11. Success metrics (ship only if these move)

**Qualitative (dogfood on `docs-bundle` + a dense import bundle):**

- Cold starter can name a useful next doc within 20 seconds without panning.
- Dependency question (“what feeds X?”) answered without enabling Advanced.
- Path between two known concepts found without Shift-click.

**Quantitative:**

- Median edges rendered on Flows seed view ≤ 3× path length (bundles count
  as one).
- ≥ 70% of Discover top-5 cards get a click in moderated sessions.
- Time-to-first-concept-open from `/__graph` drops vs current Map default.
- Support/agent questions of the form “how do I find X in the graph?” drop.

**Non-goals for this reimagination:**

- Competing with Gephi for million-edge visual analytics
- 3D / VR graphs
- Auto-pretty screenshots as the product definition
- Recreating the mockup-03 chrome one more time without changing the loop

---

## 12. Example walkthrough (target experience)

1. Open `/__graph` → **Discover**. Top card: “Checkout and Billing is orphaned;
   4 unlinked mentions in Orders.”  
2. Expand card → flower shows mention sites; action **Propose `fulfills`
   Orders**.  
3. User chooses **Open in Flows** with seed Orders, recipe Dependency.  
4. River shows Customers → Orders → Inventory / Fraud / Invoices. Bundle
   “3 reporting artifacts” on the right.  
5. User runs **Trace to Fraud Agent** — luminous channel; dock: “2-hop typed
   path via Profiles; alternate prose path via Policy.”  
6. **Ask agent**: “Draft a relation from Orders to Fraud Agent based on the
   prose in §Risk.” Comment loop inherits path context.

That session is impossible to do fluidly today without lens thrash and
gesture memory. It is the bar.

---

## 13. Relationship to prior design docs

| Doc | Keep | Change |
|---|---|---|
| [`graph-lenses.md`](graph-lenses.md) | Evidence: communities, centrality, layered > force for dependency, search-first | Lenses cease to be the top-level IA |
| [`ui-overhaul-plan.md`](ui-overhaul-plan.md) | Living atlas metaphor, progressive disclosure | Graph chapter retargeted to Flow Atlas |
| [`ux-vision-2026-07.md`](ux-vision-2026-07.md) | Quiet chrome, calm defaults | “Graph as a place” becomes “Flows as the place”; Discover is the foyer |

---

## 14. Decision asked of stakeholders

Approve **Flow Atlas** as the graph product direction:

1. **Discover** default cold start  
2. **Flows / Rivers** as the flagship reading surface  
3. **Atlas** demoted to orientation  

If yes, Phase 0 contract lands next (seed query grammar + discover viewer
schema + River layout algorithm choice). If no, say which job (J1–J5) should
be demoted — do not ask for another visual-only pass on Map.
