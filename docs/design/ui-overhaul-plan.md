# Wiki & Graph UI Overhaul Plan

Investigation date: 2026-07-01, against the live server
run on `samples/showcase` ("Northwind Coffee"). Bug-level findings from the
same review touch these surfaces and should ride along with this work.

> **Follow-up (2026-07-27):** Phases 1–5 largely landed at the component
> level. For a current assessment of what still holds the product back
> (composition / chrome / first viewport) and aspirational mockups, see
> [`ux-vision-2026-07.md`](ux-vision-2026-07.md).

## 1. What the investigation found

### Current state — wiki

Evidence: live screenshots of `/`, `/services/checkout.md` (light + dark +
mobile), search overlay.

The foundations are better than the surface. `wiki.css` already has a real
token system (spacing/type scales, a deliberate teal accent with documented
WCAG ratios, OKLCH state colors, dark theme) — but the composition on top of
it produces a page that reads like unstyled GitHub markdown:

- **No content measure.** Body text stretches the full viewport (~1550 px at
  1600 wide). Long paragraphs are unreadable; every page feels like a README.
- **The index is a wall of bullets.** H2 per type + `<ul>` of links. No cards,
  no counts, no descriptions beyond one clause, no recency, no entry points
  ("start here", recently changed, most connected). First impression is 1998.
- **Concept pages have no visual anatomy.** Title, tag pill, then undifferentiated
  markdown. The frontmatter facts (description/resource/tags/entities) — the
  *structured* value of OKF — render as a plain definition list that visually
  disappears. "Links to" is a bare link list with no direction, no type, no
  grouping; backlinks are invisible on the page (only in the Related sidebar).
- **Sidebar boxes are all equal weight.** Related / Sections / Quick actions are
  three identical bordered white boxes; Quick actions render as what look like
  text inputs, not buttons (`.okf-qa button` styled like fields). On mobile the
  three boxes stack *above* the title — you scroll ~700 px before content.
- **Type is invisible.** A SERVICE chip and a PLAYBOOK chip differ only in hue
  of one small pill. Nothing else on the page (accent, header band, icon)
  carries the concept type. The auto type-hash palette exists (used by the
  graph) but the wiki ignores it.
- **Search overlay is functional but bare** — floating input + result rows,
  no type icons, no grouping, no snippet/highlight, no recent-pages memory.
- Dark mode is competently done (tokens flip; no unreadable areas found).

### Current state — graph

Evidence: live `/__graph` light + dark, hover state, plus
`docs/screenshots/graph-signal-controls/*` and the single-file viewer shot.

- **The layout piles everything into ~1/3 of the canvas.** 14 nodes occupy a
  ~600×500 blob in the middle; the bottom half is dead space; the orphan node
  floats alone far below. Repeated cose-parameter tuning (commits `1a837d4`,
  `b3ad79f`) improved it but plain cose fundamentally under-spreads this graph.
- **Labels collide constantly.** Node labels are plates under the circles
  ("Customer SLA Tiers" over "Orders"; "ADO on Adopt Event Sourcing for
  Orders" wraps to 3 lines and lands on 2 neighbours). In dark mode the
  opaque label plates make the pileup worse. Rotated edge labels
  ("…it serv…", "currenc…") thread illegibly through the cluster.
- **Nodes carry almost no information.** Flat colored circles, hue = type hash,
  size = degree (22–44 px). No shape/icon per type, no state (fresh/stale,
  commented, broken links), nothing to reward looking at a node.
- **Edges are uniform pale-gray spaghetti** at the default (typed colors and
  dashes exist but only via the "Relations" preset). Direction arrowheads are
  faint at 1.2–4.8 px widths.
- **Interaction is thin.** Click selects + fills the right panel (good), but:
  no hover tooltip, no neighborhood highlight on hover (`.dim` exists but only
  presets use it), no double-click-to-open, no zoom-to-fit affordance beyond
  "Reset view", no minimap, no keyboard support, no path/exploration verbs.
- **"Signal controls" speaks engineer, not user.** "Group strength: Medium",
  "Cluster separation: Clear", six preset buttons with no visual preview of
  what they do. It occupies the prime top-left slot, always expanded, while
  the legend hides bottom-left as a separate floating box.
- **The canvas is dead.** No live updates (graph SSE events aren't consumed),
  no animation on layout change beyond reposition, no physics
  feel, nothing that invites play.

### What to keep

- The right-hand detail panel ("Why this connects", relations, schema) is
  genuinely good — better than most graph tools. Keep and lean into it.
- The token system, dark theme discipline, and the a11y annotations in CSS.
- Presets as a concept (one-click intents) — just make them legible.
- The single-shared-renderer architecture (server-rendered concept HTML reused
  by SSE patches) — all restyling below is CSS/template-level and preserves it.

## 2. Design direction

One sentence: **from "markdown dump with a graph applet" to "a living atlas of
the bundle"** — the wiki reads like a designed knowledge product, the graph is
the map you *want* to open, and both share one visual language (type = color +
icon, everywhere).

Principles:

1. **Type is the identity system.** Fix a curated 8-color palette + icon set
   for concept types (extendable by hash for unknown types). The same
   color+icon pair appears on: index cards, concept page header band, chips,
   search results, graph nodes, legend. This single move ties the whole
   product together and is cheap.
2. **Structured data gets structured chrome.** Frontmatter facts become an
   infobox; relations become directional typed cards; entities become linked
   chips. The markdown body is for prose only.
3. **The graph is a place, not a debug view.** Space, motion, and progressive
   disclosure (overview → hover → focus → open) make it explorable; controls
   speak user intents ("Spread things out", "Show me hubs") not physics.

## 3. Wiki overhaul

### 3.1 Layout & typography (highest leverage-per-line)

- Content measure: cap the article at `~76ch` (`.okf-doc { max-width: 76ch }`),
  centered between the left rail and a new right rail; keep full-bleed only
  for tables/code that need it (`.okf-wide` opt-out).
- Type scale: headings move to a distinctive display stack (e.g. system serif
  or a bundled variable font — license-check first; `Source Serif`/`STIX` feel
  right for "atlas") while body stays sans. Increase h1 to `--okf-text-xl`+,
  add letter-spacing to the small-caps section labels.
- Rhythm: consistent `--okf-space-6/7` between sections; hairline dividers
  (`--okf-border`) instead of the current heavy `<hr>`-look.
- Files: `viewer/static/wiki.css` (layout sections), `viewer/templates/
  concept_page.html` (wrap article in the new grid). No JS changes.

### 3.2 Index page → dashboard

- Hero band: bundle title, description, live stats chips (concepts, links,
  last-change relative time, health = broken-link count from the existing
  validate data), and a prominent "Open graph" card with a mini static
  thumbnail of the graph (see 4.6 export).
- Type sections become **card grids**: each concept a card with type icon +
  tinted left border, title, description, tag chips, and a footer line
  (outgoing/incoming counts — already computed for the graph payload).
- Add a "Recently changed" rail fed by the existing activity log
  (`/__data/events` already exists for the studio) — top 5, relative times.
- Files: `render.py` index builder (it already has per-concept summaries),
  `wiki.css`; live + static builds share the template, so both get it.

### 3.3 Concept page anatomy

Top to bottom:

1. **Header band**: breadcrumb (keep), then a type-tinted band: icon + type
   name, title, description as a styled lede, entity chips (pill + icon,
   clickable → search), resource link as a proper button.
2. **Infobox** (right rail on wide screens, collapsible card on mobile):
   the frontmatter facts, rev/last-modified, tags, and the relation list as
   `[type badge] → target` rows with direction arrows. This replaces the
   buried "All fields" `<details>` and the flat ENTITIES list.
3. **Body** at 76ch with the improved typography.
4. **Connections footer**: replace "Links to" bare list with two card rows —
   "This concept links to" and "Referenced by" (backlink data already exists —
   the Related sidebar computes it), each card showing type icon, title, and
   the *relationship label* when typed (e.g. `derived_from`).
5. Prev/next-in-section footer nav (cheap, from the index ordering).
- Sidebar: Related/Sections merge into one sticky rail with scroll-spy on
  sections (IntersectionObserver, ~30 lines in `wiki.js`); Quick actions
  restyle as real buttons with icons, demoted below. Mobile: rails collapse
  into a sticky "On this page" disclosure *below* the header band, never above
  the title.
- Files: `viewer/templates/concept_page.html`, `viewer/markdown.py` (emit
  infobox/connections structure), `wiki.css`, small `wiki.js` scroll-spy.
  Keep the renderer shared with SSE patching (`applyDoc` targets the body
  container only — verify IDs stay stable).

### 3.4 Search & hover polish

- Search results: type icon + tinted chip, matched-text highlight (the match
  offsets are already in the search payload), group by type when >6 results,
  and "recent pages" when the query is empty. `wiki.js` + `studio.css`.
- Link hover popovers (exist already): restyle as mini concept cards (type
  band + description + link counts) instead of raw text; keep the fetch race
  fence intact while in there.

### 3.5 Engagement layer (wiki)

- Per-type accent theming: the header band tint follows the concept type.
- Reading affordances: heading anchor links on hover, "copy id" chip next to
  the breadcrumb, estimated reading time in the header band (trivial, and it
  signals "this is a document that respects your time").
- Surface the collaboration features the studio already has: comment counts
  as a small chip on index cards and in Connections cards (data exists via
  `/__comments`), so the wiki advertises where conversations are happening.

## 4. Graph overhaul

### 4.1 Layout: replace bare cose with fcose + spacing discipline

- Adopt the **fcose** extension (same cytoscape ecosystem, CDN-able, MIT):
  quality force layout with `nodeRepulsion`/`idealEdgeLength` that actually
  spreads, plus built-in support for fixing node groups. Pin exact version
  with SRI like cytoscape itself (extends the `render.py:564` pattern and
  preserves the pinned-CDN posture).
- Post-layout **label-collision pass**: cytoscape can't avoid label overlap,
  but we control label geometry — after layout, measure label boxes and nudge
  colliding nodes apart (simple greedy pass, ~60 lines) or switch colliding
  pairs to on-hover labels. Combined with fcose spread this kills the pileup.
- **Orphan shelf**: nodes with no edges dock into a labelled tray at the
  bottom edge ("Not yet linked — 1") instead of floating in space; declutters
  and turns orphans into a visible authoring TODO.
- Zoom-dependent label policy (partially exists): < threshold show nothing,
  mid show node labels, close show edge labels — tune so the default view
  never draws edge labels through nodes.
- Files: `graph.js` (layout config ~line 1447, label styles ~line 811),
  `render.py` (CDN tag), `graph_page.html`.

### 4.2 Node visual language

- **Shape + icon per type**: cytoscape supports `shape` and background SVGs —
  give each type its icon (same set as the wiki) rendered as a white glyph on
  the type-colored disc; distinct shapes for the two structural standouts
  (round-rect for services, barrel for tables/datasets is a natural fit).
- Size stays degree-driven but with a wider, eased range; add a subtle darker
  ring; hub nodes (top-decile degree) get a soft halo instead of just size.
- **State badges**: small corner dots for "has comments" (accent) and "broken
  links" (error token) — both datasets already exist server-side.
- Labels: shorter default (`text-max-width` 90, ellipsize, full name on
  hover/selection), lighter plate (semi-transparent, no plate at all in light
  mode where contrast allows).

### 4.3 Edge language

- Typed edges get their hue/dash **always** (not only in the Relations
  preset), at low saturation so the default view stays calm; untyped edges
  stay neutral gray.
- Direction: larger arrowheads scaled with edge width; on hover/selection the
  edge animates a subtle dash-offset "flow" toward the target (CSS-free,
  cytoscape style animation ~20 lines) — direction becomes felt, not squinted.
- Multi-edges between the same pair: bundle to one edge with a count chip,
  expanding on selection.

### 4.4 Interaction model (the engagement core)

Progressive disclosure, each level cheap to reach:

1. **Hover** → node lifts (scale 1.15 animated), its neighborhood stays full
   opacity while the rest dims to 0.25 (the `.dim` machinery exists — wire it
   to hover with a 120 ms debounce); a **tooltip card** (type band, title,
   one-line description, link counts) follows the cursor. Edge hover shows
   its relationship label.
2. **Click** → selects + fills the right panel (as today), plus pins the
   neighborhood highlight and shows a small in-canvas action bar next to the
   node: "Open page · Focus · Hide others".
3. **Double-click / Enter** → navigates to the concept page.
4. **Focus mode** (exists as a preset) becomes the "Focus" verb on any node:
   animated re-layout of the 1–2-hop neighborhood, breadcrumb chip at top
   ("Focused on Orders — show all"), depth stepper.
5. **Path mode**: select node A, shift-click node B → highlight shortest
   path(s) (BFS, trivial at these sizes), read out the chain in the right
   panel ("Orders → derived_from → Subscriptions"). This is the single most
   "aha"-generating graph feature for knowledge bases.
6. **Keyboard**: arrows walk edges from the selected node, Enter opens, `/`
   focuses search, `f` fits, Esc clears — plus focus rings on the canvas via
   cytoscape's `:selected` styling (a11y win too).

### 4.5 Controls & framing

- Rename/reframe **Signal controls → "Lenses"**, collapsed by default into a
  compact segmented bar of the 6 presets, each with a 60×40 SVG thumbnail
  sketching its effect; the sliders move into an "Advanced" disclosure with
  human labels ("Breathing room", "Pull groups together").
- Merge the legend into the same panel as clickable **filter chips** (click a
  type = toggle visibility, alt-click = solo) — replacing the "All types"
  dropdown; add an edge-type row when typed relations exist.
- Persistent bottom-right cluster: zoom in/out/fit, fullscreen, minimap
  toggle (cytoscape-minimap ext, or a 60-line custom canvas dot-map), and
  layout re-run with animation.
- Search-in-graph gets type-ahead highlighting: matches glow + non-matches
  dim as you type (the search index is already client-side).

### 4.6 Liveness & delight

- **Consume graph SSE events**: new/changed/removed nodes
  animate in/out (fade + scale from the linking node's position); a small
  toast chip "Graph updated · re-run layout" when structure changed while the
  user was mid-exploration (never yank the viewport).
- Presence: collaborator halos already exist — extend with cursor-position
  ghosts on the canvas (data already flows through the presence channel).
- Layout transitions animate (`animate: 'end'`, 400 ms ease) for every preset
  change, so lens switches read as camera moves, not teleports.
- First-visit micro-tour: 3 coach marks (Lenses / hover to explore / double
  click to open), dismissed forever in localStorage. ~40 lines.
- Static-site export (`render.py`) gets the same visuals minus live bits;
  index hero thumbnail (3.2) reuses the export at build time via a headless
  snapshot already available in `scripts/capture_*` tooling.

## 5. Phasing

Ordered so every phase ships visible value and nothing blocks on a rewrite.
"Size" is relative engineering effort (S < M < L).

| Phase | Contents | Size | Files |
|---|---|---|---|
| **1. Foundations** | Type palette+icon set as tokens; content measure; typography scale; index card grid; header band; sidebar restyle (buttons, merged rail) | M | wiki.css, concept_page.html, render.py index |
| **2. Concept anatomy** | Infobox; Connections cards w/ backlinks + typed badges; scroll-spy; search/hover polish; mobile order fix | M | markdown.py, wiki.js, templates |
| **3. Graph visual language** | fcose + SRI pin; label-collision pass; orphan shelf; node shapes/icons/badges; typed edges always-on; arrowheads | L | graph.js, render.py, graph_page.html |
| **4. Graph interaction** | Hover dim+tooltip; action bar; focus verb; path mode; keyboard; Lenses reframe + filter chips; zoom cluster + minimap | L | graph.js, graph.css |
| **5. Liveness** | Graph SSE consumption + animated updates; presence ghosts; micro-tour; index recent-changes rail; comment-count chips | M | graph.js, live.js, studio.js, render.py |

Preserve existing ride-along fixes per phase: highlight.js theme selection in
1; popover race fencing in 2; CDN pins and live graph events in 3/5;
graph-page comment concept handling in 4.

## 6. Verification

- Extend `scripts/capture_signal_controls.py` pattern into
  `scripts/capture_ui_overhaul.py`: index, concept (light/dark/mobile),
  graph default/hover/focus/path — before/after pairs per phase, written to
  `docs/screenshots/` (optimized to avoid repo bloat — consider lossy-
  compressing with `pngquant`/`oxipng` or storing only the "after").
- Existing browser test suites (`test_viewer_browser.py`,
  `test_studio_iter*_browser.py`, `test_js_lint.py`) gate regressions; new
  interactions get the same treatment (hover dim, path mode, keyboard).
- A11y checkpoints: keep the WCAG-ratio comments in CSS up to date for every
  new token pair; keyboard graph nav closes the current canvas a11y gap.
