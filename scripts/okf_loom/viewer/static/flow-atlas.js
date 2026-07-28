/* OKF Flow Atlas — Discover | Flows | Atlas mode controller.
 *
 * Depends on graph.js exporting window.__okfLoomGraph after Cytoscape mounts.
 * Implements docs/design/graph-flow-reimagine.md Phase 1–4 surfaces.
 */
(function () {
  "use strict";

  var MODE_KEY = "okf-flow-atlas-mode";
  var state = {
    mode: "discover",
    seed: null,
    recipe: "knowledge", // knowledge | dependency
    density: "spine", // spine | labeled | dense
    selectedFinding: null,
    traceTarget: null,
  };

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    });
    (kids || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function api() {
    return window.__okfLoomGraph || null;
  }

  function findings() {
    var a = api();
    if (!a) return [];
    var list = a.findings ? a.findings() : [];
    if (list && list.length) return list;
    var b = a.bundle || window.__okfLoomGraphData;
    return (b && b.findings) || [];
  }

  function labelOf(id) {
    var a = api();
    if (!a || !id) return id || "";
    var n = a.nodeIndex && a.nodeIndex[id];
    return (n && (n.label || n.id)) || id;
  }

  function setMode(mode) {
    if (mode !== "discover" && mode !== "flows" && mode !== "atlas") return;
    state.mode = mode;
    try { localStorage.setItem(MODE_KEY, mode); } catch (e) {}
    var main = $("#okf-main");
    if (main) main.setAttribute("data-atlas-mode", mode);
    $$(".okf-mode-switch__btn").forEach(function (btn) {
      var on = btn.getAttribute("data-mode") === mode;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    var discover = $("#okf-discover");
    if (discover) discover.hidden = mode !== "discover";
    var lensesWrap = $("#okf-rail-lenses-wrap");
    if (lensesWrap) lensesWrap.hidden = mode !== "atlas";
    renderContextRail();
    if (mode === "discover") {
      renderDiscoverBoard();
      renderInsightIdle("Pick a finding to see evidence and actions.");
    } else if (mode === "flows") {
      enterFlows();
    } else {
      enterAtlas();
    }
  }

  function renderContextRail() {
    var title = $("#okf-rail-context-title");
    var body = $("#okf-rail-context-body");
    if (!body) return;
    body.innerHTML = "";
    if (title) {
      title.textContent =
        state.mode === "discover" ? "Discover" :
        state.mode === "flows" ? "Flows" : "Atlas";
    }
    if (state.mode === "discover") {
      body.appendChild(el("p", { class: "okf-graph-rail__blurb okf-muted" }, [
        "Gaps, orphans, and starting points — expand a card for a local flower.",
      ]));
      var n = findings().length;
      body.appendChild(el("p", { class: "okf-graph-rail__stat" }, [
        String(n) + " finding" + (n === 1 ? "" : "s"),
      ]));
    } else if (state.mode === "flows") {
      body.appendChild(el("p", { class: "okf-graph-rail__blurb okf-muted" }, [
        "Seed a concept, pick a recipe, read the river left → right.",
      ]));
      var seedLab = el("label", { class: "okf-flow-seed" });
      seedLab.appendChild(el("span", { class: "okf-graph-rail__title" }, ["Seed"]));
      var seedInput = el("input", {
        type: "search",
        id: "okf-flow-seed",
        placeholder: "Concept id or title…",
        value: state.seed ? labelOf(state.seed) : "",
        "aria-label": "Flow seed",
      });
      seedInput.addEventListener("change", function () {
        resolveSeed(seedInput.value);
      });
      seedInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          resolveSeed(seedInput.value);
        }
      });
      seedLab.appendChild(seedInput);
      body.appendChild(seedLab);

      var recipes = el("div", { class: "okf-flow-recipes", role: "group", "aria-label": "Flow recipe" });
      [["knowledge", "Knowledge"], ["dependency", "Dependency"]].forEach(function (pair) {
        var b = el("button", {
          type: "button",
          class: "okf-flow-recipes__btn" + (state.recipe === pair[0] ? " is-active" : ""),
          "data-recipe": pair[0],
        }, [pair[1]]);
        b.addEventListener("click", function () {
          state.recipe = pair[0];
          renderContextRail();
          if (state.seed) runRiver();
        });
        recipes.appendChild(b);
      });
      body.appendChild(recipes);

      var dens = el("div", { class: "okf-flow-density", role: "group", "aria-label": "Edge density" });
      [["spine", "Spine"], ["labeled", "Labeled"], ["dense", "Dense"]].forEach(function (pair) {
        var b = el("button", {
          type: "button",
          class: "okf-flow-density__btn" + (state.density === pair[0] ? " is-active" : ""),
          "data-density": pair[0],
        }, [pair[1]]);
        b.addEventListener("click", function () {
          state.density = pair[0];
          applyDensity();
          renderContextRail();
        });
        dens.appendChild(b);
      });
      body.appendChild(dens);

      var traceWrap = el("label", { class: "okf-flow-seed" });
      traceWrap.appendChild(el("span", { class: "okf-graph-rail__title" }, ["Trace to"]));
      var traceIn = el("input", {
        type: "search",
        id: "okf-flow-trace",
        placeholder: "Target concept…",
        "aria-label": "Trace path target",
      });
      var traceBtn = el("button", { type: "button", class: "okf-signal__btn" }, ["Trace path"]);
      function doTrace() {
        var tid = resolveId(traceIn.value);
        if (!tid || !state.seed) return;
        var g = api();
        if (!g || !g.cy) return;
        var a = g.cy.getElementById(state.seed);
        var b = g.cy.getElementById(tid);
        if (a.length && b.length && g.showPathBetween) g.showPathBetween(a, b);
        state.traceTarget = tid;
        setInsightVerdict("Path " + labelOf(state.seed) + " → " + labelOf(tid));
      }
      traceBtn.addEventListener("click", doTrace);
      traceIn.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); doTrace(); }
      });
      traceWrap.appendChild(traceIn);
      body.appendChild(traceWrap);
      body.appendChild(traceBtn);
    } else {
      body.appendChild(el("p", { class: "okf-graph-rail__blurb okf-muted" }, [
        "Orientation map. Starts on the largest continent — not the global soup.",
      ]));
      var dens2 = el("div", { class: "okf-flow-density", role: "group", "aria-label": "Edge density" });
      [["spine", "Spine"], ["labeled", "Labeled"], ["dense", "Dense"]].forEach(function (pair) {
        var b = el("button", {
          type: "button",
          class: "okf-flow-density__btn" + (state.density === pair[0] ? " is-active" : ""),
          "data-density": pair[0],
        }, [pair[1]]);
        b.addEventListener("click", function () {
          state.density = pair[0];
          applyDensity();
          renderContextRail();
        });
        dens2.appendChild(b);
      });
      body.appendChild(dens2);
    }
  }

  function resolveId(q) {
    q = (q || "").trim();
    if (!q) return null;
    var g = api();
    if (!g || !g.nodeIndex) return null;
    if (g.nodeIndex[q]) return q;
    var lower = q.toLowerCase();
    var ids = Object.keys(g.nodeIndex);
    for (var i = 0; i < ids.length; i++) {
      var d = g.nodeIndex[ids[i]];
      if ((d.label || "").toLowerCase() === lower) return ids[i];
    }
    for (var j = 0; j < ids.length; j++) {
      var d2 = g.nodeIndex[ids[j]];
      if (((d2.label || "") + " " + ids[j]).toLowerCase().indexOf(lower) >= 0) return ids[j];
    }
    return null;
  }

  function resolveSeed(q) {
    var id = resolveId(q);
    if (!id) {
      setInsightVerdict("No concept matched “" + q + "”. Try a title or id.");
      return;
    }
    state.seed = id;
    runRiver();
  }

  function applyDensity() {
    var g = api();
    if (!g || !g.controlState) return;
    if (state.density === "dense") {
      g.controlState.showAllEdges = true;
      g.controlState.showEdgeLabels = true;
      g.controlState.relationEdges = true;
    } else if (state.density === "labeled") {
      g.controlState.showAllEdges = false;
      g.controlState.showEdgeLabels = true;
      g.controlState.relationEdges = true;
    } else {
      g.controlState.showAllEdges = false;
      g.controlState.showEdgeLabels = false;
      g.controlState.relationEdges = false;
    }
    if (g.applyFilters) g.applyFilters();
  }

  function kindMeta(kind) {
    var map = {
      start_here: { label: "Start here", tone: "start" },
      orphan: { label: "Orphan", tone: "gap" },
      unlinked_mentions: { label: "Unlinked mention", tone: "gap" },
      stale_hub: { label: "Going stale", tone: "stale" },
      missing_relations_hint: { label: "Missing relation", tone: "gap" },
      broken_links: { label: "Broken link", tone: "warn" },
    };
    return map[kind] || { label: kind || "Finding", tone: "info" };
  }

  function renderDiscoverBoard() {
    var board = $("#okf-discover-board");
    if (!board) return;
    board.innerHTML = "";
    var list = findings().slice();
    if (!list.length) {
      board.appendChild(el("p", { class: "okf-muted" }, [
        "No findings yet — open Atlas to explore, or seed Flows with a concept.",
      ]));
      return;
    }
    var groups = {};
    list.forEach(function (f) {
      var k = f.kind || "other";
      (groups[k] = groups[k] || []).push(f);
    });
    var order = ["start_here", "orphan", "unlinked_mentions", "stale_hub", "broken_links", "missing_relations_hint"];
    var seen = {};
    order.concat(Object.keys(groups)).forEach(function (kind) {
      if (seen[kind] || !groups[kind]) return;
      seen[kind] = true;
      var meta = kindMeta(kind);
      var section = el("section", { class: "okf-discover__section" });
      section.appendChild(el("h3", { class: "okf-discover__section-title" }, [
        meta.label + " · " + groups[kind].length,
      ]));
      groups[kind].forEach(function (f) {
        section.appendChild(findingCard(f, meta));
      });
      board.appendChild(section);
    });
  }

  function findingCard(f, meta) {
    meta = meta || kindMeta(f.kind);
    var card = el("article", {
      class: "okf-discover-card okf-discover-card--" + meta.tone,
      tabindex: "0",
      role: "button",
    });
    card.appendChild(el("div", { class: "okf-discover-card__kicker" }, [meta.label]));
    card.appendChild(el("h4", { class: "okf-discover-card__title" }, [
      f.title || f.message || labelOf(f.concept_id),
    ]));
    card.appendChild(el("p", { class: "okf-discover-card__msg okf-muted" }, [
      f.message || "",
    ]));
    if (f.concept_id || f.target_concept_id) {
      var bits = [];
      if (f.concept_id) bits.push(labelOf(f.concept_id));
      if (f.target_concept_id) bits.push("→ " + labelOf(f.target_concept_id));
      card.appendChild(el("p", { class: "okf-discover-card__ids" }, [bits.join(" ")]));
    }
    var actions = el("div", { class: "okf-discover-card__actions" });
    var openBtn = el("button", { type: "button", class: "okf-discover-card__btn" }, ["Expand"]);
    openBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      activateFinding(f, true);
    });
    actions.appendChild(openBtn);
    if (f.concept_id) {
      var flowBtn = el("button", { type: "button", class: "okf-discover-card__btn okf-discover-card__btn--primary" }, ["Open in Flows"]);
      flowBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        state.seed = f.concept_id;
        setMode("flows");
      });
      actions.appendChild(flowBtn);
    }
    card.appendChild(actions);
    card.addEventListener("click", function () { activateFinding(f, true); });
    card.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activateFinding(f, true);
      }
    });
    return card;
  }

  function activateFinding(f, flower) {
    state.selectedFinding = f;
    var g = api();
    showActions(f.concept_id || f.target_concept_id);
    setInsightVerdict(f.message || f.title || "Finding");
    renderFindingInsight(f);
    if (flower && f.concept_id && g) {
      // Temporarily show canvas flower under the board via Focus lens.
      var discover = $("#okf-discover");
      if (discover) discover.classList.add("okf-discover--flower");
      g.controlState.focusEnabled = true;
      g.controlState.focusDepth = 1;
      g.setFocusRoot(f.concept_id);
      if (g.applyLens) g.applyLens("focus");
      if (g.showDetail) g.showDetail(f.concept_id);
      if (f.target_concept_id && g.cy && g.showPathBetween) {
        var a = g.cy.getElementById(f.concept_id);
        var b = g.cy.getElementById(f.target_concept_id);
        if (a.length && b.length) {
          try { g.showPathBetween(a, b); } catch (err) {}
        }
      }
    }
  }

  function renderFindingInsight(f) {
    var empty = $("#detail-empty");
    var content = $("#detail-content");
    if (content) content.hidden = true;
    if (!empty) return;
    empty.hidden = false;
    empty.classList.add("okf-lens-summary");
    empty.innerHTML = "";
    var meta = kindMeta(f.kind);
    empty.appendChild(el("p", { class: "okf-lens-summary__verdict-kicker" }, ["Lens verdict"]));
    empty.appendChild(el("p", { class: "okf-lens-summary__verdict" }, [f.message || f.title || ""]));
    if (f.action) {
      empty.appendChild(el("p", { class: "okf-muted" }, ["Suggested action: " + f.action]));
    }
    var score = typeof f.score === "number" ? Math.round(f.score * 100) + "%" : "";
    empty.appendChild(el("p", { class: "okf-muted" }, [
      meta.label + (score ? " · confidence " + score : ""),
    ]));
  }

  function enterFlows() {
    var discover = $("#okf-discover");
    if (discover) {
      discover.hidden = true;
      discover.classList.remove("okf-discover--flower");
    }
    applyDensity();
    if (state.seed) runRiver();
    else {
      // Pick a strong start_here or first hub.
      var starts = findings().filter(function (f) { return f.kind === "start_here" && f.concept_id; });
      if (starts.length) state.seed = starts[0].concept_id;
      else {
        var g = api();
        if (g && g.cy) {
          var best = null, bestD = -1;
          g.cy.nodes().forEach(function (n) {
            var d = n.degree(false);
            if (d > bestD) { bestD = d; best = n.id(); }
          });
          state.seed = best;
        }
      }
      renderContextRail();
      if (state.seed) runRiver();
      else setInsightVerdict("Type a seed concept to draw the river.");
    }
  }

  function runRiver() {
    var g = api();
    if (!g || !state.seed) return;
    applyDensity();
    // Knowledge uses type-aware flow; dependency emphasises typed relation edges.
    if (state.recipe === "dependency") {
      g.controlState.showEdgeLabels = true;
      g.controlState.relationEdges = true;
      g.controlState.showAllEdges = state.density === "dense";
    }
    g.controlState.focusEnabled = true;
    g.controlState.focusDepth = 2;
    g.setFocusRoot(state.seed);
    if (g.applyLens) g.applyLens("flow");
    if (g.showDetail) g.showDetail(state.seed);
    showActions(state.seed);
    var up = 0, down = 0;
    try {
      var node = g.cy.getElementById(state.seed);
      up = node.incomers("node").length;
      down = node.outgoers("node").length;
    } catch (e) {}
    setInsightVerdict(
      labelOf(state.seed) + " · " + state.recipe + " river — " +
      up + " upstream, " + down + " downstream (2-hop focus)."
    );
  }

  function enterAtlas() {
    var discover = $("#okf-discover");
    if (discover) {
      discover.hidden = true;
      discover.classList.remove("okf-discover--flower");
    }
    var g = api();
    if (!g) return;
    applyDensity();
    g.controlState.focusEnabled = false;
    g.setFocusRoot(null);
    if (g.clearPath) g.clearPath();
    if (g.applyLens) g.applyLens("map");
    // Continent-first: fit largest community after layout settles.
    setTimeout(function () { fitLargestContinent(); }, 700);
    setInsightVerdict("Atlas orientation — largest continent framed. Use recipes for Themes / Bridges / Recent.");
    renderInsightIdle("Click a continent member or use Atlas recipes in the rail.");
  }

  function fitLargestContinent() {
    var g = api();
    if (!g || !g.cy || !g.communityOf) return;
    var com = g.communityOf() || {};
    var counts = {};
    Object.keys(com).forEach(function (id) {
      var c = com[id];
      counts[c] = (counts[c] || 0) + 1;
    });
    var best = null, bestN = 0;
    Object.keys(counts).forEach(function (c) {
      if (counts[c] > bestN) { bestN = counts[c]; best = Number(c); }
    });
    if (best == null) {
      if (g.overlayAwareFit) g.overlayAwareFit();
      return;
    }
    var eles = g.cy.collection();
    Object.keys(com).forEach(function (id) {
      if (com[id] === best) {
        var n = g.cy.getElementById(id);
        if (n.length) eles = eles.union(n);
      }
    });
    if (eles.length) {
      try { g.cy.fit(eles, 48); } catch (e) {
        if (g.overlayAwareFit) g.overlayAwareFit();
      }
    }
  }

  function showActions(conceptId) {
    var bar = $("#okf-insight-actions");
    if (!bar) return;
    bar.hidden = !conceptId;
    bar.dataset.conceptId = conceptId || "";
  }

  function setInsightVerdict(text) {
    var head = $("#okf-detail-lenshead");
    if (!head) return;
    var q = head.querySelector(".okf-signal__question");
    if (q) q.textContent = text;
    else {
      var p = el("p", { class: "okf-signal__question" }, [text]);
      head.insertBefore(p, head.firstChild);
    }
  }

  function renderInsightIdle(msg) {
    var empty = $("#detail-empty");
    var content = $("#detail-content");
    if (content) content.hidden = true;
    if (!empty) return;
    empty.hidden = false;
    empty.textContent = msg || "";
  }

  function wireActions() {
    var bar = $("#okf-insight-actions");
    if (!bar || bar._wired) return;
    bar._wired = true;
    bar.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var act = btn.getAttribute("data-act");
      var cid = bar.dataset.conceptId;
      var g = api();
      if (act === "open" && cid) {
        var open = $("#detail-open-link");
        if (open && open.href) window.location.href = open.href;
        else if (g && g.showDetail) {
          g.showDetail(cid);
          var o2 = $("#detail-open-link");
          if (o2 && o2.href) window.location.href = o2.href;
        }
      } else if (act === "trace") {
        setMode("flows");
        var traceIn = $("#okf-flow-trace");
        if (traceIn) {
          traceIn.focus();
          setInsightVerdict("Enter a target and press Trace path.");
        }
      } else if (act === "propose") {
        proposeLink(cid);
      } else if (act === "ask") {
        askAgent(cid);
      }
    });
  }

  function proposeLink(cid) {
    var f = state.selectedFinding;
    var msg = "Propose a link";
    if (f && f.concept_id && f.target_concept_id) {
      msg = "Please add a link from " + f.concept_id + " to " + f.target_concept_id +
        (f.action ? " (" + f.action + ")" : "") + ". Finding: " + (f.message || "");
    } else if (cid) {
      msg = "Please suggest typed relations to connect orphan/hub “" + labelOf(cid) + "” (" + cid + ").";
    }
    prefillAgent(msg);
  }

  function askAgent(cid) {
    var bits = ["Looking at the Flow Atlas."];
    if (state.mode) bits.push("Mode: " + state.mode + ".");
    if (state.seed) bits.push("River seed: " + state.seed + " (" + labelOf(state.seed) + ").");
    if (state.traceTarget) bits.push("Traced to: " + state.traceTarget + ".");
    if (cid) bits.push("Focus concept: " + cid + " (" + labelOf(cid) + ").");
    if (state.selectedFinding) bits.push("Finding: " + (state.selectedFinding.message || ""));
    bits.push("What should I read or link next?");
    prefillAgent(bits.join(" "));
  }

  function prefillAgent(text) {
    // Prefer studio comment composer / command palette; fall back to clipboard.
    var composer = document.querySelector(
      "#okf-comment-composer textarea, .okf-composer textarea, textarea[name='comment'], .okf-presence-menu"
    );
    var ta = document.querySelector(
      ".okf-panel textarea, #okf-panel textarea, .okf-comment-composer textarea"
    );
    if (ta) {
      ta.focus();
      ta.value = text;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      setInsightVerdict("Drafted ask for the agent — send from the comment panel.");
      return;
    }
    // Open palette if available
    try {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
    } catch (e) {}
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        setInsightVerdict("Copied ask to clipboard — paste into Comments or Commands.");
      }).catch(function () {
        setInsightVerdict(text);
      });
    } else {
      setInsightVerdict(text);
    }
  }

  function parseSeedQuery(q) {
    q = (q || "").trim();
    if (!q) return null;
    var m;
    if ((m = /^from:(.+)$/i.exec(q))) {
      return { type: "seed", value: m[1].trim() };
    }
    if ((m = /^gap:(.+)$/i.exec(q))) {
      return { type: "gap", value: m[1].trim().toLowerCase() };
    }
    if ((m = /^rel:(.+)$/i.exec(q))) {
      return { type: "rel", value: m[1].trim() };
    }
    return { type: "search", value: q };
  }

  function wireSearch() {
    var input = $("#okf-search");
    if (!input || input._flowWired) return;
    input._flowWired = true;
    input.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      var parsed = parseSeedQuery(input.value);
      if (!parsed) return;
      e.preventDefault();
      e.stopPropagation();
      if (parsed.type === "seed") {
        state.seed = resolveId(parsed.value);
        setMode("flows");
      } else if (parsed.type === "gap") {
        setMode("discover");
        // Filter board visually
        var board = $("#okf-discover-board");
        if (board) {
          $$(".okf-discover-card", board).forEach(function (card) {
            var kick = (card.querySelector(".okf-discover-card__kicker") || {}).textContent || "";
            var show = kick.toLowerCase().indexOf(parsed.value) >= 0 ||
              parsed.value === "orphan" && card.className.indexOf("gap") >= 0;
            card.hidden = parsed.value !== "all" && !show &&
              card.className.indexOf(parsed.value) < 0 &&
              kick.toLowerCase().indexOf(parsed.value) < 0;
          });
        }
      } else if (parsed.type === "rel") {
        state.recipe = "dependency";
        state.density = "labeled";
        setMode("flows");
        setInsightVerdict("Dependency recipe · highlighting relation “" + parsed.value + "”.");
      } else {
        var id = resolveId(parsed.value);
        if (id) {
          state.seed = id;
          setMode("flows");
        }
      }
    }, true);
  }

  function wireModes() {
    $$(".okf-mode-switch__btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setMode(btn.getAttribute("data-mode"));
      });
    });
  }

  function boot() {
    if (!document.body.classList.contains("okf-viewer--graph")) return;
    wireModes();
    wireActions();
    wireSearch();
    var saved = null;
    try { saved = localStorage.getItem(MODE_KEY); } catch (e) {}
    var initial = ($("#okf-graph") && $("#okf-graph").getAttribute("data-initial-mode")) || "discover";
    setMode(saved || initial);
  }

  function waitForGraph() {
    if (api()) {
      boot();
      return;
    }
    var tries = 0;
    var t = setInterval(function () {
      tries++;
      if (api() || tries > 80) {
        clearInterval(t);
        if (api()) boot();
      }
    }, 100);
    window.addEventListener("okf-loom:graphReady", function () {
      clearInterval(t);
      boot();
    }, { once: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", waitForGraph);
  } else {
    waitForGraph();
  }
})();
