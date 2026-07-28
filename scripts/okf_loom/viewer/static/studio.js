/* OKF Studio - the studio UI (spec §8 / §9 / §12 / §13).
 *
 * Owns the presentation layer; live.js owns the SSE transport + the
 * no-refresh body patch. studio.js subscribes to window.okfLoomLive for
 * presence/activity/comment/graph events and renders every studio surface.
 *
 * Surfaces:
 *   - Studio bar: presence chip, view-mode switch (Rendered|Source|Split),
 *     Comments / Changes panel toggles, connection indicator, palette btn.
 *   - Comments (§9): selection → "Comment for agent" affordance, composer
 *     with optimistic posting, margin markers, panel listing open/resolved
 *     per concept + across the bundle, filterable.
 *   - Presence (current spec §12): status chip; focused concept highlighted.
 *   - Change list (§12.2): live timeline from /__data/events with one-click
 *     Undo (single + group) where undoable; filterable.
 *   - Command palette (§13.4): Ctrl/Cmd-K, keyboard-first.
 *   - Extension API (§13.6): okfLoomStudio.register(kind, impl). Wired kinds:
 *     {panel, viewMode}. Reserved (forward-compat, accepted + warned):
 *     {toolbar, graphDecorator, suggestionRenderer}.
 *   - Themes (§13.5): light/dark/pastel/sepia/midnight/auto, honouring
 *     saved choice + bootstrap + OS pref.
 *
 * Security: untrusted strings (comment bodies, summaries, ids) go through
 * textContent only. The only innerHTML assignment is the server-rendered
 * concept body (same escaped renderer as the initial page - §7.3). All
 * mutating fetches attach X-OKF-Token from window.__OKF_LOOM_STUDIO__.token.
 *
 * Vanilla ES module, no framework, no bundler. Degrades gracefully: a
 * no-op when window.__OKF_LOOM_STUDIO__ is absent.
 */
(function () {
  "use strict";

  // CSP-safe bootstrap read (see live.js for rationale). studio.js reuses
  // window.__OKF_LOOM_STUDIO__ once live.js publishes it, but reads the data
  // block directly too so the two modules are independently robust.
  function readBoot() {
    if (window.__OKF_LOOM_STUDIO__ && typeof window.__OKF_LOOM_STUDIO__ === "object") {
      return window.__OKF_LOOM_STUDIO__;
    }
    const node = document.getElementById("okf-studio-bootstrap");
    if (node) {
      try {
        const cfg = JSON.parse(node.textContent || "{}");
        window.__OKF_LOOM_STUDIO__ = cfg;
        return cfg;
      } catch (e) { /* fall through */ }
    }
    return null;
  }
  const BOOT = readBoot();
  if (!BOOT) return;
  // iter1 CRI-019: mark <html> as studio-booted ASAP (synchronously, before
  // any async work) so the SSR fallback banner hides the instant studio.js
  // loads. If studio.js fails to load entirely, the class is never added and
  // the banner stays visible, which is exactly the failure case it exists for.
  document.documentElement.classList.add("okf-studio-booted");
  const EDIT = BOOT.edit !== false; // read-only kiosk when false
  const TOKEN = BOOT.token || "";
  const REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ====================================================================
  // 0. Helpers
  // ====================================================================
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "html") n.innerHTML = v; // ONLY for trusted/server HTML
      else if (k === "dataset") for (const d in v) n.dataset[d] = v[d];
      else if (k === "on" && typeof v === "object") for (const ev in v) n.addEventListener(ev, v[ev]);
      else if (k === "hidden") {
        // Treat "", true, "hidden", "until-found" as present (boolean IDL
        // property - assigning "" directly would coerce to false / visible).
        n.hidden = (v === true || v === "" || v === "hidden" || v === "until-found");
      }
      else if (k in n && k !== "list") {
        try { n[k] = v; } catch (e) { n.setAttribute(k, v); }
      } else n.setAttribute(k, v);
    }
    if (kids) appendKids(n, kids);
    return n;
  }
  function appendKids(n, kids) {
    if (!kids) return;
    if (!Array.isArray(kids)) kids = [kids];
    kids.forEach((k) => {
      if (k == null) return;
      if (typeof k === "string" || typeof k === "number") n.appendChild(document.createTextNode(String(k)));
      else n.appendChild(k);
    });
  }

  async function tokenFetch(url, opts) {
    opts = opts || {};
    // §9.4 conflict UX (INTENT-008): preserve the ORIGINAL body object so a
    // 409 with ``conflict: true`` from /__apply can surface a modal whose
    // "Take the agent's" action re-submits the apply WITHOUT ``expected_rev``
    // (force-overwrite). The body is JSON-stringified just below; we hold
    // the live object reference here.
    const originalBodyObj = (opts.body && typeof opts.body !== "string") ? opts.body : null;
    const headers = Object.assign({ "X-OKF-Token": TOKEN }, opts.headers || {});
    if (opts.body && typeof opts.body !== "string") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    opts.headers = headers;
    const res = await fetch(url, opts);
    // §9.4: detect a conflict response from /__apply and surface the modal.
    // The caller's await resolves to a Response chosen by the user action:
    //   - "Keep mine" / Esc → the original 409 Response (caller sees !ok).
    //   - "Take the agent's" → a fresh fetch WITHOUT expected_rev.
    if (res.status === 409 && url === "/__apply") {
      try {
        const cloned = res.clone();
        const data = await cloned.json();
        if (data && data.conflict) {
          return showConflictModal({
            data, url, opts, originalBodyObj, originalResponse: res,
          });
        }
      } catch (e) { /* not JSON or no conflict field — fall through */ }
    }
    return res;
  }

  function currentConceptId() {
    let p = window.location.pathname.replace(/^\/+/, "").replace(/\/+$/, "");
    if (p.endsWith(".md")) p = p.slice(0, -3);
    return p;
  }
  const isConceptPage = () => !!document.querySelector("article.okf-page__main");

  function fmtTime(ts) {
    if (!ts) return "";
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return String(ts);
      return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch (e) { return String(ts); }
  }
  // Phase 5: human relative time for the index recency rail ("4m ago").
  function fmtAgo(ts) {
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return "";
      const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
      if (s < 60) return "just now";
      if (s < 3600) return Math.round(s / 60) + "m ago";
      if (s < 86400) return Math.round(s / 3600) + "h ago";
      return Math.round(s / 86400) + "d ago";
    } catch (e) { return ""; }
  }
  function shortId(id) { return id ? id.slice(-6) : ""; }

  // ---- toast region ----------------------------------------------------
  let toastRegion;
  function toast(msg, opts) {
    opts = opts || {};
    if (!toastRegion) {
      toastRegion = el("div", { class: "okf-toast-region", role: "status", "aria-live": "polite", "aria-atomic": "false" });
      document.body.appendChild(toastRegion);
    }
    const node = el("div", { class: "okf-toast", dataset: { tone: opts.tone || "" } });
    node.appendChild(el("span", { class: "okf-toast__msg" }));
    node.querySelector(".okf-toast__msg").innerHTML = ""; // safety
    if (typeof msg === "string") {
      node.querySelector(".okf-toast__msg").textContent = msg;
    } else if (msg && msg.nodeType) {
      node.querySelector(".okf-toast__msg").appendChild(msg);
    }
    const close = el("button", { class: "okf-toast__close", type: "button", "aria-label": "Dismiss notification", text: "×" });
    node.appendChild(close);
    toastRegion.appendChild(node);
    const dismiss = () => { if (node.parentNode) node.parentNode.removeChild(node); };
    close.addEventListener("click", dismiss);
    if (!opts.sticky) setTimeout(dismiss, opts.ttl || 4500);
    return { close: dismiss, node };
  }

  // ====================================================================
  // 1. State
  // ====================================================================
  const state = {
    view: readView(),                // rendered | source | split
    conceptId: currentConceptId(),
    doc: null,                       // cached /__data/doc for the open concept
    graph: null,                     // cached /__data/graph.json
    comments: [],                    // [{id, concept, anchor, state, body, claimed_by, resolved_activity, reply, ts}]
    events: [],                      // change-list rows
    presence: { state: "idle" },
    presenceHistory: [],   // iter2 G13: [{state, focus, ts}] recent transitions (cap 24)
    openPanel: null,                 // current slide-over panel id
    draftBody: "",                   // preserved across view toggles + patches
    draftAnchor: { kind: "concept", ref: currentConceptId() },
    selectionDraft: null,             // last visible text selection for the comment affordance
    filters: { actor: "", concept: "", action: "" },
    nextCommentSeq: 1,               // for local optimistic ids
    commentView: loadCommentView(),  // panel prefs (sort/filter/expand)
  };
  function readView() {
    const q = new URLSearchParams(window.location.search).get("view");
    if (q === "source" || q === "split") return q;
    return "rendered";
  }

  // ====================================================================
  // 2. Themes (§13.5)
  // ====================================================================
  // Theme names + button glyphs. KEEP IN SYNC with the copies in wiki.js /
  // graph.js and render.py:_theme_button_html.
  const THEMES = ["light", "dark", "pastel", "sepia", "midnight"];
  const THEME_GLYPHS = { light: "☀", dark: "☾", pastel: "✿", sepia: "☕", midnight: "★" };
  function effectiveTheme(choice) {
    if (THEMES.indexOf(choice) >= 0) return choice;
    // auto: follow OS preference
    return (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  function applyThemeAttr(t, opts) {
    if (THEMES.indexOf(t) < 0) t = "light";
    document.documentElement.setAttribute("data-theme", t);
    // Persist by default so a palette-chosen theme survives navigation
    // (wiki.js reads localStorage['okf-theme'] on every page). Boot and
    // "Auto (follow OS)" pass persist:false — a resolved OS preference
    // must not be frozen as an explicit user choice.
    if (!opts || opts.persist !== false) {
      try { localStorage.setItem("okf-theme", t); } catch (e) {}
    }
    // Keep the existing topbar cycle button (wiki.js) in sync if present.
    const tb = document.getElementById("okf-theme");
    if (tb) {
      tb.textContent = THEME_GLYPHS[t];
      tb.setAttribute("title", "Theme: " + t + " — click to cycle");
      tb.setAttribute("aria-label", "Change colour theme (current: " + t + ")");
      tb.removeAttribute("aria-pressed");
    }
  }
  function currentThemeChoice() {
    const t = document.documentElement.getAttribute("data-theme");
    return THEMES.indexOf(t) >= 0 ? t : "light";
  }
  // Apply the bootstrap theme on boot. A saved user choice (wiki.js theme
  // button / command palette) outranks the server-side studio.theme config —
  // otherwise every navigation would stomp the user's pick back to the
  // config default.
  (function bootTheme() {
    let saved = null;
    try { saved = localStorage.getItem("okf-theme"); } catch (e) {}
    const t = (saved && THEMES.indexOf(saved) >= 0)
      ? saved
      : effectiveTheme(BOOT.theme || "auto");
    applyThemeAttr(t, { persist: false });
  })();

  // ====================================================================
  // 3. Studio chrome (inline in the sticky topbar — one bar, quiet defaults)
  // ====================================================================
  // Atlas composition pass: the old dual chrome (topbar + sibling studio bar)
  // is gone. Studio controls mount INTO `.okf-topbar` as an inline cluster so
  // every page pays one ~52px sticky tax. Presence collapses into a drawer;
  // Comments/Changes only shout when counts are non-zero.
  const bar = el("div", { class: "okf-studio-bar okf-studio-bar--inline", role: "toolbar", "aria-label": "Studio controls" });
  const leftGroup = el("div", { class: "okf-studio-bar__group" });
  const rightGroup = el("div", { class: "okf-studio-bar__group okf-studio-bar__group--right" });

  // Presence chip (compact; full label lives in the drawer).
  const presenceDot = el("span", { class: "okf-presence__dot", "aria-hidden": "true" });
  const presenceLabel = el("span", { class: "okf-presence__label" });
  presenceLabel.appendChild(el("span", { class: "okf-presence__actor", text: "Agent" }));
  presenceLabel.appendChild(document.createTextNode(" idle"));
  const presenceChip = el("span", { class: "okf-presence", "data-state": "idle", role: "status",
    "aria-live": "polite", "aria-label": "Agent presence: idle" },
    [presenceDot, presenceLabel]);

  // Watching toggle (lives in the presence drawer).
  const watchingToggle = el("button", {
    type: "button",
    class: "okf-studiobtn okf-watch-toggle",
    "aria-pressed": "false",
    "aria-label": "Agent watching, currently off",
    title: "Toggle proactive agent watching (§3)",
  });
  watchingToggle.appendChild(el("span", { class: "okf-watch-toggle__icon", "aria-hidden": "true", text: "\u25C9" }));
  watchingToggle.appendChild(el("span", { class: "okf-watch-toggle__label", text: "Watching" }));
  watchingToggle.addEventListener("click", () => {
    const nowOn = watchingToggle.getAttribute("aria-pressed") !== "true";
    watchingToggle.setAttribute("aria-pressed", nowOn ? "true" : "false");
    tokenFetch("/__presence", {
      method: "POST",
      body: { actor: "agent", state: nowOn ? "watching" : "idle" },
    }).catch(() => {
      watchingToggle.setAttribute("aria-pressed", nowOn ? "false" : "true");
      toast("Could not update agent watching state.", { tone: "error" });
    });
  });

  // Connection indicator (driven by live.js hub).
  const connDot = el("span", { class: "okf-conn__dot", "aria-hidden": "true" });
  const connLabel = el("span", { text: "Live" });
  const connChip = el("span", { class: "okf-conn", "data-state": "online",
    title: "Live updates connection", role: "status", "aria-live": "polite",
    "aria-label": "Live updates connection: online" }, [connDot, connLabel]);

  // Presence drawer: one quiet control that expands to watching / live / activity.
  const presenceMenu = el("div", { class: "okf-presence-menu" });
  const presenceBtn = el("button", {
    type: "button",
    class: "okf-studiobtn okf-presence-menu__btn",
    "aria-expanded": "false",
    "aria-haspopup": "true",
    "aria-controls": "okf-presence-drawer",
    title: "Agent presence and studio status",
  });
  const presenceBtnDot = el("span", { class: "okf-presence__dot", "aria-hidden": "true" });
  const presenceBtnText = el("span", { class: "okf-presence-menu__btn-label", text: "Agent" });
  presenceBtn.appendChild(presenceBtnDot);
  presenceBtn.appendChild(presenceBtnText);
  const presenceDrawer = el("div", {
    class: "okf-presence-menu__drawer",
    id: "okf-presence-drawer",
    hidden: "hidden",
    role: "region",
    "aria-label": "Agent presence",
  });
  presenceDrawer.appendChild(presenceChip);
  presenceDrawer.appendChild(watchingToggle);
  presenceDrawer.appendChild(connChip);
  presenceMenu.appendChild(presenceBtn);
  presenceMenu.appendChild(presenceDrawer);
  function setPresenceDrawer(open) {
    presenceBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) presenceDrawer.removeAttribute("hidden");
    else presenceDrawer.setAttribute("hidden", "hidden");
  }
  presenceBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    setPresenceDrawer(presenceBtn.getAttribute("aria-expanded") !== "true");
  });
  document.addEventListener("click", (e) => {
    if (!presenceMenu.contains(e.target)) setPresenceDrawer(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setPresenceDrawer(false);
  });
  // Keep the compact button's dot in sync with the presence chip.
  const _presenceObs = new MutationObserver(() => {
    presenceBtnDot.style.background = getComputedStyle(presenceDot).backgroundColor;
    const st = presenceChip.getAttribute("data-state") || "idle";
    presenceBtn.setAttribute("data-state", st);
    presenceBtn.setAttribute("aria-label", "Agent presence: " + st);
    const actor = $(".okf-presence__actor", presenceLabel);
    presenceBtnText.textContent = (actor && actor.textContent) || "Agent";
  });
  try { _presenceObs.observe(presenceChip, { attributes: true, subtree: true, childList: true, characterData: true }); } catch (e) {}

  leftGroup.appendChild(presenceMenu);

  // View-mode switch (concept pages only)
  const viewSwitch = el("div", { class: "okf-viewswitch", role: "group", "aria-label": "View mode" });
  function viewBtn(mode, label) {
    const b = el("button", { type: "button", class: "okf-viewswitch__btn",
      "aria-pressed": state.view === mode ? "true" : "false", text: label });
    b.addEventListener("click", () => setView(mode));
    b.dataset.mode = mode;
    return b;
  }
  const viewBtns = {
    rendered: viewBtn("rendered", "Rendered"),
    source: viewBtn("source", "Source"),
    split: viewBtn("split", "Split"),
  };
  Object.keys(viewBtns).forEach((k) => viewSwitch.appendChild(viewBtns[k]));

  // Panel toggle buttons — quiet until there is something to see.
  const commentsBtn = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--count",
    "aria-expanded": "false", "aria-controls": "okf-panel", "data-count": "0", text: "Comments" });
  const commentsBadge = el("span", { class: "okf-badge", "aria-hidden": "true", "data-count": "0", text: "0" });
  commentsBtn.insertBefore(commentsBadge, commentsBtn.firstChild);
  commentsBtn.addEventListener("click", () => togglePanel("comments"));

  const changesBtn = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--count",
    "aria-expanded": "false", "aria-controls": "okf-panel", "data-count": "0", text: "Changes" });
  const changesBadge = el("span", { class: "okf-badge", "aria-hidden": "true", "data-count": "0", text: "0" });
  changesBtn.insertBefore(changesBadge, changesBtn.firstChild);
  changesBtn.addEventListener("click", () => togglePanel("changes"));

  const isApplePlatform = /Mac|iPhone|iPad|iPod/.test(
    (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "");
  const paletteHint = isApplePlatform ? "⌘K" : "Ctrl+K";
  const paletteBtn = el("button", { type: "button", class: "okf-studiobtn okf-palettebtn",
    "aria-label": "Open the command palette (" + (isApplePlatform ? "Command K" : "Control K") + ")",
    title: "Search pages and run commands  (" + paletteHint + ")" },
    [document.createTextNode("Commands "),
     el("kbd", { class: "okf-kbd", "aria-hidden": "true", text: paletteHint })]);
  paletteBtn.addEventListener("click", openPalette);

  rightGroup.appendChild(commentsBtn);
  rightGroup.appendChild(changesBtn);
  rightGroup.appendChild(paletteBtn);
  bar.appendChild(leftGroup);
  bar.appendChild(rightGroup);

  function mountBar() {
    const topbar = $(".okf-topbar");
    const controls = topbar && $(".okf-topbar__controls", topbar);
    if (controls) {
      // Prefer mounting inside the sticky topbar so chrome is one row.
      // Place studio cluster after Graph/Index/theme (end of controls).
      controls.appendChild(bar);
      topbar.classList.add("okf-topbar--studio");
    } else if (topbar && topbar.parentNode) {
      topbar.parentNode.insertBefore(bar, topbar.nextSibling);
      bar.classList.remove("okf-studio-bar--inline");
    } else {
      document.body.insertBefore(bar, document.body.firstChild);
      bar.classList.remove("okf-studio-bar--inline");
    }
    if (isConceptPage()) {
      leftGroup.appendChild(viewSwitch);
    }
    // Hero-ise the search field when present.
    const searchInput = topbar && topbar.querySelector('input[type="search"]');
    if (searchInput && !searchInput.getAttribute("placeholder")) {
      searchInput.setAttribute("placeholder", "Search the atlas\u2026");
    } else if (searchInput && /Search/i.test(searchInput.getAttribute("placeholder") || "")) {
      searchInput.setAttribute("placeholder", "Search the atlas\u2026");
    }
  }

  // ====================================================================
  // 4. View modes (§8) + applyDoc(doc)
  // ====================================================================
  let sourcePre = null;
  let viewWrap = null;
  let splitDivider = null;
  // iter2 G8: split-pane niceties — draggable divider (pointer + keyboard)
  // and proportional synced scroll between the rendered + source panes.
  // splitPct is the rendered-pane fraction (0.2–0.8). Persisted to
  // localStorage so a user's preferred split survives reloads.
  const SPLIT_MIN = 0.2, SPLIT_MAX = 0.8;
  let splitPct = 0.5;
  try {
    const saved = parseFloat(localStorage.getItem("okf-split-pct"));
    if (!isNaN(saved) && saved >= SPLIT_MIN && saved <= SPLIT_MAX) splitPct = saved;
  } catch (e) {}
  let splitSyncGuard = false;  // prevents A→B→A scroll-feedback loops

  function applySplitPct(pct) {
    splitPct = Math.max(SPLIT_MIN, Math.min(SPLIT_MAX, pct));
    if (viewWrap) viewWrap.style.setProperty("--okf-split-pct", splitPct + "fr");
    if (splitDivider) {
      splitDivider.setAttribute("aria-valuenow", String(Math.round(splitPct * 100)));
      splitDivider.setAttribute("aria-valuetext",
        "Rendered pane " + Math.round(splitPct * 100) + " percent, source " + Math.round((1 - splitPct) * 100) + " percent");
    }
  }

  function ensureViewWrap() {
    if (viewWrap) return;
    const body = $(".okf-page__body");
    if (!body) return;
    viewWrap = el("div", { class: "okf-view", dataset: { okfView: state.view } });
    body.parentNode.insertBefore(viewWrap, body);
    viewWrap.appendChild(body);
    // iter2 G8: draggable divider between the rendered + source panes. Lives
    // in the DOM always but only visible/active in split mode (CSS hides it
    // otherwise + under 900px). role=separator + aria-orientation so AT + kb
    // users can resize with Arrow Left / Right (3% per press, Shift = 10%).
    splitDivider = el("div", {
      class: "okf-split__divider",
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "Resize rendered and source panes. Arrow keys to adjust.",
      "aria-valuemin": "20", "aria-valuemax": "80", "aria-valuenow": "50",
      tabindex: "0",
    });
    viewWrap.appendChild(splitDivider);
    sourcePre = el("pre", { class: "okf-source", hidden: state.view === "rendered",
      "aria-label": "Source markdown (read-only)" });
    viewWrap.appendChild(sourcePre);
    wireSplitDivider();
    wireSplitScroll();
    applySplitPct(splitPct);
  }

  // Draggable divider: pointer drag + keyboard arrows. The drag uses
  // pointer events (works for mouse + touch + pen) and updates the split
  // ratio from the pointer's X within the viewWrap bounding rect.
  function wireSplitDivider() {
    if (!splitDivider || !viewWrap) return;
    let dragging = false;
    splitDivider.addEventListener("pointerdown", (e) => {
      if (!isSplitView()) return;
      dragging = true;
      splitDivider.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    splitDivider.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const rect = viewWrap.getBoundingClientRect();
      if (!rect.width) return;
      const pct = (e.clientX - rect.left) / rect.width;
      applySplitPct(pct);
    });
    const endDrag = (e) => {
      if (!dragging) return;
      dragging = false;
      try { splitDivider.releasePointerCapture(e.pointerId); } catch (err) {}
      try { localStorage.setItem("okf-split-pct", String(splitPct)); } catch (err) {}
    };
    splitDivider.addEventListener("pointerup", endDrag);
    splitDivider.addEventListener("pointercancel", endDrag);
    // Keyboard: Arrow Left/Right adjust 3%, Shift+Arrow 10%. Home/End reset
    // to the extremes (clamped to SPLIT_MIN/MAX).
    splitDivider.addEventListener("keydown", (e) => {
      if (!isSplitView()) return;
      const step = e.shiftKey ? 0.10 : 0.03;
      let handled = true;
      if (e.key === "ArrowLeft") applySplitPct(splitPct - step);
      else if (e.key === "ArrowRight") applySplitPct(splitPct + step);
      else if (e.key === "Home") applySplitPct(SPLIT_MIN);
      else if (e.key === "End") applySplitPct(SPLIT_MAX);
      else handled = false;
      if (handled) {
        e.preventDefault();
        try { localStorage.setItem("okf-split-pct", String(splitPct)); } catch (err) {}
      }
    });
  }

  // Proportional synced scroll: scrolling one pane scrolls the other by the
  // same fraction of its scroll range. The guard flag breaks the feedback
  // loop (B's scroll firing back into A).
  function wireSplitScroll() {
    const body = $(".okf-page__body");
    if (!body || !sourcePre) return;
    const sync = (src, dst) => {
      if (splitSyncGuard) return;
      if (!isSplitView()) return;
      const max = src.scrollHeight - src.clientHeight;
      if (max <= 0) return;
      const ratio = src.scrollTop / max;
      const dstMax = dst.scrollHeight - dst.clientHeight;
      if (dstMax > 0) {
        splitSyncGuard = true;
        dst.scrollTop = ratio * dstMax;
        splitSyncGuard = false;
      }
    };
    body.addEventListener("scroll", () => sync(body, sourcePre), { passive: true });
    sourcePre.addEventListener("scroll", () => sync(sourcePre, body), { passive: true });
  }

  function isSplitView() {
    return !!viewWrap && viewWrap.dataset.okfView === "split"
      && window.matchMedia("(min-width: 901px)").matches;
  }

  function setView(mode) {
    if (mode !== "rendered" && mode !== "source" && mode !== "split") return;
    state.view = mode;
    ensureViewWrap();
    if (viewWrap) viewWrap.dataset.okfView = mode;
    if (sourcePre) sourcePre.hidden = (mode === "rendered");
    if (splitDivider) splitDivider.hidden = (mode !== "split");
    Object.keys(viewBtns).forEach((k) => viewBtns[k].setAttribute("aria-pressed", k === mode ? "true" : "false"));
    // Deep-link via ?view=. Preserves everything else (no reload).
    const url = new URL(window.location.href);
    if (mode === "rendered") url.searchParams.delete("view");
    else url.searchParams.set("view", mode);
    window.history.replaceState(null, "", url.toString());
    // Source/split need the raw markdown; load lazily.
    if (mode !== "rendered") ensureSourceLoaded();
  }

  function ensureSourceLoaded() {
    if (!sourcePre || sourcePre.dataset.loaded === "1") return;
    const id = state.conceptId;
    if (!id) return;
    fetch("/__data/doc?id=" + encodeURIComponent(id), { headers: { Accept: "application/json" } })
      .then((r) => r.ok ? r.json() : null)
      .then((doc) => {
        if (!doc) return;
        state.doc = doc;
        if (sourcePre) {
          sourcePre.textContent = doc.raw || "";
          sourcePre.dataset.loaded = "1";
        }
      })
      .catch((e) => console.error("[okf-studio] source load failed", e));
  }

  // Called by live.js after a `changed` on the open concept. Renders the
  // doc in the current view mode and updates title/description in place.
  // iter1 CRI-001: replaces the whole-body innerHTML swap with a block-level
  // diff that patches only the changed/moved/inserted/removed blocks, so a
  // single sentence edit no longer reads as a full-body flash. Returns true
  // so live.js knows the scoped pulse was handled here (not in its fallback).
  function applyDoc(doc, opts) {
    opts = opts || {};
    if (!doc) return false;
    state.doc = doc;
    const titleEl = $(".okf-page__title");
    if (titleEl && typeof doc.title === "string" && doc.title !== titleEl.textContent) {
      titleEl.textContent = doc.title;
    }
    const descEl = $(".okf-page__description");
    if (descEl && typeof doc.description === "string") descEl.textContent = doc.description;
    const body = $(".okf-page__body");
    let changedBlocks = [];
    if (body && typeof doc.html === "string") {
      // Server-rendered HTML from the SAME escaped renderer the page used.
      // Block-diff it against the live body instead of swapping wholesale.
      const sel = saveSelectionAcrossPatch(body);
      changedBlocks = diffAndPatchBody(body, doc.html);
      sel.restore(changedBlocks);
    }
    if (sourcePre) {
      sourcePre.textContent = doc.raw || "";
      sourcePre.dataset.loaded = "1";
    }
    // Re-bind comment affordance + re-apply text-range marks + margin markers
    // over the patched body (iter1 CRI-002). Marks survive because they are
    // re-resolved from persisted anchors after every patch.
    bindSelectionAffordance();
    applyCommentMarks();
    applyPendingDraftMark();
    rebuildMarginMarkers();
    // Scoped pulse: flash ONLY the changed blocks, not the whole body. If
    // nothing changed (e.g. a no-op re-render), no pulse at all.
    if (opts.pulse && changedBlocks.length) pulseBlocks(changedBlocks);
    // Notify renderers.js (mermaid, highlight.js, KaTeX) that the body
    // content changed — but ONLY when blocks actually changed. Firing on
    // every patch (including no-op re-renders) causes a visible flash
    // as mermaid re-renders diagrams that didn't change.
    if (changedBlocks.length > 0) {
      try { window.dispatchEvent(new CustomEvent("okf-loom:bodyPatched")); } catch (e) {}
    }
    return true; // live.js must NOT also pulse the whole body.
  }

  // ---- block-level diff + patch (iter1 CRI-001) ------------------------
  // Splits rendered HTML at block boundaries (heading / paragraph / list /
  // table / pre / blockquote / hr), aligns old vs new via LCS on a
  // normalised signature, and applies the minimal DOM mutation set. List
  // and table blocks that exist on both sides but differ recurse one level
  // to diff their <li>/<tr> children, so "agent added one item to a list"
  // swaps only that item, not the whole list. Depth is capped at 2 so a
  // pathological nested structure can't blow the stack.
  const BLOCK_SELECTOR = "h1,h2,h3,h4,h5,h6,p,ul,ol,table,pre,blockquote,hr,div";
  function blockChildren(parent) {
    const out = [];
    for (let n = parent.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 1) out.push(n);
    }
    return out;
  }
  function blockSig(el) {
    // Whitespace-collapsed text + tag + id (heading ids are stable anchors
    // for comment marks; including them keeps a renamed heading "changed").
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute("id") || "";
    // Mermaid/math blocks: after CDN rendering (mermaid.js, KaTeX), the
    // element's textContent changes from the raw source to the rendered
    // SVG/MathML output. Use the data attribute (the original source) for
    // the signature so the diff treats "rendered" and "raw" versions of
    // the SAME diagram as equal — preventing a visible flash-to-raw-text
    // on live patches that don't actually change the diagram.
    if (el.classList && (el.classList.contains("mermaid") || el.classList.contains("math"))) {
      var source = el.getAttribute("data-source") || el.textContent || "";
      return tag + "|" + id + "|" + source.replace(/\s+/g, " ").trim().slice(0, 200);
    }
    // Enhancement wrappers (renderers.js table/code UX): sign as the INNER
    // block so an enhanced live table/pre compares equal to the bare
    // server-rendered element on the other side of the diff. The tablewrap
    // uses data-source (the original text captured at enhance time) because
    // user-applied sorting reorders the live textContent without the
    // content having changed. Full-length (no 200-char slice) on both the
    // wrapper AND bare table/pre sides: a sorted table means row-level
    // recursion can't reconcile order, so equality must be exact — a
    // truncated signature would silently drop edits past the prefix.
    if (el.classList && el.classList.contains("okf-tablewrap")) {
      return "table|" + id + "|" + (el.getAttribute("data-source") || "");
    }
    if (el.classList && el.classList.contains("okf-codewrap")) {
      var inner = el.querySelector("pre");
      var innerText = inner ? (inner.textContent || "") : "";
      return "pre|" + id + "|" + innerText.replace(/\s+/g, " ").trim();
    }
    if (tag === "table" || tag === "pre") {
      return tag + "|" + id + "|" + (el.textContent || "").replace(/\s+/g, " ").trim();
    }
    const text = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200);
    return tag + "|" + id + "|" + text;
  }
  function parseHtmlToBlocks(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return blockChildren(tmp);
  }
  // Standard LCS DP over signature arrays → edit ops. Returns an array of
  // {op: "keep"|"replace"|"insert"|"remove", oldEl?, newEl?}.
  function lcsOps(oldBlocks, newBlocks) {
    const n = oldBlocks.length, m = newBlocks.length;
    const oldSigs = oldBlocks.map(blockSig);
    const newSigs = newBlocks.map(blockSig);
    // dp[i][j] = LCS length of oldSigs[i:] and newSigs[j:].
    const dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = oldSigs[i] === newSigs[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (oldSigs[i] === newSigs[j]) { ops.push({ op: "keep", oldEl: oldBlocks[i], newEl: newBlocks[j] }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ op: "remove", oldEl: oldBlocks[i] }); i++; }
      else { ops.push({ op: "insert", newEl: newBlocks[j] }); j++; }
    }
    while (i < n) { ops.push({ op: "remove", oldEl: oldBlocks[i] }); i++; }
    while (j < m) { ops.push({ op: "insert", newEl: newBlocks[j] }); j++; }
    return ops;
  }
  // diffAndPatchBody mutates `parent` to match `newHtml` at the block level.
  // Returns the list of newly-inserted or replaced element nodes (for the
  // scoped pulse). Recurses into matching list/table blocks that differ.
  function diffAndPatchBody(parent, newHtml) {
    const newBlocks = parseHtmlToBlocks(newHtml);
    return diffChildren(parent, newBlocks, 0);
  }
  function diffChildren(parent, newBlocks, depth) {
    const oldBlocks = blockChildren(parent);
    const ops = lcsOps(oldBlocks, newBlocks);
    const changed = [];
    // Apply ops. We rebuild the child list by walking ops and using
    // parent.insertBefore / removeChild so untouched nodes keep their
    // identity (and any in-body selection/focus on them survives).
    let cursor = parent.firstChild; // node we're currently considering in the live DOM
    for (let k = 0; k < ops.length; k++) {
      const op = ops[k];
      if (op.op === "keep") {
        // Recurse into matching containers whose children may differ.
        if (depth < 2 && (op.oldEl.tagName === "UL" || op.oldEl.tagName === "OL" || op.oldEl.tagName === "TABLE")) {
          const innerChanged = diffChildren(op.oldEl, blockChildren(op.newEl), depth + 1);
          if (innerChanged.length) { changed.push.apply(changed, innerChanged); }
        }
        cursor = op.oldEl.nextSibling;
      } else if (op.op === "replace") {
        // (Not produced by lcsOps directly; handled as remove+insert.)
      } else if (op.op === "remove") {
        const next = op.oldEl.nextSibling;
        parent.removeChild(op.oldEl);
        cursor = next;
      } else if (op.op === "insert") {
        const imported = parent.ownerDocument.importNode(op.newEl, true);
        parent.insertBefore(imported, cursor);
        changed.push(imported);
      }
    }
    return changed;
  }
  function pulseBlocks(blocks) {
    if (REDUCED_MOTION) return;
    blocks.forEach((b) => {
      b.classList.remove("okf-pulse");
      void b.offsetWidth; // restart the animation on consecutive patches
      b.classList.add("okf-pulse");
    });
  }

  // ---- selection preservation across a block patch (iter1 CRI-001) -----
  // Captures the current in-body selection. Because unchanged blocks keep
  // their DOM identity (diffChildren never touches them), a selection that
  // lives entirely inside an unchanged block survives automatically. Only
  // selections spanning a CHANGED block need a best-effort text-search
  // restore after the patch. Returns {restore(changedBlocks)}.
  function saveSelectionAcrossPatch(body) {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return { restore() {} };
    const range = sel.getRangeAt(0);
    if (!body.contains(range.commonAncestorContainer)) return { restore() {} };
    const text = sel.toString();
    if (!text) return { restore() {} };
    // Record the text of the block containing the anchor so we can tell
    // whether that block was replaced.
    const anchorBlock = containingBlock(range.startContainer, body);
    const anchorBlockText = anchorBlock ? (anchorBlock.textContent || "").replace(/\s+/g, " ").trim() : "";
    return {
      _text: text,
      _anchorBlockText: anchorBlockText,
      restore(changedBlocks) {
        if (!text) return;
        // If the anchor block wasn't changed, the live Selection is still
        // valid (the DOM node is untouched). Only re-resolve when a changed
        // block overlaps the prior selection.
        const changedSigs = changedBlocks.map((b) => (b.textContent || "").replace(/\s+/g, " ").trim());
        const anchorChanged = !anchorBlockText || changedSigs.indexOf(anchorBlockText) >= 0;
        if (!anchorChanged) return; // selection survived untouched
        // Best-effort: find the text anywhere in the body and reselect it.
        const found = findTextNode(body, text);
        if (!found) return;
        try {
          const r = document.createRange();
          r.setStart(found.node, found.start);
          r.setEnd(found.node, found.end);
          sel.removeAllRanges();
          sel.addRange(r);
        } catch (e) { /* give up silently; selection is best-effort */ }
      },
    };
  }
  function containingBlock(node, root) {
    let el = node.nodeType === 1 ? node : node.parentElement;
    while (el && el !== root) {
      if (el.tagName === "P" || /^H[1-6]$/.test(el.tagName) || el.tagName === "LI" ||
          el.tagName === "UL" || el.tagName === "OL" || el.tagName === "TABLE" ||
          el.tagName === "PRE" || el.tagName === "BLOCKQUOTE") return el;
      el = el.parentElement;
    }
    return root;
  }
  function findTextNode(root, text) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
      const idx = node.nodeValue ? node.nodeValue.indexOf(text) : -1;
      if (idx >= 0) return { node: node, start: idx, end: idx + text.length };
    }
    return null;
  }

  // ====================================================================
  // 5. Comments (§9)
  // ====================================================================
  // Initial state reconstructed from the events feed (comment events), then
  // kept live by the `comment` SSE signal. Each event carries the full
  // latest record for an id (see studio.post_comment/update_comment).
  async function loadComments() {
    try {
      // ARCH4-003 / QUA4-001 fix: fetch canonical comment state from
      // /__comments (directives.jsonl, last-write-wins) instead of
      // reconstructing from /__data/events (which had a desc-order +
      // last-iteration inversion bug showing claimed/resolved as "open").
      const [commentRes, eventRes] = await Promise.all([
        fetch("/__comments", { headers: { Accept: "application/json" } }),
        fetch("/__data/events?limit=500", { headers: { Accept: "application/json" } }),
      ]);
      const commentData = await commentRes.json();
      const eventData = await eventRes.json();
      // State-ownership fence (async-lifecycle): live SSE events may have been
      // upserted into state.events WHILE this initial fetch was in flight — a
      // slow /__data/events response must NOT wipe a live burst that already
      // rendered. Merge by stable id: the fetched snapshot is the baseline; a
      // live record wins on conflict (it carries the freshest fields + any
      // burst/group tags applied on arrival); and live events absent from the
      // snapshot (they arrived after the server built it, e.g. an id-less
      // changed/created/removed signal) stay on top in arrival order.
      const liveEvents = state.events || [];
      const liveById = new Map();
      liveEvents.forEach((e) => { if (e && e.id) liveById.set(e.id, e); });
      const seenIds = new Set();
      const mergedEvents = [];
      (eventData.events || []).forEach((e) => {
        if (!e) return;
        if (e.id && liveById.has(e.id)) {
          mergedEvents.push(liveById.get(e.id));  // keep the live record (tags intact)
          seenIds.add(e.id);
        } else {
          mergedEvents.push(e);
          if (e.id) seenIds.add(e.id);
        }
      });
      const liveExtras = liveEvents.filter((e) => e && (!e.id || !seenIds.has(e.id)));
      state.events = liveExtras.concat(mergedEvents);
      // Use canonical comment state from the server.
      state.comments = (commentData.comments || []).slice().sort(byTsDesc);
      renderCommentsPanel();
      renderChangeList();
      updateBadges();
      // iter1 CRI-002: apply text-range marks now that comments are loaded.
      applyCommentMarks();
      rebuildMarginMarkers();
    } catch (e) {
      console.error("[okf-studio] load comments/events failed", e);
    }
  }
  function byTsDesc(a, b) {
    const ta = a.ts || "", tb = b.ts || "";
    if (ta < tb) return 1; if (ta > tb) return -1; return 0;
  }
  // id-based upsert (last write wins). Guards against the SSE-vs-POST race
  // where the `comment` event and the POST /__comment response both carry the
  // confirmed record - without dedup the optimistic row + the SSE insert +
  // the POST reconcile could leave 2-3 copies of the same id in state.
  function upsertComment(c) {
    if (!c || !c.id) return;
    const idx = state.comments.findIndex((x) => x.id === c.id);
    if (idx >= 0) state.comments[idx] = Object.assign({}, state.comments[idx], c);
    else state.comments.unshift(Object.assign({ ts: new Date().toISOString() }, c));
    state.comments.sort(byTsDesc);
  }

  // --- selection affordance --------------------------------------------
  // iter1 CRI-017: aria-live announces the affordance when it appears (it
  // shows in response to a user selection), and the leading glyph is an
  // inline SVG with aria-hidden instead of an emoji.
  let affordance;
  const COMMENT_ICON_SVG = '<svg class="okf-comment-afford__icon" viewBox="0 0 16 16" fill="none" aria-hidden="true" focusable="false"><path d="M2 3h12v8H6l-3 3v-3H2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  function ensureAffordance() {
    if (affordance) return affordance;
    affordance = el("div", { class: "okf-comment-afford", hidden: "", role: "status", "aria-live": "polite" });
    const btn = el("button", { type: "button", class: "okf-studiobtn", "aria-label": "Comment for agent on the selected text" });
    btn.innerHTML = COMMENT_ICON_SVG;
    btn.appendChild(document.createTextNode(" Comment"));
    btn.addEventListener("mousedown", (e) => e.preventDefault()); // keep selection
    btn.addEventListener("click", onAffordanceClick);
    affordance.appendChild(btn);
    document.body.appendChild(affordance);
    return affordance;
  }
  // Stable debounced handlers, created once: bindSelectionAffordance is
  // re-run after every SSE body patch (applyDoc), and a fresh closure per
  // call would stack a new listener each time (addEventListener only
  // dedupes identical function references) — an unbounded leak over a
  // live session.
  const _affordanceOnSelection = debounce(updateAffordance, 120);
  const _affordanceOnResize = debounce(hideAffordance, 120);
  function bindSelectionAffordance() {
    if (!EDIT) return;
    // Bind on concept pages AND graph pages (the graph detail panel
    // has renderable content that should be selectable + commentable).
    if (!isConceptPage() && !document.getElementById("detail-body")) return;
    ensureAffordance();
    // On concept pages, .okf-page__body exists. On graph pages, it doesn't
    // but #detail-body does — don't bail if one is missing, just check the
    // other in getSelectionInBody. Stable references make re-binding after
    // each patch a no-op.
    document.addEventListener("selectionchange", _affordanceOnSelection);
    document.addEventListener("scroll", hideAffordance, { passive: true });
    window.addEventListener("resize", _affordanceOnResize);
  }
  function getSelectionInBody() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const range = sel.getRangeAt(0);
    // Check the rendered body, the source pane, AND the graph detail panel
    // so comments work in all three surfaces.
    const body = $(".okf-page__body");
    const source = $(".okf-source");
    const graphDetail = document.getElementById("detail-body");
    const inRendered = body && body.contains(range.commonAncestorContainer);
    const inSource = source && source.contains(range.commonAncestorContainer);
    const inGraph = graphDetail && graphDetail.contains(range.commonAncestorContainer);
    if (!inRendered && !inSource && !inGraph) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    return { sel, range, text, inSource, inGraph };
  }
  // iter1 CRI-002: find the nearest heading WITH AN ID (the renderer stamps
  // stable slug ids on every heading) so the comment anchor survives block
  // re-rendering. Falls back to the heading text if no id is present.
  function nearestHeadingAnchor(node) {
    const body = $(".okf-page__body");
    if (!body) return { id: "", text: "" };
    const headings = $$("h1, h2, h3, h4, h5, h6", body);
    const startNode = (node && node.nodeType === 1) ? node : (node && node.parentElement);
    if (!startNode) return { id: "", text: "" };
    let last = null;
    for (let i = 0; i < headings.length; i++) {
      if (startNode.compareDocumentPosition(headings[i]) & Node.DOCUMENT_POSITION_PRECEDING) {
        last = headings[i];
      } else break;
    }
    if (!last) return { id: "", text: "" };
    return { id: last.getAttribute("id") || "", text: (last.textContent || "").trim() };
  }
  function updateAffordance() {
    if (!affordance) return;
    const inBody = getSelectionInBody();
    if (!inBody) { affordance.hidden = true; state.selectionDraft = null; return; }
    let rect;
    try { rect = inBody.range.getBoundingClientRect(); } catch (e) { affordance.hidden = true; state.selectionDraft = null; return; }
    if (!rect || (rect.width === 0 && rect.height === 0)) { affordance.hidden = true; state.selectionDraft = null; return; }
    state.selectionDraft = selectionDraftFromRange(inBody);
    affordance.hidden = false;
    const btn = affordance.firstChild;
    const bw = btn.offsetWidth || 180;
    const bh = btn.offsetHeight || 36;
    const margin = 8;
    const maxLeft = Math.max(margin, window.innerWidth - bw - margin);
    const left = Math.min(Math.max(margin, rect.left + rect.width / 2 - bw / 2), maxLeft);
    let top = rect.bottom + 6;
    if (top + bh > window.innerHeight - margin) top = rect.top - bh - 6;
    top = Math.min(Math.max(margin, top), Math.max(margin, window.innerHeight - bh - margin));
    affordance.style.left = left + "px";
    affordance.style.top = top + "px";
  }
  function hideAffordance() {
    if (affordance) affordance.hidden = true;
    state.selectionDraft = null;
  }
  function onAffordanceClick() {
    const inBody = getSelectionInBody() || resolveSelectionDraft();
    if (!inBody) return;
    // Determine which concept the comment is about. On the graph page,
    // the detail panel shows a different concept than state.conceptId.
    var commentConcept = commentConceptForSelection(inBody);
    if (inBody.inSource) {
      state.draftAnchor = {
        kind: "source",
        ref: inBody.text.slice(0, 140),
        concept: commentConcept,
      };
    } else {
      const heading = nearestHeadingAnchor(inBody.range.startContainer);
      const localId = "local-" + (state.nextCommentSeq++);
      const mark = wrapRangeInMark(inBody.range, localId, "open");
      state.draftAnchor = {
        kind: "text",
        ref: inBody.text.slice(0, 140),
        block_id: heading.id,
        block_text: heading.text,
        section: heading.text,
        concept: commentConcept,
      };
      state._pendingMarkId = localId;
    }
    state.draftBody = "";
    openPanel("comments", { focusComposer: true });
    hideAffordance();
    try { window.getSelection().removeAllRanges(); } catch (e) {}
  }

  function selectionDraftFromRange(inBody) {
    const draft = {
      text: inBody.text,
      inSource: !!inBody.inSource,
      inGraph: !!inBody.inGraph,
      concept: commentConceptForSelection(inBody),
      block_id: "",
      block_text: "",
      range: null,
    };
    if (!inBody.inSource && !inBody.inGraph) {
      const heading = nearestHeadingAnchor(inBody.range.startContainer);
      draft.block_id = heading.id;
      draft.block_text = heading.text;
    }
    try { draft.range = inBody.range.cloneRange(); } catch (e) { draft.range = null; }
    return draft;
  }

  function selectionDraftRoot(draft) {
    if (!draft) return null;
    if (draft.inSource) return $(".okf-source");
    if (draft.inGraph) return document.getElementById("detail-body");
    return $(".okf-page__body");
  }

  function resolveSelectionDraft() {
    const draft = state.selectionDraft;
    if (!draft || !draft.text) return null;
    const root = selectionDraftRoot(draft);
    if (!root) return null;
    if (draft.range && root.contains(draft.range.commonAncestorContainer)) {
      try {
        return {
          range: draft.range.cloneRange(),
          text: draft.text,
          inSource: draft.inSource,
          inGraph: draft.inGraph,
        };
      } catch (e) { /* fall through to text re-resolution */ }
    }
    let found = null;
    if (!draft.inSource && !draft.inGraph && draft.block_id) {
      const block = root.querySelector('#' + cssEscape(draft.block_id));
      if (block) found = findTextNode(block, draft.text);
    }
    if (!found) found = findTextNode(root, draft.text);
    if (!found) return null;
    try {
      const range = document.createRange();
      range.setStart(found.node, found.start);
      range.setEnd(found.node, found.end);
      return { range, text: draft.text, inSource: draft.inSource, inGraph: draft.inGraph };
    } catch (e) { return null; }
  }

  function commentConceptForSelection(inBody) {
    var commentConcept = state.conceptId;
    if (inBody && inBody.inGraph) {
      var graphBody = document.getElementById("detail-body");
      if (graphBody) commentConcept = graphBody.getAttribute("data-concept-id") || commentConcept;
    }
    return commentConcept;
  }

  // ---- comment text-range marks (iter1 CRI-002) -----------------------
  // Wraps a Range in <mark data-comment-id class="okf-comment-mark">. Used
  // optimistically on comment creation and re-applied from persisted anchors
  // after every body patch. surroundContents fails when the range crosses
  // element boundaries, so we extract + rewrap node-by-node.
  function wrapRangeInMark(range, commentId, commentState) {
    try {
      const mark = el("mark", { class: "okf-comment-mark", "data-comment-id": commentId });
      if (commentState) mark.setAttribute("data-comment-state", commentState);
      range.surroundContents(mark);
      return mark;
    } catch (e) {
      // Range crosses element boundaries: fall back to wrapping each text
      // node segment. Collect the wrapped marks so the caller can track them.
      return wrapRangeAcrossElements(range, commentId, commentState);
    }
  }
  function wrapRangeAcrossElements(range, commentId, commentState) {
    const marks = [];
    const nodes = textNodesInRange(range);
    nodes.forEach((tn) => {
      const parent = tn.parentNode;
      if (!parent) return;
      const start = (tn === range.startContainer) ? range.startOffset : 0;
      const end = (tn === range.endContainer) ? range.endOffset : tn.nodeValue.length;
      if (start >= end) return;
      const sub = tn.nodeValue.slice(start, end);
      if (!sub) return;
      const mark = el("mark", { class: "okf-comment-mark", "data-comment-id": commentId });
      if (commentState) mark.setAttribute("data-comment-state", commentState);
      mark.appendChild(document.createTextNode(sub));
      const frag = document.createDocumentFragment();
      const before = tn.nodeValue.slice(0, start);
      const after = tn.nodeValue.slice(end);
      if (before) frag.appendChild(document.createTextNode(before));
      frag.appendChild(mark);
      if (after) frag.appendChild(document.createTextNode(after));
      parent.replaceChild(frag, tn);
      marks.push(mark);
    });
    return marks[0] || null;
  }
  function textNodesInRange(range) {
    const out = [];
    if (range.commonAncestorContainer.nodeType === Node.TEXT_NODE) {
      if (range.intersectsNode(range.commonAncestorContainer)) {
        out.push(range.commonAncestorContainer);
      }
      return out;
    }
    const walker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!range.intersectsNode(n)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let n; while ((n = walker.nextNode())) out.push(n);
    return out;
  }
  // Removes a comment's mark(s) by id, unwrapping the text back into the
  // flow. Used on failed post and to clear a stale optimistic mark.
  function removeCommentMark(commentId) {
    if (!commentId) return;
    const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(commentId) + '"]');
    marks.forEach((m) => {
      const parent = m.parentNode;
      if (!parent) return;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize(); // merge adjacent text nodes back together
    });
  }
  function renameCommentMark(oldId, newId) {
    if (!oldId || !newId || oldId === newId) return;
    const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(oldId) + '"]');
    marks.forEach((m) => m.setAttribute("data-comment-id", newId));
  }
  function setCommentMarkState(commentId, commentState) {
    if (!commentId) return;
    const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(commentId) + '"]');
    marks.forEach((m) => {
      if (commentState) m.setAttribute("data-comment-state", commentState);
      else m.removeAttribute("data-comment-state");
    });
  }
  // (Re)apply marks for every comment on the open concept whose anchor is a
  // text selection. Idempotent: skips ids that already have a live mark.
  // Marks whose ref text can no longer be found get the --stale style and
  // the comment is flagged so the card can say "anchor moved".
  function applyCommentMarks() {
    if (!isConceptPage()) return;
    const body = $(".okf-page__body");
    if (!body) return;
    const mine = state.comments.filter((c) => c.concept === state.conceptId && c.anchor && c.anchor.kind === "text" && c.anchor.ref);
    // Index existing marks so we skip re-wrapping.
    const existing = Object.create(null);
    $$(".okf-comment-mark", body).forEach((m) => {
      const id = m.getAttribute("data-comment-id");
      if (id) (existing[id] || (existing[id] = [])).push(m);
    });
    mine.forEach((c) => {
      c._stale = false;
      if (existing[c.id] && existing[c.id].length) {
        // Mark exists: just sync its state attribute.
        setCommentMarkState(c.id, c.state || "open");
        return;
      }
      // Resolve the anchor: prefer the block_id heading, else search the
      // whole body. Wrap the first match of the ref snippet.
      const block = c.anchor.block_id ? body.querySelector('#' + cssEscape(c.anchor.block_id)) : null;
      const root = block || body;
      const found = findTextNode(root, c.anchor.ref);
      if (!found) {
        // Try the full body as a last resort (block may have been renamed).
        const found2 = block ? findTextNode(body, c.anchor.ref) : null;
        if (!found2) { c._stale = true; return; }
        wrapTextNode(found2, c.id, c.state || "open");
        return;
      }
      wrapTextNode(found, c.id, c.state || "open");
    });
  }
  // A user can select text and open the comment composer before the lazy
  // /__data/doc load (or a live patch) settles. The body patch correctly
  // re-applies persisted comments via applyCommentMarks(), but a not-yet-sent
  // draft only exists as state._pendingMarkId + state.draftAnchor. Re-resolve
  // that pending local mark as well so the user-visible selection highlight
  // does not disappear underneath the open composer.
  function applyPendingDraftMark() {
    if (!isConceptPage()) return;
    const pendingId = state._pendingMarkId;
    const anchor = state.draftAnchor || {};
    if (!pendingId || anchor.kind !== "text" || !anchor.ref) return;
    if (anchor.concept && anchor.concept !== state.conceptId) return;
    const body = $(".okf-page__body");
    if (!body) return;
    const selector = '.okf-comment-mark[data-comment-id="' + cssEscape(pendingId) + '"]';
    if (body.querySelector(selector)) return;
    const block = anchor.block_id ? body.querySelector('#' + cssEscape(anchor.block_id)) : null;
    const found = (block && findTextNode(block, anchor.ref)) || findTextNode(body, anchor.ref);
    if (found) wrapTextNode(found, pendingId, "open");
  }
  function wrapTextNode(found, commentId, commentState) {
    try {
      const range = document.createRange();
      range.setStart(found.node, found.start);
      range.setEnd(found.node, found.end);
      const mark = el("mark", { class: "okf-comment-mark", "data-comment-id": commentId });
      if (commentState) mark.setAttribute("data-comment-state", commentState);
      range.surroundContents(mark);
    } catch (e) { /* selection crossed a boundary; skip this mark */ }
  }
  function jumpToCommentMark(commentId) {
    const mark = document.querySelector('.okf-comment-mark[data-comment-id="' + cssEscape(commentId) + '"]');
    if (!mark) return false;
    mark.scrollIntoView({ block: "center", behavior: REDUCED_MOTION ? "auto" : "smooth" });
    if (!REDUCED_MOTION) {
      mark.classList.remove("okf-pulse");
      void mark.offsetWidth;
      mark.classList.add("okf-pulse");
    }
    return true;
  }
  function cssEscape(s) {
    // Minimal CSS.escape polyfill (attribute selector on comment ids, which
    // are ULID/local-* strings - safe character set, but guard anyway).
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, (ch) => "\\" + ch);
  }

  // --- composer ---------------------------------------------------------
  function composerNode() {
    const wrap = el("div", { class: "okf-composer" });
    const anchorRow = el("div", { class: "okf-composer__anchor", "aria-live": "polite" });
    const anchorLabel = el("span", { text: "On: " });
    const anchorRef = el("code");
    anchorRow.appendChild(anchorLabel);
    anchorRow.appendChild(anchorRef);
    const textarea = el("textarea", { class: "okf-composer__textarea", rows: "3",
      "aria-label": "Comment for agent", placeholder: "Ask the agent to enrich, link, extract, rewrite…" });
    textarea.value = state.draftBody || "";
    textarea.addEventListener("input", () => { state.draftBody = textarea.value; });
    const actions = el("div", { class: "okf-composer__actions" });
    // iter3 CRI3-011: was "⏎ to send · Esc clears selection anchor" — a
    // bare emoji glyph that renders inconsistently across platforms
    // (Apple's return-symbol, some Windows fonts show a missing-glyph
    // box) and was the only non-SVG/Unicode-glyph icon strategy in the
    // composer. Use a <kbd> element per WCAG/UU conventions for keyboard
    // hints: accessible (semantics), consistent across platforms, and
    // conventional for kbd hints. The studio.css already styles kbd
    // (wiki.css:516 .okf-prose code applies; composer adds its own
    // .okf-composer__kbd weight).
    const hint = el("span", { class: "okf-composer__hint" });
    hint.appendChild(el("kbd", { class: "okf-composer__kbd", text: "Enter" }));
    hint.appendChild(document.createTextNode(" to send · "));
    hint.appendChild(el("kbd", { class: "okf-composer__kbd", text: "Esc" }));
    hint.appendChild(document.createTextNode(" clears selection anchor"));
    const cancel = el("button", { type: "button", class: "okf-iconbtn", text: "Cancel" });
    const submit = el("button", { type: "button", class: "okf-studiobtn", text: "Send" });
    function refreshAnchor() {
      const a = state.draftAnchor || { kind: "concept", ref: state.conceptId };
      anchorRef.textContent = a.kind === "text" ? ("“" + (a.ref || "") + "”" + (a.section ? "  § " + a.section : "")) : (a.ref || state.conceptId);
    }
    refreshAnchor();
    cancel.addEventListener("click", () => {
      state.draftAnchor = { kind: "concept", ref: state.conceptId };
      state.draftBody = "";
      textarea.value = "";
      refreshAnchor();
    });
    submit.addEventListener("click", () => postCommentFromComposer(textarea, submit, refreshAnchor));
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); postCommentFromComposer(textarea, submit, refreshAnchor); }
      else if (e.key === "Escape") { cancel.click(); }
    });
    actions.appendChild(hint); actions.appendChild(cancel); actions.appendChild(submit);
    wrap.appendChild(anchorRow); wrap.appendChild(textarea); wrap.appendChild(actions);
    wrap._focus = () => { try { textarea.focus(); } catch (e) {} };
    wrap._setAnchorConceptLevel = () => {
      state.draftAnchor = { kind: "concept", ref: state.conceptId };
      refreshAnchor();
    };
    return wrap;
  }
  async function postCommentFromComposer(textarea, submit, refreshAnchor) {
    const body = (textarea.value || "").trim();
    if (!body) { textarea.focus(); return; }
    if (!EDIT) { toast("Commenting is disabled (read-only studio).", { tone: "error" }); return; }
    const anchor = state.draftAnchor || { kind: "concept", ref: state.conceptId };
    // The anchor's concept wins over state.conceptId: on the graph page
    // state.conceptId is the literal "__graph", while onAffordanceClick
    // resolved the detail panel's real concept into draftAnchor.concept.
    // The server takes the top-level `concept` field verbatim, so posting
    // state.conceptId there would file the comment against a nonexistent
    // concept.
    const concept = anchor.concept || state.conceptId;
    var parentForPost = state.replyTo || null;
    state.replyTo = null; // clear after capturing
    // Optimistic: insert a local "posting" comment immediately. The id is
    // the same one used for the mark wrapped in onAffordanceClick, so the
    // mark and the optimistic row stay linked.
    const localId = state._pendingMarkId || ("local-" + (state.nextCommentSeq++));
    state._pendingMarkId = null;
    const optimistic = {
      id: localId, concept, anchor, body, state: "open", claimed_by: null,
      resolved_activity: [], ts: new Date().toISOString(), _posting: true,
      parent_id: parentForPost, // carry the parent so the optimistic row
                                // appears in the right thread immediately
    };
    state.comments.unshift(optimistic);
    renderCommentsPanel(); updateBadges(); rebuildMarginMarkers();
    textarea.value = ""; state.draftBody = "";
    state.draftAnchor = { kind: "concept", ref: state.conceptId };
    refreshAnchor();
    submit.disabled = true; submit.textContent = "Sending…";
    try {
      const res = await tokenFetch("/__comment", {
        method: "POST",
        body: { concept, body, anchor, actor: "user", detail: {}, parent_id: parentForPost || null },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || ("HTTP " + res.status));
      // Drop the optimistic row, then upsert the server-confirmed record by
      // id (handles the SSE race that may have inserted it already).
      const confirmed = data.comment || {};
      const confirmedId = confirmed.id || localId;
      state.comments = state.comments.filter((c) => c.id !== localId);
      upsertComment(confirmed);
      // iter1 CRI-002: rename the optimistic mark to the confirmed id so the
      // highlight survives the optimistic→confirmed transition.
      if (confirmedId !== localId) renameCommentMark(localId, confirmedId);
      renderCommentsPanel(); updateBadges(); applyCommentMarks(); rebuildMarginMarkers();
      toast("Comment posted.", { tone: "success" });
    } catch (e) {
      // Roll back the optimistic row AND the optimistic mark.
      state.comments = state.comments.filter((c) => c.id !== localId);
      removeCommentMark(localId);
      renderCommentsPanel(); updateBadges(); applyCommentMarks(); rebuildMarginMarkers();
      // iter1 CRI-009: em dash replaced with a period.
      toast("Comment failed: " + (e.message || e) + ". Your text is still in the composer.", { tone: "error", ttl: 7000 });
      textarea.value = body; state.draftBody = body;
      state.draftAnchor = anchor; refreshAnchor();
    } finally {
      submit.disabled = false; submit.textContent = "Send";
    }
  }

  // --- margin markers ---------------------------------------------------
  // iter1 CRI-002/CRI-014: markers now anchor to the commented <mark>
  // element (not the section heading), so the indicator sits beside the
  // exact text the user pointed at. Stale comments (mark not found after a
  // patch) get no marker. Resolved comments collapse to a small stub.
  function rebuildMarginMarkers() {
    const article = $("article.okf-page__main");
    if (!article) return;
    let rail = $(".okf-comment-rail", article);
    if (!EDIT || !isConceptPage()) { if (rail) rail.remove(); return; }
    if (!rail) {
      // The article needs relative positioning for the absolute rail.
      const cs = getComputedStyle(article);
      if (cs.position === "static") article.style.position = "relative";
      rail = el("div", { class: "okf-comment-rail", "aria-hidden": "true" });
      article.appendChild(rail);
    }
    rail.innerHTML = "";
    const mine = state.comments.filter((c) => c.concept === state.conceptId);
    if (!mine.length) return;
    const body = $(".okf-page__body") || article;
    const placed = []; // {top, comment} to stack overlapping markers
    mine.forEach((c) => {
      const marks = $$('.okf-comment-mark[data-comment-id="' + cssEscape(c.id) + '"]', body);
      if (!marks.length) return; // stale or not-yet-applied: no marker
      // Use the first mark's vertical position; stack if it overlaps a prior.
      const first = marks[0];
      const top = (first.offsetTop != null)
        ? first.offsetTop
        : (first.getBoundingClientRect().top - article.getBoundingClientRect().top + article.scrollTop);
      // Stack: nudge down if within 22px of a prior marker.
      let adjusted = top;
      for (let attempt = 0; attempt < 8; attempt++) {
        const clash = placed.some((p) => Math.abs(p.top - adjusted) < 22);
        if (!clash) break;
        adjusted += 20;
      }
      placed.push({ top: adjusted, comment: c });
      const isResolved = c.state === "resolved";
      // iter2 CRI2-010: the marker is a real <button> (keyboard-focusable,
      // click opens the comment) but had only a visual title=, so screen
      // readers announced an empty button. Give it an accessible name that
      // conveys what it's on + its lifecycle state. Comments in this model
      // are user-authored (the agent claims/resolves), so the actor is the
      // user; claimed_by (the agent) surfaces in the state chip + panel.
      const sel = (c.anchor && c.anchor.ref) ? ("\u201c" + c.anchor.ref + "\u201d") : ("\u201c" + c.body.slice(0, 60) + "\u201d");
      const ariaLabel = "Comment by user on " + sel + ", " + (c.state || "open");
      const marker = el("button", {
        type: "button",
        class: "okf-comment-marker" + (isResolved ? " okf-comment-marker--stub" : ""),
        "data-state": c.state || "open",
        "data-comment-id": c.id,
        "aria-label": ariaLabel,
        title: (isResolved ? "Resolved: " : "Comment on ") + sel,
        style: { top: adjusted + "px" },
      });
      if (!isResolved) marker.textContent = "•";
      marker.addEventListener("click", () => {
        const jumped = jumpToCommentMark(c.id);
        if (!jumped) openPanel("comments"); else openPanel("comments");
      });
      rail.appendChild(marker);
    });
  }

  // --- comments panel render -------------------------------------------
  //
  // Comments panel structure. The panel is split into three persistent zones that
  // live inside panelBody for the lifetime of one "open" session:
  //
  //   .okf-comment-composer-section  — composer (or read-only notice)
  //   .okf-comment-toolbar-wrap      — sort / time / archive / expand-all
  //   .okf-comment-list-wrap         — the threaded list (2-level cap)
  //
  // Each render mutates ONE zone's innerHTML at a time. The composer zone
  // is left untouched when the user is typing in it (preserveComposer
  // guard below) so a live SSE rebuild NEVER blurs the textarea or wipes
  // the in-progress draft. The toolbar + list always rebuild — their
  // state lives in state.commentView / state.comments, never in DOM. Scroll
  // position is saved + restored around the list rebuild so the user
  // isn't yanked back to the top when a new comment arrives.
  //
  // Threading: 2 levels max (root → reply → reply-to-reply). Anything
  // deeper is rendered flat at level 2 (buildCommentTree /
  // effectiveLevel). Orphans (parent_id pointing at a comment not in the
  // current set) are hoisted to roots. Cycles are guarded.
  //
  // State: view prefs (sort / timeFilter / showArchived / expanded /
  // allExpanded) live in state.commentView, persisted to localStorage under
  // "okf:commentView" so they survive a refresh.

  // localStorage key for commentView. Inlined into load/save (rather than
  // a shared var) so the state literal at the top of the IIFE can call
  // loadCommentView() at init time without depending on a var-declaration
  // hoisting order: a top-level `var KEY = "..."` would still be undefined
  // when the state object literal runs.
  var COMMENT_VIEW_KEY = "okf:commentView";
  function loadCommentView() {
    var defaults = {
      sort: "newest",        // newest | status | updated
      timeFilter: "all",     // today | 7days | all
      showArchived: false,
      expanded: {},          // { commentId: true/false }
      allExpanded: true,     // global default for threads w/o an override
    };
    try {
      var raw = localStorage.getItem("okf:commentView");
      if (!raw) return Object.assign({}, defaults);
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return Object.assign({}, defaults);
      return Object.assign({}, defaults, parsed, {
        expanded: Object.assign({}, parsed.expanded || {}),
      });
    } catch (e) {
      return Object.assign({}, defaults);
    }
  }
  function saveCommentView() {
    try {
      localStorage.setItem("okf:commentView", JSON.stringify(state.commentView));
    } catch (e) { /* localStorage may be unavailable (private mode, quota); non-fatal */ }
  }

  // Status rank for "sort by status" — open first, then claimed, then
  // resolved, then dismissed, then archived. Anything unknown sorts last.
  // NOTE: use nullish check, not `|| 5` — open's rank IS 0 (falsy), so
  // `0 || 5` would incorrectly fall through to 5 and bury open comments
  // at the bottom of a status sort.
  function commentStatusRank(s) {
    // Archived is no longer a state value (separate track).
    var v = ({ open: 0, claimed: 1, resolved: 2, dismissed: 3 })[s];
    return v === undefined ? 4 : v;
  }
  function sortRoots(list, mode) {
    var arr = list.slice();
    if (mode === "status") {
      arr.sort(function (a, b) {
        var ra = commentStatusRank(a.state), rb = commentStatusRank(b.state);
        if (ra !== rb) return ra - rb;
        return byTsDesc(a, b);
      });
    } else {
      // "newest" and "updated" both fall back to ts-desc. The directives
      // feed is append-only with last-write-wins, so the visible ts on a
      // root already reflects its latest activity (state transition OR new
      // reply post, both of which append a record). The two sorts are
      // distinct menu options because future schema work may split
      // created_ts from updated_ts; the client code is identical today.
      arr.sort(byTsDesc);
    }
    return arr;
  }
  // Epoch-ms within the time-filter window. today = last 24h, 7days = last
  // 7d. Sliding window in UTC epoch ms — no DST / timezone sensitivity
  // (the cutoff is "now - window", independent of the user's zone).
  function commentInTimeWindow(c, mode) {
    if (!mode || mode === "all") return true;
    if (!c.ts) return false;
    var t = Date.parse(c.ts);
    if (isNaN(t)) return false;
    var cutoff = Date.now() - (mode === "today" ? 24 * 3600 * 1000 : 7 * 24 * 3600 * 1000);
    return t >= cutoff;
  }

  // Build the 2-level-capped thread tree. Returns:
  //   roots              — array of effective-level-0 comments (sorted ts-desc)
  //   directChildren(id) — level-1 replies (children of a root)
  //   level2Of(id)       — flat list of all transitive descendants of a
  //                        level-1 reply, rendered at level 2
  //   byId               — id → comment lookup
  function buildCommentTree(comments) {
    var byId = {};
    comments.forEach(function (c) { if (c && c.id) byId[c.id] = c; });
    var byParent = {};
    comments.forEach(function (c) {
      if (!c || !c.parent_id) return;
      if (!byParent[c.parent_id]) byParent[c.parent_id] = [];
      byParent[c.parent_id].push(c);
    });
    function effectiveLevel(comment) {
      var lvl = 0;
      var guard = {};
      var cur = comment;
      // Walk parent chain until we hit a root, a missing parent, or a
      // cycle. Cap at 2 (rendered depth); guard set defends against
      // cycles in malformed data.
      while (cur && cur.parent_id && byId[cur.parent_id]) {
        if (guard[cur.id]) break;
        guard[cur.id] = true;
        lvl++;
        cur = byId[cur.parent_id];
        if (lvl >= 2) break; // 2-level cap; deeper renders as level 2
      }
      return lvl;
    }
    var roots = comments.filter(function (c) { return effectiveLevel(c) === 0; });
    roots = roots.slice().sort(byTsDesc);
    function directChildren(id) {
      return (byParent[id] || []).slice().sort(byTsDesc);
    }
    // Flat BFS over all descendants of a level-1 reply. The `seen` set
    // guards against cycles; everything collected renders at level 2.
    function level2Of(id) {
      var out = [];
      var stack = (byParent[id] || []).slice();
      var seen = {};
      while (stack.length) {
        var c = stack.shift();
        if (!c || seen[c.id]) continue;
        seen[c.id] = true;
        out.push(c);
        var kids = byParent[c.id] || [];
        for (var i = 0; i < kids.length; i++) stack.push(kids[i]);
      }
      return out.sort(byTsDesc);
    }
    // All transitive descendants of a root (for the time-filter "any
    // activity in this thread" rule).
    function threadHasRecentActivity(root, mode) {
      if (commentInTimeWindow(root, mode)) return true;
      var stack = (byParent[root.id] || []).slice();
      var seen = {};
      while (stack.length) {
        var c = stack.shift();
        if (!c || seen[c.id]) continue;
        seen[c.id] = true;
        if (commentInTimeWindow(c, mode)) return true;
        var kids = byParent[c.id] || [];
        for (var i = 0; i < kids.length; i++) stack.push(kids[i]);
      }
      return false;
    }
    return {
      roots: roots, byId: byId, byParent: byParent,
      directChildren: directChildren, level2Of: level2Of,
      threadHasRecentActivity: threadHasRecentActivity,
    };
  }

  // Expand/collapse. Per-comment overrides live in commentView.expanded
  // and take precedence over the global allExpanded default. Toggling a
  // single thread writes only that one id; "expand/collapse all" resets
  // the per-comment overrides and flips the global default.
  function isCommentExpanded(commentId) {
    if (Object.prototype.hasOwnProperty.call(state.commentView.expanded, commentId)) {
      return !!state.commentView.expanded[commentId];
    }
    return !!state.commentView.allExpanded;
  }
  function setCommentExpanded(commentId, expanded) {
    state.commentView.expanded[commentId] = !!expanded;
    saveCommentView();
  }
  function toggleCommentExpand(commentId) {
    setCommentExpanded(commentId, !isCommentExpanded(commentId));
    renderCommentsPanel();
  }
  function toggleAllExpand() {
    state.commentView.allExpanded = !state.commentView.allExpanded;
    state.commentView.expanded = {};
    saveCommentView();
    renderCommentsPanel();
  }

  function renderCommentsPanel() {
    if (state.openPanel !== "comments") return;
    var body = panelBodyEl();
    if (!body) return;

    // Resolve or create the three persistent zones. On the first render
    // of an open session they don't exist yet; create + append them. On
    // subsequent renders (SSE, user toggles, etc.) they persist, so the
    // composer textarea node survives across rebuilds.
    var composerZone = body.querySelector(".okf-comment-composer-section");
    var toolbarZone = body.querySelector(".okf-comment-toolbar-wrap");
    var listZone = body.querySelector(".okf-comment-list-wrap");
    var initial = !(composerZone && toolbarZone && listZone);
    if (initial) {
      body.innerHTML = "";
      composerZone = el("div", { class: "okf-panel__section okf-comment-composer-section" });
      toolbarZone = el("div", { class: "okf-comment-toolbar-wrap" });
      listZone = el("div", { class: "okf-comment-list-wrap" });
      body.appendChild(composerZone);
      body.appendChild(toolbarZone);
      body.appendChild(listZone);
    }

    // Preserve the composer zone if the user is interacting with it. The
    // textarea is never removed from the DOM during a typing session, so
    // focus + selection + draft are all retained byte-for-byte.
    var ta = composerZone.querySelector(".okf-composer__textarea");
    var active = document.activeElement;
    var preserveComposer = !initial && ta &&
      (ta === active || (ta.value && ta.value.trim().length > 0));
    if (!preserveComposer) {
      var composerScroll = body.scrollTop;
      composerZone.innerHTML = "";
      if (EDIT) {
        appendComposerContents(composerZone, body);
      } else {
        composerZone.appendChild(el("p", {
          class: "okf-empty",
          text: "Commenting is disabled (read-only studio).",
        }));
      }
      body.scrollTop = composerScroll;
    }

    // Rebuild the toolbar + list, EXCEPT when the user is mid-
    // reply in an inline composer. The inline composer lives inside
    // listZone; rebuilding would destroy its textarea, losing the draft
    // and focus. Skip the whole list rebuild in that case — the next
    // SSE update (after the user submits or cancels) will catch up.
    // The toolbar zone has no user-input elements, so it's safe to
    // always rebuild.
    var listScroll = body.scrollTop;
    toolbarZone.innerHTML = "";
    toolbarZone.appendChild(buildCommentToolbar());
    var inlineBusy = _inlineReplyBusy();
    if (!inlineBusy) {
      listZone.innerHTML = "";
      listZone.appendChild(buildCommentList());
    }
    body.scrollTop = listScroll;
  }

  // Returns true if there's an inline reply composer (the per-card
  // textarea created by buildCommentActions' Reply button) that is
  // currently focused OR contains non-empty draft text. Used to skip the
  // comment-list rebuild during renderCommentsPanel so a live update can
  // never destroy a draft the user is actively typing.
  //
  // Mirrors the top-level composer guard at line ~1440. Both guards exist
  // because the two composers live in different DOM zones (composerZone
  // vs listZone) with different rebuild semantics.
  function _inlineReplyBusy() {
    var inline = document.querySelector(".okf-inline-reply textarea");
    if (!inline) return false;
    var active = document.activeElement;
    if (inline === active) return true;
    var v = (inline.value || "").trim();
    return v.length > 0;
  }

  // Build the composer block (h3 + reply context + composerNode). Honors
  // the openPanel({focusComposer:true}) flag exactly once — the flag is
  // consumed on first build so an SSE rebuild mid-typing can't steal focus.
  function appendComposerContents(zone, body) {
    var replyContext = "";
    if (state.replyTo) {
      var parent = state.comments.find(function (c) { return c.id === state.replyTo; });
      if (parent) replyContext = "Replying to: " + (parent.body || "").slice(0, 60);
    }
    var composer = composerNode();
    if (replyContext) {
      var ctx = el("div", { class: "okf-composer__reply-context", text: replyContext });
      composer.insertBefore(ctx, composer.firstChild);
    }
    zone.appendChild(el("h3", {
      class: "okf-panel__section-title",
      text: replyContext ? "Reply" : "Ask the agent",
    }));
    zone.appendChild(composer);
    if (composer._focus && body && body._focusComposer) {
      body._focusComposer = false; // consume: only the openPanel opener focuses
      setTimeout(function () { try { composer._focus(); } catch (e) {} }, 30);
    }
  }

  // Toolbar: sort + time-filter selects on the left, "show archived" +
  // "expand/collapse all" toggles on the right. Each control writes back
  // to state.commentView, persists, and re-renders.
  function buildCommentToolbar() {
    var tb = el("div", {
      class: "okf-comment-toolbar",
      role: "region",
      "aria-label": "Comment view controls",
    });
    var g1 = el("div", { class: "okf-comment-toolbar__group" });
    var sortSel = el("select", {
      class: "okf-comment-toolbar__select",
      "aria-label": "Sort comments",
      title: "Sort order",
    });
    [["newest", "Newest"], ["status", "Status"], ["updated", "Updated"]].forEach(function (opt) {
      var o = el("option", { value: opt[0], text: opt[1] });
      if (state.commentView.sort === opt[0]) o.selected = true;
      sortSel.appendChild(o);
    });
    sortSel.addEventListener("change", function () {
      state.commentView.sort = sortSel.value;
      saveCommentView();
      renderCommentsPanel();
    });
    g1.appendChild(sortSel);

    var timeSel = el("select", {
      class: "okf-comment-toolbar__select",
      "aria-label": "Filter comments by time",
      title: "Time window",
    });
    [["all", "All time"], ["today", "Today"], ["7days", "Last 7 days"]].forEach(function (opt) {
      var o = el("option", { value: opt[0], text: opt[1] });
      if (state.commentView.timeFilter === opt[0]) o.selected = true;
      timeSel.appendChild(o);
    });
    timeSel.addEventListener("change", function () {
      state.commentView.timeFilter = timeSel.value;
      saveCommentView();
      renderCommentsPanel();
    });
    g1.appendChild(timeSel);
    tb.appendChild(g1);

    var g2 = el("div", { class: "okf-comment-toolbar__group" });
    var archBtn = el("button", {
      type: "button",
      class: "okf-studiobtn okf-comment-toolbar__toggle",
      "aria-pressed": state.commentView.showArchived ? "true" : "false",
      text: state.commentView.showArchived ? "Hide archived" : "Show archived",
    });
    archBtn.addEventListener("click", function () {
      state.commentView.showArchived = !state.commentView.showArchived;
      saveCommentView();
      renderCommentsPanel();
    });
    g2.appendChild(archBtn);

    var allOpen = state.commentView.allExpanded;
    var expBtn = el("button", {
      type: "button",
      class: "okf-studiobtn okf-comment-toolbar__toggle",
      text: allOpen ? "Collapse all" : "Expand all",
      title: allOpen ? "Collapse every thread" : "Expand every thread",
    });
    expBtn.addEventListener("click", toggleAllExpand);
    g2.appendChild(expBtn);
    tb.appendChild(g2);

    return tb;
  }

  function buildCommentList() {
    var wrap = el("div", { class: "okf-comment-list" });
    var tree = buildCommentTree(state.comments);

    // Apply archive + time filters at the ROOT level. Replies inherit
    // visibility from their root (collapsing a thread hides everything;
    // expanding shows all descendants regardless of their own ts).
    var visibleRoots = tree.roots.filter(function (root) {
      if (root.archived && !state.commentView.showArchived) return false;
      if (!tree.threadHasRecentActivity(root, state.commentView.timeFilter)) return false;
      return true;
    });
    var sorted = sortRoots(visibleRoots, state.commentView.sort);

    if (sorted.length === 0) {
      wrap.appendChild(el("p", { class: "okf-empty", text: commentListEmptyMessage() }));
      return wrap;
    }
    sorted.forEach(function (root) {
      wrap.appendChild(renderRootCard(root, tree));
    });
    return wrap;
  }

  function commentListEmptyMessage() {
    if (!state.comments || state.comments.length === 0) {
      return "No comments yet. Select text and click Comment, or use the composer above.";
    }
    var hasArchived = state.comments.some(function (c) { return !!c.archived; });
    if (!state.commentView.showArchived && hasArchived &&
        state.comments.every(function (c) { return !!c.archived; })) {
      return "All comments are archived. Toggle “Show archived” to see them.";
    }
    if (state.commentView.timeFilter !== "all") {
      return "No comments in this time window. Try a wider filter.";
    }
    return "No comments match the current filter.";
  }

  function renderRootCard(root, tree) {
    var expanded = isCommentExpanded(root.id);
    // Gather all replies for the collapsed preview + thread-resolved check.
    var allReplies = [];
    tree.directChildren(root.id).forEach(function (l1) {
      allReplies.push(l1);
      tree.level2Of(l1.id).forEach(function (l2) { allReplies.push(l2); });
    });
    var threadResolved = root.state === "resolved" &&
      allReplies.every(function (r) { return r.state === "resolved"; });
    var card = commentCard(root, 0, {
      expanded: expanded,
      replies: allReplies,
      isRoot: true,
      threadResolved: threadResolved,
    });
    if (!expanded) return card;
    var l1 = tree.directChildren(root.id);
    if (!l1.length) return card;
    var children = el("div", { class: "okf-comment__children" });
    l1.forEach(function (reply) {
      children.appendChild(commentCard(reply, 1, { isRoot: false }));
      var l2 = tree.level2Of(reply.id);
      if (l2.length) {
        var inner = el("div", { class: "okf-comment__children" });
        l2.forEach(function (r2) { inner.appendChild(commentCard(r2, 2, { isRoot: false })); });
        children.appendChild(inner);
      }
    });
    card.appendChild(children);
    return card;
  }

  // level: 0 (root), 1 (reply), 2 (reply-to-reply, or anything deeper that
  // has been capped to level 2 by buildCommentTree). opts.expanded only
  // applies to roots; replies never collapse independently.
  function commentCard(c, level, opts) {
    level = level || 0;
    opts = opts || {};
    var expanded = Object.prototype.hasOwnProperty.call(opts, "expanded")
      ? !!opts.expanded
      : true;
    var isRoot = level === 0;
    var collapsed = isRoot && !expanded;

    var cls = "okf-comment okf-comment--level-" + level;
    if (level > 0) cls += " okf-comment--reply"; // compatibility hook for extensions
    if (collapsed) cls += " okf-comment--collapsed";

    var card = el("div", {
      class: cls,
      id: "comment-" + (c.id || ""),
      dataset: { state: c.state || "open", level: String(level) },
    });

    // Header row: chevron (root only) + state chip + anchor + meta.
    var header = el("div", { class: "okf-comment__header" });
    if (isRoot) {
      var chev = el("button", {
        type: "button",
        class: "okf-comment__chevron",
        "aria-expanded": expanded ? "true" : "false",
        "aria-controls": "comment-" + (c.id || ""),
        "aria-label": (expanded ? "Collapse" : "Expand") + " thread" +
          (c.body ? ": " + c.body.slice(0, 60) : ""),
        title: expanded ? "Collapse thread" : "Expand thread",
      });
      chev.addEventListener("click", function () { toggleCommentExpand(c.id); });
      header.appendChild(chev);
    }
    header.appendChild(el("span", { class: "okf-comment__state", text: c.state || "open" }));
    if (c.archived) {
      header.appendChild(el("span", { class: "okf-comment__archived-chip", text: "archived" }));
    }
    appendAnchorChip(header, c);
    // Show created-at + updated-at timestamps. Server stamps updated_at on
    // every transition (claim/resolve/dismiss/reopen/archive/unarchive/
    // reply). If they differ, show both; otherwise show just created.
    var createdTs = c.ts || "";
    var updatedTs = c.updated_at || c.ts || "";
    if (createdTs && updatedTs && createdTs !== updatedTs) {
      header.appendChild(el("span", { class: "okf-comment__ts", text: "created " + fmtTime(createdTs) }));
      header.appendChild(el("span", { class: "okf-comment__ts okf-comment__ts--updated", text: "updated " + fmtTime(updatedTs) }));
    } else {
      header.appendChild(el("span", { class: "okf-comment__ts", text: fmtTime(createdTs) }));
    }
    if (c._posting) header.appendChild(el("span", { class: "okf-comment__posting", text: "posting…" }));
    card.appendChild(header);

    if (collapsed) {
      // Collapsed preview: show short body for EVERY comment in the thread
      // (root + all replies), not just the root. Each preview is a one-line
      // ellipsis with the actor prefix.
      //
      // Two summary fields, shown in priority order:
      //   - ``summary`` (resolve-time "what the agent did") — explicit,
      //     takes precedence when set (typically on agent replies).
      //   - ``request_summary`` (claim-time "what was asked" OR auto-
      //     derived from body) — always present, used for user comments
      //     and as a fallback.
      // The preview reads as a status board: each agent reply shows its
      // "done" tag; each user comment shows its "ask" tag.
      var allInThread = [c];
      if (opts.replies) {
        opts.replies.forEach(function (r) { allInThread.push(r); });
      }
      allInThread.forEach(function (tc) {
        var bodyText = (tc.body || "").trim();
        var doneSummary = (tc.summary || "").trim();
        var askSummary = (tc.request_summary || "").trim();
        var who = tc.actor === "agent" ? "Agent" : "You";
        var line = el("div", { class: "okf-comment__preview" });
        line.appendChild(el("span", { class: "okf-comment__preview-who", text: who + ": " }));
        if (doneSummary) {
          // Agent's "done" tag — accent + italic.
          var dSlice = doneSummary.length > 100 ? doneSummary.slice(0, 100) + "\u2026" : doneSummary;
          line.appendChild(el("span", { class: "okf-comment__preview-summary", text: dSlice }));
        } else if (askSummary) {
          // Auto-derived or claim-set "ask" tag — muted + italic.
          var aSlice = askSummary.length > 100 ? askSummary.slice(0, 100) + "\u2026" : askSummary;
          line.appendChild(el("span", { class: "okf-comment__preview-ask", text: aSlice }));
        } else {
          var previewText = bodyText.length > 100 ? bodyText.slice(0, 100) + "\u2026" : bodyText;
          line.appendChild(document.createTextNode(previewText));
        }
        card.appendChild(line);
      });
      return card;
    }

    // Expanded body + ask/done summaries + reply + resolved details + actions.
    card.appendChild(el("div", { class: "okf-comment__body", text: c.body || "" }));
    // Two summary chips side by side.
    //   - request_summary: short of what was ASKED (auto-derived from body
    //     or set explicitly via claim --summary). Labelled "asked".
    //   - summary: short of what the agent DID (set via resolve --summary).
    //     Labelled "done".
    // Both are shown above the longform reply so the card reads as
    // ask → done → detail at a glance.
    if (c.request_summary) {
      var askEl = el("div", { class: "okf-comment__summary okf-comment__summary--ask" });
      askEl.appendChild(el("span", { class: "okf-comment__summary-label", text: "asked: " }));
      askEl.appendChild(document.createTextNode(c.request_summary));
      card.appendChild(askEl);
    }
    if (c.summary) {
      var doneEl = el("div", { class: "okf-comment__summary okf-comment__summary--done" });
      doneEl.appendChild(el("span", { class: "okf-comment__summary-label", text: "done: " }));
      doneEl.appendChild(document.createTextNode(c.summary));
      card.appendChild(doneEl);
    }
    if (c.reply) {
      var rep = el("div", { class: "okf-comment__reply" });
      rep.appendChild(el("strong", { text: "Agent: " }));
      rep.appendChild(document.createTextNode(c.reply));
      card.appendChild(rep);
    }
    if (c.resolved_activity && c.resolved_activity.length) {
      var det = el("details");
      det.appendChild(el("summary", {
        text: "Resolved by " + c.resolved_activity.length + " change(s). Jump",
      }));
      c.resolved_activity.forEach(function (aid) {
        var a = el("a", { href: "#", text: "change #" + shortId(String(aid)) });
        a.addEventListener("click", function (e) {
          e.preventDefault();
          jumpToActivity(String(aid));
        });
        det.appendChild(el("div", {}, [a]));
      });
      card.appendChild(det);
    }

    // Action row. Hidden in read-only studios — the writes would 403 and
    // presenting dead verbs is worse than hiding them.
    if (EDIT) {
      card.appendChild(buildCommentActions(c, {
        isRoot: opts.isRoot,
        threadResolved: opts.threadResolved,
      }));
    }
    return card;
  }

  function appendAnchorChip(header, c) {
    if (c.anchor && c.anchor.kind === "text" && c.anchor.ref) {
      var jump = el("button", {
        type: "button",
        class: "okf-comment__anchor",
        title: "Jump to the commented text",
        text: "“" + c.anchor.ref + "”",
      });
      jump.addEventListener("click", function () {
        var ok = jumpToCommentMark(c.id);
        if (!ok) toast("The commented text was edited or removed.", { tone: "info", ttl: 4000 });
      });
      header.appendChild(jump);
      if (c._stale) header.appendChild(el("span", { class: "okf-comment__posting", text: "anchor moved" }));
    } else if (c.concept) {
      var a = el("a", { href: "/" + c.concept });
      a.textContent = c.concept;
      header.appendChild(document.createTextNode("on "));
      header.appendChild(a);
    }
  }

  function buildCommentActions(c, opts) {
    opts = opts || {};
    var isRoot = !!opts.isRoot;
    var threadResolved = !!opts.threadResolved;
    var actions = el("div", { class: "okf-comment__actions" });
    var replyBtn = el("button", { type: "button", class: "okf-comment__action", text: "Reply" });
    replyBtn.addEventListener("click", function () {
      // Insert an inline reply composer directly below this comment card.
      // Remove any existing inline composer first (one at a time).
      var existing = document.querySelector(".okf-inline-reply");
      if (existing) existing.remove();
      var card = document.getElementById("comment-" + c.id);
      if (!card) return;
      var replyWrap = el("div", { class: "okf-inline-reply" });
      // Context indicator.
      replyWrap.appendChild(el("div", {
        class: "okf-composer__reply-context",
        text: "Replying to: " + (c.body || "").slice(0, 80),
      }));
      // Inline textarea.
      var ta = el("textarea", {
        class: "okf-composer__textarea okf-inline-reply__textarea",
        rows: "2",
        placeholder: "Reply…",
        "aria-label": "Reply to comment",
      });
      replyWrap.appendChild(ta);
      // Action row.
      var row = el("div", { class: "okf-composer__actions" });
      var submit = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Reply" });
      var cancel = el("button", { type: "button", class: "okf-studiobtn", text: "Cancel" });
      cancel.addEventListener("click", function () { replyWrap.remove(); });
      submit.addEventListener("click", function () {
        var body = (ta.value || "").trim();
        if (!body) { ta.focus(); return; }
        submit.disabled = true; submit.textContent = "Sending…";
        tokenFetch("/__comment", {
          method: "POST",
          body: {
            concept: c.concept || state.conceptId,
            body: body,
            anchor: { kind: "concept", ref: c.concept || state.conceptId },
            actor: "user",
            detail: {},
            parent_id: c.id,
          },
        }).then(function (res) { return res.json(); }).then(function (data) {
          if (!data.ok) throw new Error(data.error || "failed");
          upsertComment(data.comment);
          // Server auto-unarchives parent on reply; refresh the parent too.
          if (c.parent_id) {
            var p = state.comments.find(function (x) { return x.id === c.parent_id; });
            if (p && p.archived) p.archived = false;
          }
          replyWrap.remove();
          renderCommentsPanel();
          toast("Reply posted.", { tone: "success" });
        }).catch(function (e) {
          submit.disabled = false; submit.textContent = "Reply";
          toast("Reply failed: " + e.message, { tone: "error" });
        });
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit.click(); }
        if (e.key === "Escape") { replyWrap.remove(); }
      });
      row.appendChild(submit);
      row.appendChild(cancel);
      replyWrap.appendChild(row);
      // Insert AFTER the card's children container (if any) so the reply
      // box appears at the bottom of the thread, right where the Reply
      // button is. Fall back to after the card itself.
      var childrenEl = card.querySelector(":scope > .okf-comment__children");
      if (childrenEl) {
        childrenEl.appendChild(replyWrap);
      } else {
        card.appendChild(replyWrap);
      }
      setTimeout(function () { ta.focus(); }, 30);
    });
    actions.appendChild(replyBtn);

    // Edit: amend the body of a not-yet-resolved comment in place (the
    // enter-too-soon fix). Shown on user-authored comments only — agent
    // comments/replies are the agent's record, not the user's to rewrite.
    // The server rejects edits on resolved/dismissed comments (409).
    if (c.actor !== "agent" && c.state !== "resolved" && c.state !== "dismissed") {
      var editBtn = el("button", { type: "button", class: "okf-comment__action", text: "Edit" });
      editBtn.addEventListener("click", function () {
        var existing = document.querySelector(".okf-inline-reply");
        if (existing) existing.remove();
        var card = document.getElementById("comment-" + c.id);
        if (!card) return;
        var editWrap = el("div", { class: "okf-inline-reply okf-inline-edit" });
        editWrap.appendChild(el("div", {
          class: "okf-composer__reply-context",
          text: "Editing comment",
        }));
        var ta = el("textarea", {
          class: "okf-composer__textarea okf-inline-reply__textarea",
          rows: "3",
          "aria-label": "Edit comment",
        });
        ta.value = c.body || "";
        editWrap.appendChild(ta);
        var row = el("div", { class: "okf-composer__actions" });
        var save = el("button", { type: "button", class: "okf-studiobtn okf-studiobtn--primary", text: "Save" });
        var cancelEdit = el("button", { type: "button", class: "okf-studiobtn", text: "Cancel" });
        cancelEdit.addEventListener("click", function () { editWrap.remove(); });
        save.addEventListener("click", function () {
          var body = (ta.value || "").trim();
          if (!body) { ta.focus(); return; }
          if (body === (c.body || "").trim()) { editWrap.remove(); return; }
          save.disabled = true; save.textContent = "Saving…";
          tokenFetch("/__comment-update", {
            method: "POST",
            body: { id: c.id, body: body },
          }).then(function (res) { return res.json(); }).then(function (data) {
            if (!data.ok) throw new Error(data.error || "failed");
            upsertComment(data.comment);
            editWrap.remove();
            renderCommentsPanel();
            toast("Comment updated.", { tone: "success" });
          }).catch(function (e) {
            save.disabled = false; save.textContent = "Save";
            toast("Edit failed: " + e.message, { tone: "error" });
          });
        });
        ta.addEventListener("keydown", function (e) {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save.click(); }
          if (e.key === "Escape") { editWrap.remove(); }
        });
        row.appendChild(save);
        row.appendChild(cancelEdit);
        editWrap.appendChild(row);
        card.appendChild(editWrap);
        setTimeout(function () { ta.focus(); }, 30);
      });
      actions.appendChild(editBtn);
    }

    // Lifecycle verbs (Cancel / Reopen). These DO NOT touch the archive
    // flag — archive is a separate track.
    if (c.state === "open" && !c.claimed_by) {
      var cancelBtn = el("button", { type: "button", class: "okf-comment__action", text: "Cancel" });
      cancelBtn.addEventListener("click", function () { updateCommentState(c.id, "dismissed"); });
      actions.appendChild(cancelBtn);
    }
    if (c.state === "dismissed") {
      var reopenBtn = el("button", { type: "button", class: "okf-comment__action", text: "Reopen" });
      reopenBtn.addEventListener("click", function () { updateCommentState(c.id, "open"); });
      actions.appendChild(reopenBtn);
    }

    // Archive / Unarchive: ONLY on the root comment. Archive requires
    // every comment in the thread to be resolved (server-enforced, but
    // we also disable the button client-side to give a clear affordance).
    // Unarchive is always available on an archived root.
    if (isRoot) {
      if (c.archived) {
        var unarchBtn = el("button", {
          type: "button",
          class: "okf-comment__action",
          text: "Unarchive",
        });
        unarchBtn.addEventListener("click", function () { setCommentArchived(c.id, false); });
        actions.appendChild(unarchBtn);
      } else if (threadResolved) {
        var archBtn2 = el("button", {
          type: "button",
          class: "okf-comment__action",
          text: "Archive thread",
          title: "Archive this thread. Requires all comments resolved.",
        });
        archBtn2.addEventListener("click", function () { setCommentArchived(c.id, true); });
        actions.appendChild(archBtn2);
      } else {
        // Disabled affordance so the user can see what's missing.
        var archDisabled = el("button", {
          type: "button",
          class: "okf-comment__action okf-comment__action--disabled",
          text: "Archive thread",
          disabled: true,
          title: "Archive is available once every comment in the thread is resolved.",
        });
        actions.appendChild(archDisabled);
      }
    }
    return actions;
  }

  async function updateCommentState(commentId, newState) {
    try {
      await tokenFetch("/__comment-update", {
        method: "POST",
        body: { id: commentId, state: newState },
      });
      var c = state.comments.find(function (x) { return x.id === commentId; });
      if (c) {
        c.state = newState;
        // Stamp a fresh updated_at so "newest"/"updated" sorts reorder
        // correctly before the SSE echo arrives.
        c.updated_at = new Date().toISOString();
        renderCommentsPanel();
      }
    } catch (e) {
      toast("Failed to update comment.", { tone: "error" });
    }
  }

  // Archive is a SEPARATE track from lifecycle state. Setting
  // archived=true/false does NOT touch open/resolved/dismissed. The server
  // enforces "archive only on root + only when whole thread is resolved";
  // we surface the rejection reason in the toast.
  async function setCommentArchived(commentId, archived) {
    try {
      var res = await tokenFetch("/__comment-update", {
        method: "POST",
        body: { id: commentId, archived: archived },
      });
      var data = await res.json();
      if (!data.ok) {
        var msg = data.error || "failed";
        if (data.unresolved_ids && data.unresolved_ids.length) {
          msg = "Resolve every comment in the thread first (" +
            data.unresolved_ids.length + " unresolved).";
        }
        toast(msg, { tone: "error", ttl: 6000 });
        return;
      }
      var c = state.comments.find(function (x) { return x.id === commentId; });
      if (c) {
        c.archived = archived;
        c.updated_at = new Date().toISOString();
        renderCommentsPanel();
        toast(archived ? "Thread archived." : "Thread unarchived.",
          { tone: "info" });
      }
    } catch (e) {
      toast("Failed to update archive.", { tone: "error" });
    }
  }

  // ====================================================================
  // 6. Presence (current spec §12)
  // ====================================================================
  function renderPresence(p) {
    state.presence = p || { state: "idle" };
    const st = p && p.state || "idle";
    // iter2 G13: record presence transitions for the Agent-activity panel's
    // presence-history section (unique content the Changes panel doesn't
    // show). Dedup consecutive identical (state, focus) so a noisy feed
    // doesn't flood the log; cap at 24 entries.
    const focus = (p && p.focus) || "";
    // The free-text progress line ("linking 3 of 7 tables…") is part of the
    // dedup key so mid-pass message updates land in the presence history —
    // that history IS the progress log for a long multi-concept pass.
    const message = (p && p.message) || "";
    const last = state.presenceHistory[0];
    if (!last || last.state !== st || last.focus !== focus || (last.message || "") !== message) {
      state.presenceHistory.unshift({ state: st, focus: focus, message: message, ts: new Date().toISOString() });
      if (state.presenceHistory.length > 24) state.presenceHistory.length = 24;
    }
    presenceChip.dataset.state = st;
    presenceChip.setAttribute("aria-label", "Agent presence: " + st
      + (p && p.focus ? " " + p.focus : "")
      + (message ? " — " + message : ""));
    presenceLabel.innerHTML = "";
    presenceLabel.appendChild(el("span", { class: "okf-presence__actor", text: "Agent" }));
    const verb = ({ idle: "idle", watching: "watching", thinking: "thinking about", editing: "editing" })[st] || st;
    presenceLabel.appendChild(document.createTextNode(" " + verb));
    if (p && p.focus) {
      presenceLabel.appendChild(document.createTextNode(" "));
      const a = el("a", { href: "/" + p.focus });
      a.textContent = p.focus;
      presenceLabel.appendChild(a);
    }
    if (message) {
      presenceLabel.appendChild(el("span", {
        class: "okf-presence__message",
        text: " — " + message,
        title: message,
      }));
    }
    // iter2 G11: keep the "Agent watching" toggle in sync with the agent's
    // live presence. Only "watching" reads as on; every other state (idle,
    // thinking, editing) reads as off. This is the canonical source — the
    // click handler's optimistic toggle is reverted here if the POST failed.
    // iter3 CRI3-010: also keep the concise aria-label in sync with state
    // so the screen-reader name reflects the live value (not just the
    // initial "currently off").
    if (watchingToggle) {
      const watchingOn = st === "watching";
      watchingToggle.setAttribute("aria-pressed", watchingOn ? "true" : "false");
      watchingToggle.setAttribute("aria-label",
        "Agent watching, currently " + (watchingOn ? "on" : "off"));
    }
    highlightFocusedConcept(p && p.focus);
  }
  function highlightFocusedConcept(focus) {
    stampConceptIds();
    // iter1 CRI-004: clear every presence highlight (list rows + article),
    // then re-apply to the matching row if any. Idle (no focus) leaves the
    // workspace clean so a stale "agent here" cue never lingers.
    $$(".okf-presence-focus").forEach((n) => n.classList.remove("okf-presence-focus"));
    $$(".okf-focus-highlight").forEach((n) => n.classList.remove("okf-focus-highlight"));
    state._lastFocus = focus || null;
    if (!focus) return;
    // On the open concept page, outline the article.
    if (focus === state.conceptId) {
      const article = $("article.okf-page__main");
      if (article) article.classList.add("okf-focus-highlight");
    }
    // On index/search pages, highlight the matching row by data-concept-id.
    const row = document.querySelector('[data-concept-id="' + cssEscape(focus) + '"]');
    if (row) row.classList.add("okf-presence-focus");
  }
  // iter1 CRI-004: stamp data-concept-id on list/search/sidebar rows so the
  // presence focus highlight can resolve against them. Done lazily once per
  // page (rows are server-rendered; a patch would re-trigger via boot).
  let conceptIdsStamped = false;
  function stampConceptIds() {
    if (conceptIdsStamped) return;
    conceptIdsStamped = true;
    $$(".okf-concept-list li").forEach((li) => {
      if (li.getAttribute("data-concept-id")) return;
      const a = li.querySelector("a[href]");
      const id = hrefToConceptId(a && a.getAttribute("href"));
      if (id) li.setAttribute("data-concept-id", id);
    });
    $$(".okf-search-result").forEach((art) => {
      if (art.getAttribute("data-concept-id")) return;
      const a = art.querySelector("a[href]");
      const id = hrefToConceptId(a && a.getAttribute("href"));
      if (id) art.setAttribute("data-concept-id", id);
    });
    $$(".okf-local-graph__node").forEach((btn) => {
      if (btn.getAttribute("data-concept-id")) return;
      const t = btn.getAttribute("data-target");
      if (t) btn.setAttribute("data-concept-id", t);
    });
  }
  // ---- Phase 5: index engagement (recency rail + comment chips) --------
  // Runs on index pages only, after loadGraph()/loadComments() resolve
  // (called from both — idempotent; the rail rebuilds so labels upgrade
  // once graph data lands). Everything reads state the studio already
  // fetched, so this costs no extra requests.
  function buildIndexEngagement() {
    if (!document.body.classList.contains("okf-viewer--index")) return;
    const titleOf = {};
    if (state.graph && Array.isArray(state.graph.nodes)) {
      state.graph.nodes.forEach((n) => { if (n && n.data) titleOf[n.data.id] = n.data.label; });
    }
    // 1) Comment-count chips on concept cards.
    const counts = {};
    (state.comments || []).forEach((c) => {
      if (!c || !c.concept) return;
      if (c.state === "archived") return;
      counts[c.concept] = (counts[c.concept] || 0) + 1;
    });
    $$(".okf-concept-list li[data-concept-id]").forEach((li) => {
      const id = li.getAttribute("data-concept-id");
      if (!counts[id]) return;
      let chip = li.querySelector(".okf-card__comments");
      if (!chip) {
        chip = el("span", { class: "okf-card__comments" });
        const meta = li.querySelector(".okf-card__meta");
        if (meta) meta.appendChild(chip);
        else li.appendChild(el("span", { class: "okf-card__meta" }, [chip]));
      }
      chip.textContent = counts[id] + " comment" + (counts[id] === 1 ? "" : "s");
      chip.title = chip.textContent;
    });
    // 2) Recently-changed rail — prefer the atlas orientation slot, fall
    // back to appending under the hero (legacy indexes without orientation).
    const latest = {};
    (state.events || []).forEach((ev) => {
      if (!ev || !ev.ts) return;
      if (ev.type !== "changed" && ev.type !== "created" && ev.type !== "removed" && ev.type !== "activity") return;
      (ev.ids || []).forEach((id) => {
        if (!latest[id] || latest[id].ts < ev.ts) latest[id] = { ts: ev.ts, type: ev.type };
      });
    });
    const ranked = Object.keys(latest)
      .sort((a, b) => (latest[a].ts < latest[b].ts ? 1 : -1))
      .slice(0, 5);
    const slot = $("#okf-recent-slot");
    const hero = $(".okf-hero");
    if (!ranked.length) return;
    let rail = $("#okf-recent-rail");
    if (rail) rail.remove();
    rail = el("div", { class: "okf-recent", id: "okf-recent-rail" });
    if (!slot) {
      rail.appendChild(el("span", { class: "okf-recent__label", text: "Recently changed" }));
    }
    ranked.forEach((id) => {
      const a = el("a", { class: "okf-recent__item", href: "/" + id });
      a.appendChild(el("strong", { text: titleOf[id] || id }));
      a.appendChild(document.createTextNode(" · " + fmtAgo(latest[id].ts)));
      rail.appendChild(a);
    });
    if (slot) {
      const placeholder = $(".okf-orient__empty", slot);
      if (placeholder) placeholder.remove();
      slot.appendChild(rail);
    } else if (hero) {
      hero.appendChild(rail);
    }
  }

  function hrefToConceptId(href) {
    if (!href || typeof href !== "string") return null;
    // Strip .html (static mode) AND .md (some index render paths emit the
    // source extension), plus any leading slash, so the id matches the form
    // presence.focus / currentConceptId use (e.g. "tables/orders").
    let h = href.trim().replace(/\.(html|md)$/, "").replace(/^\/+/, "");
    if (!h || h.indexOf("__") === 0 || h.indexOf("://") >= 0 || h.indexOf("#") === 0) return null;
    return h;
  }

  // ====================================================================
  // 7. Change list (§12.2) + Undo (§12.5)
  // ====================================================================
  // iter1 CRI-005 → iter2 G9: virtualization constants. iter3 CRI3-005
  // hoists these ABOVE renderChangeList so the prepend-shift compensation
  // can read CHANGE_EST_ROW_H without hitting a `const` temporal-dead-zone
  // (the function is defined before the constants were under the old
  // structure).
  const CHANGE_VIRTUAL_WINDOW = 80;   // rows kept in the DOM at any time
  const CHANGE_VIRTUAL_OVERSCAN = 20; // extra rows rendered above/below the viewport
  const CHANGE_VIRTUAL_STEP = 30;     // how far the window slides per sentinel hit
  const CHANGE_EST_ROW_H = 56;        // initial estimate; re-measured after first paint
  const CHANGE_HARD_CAP = 1000;       // display cap (gates the upfront note)
  const CHANGE_TOTAL_CAP = (window.OKF_LOOM_CHANGE_TOTAL_CAP > 0) ? window.OKF_LOOM_CHANGE_TOTAL_CAP : 10000;

  // iter3 CRI3-005: when a live event is prepended to state.events (via
  // upsertEvent → unshift), the next renderChangeList rebuild produces an
  // ordered array that is 1 row longer at the TOP. The virtualizer
  // honours savedScroll by indexing into the NEW ordered array — so the
  // same INDEX window now points at different rows (everything shifted
  // down by `prepended`). The user's reading row drifts UP by one row
  // per live event. Compensate by tracking the previous render's ordered
  // count + the active filter tuple: when the filters are unchanged AND
  // the new ordered is longer at the head (a genuine prepend, not a
  // filter relaxation), shift savedScroll by `prepended * CHANGE_EST_ROW_H`
  // so the same ROWS stay at the same on-screen offset. Only shift when
  // the user was actually scrolled into the list (savedScroll > 0); a
  // user at the top wants to see the new event arrive at the top.
  let _lastChangeListOrderedCount = 0;
  let _lastChangeListFilters = { actor: "", action: "", concept: "" };

  function renderChangeList() {
    if (state.openPanel !== "changes") return;
    const body = panelBodyEl();
    if (!body) return;
    // iter2 CRI2-003 / §7.3: renderChangeList rebuilds the panel from scratch
    // on every live event (activity/changed/created/removed). A from-scratch
    // rebuild resets scroll to the top AND drops focus/selection inside the
    // filter inputs, so a user scrolled down to row 200 reading history loses
    // their place the moment a new event lands. Capture the scroll offset +
    // the focused filter (with its caret selection) before the rebuild and
    // restore both after, so a live update is non-disruptive while the panel
    // is open. (The windowing IntersectionObserver still owns lazy row load.)
    const savedScroll = body.scrollTop;
    const active = document.activeElement;
    const savedFocus = (active && body.contains(active) && (active.tagName === "INPUT" || active.tagName === "SELECT"))
      ? { tag: active.tagName, ariaLabel: active.getAttribute("aria-label") || "",
          selStart: active.selectionStart, selEnd: active.selectionEnd,
          selDir: active.selectionDirection }
      : null;
    // iter3 CRI3-005: snapshot the previous render's bookkeeping BEFORE
    // the rebuild so we can detect a genuine prepend (vs a filter change).
    const prevOrderedCount = _lastChangeListOrderedCount;
    const prevFilters = _lastChangeListFilters;
    const filtersUnchanged =
      prevFilters.actor === state.filters.actor &&
      prevFilters.action === state.filters.action &&
      prevFilters.concept === state.filters.concept;
    body.innerHTML = "";

    const filters = el("div", { class: "okf-filters" });
    const actorSel = el("select", { "aria-label": "Filter by actor" });
    actorSel.appendChild(el("option", { value: "", text: "All actors" }));
    ["agent", "user", "cli", "disk"].forEach((a) => actorSel.appendChild(el("option", { value: a, text: a })));
    actorSel.value = state.filters.actor;
    actorSel.addEventListener("change", () => { state.filters.actor = actorSel.value; renderChangeList(); });
    const actionInput = el("input", { type: "search", placeholder: "filter action…",
      "aria-label": "Filter by action", value: state.filters.action });
    actionInput.addEventListener("input", () => { state.filters.action = actionInput.value.trim(); renderChangeList(); });
    const conceptInput = el("input", { type: "search", placeholder: "filter concept…",
      "aria-label": "Filter by concept", value: state.filters.concept });
    conceptInput.addEventListener("input", () => { state.filters.concept = conceptInput.value.trim(); renderChangeList(); });
    filters.appendChild(actorSel); filters.appendChild(actionInput); filters.appendChild(conceptInput);
    body.appendChild(filters);

    const list = el("div", { class: "okf-changes" });
    // iter3 CRI3-005: hoist `effectiveSavedScroll` and `newOrderedCount`
    // out of the else-branch so the post-rebuild scroll restore + the
    // next-render bookkeeping can both read them. effectiveSavedScroll
    // starts at savedScroll and is bumped inside the else-branch when a
    // genuine prepend is detected; newOrderedCount is 0 for the empty
    // state and `ordered.length` when rows render.
    let effectiveSavedScroll = savedScroll;
    let newOrderedCount = 0;
    // Timeline rows: agent activity (has `action`) AND bundle changes
    // (changed/created/removed - e.g. a disk save the watcher picked up).
    // comment/presence/graph/ready have their own surfaces and are excluded.
    let rows = state.events.filter((e) => {
      const t = e.type;
      if (t === "comment" || t === "presence" || t === "graph" || t === "ready") return false;
      return !!e.action || (Array.isArray(e.ids) && e.ids.length) ||
        t === "changed" || t === "created" || t === "removed";
    });
    if (state.filters.actor) rows = rows.filter((e) => (e.actor || e.origin || "") === state.filters.actor);
    if (state.filters.action) rows = rows.filter((e) => String(e.action || e.type || "").indexOf(state.filters.action) >= 0);
    if (state.filters.concept) rows = rows.filter((e) => (e.ids || []).some((id) => id.indexOf(state.filters.concept) >= 0));
    if (!rows.length) {
      list.appendChild(el("p", { class: "okf-empty", text: "No changes yet. Edit a file on disk, or the agent will act on your comments." }));
    } else {
      // Group consecutive same-group_id rows into single composite items so
      // the window count reflects what the user actually sees. iter2 G12:
      // events tagged with a burst_id (10+ standalone events in 1s) collapse
      // the same way into one expandable "Agent made N changes" row.
      const groups = Object.create(null);
      const bursts = Object.create(null);
      const ordered = [];
      rows.forEach((r) => {
        if (r.group_id) {
          if (!groups[r.group_id]) { groups[r.group_id] = []; ordered.push({ group: r.group_id }); }
          groups[r.group_id].push(r);
        } else if (r.burst_id) {
          if (!bursts[r.burst_id]) { bursts[r.burst_id] = []; ordered.push({ burst: r.burst_id }); }
          bursts[r.burst_id].push(r);
        } else ordered.push({ row: r });
      });
      newOrderedCount = ordered.length;
      // iter1 CRI-005 / iter2 G9: virtualize the rendered rows so a long
      // session keeps only the visible window in the DOM. iter2 G12 burst
      // coalescing + G10 upfront cap messaging ride the same renderer.
      //
      // iter3 CRI3-005: if this rebuild prepended rows at the head (a live
      // event arrived, filters unchanged, ordered grew), shift savedScroll
      // by `prepended * CHANGE_EST_ROW_H` so the virtualizer renders the
      // window that contains the SAME rows the user was reading (instead
      // of the same INDICES, which now point at different rows). Skip the
      // shift when the user was at the top (savedScroll === 0) — they
      // expect to see new events land at the top.
      if (
        savedScroll > 0 && filtersUnchanged && ordered.length > prevOrderedCount
      ) {
        const prepended = ordered.length - prevOrderedCount;
        effectiveSavedScroll = savedScroll + prepended * CHANGE_EST_ROW_H;
      }
      renderWindowedChanges(list, ordered, groups, {
        savedScroll: effectiveSavedScroll, savedFocus, body, bursts,
      });
    }
    body.appendChild(list);
    // Remember this render's bookkeeping for the NEXT render's prepend
    // detection (CRI3-005). Snapshot the filters too so a filter change
    // (which can also change ordered.length) is not misread as a prepend.
    _lastChangeListOrderedCount = newOrderedCount;
    _lastChangeListFilters = {
      actor: state.filters.actor,
      action: state.filters.action,
      concept: state.filters.concept,
    };
    // iter2 CRI2-003: restore the panel scroll offset + filter focus captured
    // before the rebuild. The virtualizer (renderWindowedChanges) positions
    // its initial window at the rows visible at savedScroll, so setting
    // scrollTop here lands at the same reading position without a flash.
    // iter3 CRI3-005: when a prepend shifted the window, restore the
    // SHIFTED offset so the user keeps looking at the same row.
    if (rows.length && typeof effectiveSavedScroll === "number") {
      body.scrollTop = effectiveSavedScroll;
    } else if (typeof savedScroll === "number") {
      body.scrollTop = savedScroll;
    }
    if (savedFocus) {
      const sel = savedFocus.ariaLabel
        ? body.querySelector(savedFocus.tag + '[aria-label="' + savedFocus.ariaLabel + '"]')
        : null;
      if (sel) {
        try { sel.focus({ preventScroll: true }); } catch (e) {}
        try {
          if (typeof sel.setSelectionRange === "function" && savedFocus.selStart != null) {
            sel.setSelectionRange(savedFocus.selStart, savedFocus.selEnd, savedFocus.selDir || "none");
          }
        } catch (e) {}
      }
    }
  }

  // ---- change-list virtualization (iter1 CRI-005 → iter2 G9) -----------
  // iter1 shipped incremental-prepend windowing: the first N rows rendered,
  // then a bottom sentinel IntersectionObserver appended more as the user
  // scrolled, but rows were never REMOVED, so a long session left hundreds
  // of DOM nodes attached. iter2 G9 replaces it with TRUE virtualization:
  // only a sliding window of ~CHANGE_VIRTUAL_WINDOW rows is ever in the DOM,
  // driven by top + bottom IntersectionObserver sentinels; top/bottom
  // spacers (sized by a measured row height) maintain the scrollbar
  // geometry so the user can scroll through the full history. A display
  // cap (CHANGE_HARD_CAP) gates the visible count with an UPFRONT "Showing
  // first N of M. Show all" note (G10 — shown before the user scrolls, not
  // after). The absolute virtualization ceiling is CHANGE_TOTAL_CAP (10000,
  // configurable via window.OKF_LOOM_CHANGE_TOTAL_CAP); upsertEvent caps the
  // in-memory log to match so older entries rotate out of state cleanly.
  // (Constants declared above renderChangeList — iter3 CRI3-005.)

  function renderWindowedChanges(list, ordered, groups, restore) {
    restore = restore || {};
    const bursts = restore.bursts || Object.create(null);  // iter2 G12 burst members
    const scrollContainer = restore.body || list;  // .okf-panel__body (overflow-y: auto)
    const savedScroll = (typeof restore.savedScroll === "number") ? restore.savedScroll : 0;

    // G10: the display cap. If the filtered set exceeds CHANGE_HARD_CAP,
    // surface the "Showing first N of M. Show all" note UP FRONT (before the
    // user scrolls), and virtualize only the first N until the user asks for
    // all. "Show all" raises the virtualization ceiling to CHANGE_TOTAL_CAP
    // (the absolute max; rows beyond it are rotated out of state by
    // upsertEvent).
    let virtualTotal = Math.min(ordered.length, CHANGE_HARD_CAP);
    let capped = ordered.length > CHANGE_HARD_CAP;
    if (capped) {
      const note = el("div", { class: "okf-changes__cap-note" });
      note.appendChild(document.createTextNode(
        "Showing first " + CHANGE_HARD_CAP + " of " + ordered.length + " events. "));
      const show = el("button", { type: "button", text: "Show more" });
      show.addEventListener("click", () => {
        // Raise the ceiling to the absolute cap and re-virtualize the full
        // tail. The note stays (it now reads as the absolute ceiling).
        note.parentNode && note.parentNode.removeChild(note);
        virtualTotal = Math.min(ordered.length, CHANGE_TOTAL_CAP);
        capped = ordered.length > CHANGE_TOTAL_CAP;
        if (capped) appendAbsoluteCapNote();
        rebuild();
      });
      note.appendChild(show);
      list.appendChild(note);
    }
    function appendAbsoluteCapNote() {
      const note = el("div", { class: "okf-changes__cap-note", role: "status" });
      note.appendChild(document.createTextNode(
        "Showing the most recent " + CHANGE_TOTAL_CAP + " of " + ordered.length +
        " events. Older entries are in events.jsonl."));
      list.appendChild(note);
    }

    // Virtualization scaffold: topSpacer + window rows + bottomSpacer. The
    // spacers carry the scrollbar geometry (height = offscreen-rows *
    // rowH); the window rows are the only .okf-change nodes in the DOM.
    const topSpacer = el("div", { class: "okf-changes__spacer okf-changes__spacer--top", "aria-hidden": "true" });
    const bottomSpacer = el("div", { class: "okf-changes__spacer okf-changes__spacer--bottom", "aria-hidden": "true" });
    const topSentinel = el("div", { class: "okf-changes__sentinel okf-changes__sentinel--top", "aria-hidden": "true" });
    const bottomSentinel = el("div", { class: "okf-changes__sentinel okf-changes__sentinel--bottom", "aria-hidden": "true" });
    const rowsHost = el("div", { class: "okf-changes__rows" });
    list.appendChild(topSpacer);
    list.appendChild(topSentinel);
    list.appendChild(rowsHost);
    list.appendChild(bottomSentinel);
    list.appendChild(bottomSpacer);

    let windowStart = 0;           // index of the first rendered row
    let rowH = CHANGE_EST_ROW_H;   // measured row height (updated after paint)
    const renderedIdx = new Map(); // index -> DOM node currently attached

    function clampStart(s) {
      const maxStart = Math.max(0, virtualTotal - windowSize());
      return Math.max(0, Math.min(s, maxStart));
    }
    function windowSize() {
      return Math.min(CHANGE_VIRTUAL_WINDOW, virtualTotal);
    }
    // syncWindow(start): make the rendered set exactly the window
    // [start, start + windowSize) (clamped). Removes rows that scrolled out,
    // adds rows that scrolled in, and resizes the spacers so the scrollbar
    // reflects the full virtualTotal * rowH content height.
    function syncWindow(start) {
      const want = clampStart(start);
      if (want === windowStart && renderedIdx.size > 0) return;
      windowStart = want;
      const last = Math.min(virtualTotal, want + windowSize());
      // Drop rows outside [want, last).
      Array.from(renderedIdx.keys()).forEach((i) => {
        if (i < want || i >= last) {
          const node = renderedIdx.get(i);
          if (node && node.parentNode) node.parentNode.removeChild(node);
          renderedIdx.delete(i);
        }
      });
      // Add missing rows in [want, last), in order.
      for (let i = want; i < last; i++) {
        if (renderedIdx.has(i)) continue;
        const node = changeItemOrGroup(ordered[i], groups, bursts);
        node.setAttribute("data-virtual-idx", String(i));
        // Insert before any later-rendered node; since we iterate ascending,
        // append unless a higher-index node already exists.
        let inserted = false;
        for (let j = i + 1; j < last; j++) {
          const after = renderedIdx.get(j);
          if (after) { rowsHost.insertBefore(node, after); inserted = true; break; }
        }
        if (!inserted) rowsHost.appendChild(node);
        renderedIdx.set(i, node);
      }
      topSpacer.style.height = (want * rowH) + "px";
      bottomSpacer.style.height = (Math.max(0, virtualTotal - last) * rowH) + "px";
    }
    // Measure the real row height once rows are painted, then re-sync so the
    // spacers match actual geometry (avoids scrollbar drift on wrap).
    function measureRowH() {
      const sample = rowsHost.firstElementChild;
      if (!sample) return;
      const h = sample.getBoundingClientRect().height;
      if (h > 12 && Math.abs(h - rowH) > 1) {
        rowH = h;
        // Re-sync spacer heights with the corrected height (windowStart unchanged).
        const last = Math.min(virtualTotal, windowStart + windowSize());
        topSpacer.style.height = (windowStart * rowH) + "px";
        bottomSpacer.style.height = (Math.max(0, virtualTotal - last) * rowH) + "px";
      }
    }

    // Initial window. Honour a saved scroll offset (G1): start the window at
    // the rows that would be visible at savedScroll so the restore doesn't
    // flash the top.
    const initialStart = clampStart(Math.floor((savedScroll || 0) / rowH) - CHANGE_VIRTUAL_OVERSCAN);
    syncWindow(initialStart);
    requestAnimationFrame(measureRowH);

    // IntersectionObserver sentinels: when the user scrolls to either edge
    // of the window, slide it by CHANGE_VIRTUAL_STEP (re-sync removes the
    // rows that left + adds the new ones). root is the scroll container
    // (.okf-panel__body), NOT panelShell (the .okf-panel aside does not
    // scroll; using it was a latent bug in the iter1 observer).
    function shift(delta) { syncWindow(windowStart + delta); }
    const ioOpts = { root: scrollContainer, rootMargin: "120px" };
    const topIo = new IntersectionObserver((entries) => {
      for (const ent of entries) {
        if (ent.isIntersecting && windowStart > 0) shift(-CHANGE_VIRTUAL_STEP);
      }
    }, ioOpts);
    const bottomIo = new IntersectionObserver((entries) => {
      for (const ent of entries) {
        if (ent.isIntersecting && windowStart + windowSize() < virtualTotal) shift(CHANGE_VIRTUAL_STEP);
      }
    }, ioOpts);
    topIo.observe(topSentinel);
    bottomIo.observe(bottomSentinel);

    // rebuild(): used by "Show more" to re-virtualize against a raised
    // ceiling without rebuilding the whole panel (preserves scroll + focus).
    function rebuild() {
      // Reset the window against the new virtualTotal, keeping the current
      // scroll position.
      const curScroll = scrollContainer.scrollTop;
      renderedIdx.forEach((n) => { if (n.parentNode) n.parentNode.removeChild(n); });
      renderedIdx.clear();
      const start = clampStart(Math.floor(curScroll / rowH) - CHANGE_VIRTUAL_OVERSCAN);
      syncWindow(start);
      requestAnimationFrame(measureRowH);
    }
  }
  // Builds a change row/group node without appending (used by the loader).
  function changeItemOrGroup(item, groups, bursts) {
    if (item.row) return changeRow(item.row);
    if (item.burst) {
      // iter2 G12: a burst composite (10+ standalone events in 1s). Renders
      // as a <details> so the expand is native + keyboard-accessible (Enter/
      // Space toggles). The summary is one "Agent made N changes" line; the
      // body lists the individual change rows so the user can drill in.
      const members = bursts[item.burst];
      const det = el("details", { class: "okf-change okf-change--burst", dataset: { actor: members[0].actor } });
      const sum = el("summary", { class: "okf-change__summary" });
      sum.appendChild(el("span", { class: "okf-change__icon", "aria-hidden": "true" }));
      const actorRaw = (members[0] && members[0].actor) || "agent";
      const actor = actorRaw.charAt(0).toUpperCase() + actorRaw.slice(1);
      sum.appendChild(el("span", { text: actor + " made " + members.length + " changes in one burst. " }));
      sum.appendChild(el("span", { class: "okf-change__burst-hint", text: "Expand for detail" }));
      det.appendChild(sum);
      const body = el("div", { class: "okf-change__burst-body" });
      members.forEach((m) => body.appendChild(changeRow(m)));
      det.appendChild(body);
      return det;
    }
    const members = groups[item.group];
    const wrap = el("div", { class: "okf-change okf-change--group", dataset: { actor: members[0].actor } });
    wrap.appendChild(el("span", { class: "okf-change__icon", "aria-hidden": "true" }));
    const b = el("div", { class: "okf-change__body" });
    b.appendChild(el("div", { class: "okf-change__summary", text: members.length + " changes · group pass" }));
    b.appendChild(el("div", { class: "okf-change__meta" }, [el("span", { text: members[0].actor + " · " + fmtTime(members[0].ts) })]));
    wrap.appendChild(b);
    const actions = el("div", { class: "okf-change__actions" });
    const undo = el("button", { type: "button", class: "okf-change__undo", text: "Undo group" });
    undo.addEventListener("click", () => undoGroup(item.group, undo));
    actions.appendChild(undo);
    wrap.appendChild(actions);
    return wrap;
  }

  function changeRow(r) {
    const actor = r.actor || r.origin || "system";
    const kind = r.type === "changed" ? "Saved" : r.type === "created" ? "Created" : r.type === "removed" ? "Removed" : null;
    const summary = r.summary || (kind ? (kind + " " + ((r.ids || [])[0] || "")) : (actor + " " + (r.action || "acted")));
    const row = el("div", { class: "okf-change", dataset: { actor: actor, activityId: String(r.id || "") } });
    row.appendChild(el("span", { class: "okf-change__icon", "aria-hidden": "true" }));
    const b = el("div", { class: "okf-change__body" });
    b.appendChild(el("div", { class: "okf-change__summary", text: summary }));
    const meta = el("div", { class: "okf-change__meta" });
    meta.appendChild(el("span", { text: actor + " · " + (r.action || r.type || "") + " · " + fmtTime(r.ts) }));
    (r.ids || []).forEach((id) => {
      meta.appendChild(document.createTextNode(" · "));
      const a = el("a", { href: "/" + id, text: id });
      meta.appendChild(a);
    });
    b.appendChild(meta);
    // R1 (INTENT2-007): if a comment_link event exists for this activity,
    // render a back-link to the originating comment. The comment_link event
    // is emitted by resolve_comment and carries (activity_id, comment_id).
    const link = state.events.find(
      (e) => e.type === "comment_link" && e.activity_id === r.id,
    );
    if (link) {
      const commentDiv = el("div", { class: "okf-change__comment-link" });
      commentDiv.appendChild(document.createTextNode("Resolved "));
      const cLink = el("a", {
        href: "#comment-" + link.comment_id,
        text: "comment " + (link.comment_id || "").slice(0, 10),
        title: "Jump to the comment this change resolved",
      });
      cLink.addEventListener("click", (ev) => {
        ev.preventDefault();
        openPanel("comments");
        // Defer to the next frame so the comments panel exists in the DOM.
        requestAnimationFrame(() => {
          const target = document.getElementById("comment-" + link.comment_id);
          if (target) {
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.classList.add("okf-comment--pulse");
            setTimeout(() => target.classList.remove("okf-comment--pulse"), 2000);
          }
        });
      });
      commentDiv.appendChild(cLink);
      b.appendChild(commentDiv);
    }
    row.appendChild(b);
    const actions = el("div", { class: "okf-change__actions" });
    // Only mutator-recorded activity carries undoable + a rev to restore;
    // disk/changed events have no group snapshot to undo here.
    if (r.undoable && r.id && (r.ids && r.ids[0])) {
      const undo = el("button", { type: "button", class: "okf-change__undo", text: "Undo" });
      undo.addEventListener("click", () => undoOne(r, undo));
      actions.appendChild(undo);
    }
    row.appendChild(actions);
    return row;
  }

  async function undoOne(r, btn) {
    if (!r || !r.id) return;
    // F2: the change-list row carries the content-hash rev to restore in
    // ``detail.before`` (stamped by _handle_apply from snapshot_for_undo, the
    // sha1[:12] of the prior concept bytes - NOT the integer bundle rev). Use
    // it as the rev and ``detail.before_concept`` as the concept; fall back to
    // the old ids+rev behaviour ONLY when ``detail.before`` is absent (older
    // events recorded before the pointer was wired through). The fallback rev
    // is the integer bundle rev, which undo_snapshot cannot resolve → silent
    // no-op; this is why the content-hash pointer is required.
    const before = (r.detail && r.detail.before) || null;
    const concept = before
      ? (r.detail.before_concept || (r.ids && r.ids[0]) || state.conceptId)
      : ((r.ids && r.ids[0]) || state.conceptId);
    const rev = before || r.rev || "";
    Btn.busy(btn);
    try {
      const res = await tokenFetch("/__undo", { method: "POST", body: { concept, rev } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || ("HTTP " + res.status));
      toast("Undid change on " + concept + ".", { tone: "success" });
    } catch (e) {
      toast("Undo failed: " + (e.message || e), { tone: "error" });
    } finally {
      Btn.done(btn, "Undo");
    }
  }
  async function undoGroup(groupId, btn) {
    Btn.busy(btn);
    try {
      const res = await tokenFetch("/__undo", { method: "POST", body: { group_id: groupId } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || ("HTTP " + res.status));
      toast("Reverted group pass (" + (data.restored || 0) + " file(s)).", { tone: "success" });
    } catch (e) {
      toast("Group undo failed: " + (e.message || e), { tone: "error" });
    } finally {
      Btn.done(btn, "Undo group");
    }
  }
  const Btn = {
    busy(b) { b.disabled = true; b.dataset._txt = b.textContent; b.textContent = "…"; },
    done(b, txt) { b.disabled = false; b.textContent = txt || b.dataset._txt || "Undo"; },
  };

  // ====================================================================
  // 8. Command palette (§13.4)
  // ====================================================================
  let paletteState = { open: false, items: [], active: 0, lastReturn: null };
  function buildPalette() {
    const overlay = el("div", { class: "okf-palette-overlay", hidden: "", role: "dialog",
      "aria-modal": "true", "aria-label": "Command palette" });
    const pal = el("div", { class: "okf-palette" });
    const input = el("input", { type: "search", class: "okf-palette__input",
      placeholder: "Jump to a concept, run a command…", "aria-label": "Command palette input", "aria-autocomplete": "list" });
    const list = el("ul", { class: "okf-palette__list", role: "listbox", "aria-label": "Commands" });
    const hint = el("div", { class: "okf-palette__hint" }, [
      el("span", { text: "↑↓ navigate" }), el("span", { text: "⏎ select" }), el("span", { text: "Esc close" }),
    ]);
    pal.appendChild(input); pal.appendChild(list); pal.appendChild(hint);
    overlay.appendChild(pal);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closePalette(); });
    input.addEventListener("input", () => refreshPaletteList(input.value));
    list.addEventListener("click", (e) => {
      const li = e.target.closest(".okf-palette__item");
      if (!li) return;
      paletteState.active = +li.dataset.idx;
      activatePalette();
    });
    document.body.appendChild(overlay);
    paletteState.overlay = overlay;
    paletteState.input = input;
    paletteState.list = list;
  }
  // Global keyboard wiring (registered at boot, independent of the lazy DOM
  // build, so Ctrl/Cmd-K toggles the palette on first press and arrow/enter
  // navigation works once it is open).
  function wirePaletteKeys() {
    document.addEventListener("keydown", (e) => {
      const meta = e.ctrlKey || e.metaKey;
      if (meta && (e.key === "k" || e.key === "K")) { e.preventDefault(); togglePalette(); return; }
      if (!paletteState.open) return;
      if (e.key === "Escape") { e.preventDefault(); closePalette(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
      else if (e.key === "Enter") { e.preventDefault(); activatePalette(); }
      // iter1 CRI-016: trap focus inside the modal so Tab/Shift+Tab can't
      // escape to the page behind (aria-modal="true" claims a trap).
      else if (e.key === "Tab") {
        e.preventDefault();
        trapFocusIn(paletteState.overlay, !e.shiftKey);
      }
    });
  }
  // iter1 CRI-016: focus trap shared by the palette and the slide-over panel.
  // Returns the focusable elements of a container in DOM order, skipping
  // hidden/disabled/negative-tabindex nodes.
  function focusableIn(root) {
    if (!root) return [];
    const sel = 'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])';
    return $$(sel, root).filter((n) => {
      if (n.hasAttribute("hidden")) return false;
      const cs = getComputedStyle(n);
      return cs.display !== "none" && cs.visibility !== "hidden" && cs.pointerEvents !== "none";
    });
  }
  // Move focus forward (or backward) within `root`, wrapping at the edges.
  function trapFocusIn(root, forward) {
    const focusables = focusableIn(root);
    if (!focusables.length) { try { root.focus(); } catch (e) {} return; }
    const cur = document.activeElement;
    const idx = focusables.indexOf(cur);
    let next;
    if (idx < 0) next = focusables[0];
    else if (forward) next = focusables[(idx + 1) % focusables.length];
    else next = focusables[(idx - 1 + focusables.length) % focusables.length];
    try { next.focus({ preventScroll: true }); } catch (e) {}
  }
  function openPalette() {
    if (!paletteState.overlay) buildPalette();
    paletteState.open = true;
    paletteState.overlay.hidden = false;
    paletteState.input.value = "";
    refreshPaletteList("");
    paletteState._lastFocus = document.activeElement;
    setTimeout(() => paletteState.input.focus(), 20);
  }
  function closePalette() {
    if (!paletteState.overlay) return;
    paletteState.open = false;
    paletteState.overlay.hidden = true;
    if (paletteState._lastFocus && typeof paletteState._lastFocus.focus === "function") {
      try { paletteState._lastFocus.focus(); } catch (e) {}
    }
  }
  function togglePalette() { paletteState.open ? closePalette() : openPalette(); }
  function movePalette(delta) {
    const n = paletteState.items.length;
    if (!n) return;
    paletteState.active = (paletteState.active + delta + n) % n;
    renderPaletteList();
  }
  function activatePalette() {
    const item = paletteState.items[paletteState.active];
    if (!item) return;
    closePalette();
    try { item.run(); } catch (e) { console.error(e); }
  }
  function commandItems() {
    const items = [];
    items.push({ label: "Switch view: Rendered", sub: "view mode", run: () => setView("rendered") });
    items.push({ label: "Switch view: Source", sub: "view mode", run: () => setView("source") });
    items.push({ label: "Switch view: Split", sub: "view mode", run: () => setView("split") });
    if (EDIT && isConceptPage()) items.push({ label: "Post a comment / ask the agent", sub: "comment", run: () => openPanel("comments", { focusComposer: true }) });
    items.push({ label: "Open Comments panel", sub: "panel", run: () => openPanel("comments") });
    items.push({ label: "Open Changes panel", sub: "panel", run: () => openPanel("changes") });
    items.push({ label: "Theme: Light", sub: "theme", run: () => applyThemeAttr("light") });
    items.push({ label: "Theme: Dark", sub: "theme", run: () => applyThemeAttr("dark") });
    items.push({ label: "Theme: Pastel", sub: "theme", run: () => applyThemeAttr("pastel") });
    items.push({ label: "Theme: Sepia", sub: "theme", run: () => applyThemeAttr("sepia") });
    items.push({ label: "Theme: Midnight", sub: "theme", run: () => applyThemeAttr("midnight") });
    items.push({ label: "Theme: Auto (follow OS)", sub: "theme", run: () => {
      // Clear the saved choice so the OS preference governs again.
      try { localStorage.removeItem("okf-theme"); } catch (e) {}
      applyThemeAttr(effectiveTheme("auto"), { persist: false });
    } });
    if (window.okfLoomLive && window.okfLoomLive.resync) items.push({ label: "Resync now", sub: "live", run: () => window.okfLoomLive.resync() });
    // Registered panels.
    Object.keys(panels).forEach((id) => {
      const p = panels[id];
      if (p.__builtin) return;
      items.push({ label: "Open panel: " + p.label, sub: "extension", run: () => openPanel(id) });
    });
    return items;
  }
  function conceptItems(q) {
    const nodes = (state.graph && state.graph.nodes) || [];
    const ql = q.toLowerCase();
    return nodes.map((n) => {
      const d = n.data || n;
      return {
        label: d.label || d.id,
        sub: d.id + (d.type ? " · " + d.type : ""),
        run: () => { window.location.href = "/" + d.id; },
      };
    }).filter((it) => !ql || it.label.toLowerCase().indexOf(ql) >= 0 || it.sub.toLowerCase().indexOf(ql) >= 0)
      .slice(0, 12);
  }
  function refreshPaletteList(q) {
    q = (q || "").trim();
    let items;
    if (!q) {
      items = commandItems().concat(conceptItems("").slice(0, 6));
    } else {
      const cmds = commandItems().filter((c) => c.label.toLowerCase().indexOf(q.toLowerCase()) >= 0);
      items = cmds.concat(conceptItems(q));
    }
    paletteState.items = items;
    paletteState.active = 0;
    renderPaletteList();
  }
  function renderPaletteList() {
    const list = paletteState.list;
    list.innerHTML = "";
    paletteState.items.forEach((item, i) => {
      const li = el("li", { class: "okf-palette__item", role: "option",
        "aria-selected": i === paletteState.active ? "true" : "false", dataset: { idx: String(i) } });
      li.appendChild(el("span", { class: "okf-palette__item-title", text: item.label }));
      li.appendChild(el("span", { class: "okf-palette__item-sub", text: item.sub }));
      li.appendChild(el("span", { class: "okf-palette__item-kbd", text: i === paletteState.active ? "⏎" : "" }));
      list.appendChild(li);
    });
  }

  // ====================================================================
  // 9. Slide-over panel + extension registry (§13.6)
  // ====================================================================
  const panels = {}; // id → { id, label, render(container, ctx), __builtin }
  const panelOverlay = el("div", { class: "okf-panel-overlay", hidden: "" });
  const panelShell = el("aside", { class: "okf-panel", hidden: "", role: "dialog",
    "aria-modal": "true", "aria-label": "Studio panel", tabindex: "-1" });
  const panelHeader = el("div", { class: "okf-panel__header" });
  const panelTitle = el("h2", { class: "okf-panel__title" });
  const panelClose = el("button", { type: "button", class: "okf-panel__close", "aria-label": "Close panel", text: "Esc" });
  panelHeader.appendChild(panelTitle); panelHeader.appendChild(panelClose);
  const panelBody = el("div", { class: "okf-panel__body", id: "okf-panel-body" });
  panelShell.appendChild(panelHeader); panelShell.appendChild(panelBody);
  document.body.appendChild(panelOverlay); document.body.appendChild(panelShell);
  panelOverlay.addEventListener("click", closePanel);
  panelClose.addEventListener("click", closePanel);
  document.addEventListener("keydown", (e) => {
    if (!state.openPanel) return;
    if (e.key === "Escape") { e.preventDefault(); closePanel(); }
    // iter1 CRI-016: trap focus inside the slide-over panel so Tab can't
    // reach the page behind while aria-modal="true" is claimed.
    else if (e.key === "Tab") { e.preventDefault(); trapFocusIn(panelShell, !e.shiftKey); }
  });
  function panelBodyEl() { return panelBody; }
  // iter1 CRI-016: remember the trigger so focus is restored on close.
  let panelLastFocus = null;

  function openPanel(id, opts) {
    opts = opts || {};
    const p = panels[id];
    if (!p) return;
    state.openPanel = id;
    panelShell.hidden = false;
    panelOverlay.hidden = false;
    panelTitle.textContent = p.label;
    panelShell.setAttribute("aria-label", p.label);
    // Update aria-expanded on every toggle button.
    [commentsBtn, changesBtn].forEach((b) => b.setAttribute("aria-expanded", "false"));
    const tb = ({ comments: commentsBtn, changes: changesBtn })[id];
    if (tb) tb.setAttribute("aria-expanded", "true");
    // Save the trigger so closePanel can restore focus (CRI-016).
    if (!panelLastFocus) panelLastFocus = document.activeElement;
    // Render.
    panelBody.innerHTML = "";
    panelBody._focusComposer = !!opts.focusComposer;
    try { p.render(panelBody, ctx()); } catch (e) { console.error("[okf-studio] panel render", e); }
    try { panelShell.focus(); } catch (e) {}
  }
  function closePanel() {
    state.openPanel = null;
    panelShell.hidden = true;
    panelOverlay.hidden = true;
    [commentsBtn, changesBtn].forEach((b) => b.setAttribute("aria-expanded", "false"));
    // iter1 CRI-016: restore focus to the button/link that opened the panel.
    if (panelLastFocus && typeof panelLastFocus.focus === "function") {
      try { panelLastFocus.focus({ preventScroll: true }); } catch (e) {}
    }
    panelLastFocus = null;
  }
  function togglePanel(id) { state.openPanel === id ? closePanel() : openPanel(id); }

  function ctx() {
    return {
      state, boot: BOOT, el, toast, tokenFetch,
      currentConceptId: state.conceptId,
      onLive: (type, fn) => window.okfLoomLive && window.okfLoomLive.on(type, fn),
    };
  }

  // register(kind, impl) - the documented client extension seam (§13.6).
  // iter1 CRI-007: the previously-documented {toolbar, graphDecorator,
  // suggestionRenderer} kinds were inert no-op stubs that over-promised the
  // extension surface. They are now RESERVED (accepted + warned, not wired)
  // and documented as forward-compat in the module header. Only {panel,
  // viewMode} ship wired today: `panel` powers the built-in Comments /
  // Changes / Agent-activity panels; `viewMode` lets an extension register
  // an additional read-only view mode (e.g. "outline") that joins the
  // Rendered/Source/Split switch + the ?view= deep link.
  function register(kind, impl) {
    if (kind === "panel" && impl && impl.id) {
      panels[impl.id] = Object.assign({ __builtin: false }, impl);
      // Add a bar toggle for non-builtin panels.
      if (!impl.__builtin && !impl._btn) {
        const btn = el("button", { type: "button", class: "okf-iconbtn", "aria-expanded": "false", "aria-controls": "okf-panel", text: impl.label });
        btn.addEventListener("click", () => togglePanel(impl.id));
        impl._btn = btn;
        rightGroup.insertBefore(btn, paletteBtn);
      }
      if (state.openPanel === impl.id) openPanel(impl.id);
      return impl;
    }
    if (kind === "viewMode" && impl && impl.id) {
      viewModes[impl.id] = impl;
      addViewModeButton(impl);
      return impl;
    }
    // Reserved / forward-compat kinds. Accepted so an extension that targets
    // a future toolkit version doesn't throw, but warned so the author knows
    // the hook isn't wired yet. See viewer/OVERRIDES.md §6 for the full API.
    if (kind === "toolbar" || kind === "graphDecorator" || kind === "suggestionRenderer") {
      console.warn("[okf-studio] register('" + kind + "') is reserved for a future release and is not wired in this version. See viewer/OVERRIDES.md §6 for the documented extension surface.");
      return impl;
    }
    console.warn("[okf-studio] register: unknown kind or missing impl", kind);
  }
  const viewModes = {};
  function addViewModeButton(mode) {
    // Insert a new button into the view switch group. It activates the
    // extension mode; the extension's onActivate is responsible for any
    // custom rendering (e.g. swapping in an outline panel).
    if (!isConceptPage()) return;
    const btn = el("button", { type: "button", class: "okf-viewswitch__btn",
      "aria-pressed": "false", text: mode.label || mode.id });
    btn.dataset.mode = "ext:" + mode.id;
    btn.addEventListener("click", () => {
      try { if (typeof mode.onActivate === "function") mode.onActivate(ctx()); } catch (e) { console.error(e); }
      // Mark only this extension button pressed; clear the built-in trio.
      $$(".okf-viewswitch__btn").forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
    });
    if (viewSwitch) viewSwitch.appendChild(btn);
  }

  // Built-in panels.
  // panels.comments delegates straight to renderCommentsPanel(),
  // which now preserves the composer node across rebuilds (typing guard
  // is internal — see renderCommentsPanel). The previous wrapper-level
  // "skip rebuild if typing" guard is no longer needed: the panel splits
  // into composer / toolbar / list zones and only the toolbar + list
  // rebuild on each event.
  panels.comments = {
    id: "comments", label: "Comments",
    render: function () { renderCommentsPanel(); },
    __builtin: true,
  };
  panels.changes = { id: "changes", label: "Changes", render: (c) => { c.innerHTML = ""; renderChangeList(); }, __builtin: true };

  // ---- Built-in extension panel demonstrating register(): "Agent activity" ----
  // iter2 G13 (CRI2-012): enriched with UNIQUE content the presence chip +
  // the Changes panel don't surface, so the panel earns its slot instead of
  // duplicating them. Three sections:
  //   1. The agent's OPEN/CLAIMED comment queue (comments the agent has
  //      claimed but not resolved) — distinct from the Changes panel, which
  //      shows events, not the directive queue.
  //   2. Presence history (recent state transitions) — a small log the chip
  //      can't show because it only carries the current state.
  //   3. iter3 CRI3-007: agent writes in the LAST 5 MINUTES — a unique time
  //      filter the Changes panel doesn't offer. Was a 30-row slice of the
  //      same changeRow() the Changes panel renders, which made the section
  //      a pure duplicate (the impeccable critic flagged: "a user who opens
  //      both panels side by side sees the same rows twice"). Replacing the
  //      30-row dump with a 5-minute window keeps the panel about state
  //      ("what is the agent doing RIGHT NOW"), with a "View in Changes →"
  //      link for the full history.
  register("panel", {
    id: "agent-activity",
    label: "Agent activity",
    render(container, c) {
      container.innerHTML = "";
      const st = state.presence.state || "idle";
      const verb = ({ idle: "idle", watching: "watching", thinking: "thinking about", editing: "editing" })[st] || st;
      container.appendChild(el("h3", { class: "okf-panel__section-title",
        text: "Agent · " + verb + (state.presence.focus ? " · " + state.presence.focus : "") }));

      // Section 1: the agent's claimed/open comment queue (unique).
      const claimed = state.comments.filter((cm) =>
        (cm.state === "claimed" || cm.state === "open") && cm.claimed_by === "agent");
      const q = el("div", { class: "okf-panel__section" });
      q.appendChild(el("h3", { class: "okf-panel__section-title", text: "Claimed queue (" + claimed.length + ")" }));
      if (!claimed.length) {
        q.appendChild(el("p", { class: "okf-empty", text: "No comments claimed by the agent right now." }));
      } else {
        claimed.slice(0, 20).forEach((cm) => q.appendChild(commentCard(cm)));
      }
      container.appendChild(q);

      // Section 2: presence history (unique — the chip carries only current).
      const hist = el("div", { class: "okf-panel__section" });
      hist.appendChild(el("h3", { class: "okf-panel__section-title", text: "Presence history (" + state.presenceHistory.length + ")" }));
      if (!state.presenceHistory.length) {
        hist.appendChild(el("p", { class: "okf-empty", text: "No presence transitions yet." }));
      } else {
        const log = el("ul", { class: "okf-presence-log" });
        state.presenceHistory.slice(0, 12).forEach((h) => {
          const li = el("li", { class: "okf-presence-log__item", "data-state": h.state });
          li.appendChild(el("span", { class: "okf-presence-log__state", text: h.state || "idle" }));
          if (h.focus) li.appendChild(el("span", { class: "okf-presence-log__focus", text: h.focus }));
          if (h.message) li.appendChild(el("span", { class: "okf-presence-log__message", text: h.message, title: h.message }));
          li.appendChild(el("span", { class: "okf-presence-log__ts", text: fmtTime(h.ts) }));
          log.appendChild(li);
        });
        hist.appendChild(log);
      }
      container.appendChild(hist);

      // Section 3 (iter3 CRI3-007): agent writes in the LAST 5 MINUTES.
      // Unique time-boxed filter the Changes panel doesn't offer; reads as
      // "what is the agent doing RIGHT NOW". Capped at 10 rows so the panel
      // stays compact; a "View in Changes →" button opens the full history
      // panel for everything older.
      const AGENT_RECENT_WINDOW_MS = 5 * 60 * 1000;
      const fiveMinAgo = Date.now() - AGENT_RECENT_WINDOW_MS;
      const recentAgent = state.events.filter((e) => {
        if (e.actor !== "agent") return false;
        // Parse the event ts (ISO string) to epoch ms; tolerate missing or
        // malformed ts by treating the row as NOT recent (excluded).
        const t = Date.parse(e.ts || "");
        return !isNaN(t) && t >= fiveMinAgo;
      });
      const act = el("div", { class: "okf-panel__section" });
      act.appendChild(el("h3", {
        class: "okf-panel__section-title",
        text: "Recent writes · last 5 min (" + recentAgent.length + ")",
      }));
      if (!recentAgent.length) {
        act.appendChild(el("p", {
          class: "okf-empty",
          text: "No agent writes in the last 5 minutes.",
        }));
      } else {
        recentAgent.slice(0, 10).forEach((r) => act.appendChild(changeRow(r)));
      }
      // Single link to the full Changes panel — replaces the 30-row dump
      // that previously duplicated the Changes panel verbatim.
      const viewAll = el("button", {
        type: "button",
        class: "okf-studiobtn okf-panel__section-link",
        text: "View full history in Changes",
      });
      viewAll.addEventListener("click", () => openPanel("changes"));
      act.appendChild(viewAll);
      container.appendChild(act);
    },
  });

  // ====================================================================
  // 10. Badges + cross-cutting UI updates
  // ====================================================================
  function updateBadges() {
    const open = state.comments.filter((c) => c.state === "open" || c.state === "claimed").length;
    commentsBadge.textContent = String(open);
    commentsBadge.setAttribute("data-count", String(open));
    commentsBadge.setAttribute("aria-label", open + " open comments");
    commentsBtn.setAttribute("data-count", String(open));
    commentsBtn.classList.toggle("okf-studiobtn--quiet", open === 0 && state.openPanel !== "comments");
    const acts = state.events.filter((e) => e.type === "activity" || e.action).length;
    changesBadge.textContent = String(acts);
    changesBadge.setAttribute("data-count", String(acts));
    changesBtn.setAttribute("data-count", String(acts));
    changesBtn.classList.toggle("okf-studiobtn--quiet", acts === 0 && state.openPanel !== "changes");
  }

  function jumpToActivity(aid) {
    openPanel("changes");
    // Highlight the matching row briefly. Exact match on the row's
    // data-activity-id; the old 6-char-substring scan of row textContent
    // could light up the wrong row.
    setTimeout(() => {
      const rows = $$(".okf-change", panelBody);
      const match = rows.find((r) => r.dataset.activityId === String(aid)) ||
        rows.find((r) => r.textContent.indexOf(shortId(aid)) >= 0);
      if (match) { match.scrollIntoView({ block: "center" }); match.classList.add("okf-pulse"); }
    }, 60);
  }

  // ====================================================================
  // 11. Graph cache (palette + local graph refresh + presence)
  // ====================================================================
  async function loadGraph() {
    try {
      const res = await fetch("/__data/graph.json", { headers: { Accept: "application/json" } });
      state.graph = await res.json();
    } catch (e) { /* palette still works with commands only */ }
  }
  function refreshGraph() {
    // Called by live.js on a `graph` event. Reload graph cache + rebuild
    // local sidebar pills + margin markers.
    loadGraph().then(() => { rebuildMarginMarkers(); });
  }

  // ====================================================================
  // 12. Wire live.js hub → studio surfaces
  // ====================================================================
  function wireLive() {
    if (!window.okfLoomLive) return;
    window.okfLoomLive.on("conn", (d) => {
      connChip.dataset.state = d.state;
      const label = d.state === "online" ? "online" : (d.state === "reconnecting" ? "reconnecting" : "offline");
      connLabel.textContent = d.state === "online" ? "Live" : (d.state === "reconnecting" ? "Reconnecting…" : "Offline");
      connChip.setAttribute("aria-label", "Live updates connection: " + label);
    });
    window.okfLoomLive.on("presence", renderPresence);
    window.okfLoomLive.on("comment", (c) => {
      if (!c || !c.id) return;
      upsertComment(c);
      if (state.openPanel === "comments") renderCommentsPanel();
      updateBadges();
      // iter1 CRI-002: sync the mark's state attribute (claimed/resolved
      // re-style the highlight) + rebuild markers over the open concept.
      setCommentMarkState(c.id, c.state || "open");
      applyCommentMarks();
      rebuildMarginMarkers();
      // Also push to events so the timeline reflects comment lifecycle.
      upsertEvent(Object.assign({ type: "comment" }, c));
    });
    window.okfLoomLive.on("activity", (a) => {
      if (!a) return;
      upsertEvent(a);
      if (state.openPanel === "changes") renderChangeList();
      updateBadges();
      // iter1 CRI-010: collapse a burst of activity into one toast. Events
      // that share a group_id (a scoped enrichment pass) are coalesced into
      // "Agent made N changes" with a single group-Undo. Standalone events
      // still get their own toast, but a rapid stream no longer stacks 8.
      scheduleActivityToast(a);
    });
    window.okfLoomLive.on("changed", (d) => { upsertEvent({ type: "changed", ids: d.ids, origin: d.origin, rev: d.rev, ts: new Date().toISOString() }); if (state.openPanel === "changes") renderChangeList(); });
    window.okfLoomLive.on("created", (d) => { upsertEvent({ type: "created", ids: d.ids, origin: d.origin, rev: d.rev, ts: new Date().toISOString() }); if (state.openPanel === "changes") renderChangeList(); });
    window.okfLoomLive.on("removed", (d) => { upsertEvent({ type: "removed", ids: d.ids, origin: d.origin, rev: d.rev, ts: new Date().toISOString() }); if (state.openPanel === "changes") renderChangeList(); });
    window.okfLoomLive.on("resync", () => { loadComments(); });
    // INTENT5-001 / QUA5-002: comment_link consumer — upsert the event into
    // state.events so changeRow's lookup finds it and re-renders the back-link
    // live (without a page reload). Also re-render the changes panel if open.
    window.okfLoomLive.on("comment_link", (d) => {
      upsertEvent(d);
      if (state.openPanel === "changes") renderChangeList();
    });
    window.okfLoomLive.on("agent_conflict", (d) => {
      // §9.4 conflict from the CLI agent path: surface the modal. The SSE
      // event is the FLAT conflict object (update.py append_event), so it
      // rides in as `data`; there is no HTTP request to retry, so no
      // url/opts — showConflictModal hides "Take the agent's edit" for
      // this shape (the agent's payload lives in its process, not ours).
      showConflictModal({ data: d });
    });
    window.okfLoomLive.on("open-removed", (d) => {
      toast(el("span", {}, [document.createTextNode("The open concept “" + d.id + "” was removed. "),
        el("a", { href: "/", text: "Go to index" })]), { tone: "error", sticky: true });
    });
    window.okfLoomLive.on("patched", () => { /* change list already updated via changed */ });
  }

  // ---- activity toast throttle + inline undo (iter1 CRI-010 / CRI-018) -
  // Within a short window (500ms) we coalesce activity events so a single
  // scoped-enrichment pass that writes 8 files produces ONE toast, not 8.
  // Grouped events (shared group_id) collapse to "Agent made N changes"
  // with a group-Undo; standalone events keep their own one-line toast.
  // The toast Undo button performs the undo INLINE (CRI-018) instead of
  // just opening the Changes panel.
  const ACTIVITY_TOAST_WINDOW_MS = 500;
  // iter2 CRI2-005: max inline Undo buttons in a single burst toast before
  // they collapse to one "Undo all (N)". A toast is glanceable; a 5-button
  // toast reads as a panel and the close × drifts from the summary on wrap.
  const ACTIVITY_TOAST_UNDO_CAP = 3;
  let activityToastTimer = null;
  let activityToastBuffer = [];
  function scheduleActivityToast(a) {
    activityToastBuffer.push(a);
    if (activityToastTimer) clearTimeout(activityToastTimer);
    activityToastTimer = setTimeout(flushActivityToast, ACTIVITY_TOAST_WINDOW_MS);
  }
  function flushActivityToast() {
    activityToastTimer = null;
    const batch = activityToastBuffer;
    activityToastBuffer = [];
    if (!batch.length) return;
    // Group the batch by group_id (standalone events share no group).
    const byGroup = Object.create(null);
    const standalone = [];
    batch.forEach((a) => {
      if (a.group_id) (byGroup[a.group_id] || (byGroup[a.group_id] = [])).push(a);
      else standalone.push(a);
    });
    const groups = Object.keys(byGroup);
    // One toast for the whole batch: summary line + grouped undo actions.
    const summary = buildActivitySummary(batch, groups, byGroup, standalone);
    const span = el("span", {});
    span.appendChild(document.createTextNode(summary + "  · "));
    // iter2 CRI2-005: cap inline Undo buttons so a 5-group burst doesn't
    // produce a 5-button toast (a toast is glanceable; a 5-button toast is a
    // panel). Collect the undoable targets (group ids + standalone undoable
    // events), and if there are more than ACTIVITY_TOAST_UNDO_CAP render a
    // single "Undo all (N)" button that undoes them in order; otherwise emit
    // the per-target buttons as before.
    var undoableGroupIds = groups.filter(function (gid) {
      return byGroup[gid][0] && byGroup[gid][0].undoable;
    });
    var undoableStandalone = standalone.filter(function (a) { return a.undoable; });
    var totalUndoable = undoableGroupIds.length + undoableStandalone.length;
    function appendUndoButton(label, handler) {
      var link = el("button", { type: "button", class: "okf-toast__action", text: label });
      link.addEventListener("click", function (e) {
        e.preventDefault();
        handler(link);
      });
      span.appendChild(document.createTextNode(" "));
      span.appendChild(link);
    }
    if (totalUndoable > ACTIVITY_TOAST_UNDO_CAP) {
      // Single coalesced button: undo every group + every standalone, in the
      // order they arrived. The buttons disable as each undo fires so a
      // double-click can't re-trigger.
      appendUndoButton("Undo all (" + totalUndoable + ")", function (btn) {
        btn.disabled = true;
        undoableGroupIds.forEach(function (gid) { undoGroup(gid, btn); });
        undoableStandalone.forEach(function (a) { undoOne(a, btn); });
      });
    } else {
      // Inline undo buttons (CRI-018): undo directly, don't open the panel.
      undoableGroupIds.forEach(function (gid) {
        var members = byGroup[gid];
        appendUndoButton("Undo group (" + members.length + ")", function () { undoGroup(gid); });
      });
      undoableStandalone.forEach(function (a) {
        appendUndoButton("Undo", function () { undoOne(a); });
      });
    }
    // A non-undoable batch still offers a "View" link to the change list.
    const anyUndoable = batch.some((a) => a.undoable);
    if (!anyUndoable) {
      const view = el("button", { type: "button", class: "okf-toast__action", text: "View" });
      view.addEventListener("click", (e) => { e.preventDefault(); openPanel("changes"); });
      span.appendChild(document.createTextNode(" "));
      span.appendChild(view);
    }
    toast(span, { ttl: 5000 });
  }
  function buildActivitySummary(batch, groups, byGroup, standalone) {
    // Browser-proof copy example: "Agent added 2 links to Orders · Undo". We build a
    // compact human line mapping the raw op id to a human verb + noun,
    // resolving concept ids to titles where possible. iter-1 closeout
    // visual review caught the earlier shape that surfaced the raw op id
    // ("add_links") and the raw concept id ("tables/orders") verbatim.
    const actorRaw = (batch[0] && batch[0].actor) || "agent";
    const actor = actorRaw.charAt(0).toUpperCase() + actorRaw.slice(1);
    const titleFor = (id) => {
      // Best-effort: title-case the last path segment of the concept id so
      // "tables/orders" → "Orders". The studio does not carry the bundle's
      // full title map; a CRDT-precise lookup would require an extra round
      // trip per toast, which is not worth the latency for a notification.
      if (!id || typeof id !== "string") return "";
      const seg = id.split("/").filter(Boolean).pop() || id;
      return seg.charAt(0).toUpperCase() + seg.slice(1);
    };
    if (batch.length === 1) {
      const a = batch[0];
      if (a.summary && !/_/.test(a.summary)) return a.summary;
      const hv = humanVerb(a.action, 1);
      const tgt = a.ids && a.ids[0] ? (" to " + titleFor(a.ids[0])) : "";
      return actor + " " + hv.phrase + tgt;
    }
    // Multiple: collapse by action verb.
    const verbCounts = Object.create(null);
    const targetSet = Object.create(null);
    batch.forEach((a) => {
      const verb = a.action || "change";
      verbCounts[verb] = (verbCounts[verb] || 0) + 1;
      (a.ids || []).forEach((id) => { targetSet[id] = 1; });
    });
    const targets = Object.keys(targetSet);
    const parts = Object.keys(verbCounts).map((v) => {
      const hv = humanVerb(v, verbCounts[v]);
      return hv.count + " " + hv.noun;
    });
    const targetText = targets.length === 1
      ? (" to " + titleFor(targets[0]))
      : (" to " + targets.length + " concepts");
    return actor + " added " + parts.join(", ") + targetText;
  }
  // Map raw op ids to human verb + noun forms. The shape is:
  //   {phrase: "<past-tense verb>", noun: "<plural noun>", count: N}
  // `phrase` is used in the singular case ("Agent added a link"),
  // `noun` is used in the collapsed multi case ("3 links").
  // Unknown ops fall back to "changed"/"changes" so we never surface a
  // raw snake_case token to the user (fit-and-finish rule).
  function humanVerb(action, count) {
    const map = {
      add_link:           { phrase: "added a link",     noun: "links"    },
      add_links:          { phrase: "added links",      noun: "links"    },
      remove_link:        { phrase: "removed a link",   noun: "removals" },
      add_relation:       { phrase: "added a relation", noun: "relations"},
      add_entity:         { phrase: "added an entity",  noun: "entities" },
      add_entities:       { phrase: "added entities",   noun: "entities" },
      set_frontmatter:    { phrase: "set a field",      noun: "fields"   },
      set_tag:            { phrase: "set a tag",        noun: "tags"     },
      add_tag:            { phrase: "added a tag",      noun: "tags"     },
      append_body_section:{ phrase: "added a section",  noun: "sections" },
      write_concept:      { phrase: "wrote the concept",noun: "writes"   },
      undo_restore:       { phrase: "undid a change",   noun: "undos"    },
      repair:             { phrase: "ran a repair",     noun: "repairs"  },
      auto_repair:        { phrase: "auto-repaired",    noun: "repairs"  },
    };
    const entry = map[action] || { phrase: "made a change", noun: "changes" };
    return { phrase: entry.phrase, noun: entry.noun, count: count };
  }
  function verbLabel(verb, count) {
    // Crude pluralisation good enough for the known action set.
    if (count === 1) return verb;
    if (verb.endsWith("s")) return verb;
    if (verb.endsWith("y")) return verb.slice(0, -1) + "ies";
    return verb + "s";
  }

  // iter2 G12: burst-coalescing detection. When 10+ standalone events land
  // within 1s, tag them with a shared burst_id so the change list can render
  // them as ONE expandable "Agent made N changes" row instead of N rows at
  // the top. Grouped events (group_id) are already coalesced by the group
  // renderer, so burst detection only applies to standalone events. An OPEN
  // burst keeps absorbing fast arrivals (so a burst of 12 coalesces all 12,
  // not 10 + 2); it closes when an event arrives >1s after the last one.
  const BURST_WINDOW_MS = 1000;
  const BURST_THRESHOLD = 10;
  let _burstArrivalWindow = [];  // [{id, t}]
  let _burstOpenId = null;
  let _burstLastT = 0;
  function maybeTagBurst(ev) {
    if (!ev || ev.group_id || !ev.id) return;
    const now = (ev.ts && !isNaN(Date.parse(ev.ts))) ? Date.parse(ev.ts) : Date.now();
    // Close any open burst if this event arrived after the 1s quiet gap.
    if (now - _burstLastT > BURST_WINDOW_MS) {
      _burstArrivalWindow = [];
      _burstOpenId = null;
    }
    _burstLastT = now;
    if (_burstOpenId) {
      // A burst is open: keep tagging fast arrivals into it directly.
      if (!ev.burst_id) ev.burst_id = _burstOpenId;
      return;
    }
    _burstArrivalWindow.push({ id: ev.id, t: now });
    if (_burstArrivalWindow.length >= BURST_THRESHOLD) {
      _burstOpenId = "burst-" + now;
      _burstArrivalWindow.forEach((a) => {
        const e = state.events.find((x) => x.id === a.id);
        if (e && !e.burst_id && !e.group_id) e.burst_id = _burstOpenId;
      });
      _burstArrivalWindow = [];  // window served its purpose; open burst tags directly
    }
  }

  function upsertEvent(ev) {
    if (!ev) return;
    const id = ev.id;
    if (id) {
      const i = state.events.findIndex((e) => e.id === id);
      if (i >= 0) { state.events[i] = ev; maybeTagBurst(ev); return; }
    }
    state.events.unshift(ev);
    maybeTagBurst(ev);
    // iter2 G9: raised from 4000 to 10000 to align with the change-list
    // virtualization ceiling (CHANGE_TOTAL_CAP). The virtualized list keeps
    // only ~80 rows in the DOM regardless, so a larger in-memory log no
    // longer means a larger DOM. Events beyond the cap rotate out (oldest
    // first); the full history lives in events.jsonl on disk.
    if (state.events.length > 10000) state.events.length = 10000;
  }

  // ====================================================================
  // 13. Boot
  // ====================================================================
  function debounce(fn, ms) {
    let t = null;
    return function () {
      const ctx = this, args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(() => fn.apply(ctx, args), ms);
    };
  }

  // ====================================================================
  // 9.4. Conflict-UX modal (INTENT-008) — disk-vs-agent write collision
  // ====================================================================
  // When /__apply returns 409 with {conflict: true, concept, expected_rev,
  // current_rev}, surface a modal asking the user how to resolve. Three
  // actions:
  //   * "View diff"     — open a panel that fetches /__diff and renders rows.
  //   * "Keep mine"     — dismiss (the on-disk bytes stay; the agent's write
  //                       is dropped).
  //   * "Take the agent's" — re-submit the apply WITHOUT expected_rev (force
  //                       overwrite). tokenFetch's caller sees the new Response.
  //
  // ARIA (§13.5): role="alertdialog" + aria-labelledby + focus trap + Esc.
  // Reduced-motion aware (no jarring animation; the modal simply appears).
  let conflictState = { open: false, overlay: null, lastFocus: null };

  function showConflictModal({ data, url, opts, originalBodyObj, originalResponse }) {
    // Build the overlay once; reuse across conflicts.
    if (!conflictState.overlay) _buildConflictModal();
    data = data || {};
    const concept = String(data.concept || "");
    // CLI-agent conflicts (SSE `agent_conflict`) carry no retry context —
    // there is no browser-side request to re-submit, so "Take the agent's
    // edit" is meaningless there and stays hidden. HTTP-409 conflicts
    // (tokenFetch) pass url/opts and get all three actions.
    const takeBtnEl = $(".okf-conflict__take", conflictState.overlay);
    if (takeBtnEl) takeBtnEl.hidden = !url;
    const headingId = "okf-conflict-title";
    const heading = $("#" + headingId, conflictState.overlay) ||
      conflictState.overlay.querySelector("." + conflictState.overlay.getAttribute("aria-labelledby"));
    if (heading) heading.textContent = "You edited " + concept + " in your editor while the agent was updating it.";
    // Reset the diff panel + button states.
    const diffPanel = $(".okf-conflict__diff", conflictState.overlay);
    if (diffPanel) {
      diffPanel.innerHTML = "";
      diffPanel.setAttribute("hidden", "");
    }
    const diffBtn = $(".okf-conflict__diff-btn", conflictState.overlay);
    if (diffBtn) diffBtn.disabled = false;
    conflictState.open = true;
    conflictState.overlay.hidden = false;
    conflictState.lastFocus = document.activeElement;
    // Focus the first action button after a tick (let the modal render).
    setTimeout(() => {
      const first = focusableIn(conflictState.overlay)[0];
      if (first) try { first.focus({ preventScroll: true }); } catch (e) {}
    }, 20);
    // Resolve the caller's promise once the user picks an action.
    return new Promise((resolve) => {
      conflictState._resolve = resolve;
      conflictState._originalResponse = originalResponse;
      conflictState._retryArgs = { url, opts, originalBodyObj, data };
    });
  }

  function _buildConflictModal() {
    const overlay = el("div", {
      class: "okf-conflict-overlay", hidden: "",
      role: "alertdialog", "aria-modal": "true",
      "aria-labelledby": "okf-conflict-title",
    });
    const card = el("div", { class: "okf-conflict" });
    const title = el("h2", {
      class: "okf-conflict__title", id: "okf-conflict-title",
      text: "Conflict",  // replaced per-show with the concept-named copy
    });
    const lede = el("p", {
      class: "okf-conflict__lede",
      // Honest, non-jargon phrasing. The user just needs to pick a resolution.
      text: "Pick how to resolve this edit.",
    });
    const actions = el("div", { class: "okf-conflict__actions" });
    const viewBtn = el("button", {
      type: "button", class: "okf-studiobtn okf-conflict__diff-btn",
      text: "View diff",
    });
    // iter2 CRI2-008: "Keep mine" is the primary action (the non-destructive
    // default; the user's on-disk edit stays). "Take the agent's" discards
    // the user's edit and applies the agent's, so it is visually distinct
    // (warn-toned) but NOT primary, per platform HIG guidance that
    // destructive options not be the path of least visual resistance.
    const keepBtn = el("button", {
      type: "button", class: "okf-studiobtn okf-studiobtn--primary",
      text: "Keep mine",
    });
    const takeBtn = el("button", {
      type: "button", class: "okf-studiobtn okf-conflict__take",
      // iter3 CRI3-003: was "Take the agent's" (dangling possessive; read
      // as a typo). The full verb-phrase "Take the agent's edit" pairs
      // cleanly with "Keep mine" — both are now complete predicates.
      text: "Take the agent's edit",
      title: "Discards your on-disk edit; the agent's version is applied.",
    });
    const diffPanel = el("div", {
      class: "okf-conflict__diff", "aria-live": "polite",
      "aria-label": "Line diff between your edit and the agent's", hidden: "",
    });
    actions.appendChild(viewBtn);
    actions.appendChild(keepBtn);
    actions.appendChild(takeBtn);
    card.appendChild(title);
    card.appendChild(lede);
    card.appendChild(actions);
    card.appendChild(diffPanel);
    overlay.appendChild(card);

    // "View diff": fetch /__diff?concept=…&from=…&to=… and render rows.
    // iter3 SEC3-001: /__diff requires the per-session X-OKF-Token header
    // (server.py:_handle_diff enforces it via _check_write_auth, returning
    // 403 otherwise). The bare fetch() iter-2 shipped here was missing the
    // token, so every "View diff" click in the §9.4 conflict modal returned
    // 403 and the catch handler rendered the "Diff fetch failed" fallback
    // (which the iter-1 browser test asserted as "non-empty" — a mask).
    // tokenFetch is the studio's write-fetch wrapper that attaches the
    // token from the embedded BOOT payload; using it here matches every
    // other authenticated fetch in the studio (apply, undo, presence,
    // comment).
    viewBtn.addEventListener("click", async () => {
      viewBtn.disabled = true;
      diffPanel.innerHTML = "";
      diffPanel.removeAttribute("hidden");
      const placeholder = el("div", { class: "okf-conflict__diff-loading", text: "Loading diff…" });
      diffPanel.appendChild(placeholder);
      try {
        const { data: cdata } = conflictState._retryArgs;
        const concept = encodeURIComponent(String(cdata.concept || ""));
        const fromRev = encodeURIComponent(String(cdata.expected_rev || ""));
        const toRev = encodeURIComponent(String(cdata.current_rev || ""));
        const res = await tokenFetch(
          "/__diff?concept=" + concept + "&from=" + fromRev + "&to=" + toRev,
          { headers: { Accept: "application/json" } },
        );
        const payload = await res.json();
        placeholder.remove();
        if (!payload || payload.ok === false) {
          diffPanel.appendChild(el("p", {
            class: "okf-conflict__diff-empty",
            text: payload && payload.error ? payload.error : "Diff unavailable.",
          }));
          return;
        }
        const rows = Array.isArray(payload.diff) ? payload.diff : [];
        if (!rows.length) {
          diffPanel.appendChild(el("p", {
            class: "okf-conflict__diff-empty",
            text: "No textual differences.",
          }));
          return;
        }
        const table = el("table", { class: "okf-conflict__diff-table" });
        const tbody = el("tbody");
        rows.forEach((row) => {
          const tr = el("tr", { class: "okf-conflict__diff-row okf-conflict__diff-row--" + (row.kind || "ctx") });
          tr.appendChild(el("td", { class: "okf-conflict__diff-num", text: String(row.num != null ? row.num : "") }));
          tr.appendChild(el("td", { class: "okf-conflict__diff-kind", text: row.kind === "add" ? "+" : (row.kind === "del" ? "-" : " ") }));
          const td = el("td", { class: "okf-conflict__diff-text" });
          td.textContent = String(row.text != null ? row.text : "");
          tr.appendChild(td);
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        diffPanel.appendChild(table);
      } catch (e) {
        placeholder.remove();
        diffPanel.appendChild(el("p", {
          class: "okf-conflict__diff-empty",
          text: "Diff fetch failed: " + (e && e.message ? e.message : String(e)),
        }));
      } finally {
        // Allow re-clicking to refresh.
        setTimeout(() => { viewBtn.disabled = false; }, 500);
      }
    });

    // "Keep mine": dismiss, the on-disk bytes stay. Resolve with the
    // original 409 Response so the caller sees !ok and knows the write
    // did not land.
    keepBtn.addEventListener("click", () => _closeConflict("keep"));

    // "Take the agent's": re-submit the apply WITHOUT expected_rev.
    takeBtn.addEventListener("click", async () => {
      takeBtn.disabled = true;
      keepBtn.disabled = true;
      viewBtn.disabled = true;
      try {
        const { url: rurl, opts: ropts, originalBodyObj: rbody } = conflictState._retryArgs;
        // Build a fresh body: drop expected_rev from a cloned object.
        let bodyObj = rbody;
        if (bodyObj && typeof bodyObj === "object") {
          bodyObj = Object.assign({}, bodyObj);
          delete bodyObj.expected_rev;
        }
        const rheaders = Object.assign({ "X-OKF-Token": TOKEN }, ropts.headers || {});
        if (bodyObj) {
          rheaders["Content-Type"] = "application/json";
        }
        const newRes = await fetch(rurl, {
          method: ropts.method || "POST",
          headers: rheaders,
          body: bodyObj ? JSON.stringify(bodyObj) : ropts.body,
        });
        _closeConflict("take", newRes);
      } catch (e) {
        // The retry itself failed; resolve with a synthetic 503 so the
        // caller sees an error rather than hanging.
        _closeConflict("take", new Response(JSON.stringify({ ok: false, error: String(e) }), {
          status: 503, headers: { "Content-Type": "application/json" },
        }));
      }
    });

    // Click outside the card dismisses (acts like "Keep mine").
    overlay.addEventListener("mousedown", (e) => {
      if (e.target === overlay) _closeConflict("keep");
    });

    // Global keyboard wiring (registered once).
    document.addEventListener("keydown", _conflictKeydown);

    document.body.appendChild(overlay);
    conflictState.overlay = overlay;
  }

  function _conflictKeydown(e) {
    if (!conflictState.open) return;
    if (e.key === "Escape") { e.preventDefault(); _closeConflict("keep"); }
    // Focus trap: Tab/Shift+Tab cycles inside the alertdialog.
    else if (e.key === "Tab") {
      e.preventDefault();
      trapFocusIn(conflictState.overlay, !e.shiftKey);
    }
  }

  function _closeConflict(action, newResponse) {
    if (!conflictState.open) return;
    conflictState.open = false;
    conflictState.overlay.hidden = true;
    // Re-enable buttons for the next conflict.
    $$("button", conflictState.overlay).forEach((b) => { b.disabled = false; });
    // Restore focus to the trigger.
    if (conflictState.lastFocus && typeof conflictState.lastFocus.focus === "function") {
      try { conflictState.lastFocus.focus({ preventScroll: true }); } catch (e) {}
    }
    conflictState.lastFocus = null;
    const resolve = conflictState._resolve;
    conflictState._resolve = null;
    if (!resolve) return;
    if (action === "take" && newResponse) {
      resolve(newResponse);
    } else {
      // "Keep" / Esc / outside-click → the original 409 Response.
      resolve(conflictState._originalResponse);
    }
    conflictState._originalResponse = null;
    conflictState._retryArgs = null;
  }

  // ---- sidebar panel system (atlas composition: Related + Sections only;
  // Quick Actions demoted to a single Ask-agent entry under the TOC) ----
  var SIDEBAR_KEY = "okf:sidebar";
  var SIDEBAR_PANELS = ["related", "sections"];
  var INTENTS = [
    { id: "ask-agent", label: "Ask agent\u2026", prompt: "" },
    { id: "add-section", label: "Add section", prompt: "Add a new section about" },
    { id: "add-links", label: "Add links", prompt: "Add links from this concept to" },
  ];

  function getSidebarState() {
    try {
      var s = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || "{}");
      if (!s.order || !Array.isArray(s.order)) s.order = SIDEBAR_PANELS.slice();
      // Drop legacy "intents" panel from saved orders (demoted to Ask agent).
      s.order = s.order.filter(function (id) { return id === "related" || id === "sections"; });
      SIDEBAR_PANELS.forEach(function (id) {
        if (s.order.indexOf(id) < 0) s.order.push(id);
      });
      if (!s.collapsed) s.collapsed = {};
      if (!s.width) s.width = 260;
      return s;
    } catch (e) { return { order: SIDEBAR_PANELS.slice(), collapsed: {}, width: 260 }; }
  }
  function saveSidebarState(s) {
    try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify(s)); } catch (e) {}
  }

  function buildSidebarPanels() {
    var sidebar = $(".okf-page__sidebar");
    if (!sidebar) return;
    var sbState = getSidebarState();
    document.documentElement.style.setProperty("--okf-sidebar-w", sbState.width + "px");

    var existingGraph = $(".okf-local-graph", sidebar);

    sidebar.innerHTML = "";

    sbState.order.forEach(function (panelId) {
      var panel = buildPanel(panelId, sbState, existingGraph);
      if (panel) sidebar.appendChild(panel);
    });

    // Demoted Ask-agent entry (replaces the equal-weight Quick Actions panel).
    var askWrap = el("div", { class: "okf-sidebar-ask" });
    var askBtn = el("button", {
      type: "button",
      class: "okf-sidebar-ask__btn",
      text: "Ask agent\u2026",
      title: "Open the comment composer to direct the agent",
    });
    askBtn.addEventListener("click", function () {
      state.draftBody = "";
      state.draftAnchor = { kind: "concept", ref: state.conceptId, concept: state.conceptId };
      openPanel("comments", { focusComposer: true });
    });
    askWrap.appendChild(askBtn);
    sidebar.appendChild(askWrap);

    wireSidebarDnD(sidebar, sbState);
    if (window.okfWiki && window.okfWiki.renderLocalGraph) {
      try { window.okfWiki.renderLocalGraph(); } catch (e) {}
    }
  }

  function buildPanel(panelId, sbState, existingGraph) {
    var isCollapsed = !!sbState.collapsed[panelId];
    var panel = el("div", {
      class: "okf-sidebar-panel" + (isCollapsed ? " okf-sidebar-panel--collapsed" : ""),
      "data-panel-id": panelId,
      draggable: "true",
    });
    var header = el("div", { class: "okf-sidebar-panel__header" });
    var toggle = el("button", {
      class: "okf-sidebar-panel__toggle",
      type: "button",
      "aria-label": isCollapsed ? "Expand" : "Collapse",
      text: isCollapsed ? "+" : "-",
    });
    toggle.addEventListener("click", function () {
      var p = panel.classList.toggle("okf-sidebar-panel--collapsed");
      toggle.textContent = p ? "+" : "-";
      toggle.setAttribute("aria-label", p ? "Expand" : "Collapse");
      sbState.collapsed[panelId] = p;
      saveSidebarState(sbState);
    });
    header.appendChild(toggle);
    header.appendChild(el("span", { class: "okf-sidebar-panel__title", text: sidebarPanelTitle(panelId) }));
    panel.appendChild(header);

    var body = el("div", { class: "okf-sidebar-panel__body" });
    if (panelId === "related") {
      if (existingGraph) body.appendChild(existingGraph);
    } else if (panelId === "sections") {
      buildSectionsPanel(body);
    }
    panel.appendChild(body);
    return panel;
  }

  function sidebarPanelTitle(id) {
    return ({ related: "Neighborhood", sections: "On this page" })[id] || id;
  }

  function buildSectionsPanel(body) {
    var headings = $$("h1, h2, h3, h4, h5, h6", $(".okf-page__body") || document);
    if (headings.length === 0) {
      body.appendChild(el("p", { class: "okf-fg-muted", text: "No sections." }));
      return;
    }
    var ul = el("ul", { class: "okf-sidebar-toc" });
    headings.forEach(function (h) {
      var li = el("li", { class: "toc-" + h.tagName.toLowerCase() });
      var a = el("a", { href: "#" + (h.id || ""), text: (h.textContent || "").trim().slice(0, 50) });
      a.addEventListener("click", function (e) {
        e.preventDefault();
        if (h.id) {
          var target = document.getElementById(h.id);
          if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
      li.appendChild(a);
      ul.appendChild(li);
    });
    body.appendChild(ul);
    wireTocScrollSpy(ul, headings);
  }

  // Phase 2: scroll-spy — the TOC entry whose section is currently on
  // screen carries .is-active. One shared observer per panel build; the
  // previous observer (pre-SSE-patch rebuild) is disconnected so patches
  // don't stack observers.
  var _tocObserver = null;
  function wireTocScrollSpy(ul, headings) {
    if (!("IntersectionObserver" in window)) return;
    if (_tocObserver) { _tocObserver.disconnect(); _tocObserver = null; }
    var links = $$("a", ul);
    var byId = {};
    links.forEach(function (a) {
      var id = (a.getAttribute("href") || "").slice(1);
      if (id) byId[id] = a;
    });
    function activate(id) {
      links.forEach(function (a) { a.classList.remove("is-active"); });
      if (byId[id]) byId[id].classList.add("is-active");
    }
    // Track which headings are intersecting; the topmost wins.
    var visible = {};
    _tocObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.target.id) return;
        visible[en.target.id] = en.isIntersecting;
      });
      for (var i = 0; i < headings.length; i++) {
        if (headings[i].id && visible[headings[i].id]) { activate(headings[i].id); return; }
      }
    }, { rootMargin: "-10% 0px -70% 0px" });
    headings.forEach(function (h) { if (h.id) _tocObserver.observe(h); });
  }

  function buildIntentsPanel(body) {
    // Legacy helper kept for any extension that still calls it; the default
    // sidebar no longer mounts an intents panel.
    var container = el("div", { class: "okf-sidebar-intents" });
    INTENTS.forEach(function (intent) {
      var btn = el("button", { class: "okf-sidebar-intent", type: "button", text: intent.label });
      btn.addEventListener("click", function () {
        state.draftBody = (intent.prompt ? intent.prompt + " " : "");
        state.draftAnchor = { kind: "concept", ref: state.conceptId, concept: state.conceptId };
        openPanel("comments", { focusComposer: true });
      });
      container.appendChild(btn);
    });
    body.appendChild(container);
  }

  function wireSidebarDnD(sidebar, sbState) {
    var dragSrc = null;
    sidebar.addEventListener("dragstart", function (e) {
      var panel = e.target.closest(".okf-sidebar-panel");
      if (!panel) return;
      dragSrc = panel;
      panel.classList.add("okf-sidebar-panel--dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    sidebar.addEventListener("dragend", function (e) {
      var panel = e.target.closest(".okf-sidebar-panel");
      if (panel) panel.classList.remove("okf-sidebar-panel--dragging");
      $$(".okf-sidebar-panel--drag-target", sidebar).forEach(function (p) {
        p.classList.remove("okf-sidebar-panel--drag-target");
      });
    });
    sidebar.addEventListener("dragover", function (e) {
      e.preventDefault();
      var panel = e.target.closest(".okf-sidebar-panel");
      if (!panel || panel === dragSrc) return;
      $$(".okf-sidebar-panel--drag-target", sidebar).forEach(function (p) {
        p.classList.remove("okf-sidebar-panel--drag-target");
      });
      panel.classList.add("okf-sidebar-panel--drag-target");
    });
    sidebar.addEventListener("drop", function (e) {
      e.preventDefault();
      var target = e.target.closest(".okf-sidebar-panel");
      if (!target || !dragSrc || target === dragSrc) return;
      // Insert dragSrc before or after target based on drop position.
      var rect = target.getBoundingClientRect();
      var after = (e.clientY - rect.top) > rect.height / 2;
      if (after) {
        target.parentNode.insertBefore(dragSrc, target.nextSibling);
      } else {
        target.parentNode.insertBefore(dragSrc, target);
      }
      // Save new order.
      sbState.order = $$(".okf-sidebar-panel", sidebar).map(function (p) {
        return p.getAttribute("data-panel-id");
      });
      saveSidebarState(sbState);
    });
  }

  function boot() {
    mountBar();
    wirePaletteKeys();
    if (isConceptPage()) {
      ensureViewWrap();
      setView(state.view); // also deep-links + lazy-loads source if needed
      bindSelectionAffordance();
      buildSidebarPanels();
    } else if (document.getElementById("detail-body")) {
      // Graph page: bind selection affordance for the detail panel.
      bindSelectionAffordance();
    }
    // iter1 CRI-004: stamp data-concept-id on list/search rows so presence
    // focus can highlight them (also runs on index/search pages).
    stampConceptIds();
    loadGraph().then(buildIndexEngagement);
    loadComments().then(buildIndexEngagement);
    wireLive();
    // iter1 CRI-002: apply text-range marks for any pre-existing comments
    // on the open concept (loaded by loadComments above).
    applyCommentMarks();
    rebuildMarginMarkers();
    // Initial presence fetch (best-effort; server may have none).
    tokenFetch("/__presence", { method: "POST", body: { state: "idle", actor: "user" } }).catch(() => {});
    // Apply bootstrap presence highlight if focus present.
    renderPresence(state.presence);
    // Rebuild markers after fonts/layout settle.
    setTimeout(rebuildMarginMarkers, 400);
  }

  // Expose the public API.
  window.okfLoomStudio = {
    register,
    applyDoc,
    refreshGraph,
    openPanel,
    closePanel,
    openPalette,
    setView,
    get state() { return state; },
    get panels() { return panels; },
    ctx,
    // Test/debug helpers.
    _toast: toast, _loadComments: loadComments, _renderChangeList: renderChangeList,
    _showConflictModal: (payload) => showConflictModal({
      data: payload,
      url: "/__apply",
      opts: { method: "POST" },
      originalBodyObj: { kind: "add_tag", target: payload && payload.concept, args: { tag: "x" } },
      originalResponse: new Response(JSON.stringify(payload || {}), { status: 409 }),
    }),
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
