/* OKF wiki viewer - client-side enhancements for the served/built pages.
 *
 * Three concerns:
 *   1. Theme cycle button — light/dark/pastel/sepia/midnight (persist to
 *      localStorage['okf-theme']).
 *   2. Search-as-you-type on the topbar search box (debounced; hits /__search
 *      and renders results inline OR navigates on Enter). Disabled in static
 *      builds (no /__search backend) - a notice replaces live results (P2-65).
 *   3. Hover/focus popover preview for internal links: fetches /__raw/<id> on
 *      hover OR keyboard focus, shows title + description + first lines.
 *      role="tooltip" + aria-describedby linkage (P2-70).
 *
 * Loaded via <script defer>. No CDN deps. Degrades gracefully if a route
 * is missing (e.g. on a static site without /__search).
 */
(function () {
  "use strict";

  var STORAGE_KEY = "okf-theme";
  var themeBtn = document.getElementById("okf-theme");
  var REDUCED_MOTION = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Theme cycle order + button glyphs. KEEP IN SYNC with the copies in
  // graph.js / studio.js and render.py:_theme_button_html — each context
  // loads without the others (single-file viewer, static build, studio).
  var THEMES = ["light", "dark", "pastel", "sepia", "midnight"];
  var THEME_GLYPHS = { light: "☀", dark: "☾", pastel: "✿", sepia: "☕", midnight: "★" };

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "light";
  }
  function applyTheme(t) {
    if (THEMES.indexOf(t) < 0) t = "light";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(STORAGE_KEY, t); } catch (e) {}
    if (themeBtn) {
      themeBtn.textContent = THEME_GLYPHS[t];
      themeBtn.setAttribute("title", "Theme: " + t + " — click to cycle");
      themeBtn.setAttribute("aria-label", "Change colour theme (current: " + t + ")");
      // The button cycles five themes now; it is no longer a two-state
      // toggle, so aria-pressed would be dishonest.
      themeBtn.removeAttribute("aria-pressed");
    }
  }
  // Honour saved preference on load (overrides server-side default).
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved && THEMES.indexOf(saved) >= 0) {
      applyTheme(saved);
    } else {
      // No saved preference: respect the OS preference on first visit.
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      if (mq && mq.matches) applyTheme("dark");
    }
  } catch (e) {}
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = (THEMES.indexOf(currentTheme()) + 1) % THEMES.length;
      applyTheme(THEMES[next]);
    });
  }

  // ---- Static-mode detection (P2-65) -----------------------------------
  // The server emits data-okf-enhance="1" for spa/serve and "0" for static
  // builds. Static builds have no /__search backend; live results would
  // silently 404. Detect once and show a notice instead of live search.
  var isStatic = document.body.getAttribute("data-okf-enhance") === "0";

  // ---- Local graph widget (concept page sidebar) -----------------------
  // P2-62: previously rendered unlabeled coloured dots (decorative). Now
  // renders a vertical pill list of 1-hop neighbour titles, each coloured
  // by type, with a colour swatch + truncated label. Keyboard accessible.
  // The data is embedded by the server in <div id="okf-local-graph" data-graph="...">.
  function renderLocalGraph() {
    var container = document.getElementById("okf-local-graph");
    if (!container) return;
    var raw = container.getAttribute("data-graph");
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data.nodes || !data.nodes.length) { container.style.display = "none"; return; }

    // P1-9: build neighbour URLs from a root-prefix + mode, embedded by
    // the server. Static concepts live at ``<rootPrefix><id>.html`` (e.g.
    // ``../tables/orders.html``); spa/serve concepts live at ``/<id>``.
    // Falls back to "/" + id when no prefix is embedded so the function
    // still works for older bundles / partial templates.
    var rootPrefix = container.getAttribute("data-root-prefix") || "";

    var nodes = data.nodes;
    var center = nodes.find(function (n) { return n.is_center; }) || nodes[0];
    var neighbors = nodes.filter(function (n) { return n !== center; });

    container.innerHTML = "";

    var title = document.createElement("p");
    title.className = "okf-local-graph__title";
    // P2-4 (iter-1): the widget is a flat labelled pill list of related
    // neighbours, not a graph. The title and the aside aria-label both say
    // "Related" so the affordance matches what the user sees. (The class
    // name .okf-local-graph is kept to avoid churning the CSS; only the
    // user-facing label was dishonest.)
    title.textContent = neighbors.length
      ? "Related (" + neighbors.length + ")"
      : "Related";
    container.appendChild(title);

    if (!neighbors.length) {
      var empty = document.createElement("p");
      empty.className = "okf-local-graph__empty";
      empty.textContent = "No linked concepts.";
      container.appendChild(empty);
      return;
    }

    neighbors.forEach(function (n) {
      var label = n.label || n.id;
      // P3-4: don't JS-truncate the label - `.okf-local-graph__label`
      // already ellipsizes via CSS text-overflow. Slicing on a character
      // boundary split surrogate pairs (emoji) and CJK clusters.
      // Use a <button> so it's keyboard-focusable and clickable uniformly.
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "okf-local-graph__node";
      btn.setAttribute("data-target", n.id);
      btn.setAttribute("title", label);   // full label on hover (native tooltip)
      btn.setAttribute("aria-label", "Open " + label);

      var swatch = document.createElement("span");
      swatch.className = "okf-local-graph__swatch";
      swatch.style.background = n.color || "var(--okf-accent)";
      btn.appendChild(swatch);

      var lbl = document.createElement("span");
      lbl.className = "okf-local-graph__label";
      lbl.textContent = label;
      btn.appendChild(lbl);

      var navigate = function (event) {
        event.preventDefault();
        // P1-9: static builds use <rootPrefix><id>.html; spa/serve use /<id>.
        var url = isStatic
          ? rootPrefix + n.id + ".html"
          : "/" + n.id;
        window.location.href = url;
      };
      btn.addEventListener("click", navigate);
      container.appendChild(btn);
    });
  }

  // ---- Search-as-you-type ----------------------------------------------
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var ctx = this, args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }

  var searchInputs = document.querySelectorAll('input[type="search"][name="q"]');
  searchInputs.forEach(function (input) {
    var form = input.form;
    if (!form) return;

    // P2-65: static builds serve a client-side searcher (static-search.js on
    // the search page). On non-search pages the live-suggest dropdown is not
    // wired (there is no /__search backend), so show a honest hint pointing
    // the user at the dedicated search page (which has the full client-side
    // searcher over __data/search.json).
    //
    // P2-9 (iter-3): skip the note entirely on the search page itself
    // (body.okf-viewer--search). The note exists to point users FROM other
    // pages TO the search page; on the search page the "Open search" link
    // would resolve to the page the user is already on, which is confusing
    // and redundant. The cross-file contract is: wiki.js owns the
    // non-search-page notice; static-search.js owns the live searcher on
    // the search page (and no longer needs to hideStaleNote() it).
    if (isStatic && !document.body.classList.contains("okf-viewer--search")) {
      var note = document.createElement("span");
      note.className = "okf-search-note";
      note.setAttribute("role", "note");
      var link = document.createElement("a");
      // Walk back to the bundle root: search forms are at root (__search) or
      // nested under subdirs (../ __search). Search page = the form action.
      link.href = input.form.getAttribute("action") || "./";
      var act = form.getAttribute("action") || "";
      if (act.indexOf("__search") >= 0) {
        link.href = act;
      }
      link.textContent = "Open search";
      // iter2 CRI2-002: give the link its own accessible name so screen
      // readers still announce the action when the mobile CSS hides the
      // surrounding prose (under 600px only the chip remains).
      link.setAttribute("aria-label", "Open the dedicated search page");
      // Wrap the prose text in labeled spans so the mobile stylesheet can
      // hide just the prose and keep the link chip (wiki.css @media 600px).
      var prose1 = document.createElement("span");
      prose1.className = "okf-search-note__prose";
      prose1.appendChild(document.createTextNode("Press Enter to search, or "));
      note.appendChild(prose1);
      note.appendChild(link);
      var prose2 = document.createElement("span");
      prose2.className = "okf-search-note__prose";
      prose2.appendChild(document.createTextNode("."));
      note.appendChild(prose2);
      if (input.parentNode) {
        input.parentNode.appendChild(note);
      }
      return;
    }

    // Create a live results container under the input if missing.
    var live = document.createElement("div");
    live.className = "okf-search-live";
    // Ordinary labelled list of navigation links — NOT a composite listbox.
    // A listbox/combobox would owe arrow-key + aria-activedescendant behaviour
    // we do not implement; these suggestions are plain links the user Tabs
    // through, with Escape to dismiss. The roles now match that behaviour.
    live.setAttribute("role", "list");
    live.setAttribute("aria-label", "Search suggestions");
    live.style.display = "none";
    input.parentNode.style.position = "relative";
    input.parentNode.appendChild(live);

    var run = debounce(function (q) {
      if (!q || q.length < 2) { live.style.display = "none"; live.innerHTML = ""; return; }
      var url = "/__search?q=" + encodeURIComponent(q) + "&format=json";
      fetch(url, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (items) { renderLive(live, items); })
        .catch(function () { live.style.display = "none"; });
    }, 180);

    // No aria-autocomplete / aria-controls / role=combobox on the input: those
    // promise a composite widget (arrow navigation into a listbox) that this
    // plain-links suggestion list does not implement.
    if (!live.id) live.id = "okf-search-live";

    input.addEventListener("input", function () { run(input.value.trim()); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { live.style.display = "none"; live.innerHTML = ""; }
    });
    document.addEventListener("click", function (e) {
      if (!input.parentNode.contains(e.target)) live.style.display = "none";
    });
  });

  function renderLive(container, items) {
    if (!items || !items.length) { container.style.display = "none"; return; }
    container.innerHTML = "";
    items.slice(0, 8).forEach(function (it) {
      var li = document.createElement("div");
      li.setAttribute("role", "listitem");
      var a = document.createElement("a");
      a.href = "/" + (it.concept_id || it.id);
      var title = document.createElement("strong");
      title.textContent = it.title || it.concept_id || it.id;
      var sub = document.createElement("span");
      sub.className = "okf-muted";
      sub.textContent = " " + (it.concept_id || it.id);
      a.appendChild(title);
      a.appendChild(sub);
      if (it.description || it.snippet) {
        var d = document.createElement("div");
        d.className = "okf-muted";
        d.style.fontSize = "0.8rem";
        d.textContent = (it.description || (it.snippets && it.snippets[0]) || "").slice(0, 120);
        a.appendChild(d);
      }
      a.style.display = "block";
      a.style.padding = "4px 8px";
      a.style.borderBottom = "1px solid var(--okf-border, #e2e8f0)";
      li.appendChild(a);
      container.appendChild(li);
    });
    container.style.display = "block";
    container.style.position = "absolute";
    container.style.top = "100%";
    container.style.left = "0";
    container.style.right = "0";
    container.style.background = "var(--okf-bg-elev, #fff)";
    container.style.border = "1px solid var(--okf-border-strong, #cbd5e1)";
    container.style.borderRadius = "4px";
    container.style.boxShadow = "0 4px 12px rgba(0,0,0,0.12)";
    container.style.zIndex = "30";
    container.style.maxHeight = "60vh";
    container.style.overflowY = "auto";
  }

  // ---- Hover/focus popovers on internal links (P2-70) ------------------
  // role="tooltip" + aria-describedby. Triggered on hover (mouse) AND
  // focus (keyboard). Delay reduced to ~200ms (P2-70).
  var popover = null;
  var popoverTimer = null;
  var popoverId = "okf-popover";
  // popoverCache was unused fetch memoisation; removed (P3-9).

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement("div");
    popover.className = "okf-popover";
    popover.id = popoverId;
    popover.setAttribute("role", "tooltip");
    popover.style.display = "none";
    document.body.appendChild(popover);
    return popover;
  }

  // Hover sequence token: hovering quickly across several links fires
  // overlapping fetches, and a slow EARLIER response could otherwise
  // populate the popover for the wrong link. Only the latest
  // hover's response may render; hidePopover also invalidates in-flight
  // responses so a popover can't reappear after mouseout.
  var popoverSeq = 0;
  function showPopover(target, x, y) {
    var seq = ++popoverSeq;
    fetch("/__raw/" + target)
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (raw) {
        if (raw == null || seq !== popoverSeq) return;
        // Parse title + description from the frontmatter-ish prefix.
        var fm = parseFrontmatter(raw);
        var p = ensurePopover();
        p.innerHTML = "";
        var typeEl = document.createElement("div");
        typeEl.className = "okf-popover__type";
        typeEl.textContent = fm.type || "concept";
        var titleEl = document.createElement("h4");
        titleEl.textContent = fm.title || target;
        p.appendChild(typeEl);
        p.appendChild(titleEl);
        if (fm.description) {
          var d = document.createElement("div");
          d.className = "okf-popover__body";
          d.textContent = fm.description;
          p.appendChild(d);
        }
        p.style.display = "block";
        // Position near cursor (or focus point), clamped to viewport.
        var rect = p.getBoundingClientRect();
        var px = Math.min((x || 12) + 12, window.innerWidth - rect.width - 12);
        var py = Math.min((y || 12) + 12, window.innerHeight - rect.height - 12);
        p.style.left = Math.max(12, px) + "px";
        p.style.top = Math.max(12, py) + "px";
      })
      .catch(function () {});
  }

  function hidePopover() {
    popoverSeq++; // invalidate any in-flight popover fetch
    if (popover) popover.style.display = "none";
  }

  function parseFrontmatter(raw) {
    var out = {};
    if (raw.slice(0, 4) !== "---\n") return out;
    var end = raw.indexOf("\n---", 4);
    if (end < 0) return out;
    var block = raw.slice(4, end);
    block.split(/\n/).forEach(function (line) {
      var m = line.match(/^([A-Za-z_][\w-]*):\s*(.*)$/);
      if (!m) return;
      var k = m[1], v = m[2].replace(/^['"]|['"]$/g, "");
      if (k === "tags") v = v.replace(/^\[|\]$/g, "").split(",").map(function (s) { return s.trim(); });
      out[k] = v;
    });
    return out;
  }

  function scheduleShow(target, x, y) {
    if (popoverTimer) clearTimeout(popoverTimer);
    var delay = REDUCED_MOTION ? 0 : 200;   // P2-70: ~200ms (was 350ms)
    popoverTimer = setTimeout(function () {
      showPopover(target, x, y);
    }, delay);
  }
  function cancelShow() {
    if (popoverTimer) clearTimeout(popoverTimer);
    setTimeout(hidePopover, 80);
  }

  // Attach hover AND focus handlers to all internal links (P2-70).
  function bindLinkHovers() {
    var links = document.querySelectorAll('a.okf-internal, a[href^="/"]:not([href="/"])');
    links.forEach(function (a) {
      var href = a.getAttribute("href") || "";
      // Only internal concept URLs of the form /<seg>/<seg>(...)
      if (!/^\/[A-Za-z0-9_][A-Za-z0-9_.\-/]*$/.test(href)) return;
      if (href.indexOf("/__") === 0) return;
      if (href === "/") return;
      var target = href.replace(/^\//, "");
      // aria-describedby links the trigger to its tooltip (P2-70).
      a.setAttribute("aria-describedby", popoverId);
      a.addEventListener("mouseenter", function (e) {
        scheduleShow(target, e.clientX, e.clientY);
      });
      a.addEventListener("mouseleave", cancelShow);
      // Keyboard parity (P2-70): focus shows, blur hides.
      a.addEventListener("focus", function (e) {
        var rect = a.getBoundingClientRect();
        scheduleShow(target, rect.left, rect.bottom);
      });
      a.addEventListener("blur", cancelShow);
    });
    // Ensure the tooltip node exists so aria-describedby resolves.
    ensurePopover();
    // Hide popover on Escape for keyboard users.
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") hidePopover();
    });
  }

  // Phase 2 reading affordance: hover a body heading → a ¶ anchor link
  // appears (click copies a deep link into the URL bar via navigation).
  // Only headings that already carry an id (the renderer assigns them)
  // get an anchor; markup is created once per heading.
  function bindHeadingAnchors() {
    var body = document.querySelector(".okf-page__body");
    if (!body) return;
    var heads = body.querySelectorAll("h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]");
    heads.forEach(function (h) {
      if (h.querySelector(".okf-heading-anchor")) return;
      var a = document.createElement("a");
      a.className = "okf-heading-anchor";
      a.href = "#" + h.id;
      a.textContent = "¶";
      a.setAttribute("aria-label", "Link to this section");
      h.appendChild(a);
    });
  }

  // Atlas search polish: type filter chips on the search page.
  function bindSearchFilters() {
    var toolbar = document.querySelector(".okf-search-filter");
    if (!toolbar) return;
    var chips = toolbar.querySelectorAll(".okf-search-filter__chip");
    var results = document.querySelectorAll(".okf-search-result");
    var groups = document.querySelectorAll(".okf-search-group");
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var ty = chip.getAttribute("data-type") || "";
        chips.forEach(function (c) { c.classList.toggle("is-active", c === chip); });
        results.forEach(function (r) {
          var match = !ty || r.getAttribute("data-type") === ty;
          r.hidden = !match;
        });
        groups.forEach(function (g) {
          if (!ty) { g.hidden = false; return; }
          g.hidden = g.getAttribute("data-type") !== ty;
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bindLinkHovers();
      renderLocalGraph();
      bindHeadingAnchors();
      bindSearchFilters();
    });
  } else {
    bindLinkHovers();
    renderLocalGraph();
    bindHeadingAnchors();
    bindSearchFilters();
  }
  // Re-apply after live SSE body patches (studio dispatches this event).
  window.addEventListener("okf-loom:bodyPatched", bindHeadingAnchors);
})();
