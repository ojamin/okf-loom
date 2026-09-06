/**
 * OKF renderers.js — progressive enhancement for rich content blocks.
 *
 * Two halves:
 *
 * 1. CDN renderers: Mermaid (diagrams), highlight.js (syntax highlighting),
 *    and KaTeX (math), loaded ONLY when the page contains elements that
 *    need them. Degrades gracefully: if the CDN is unreachable, the raw
 *    source text remains visible (readable but unstyled).
 *
 * 2. Local markdown UX enhancers (no CDN, no network): sortable /
 *    filterable / column-resizable tables with sticky headers and
 *    copy-as-CSV, copy buttons + language badges on code blocks, and a
 *    click-to-zoom lightbox for images. All DOM the enhancers inject is
 *    additive and idempotent; studio.js's live block-diff sees through the
 *    wrappers via their data-source attribute (same contract as mermaid).
 *
 * CSP-safe: loaded as a local <script defer> from /__static/renderers.js.
 * The CDN scripts are dynamically inserted only when needed, so pages
 * without mermaid/code/math incur zero overhead.
 *
 * Supply-chain posture: every CDN URL is pinned to an EXACT
 * version (jsdelivr per-version URLs are immutable), matching the pinned
 * cytoscape tag in render.py. <link>/<script> assets additionally carry
 * SRI integrity hashes. The two dynamic import() modules (mermaid, hljs)
 * cannot carry SRI (the import spec has no integrity slot, and their ESM
 * graphs pull sub-modules anyway) — exact-version pinning is the
 * practical mitigation there. When bumping a version, recompute hashes:
 *   curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A
 */
(function () {
  "use strict";

  // Single source of truth for CDN versions + SRI hashes.
  var PINS = {
    mermaidEsm: "https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.esm.min.mjs",
    // jsDelivr's +esm transform: the package's own es/index.js is a
    // bundler-oriented wrapper whose raw browser import provides no
    // default export (verified in-browser: highlighting silently never
    // ran). +esm serves a self-contained ESM bundle with hljs as the
    // default export, still pinned to the exact version.
    hljsEsm: "https://cdn.jsdelivr.net/npm/highlight.js@11.11.1/+esm",
    hljsThemeLight: {
      href: "https://cdn.jsdelivr.net/npm/highlight.js@11.11.1/styles/github.min.css",
      integrity: "sha384-eFTL69TLRZTkNfYZOLM+G04821K1qZao/4QLJbet1pP4tcF+fdXq/9CdqAbWRl/L",
    },
    hljsThemeDark: {
      href: "https://cdn.jsdelivr.net/npm/highlight.js@11.11.1/styles/github-dark.min.css",
      integrity: "sha384-wH75j6z1lH97ZOpMOInqhgKzFkAInZPPSPlZpYKYTOqsaizPvhQZmAtLcPKXpLyH",
    },
    katexCss: {
      href: "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.css",
      integrity: "sha384-nH0MfJ44wi1dd7w6jinlyBgljjS8EJAh2JBoRad8a3VDw2K69vfaaqm4WnR+gXtA",
    },
    katexJs: {
      src: "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.js",
      integrity: "sha384-CwjPRVHTvLiMBFjEoij+QZViMV5rhTOIp7CJzl24JEqpRDA1sJFHVXXLURktbYYp",
    },
  };

  function loadScript(pin, type) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = pin.src || pin;
      if (pin.integrity) {
        s.integrity = pin.integrity;
        s.crossOrigin = "anonymous";
      }
      if (type) s.type = type;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function loadCSS(pin) {
    return new Promise(function (resolve, reject) {
      var l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = pin.href || pin;
      if (pin.integrity) {
        l.integrity = pin.integrity;
        l.crossOrigin = "anonymous";
      }
      l.onload = function () { resolve(l); };
      l.onerror = reject;
      document.head.appendChild(l);
      return l;
    });
  }

  function isDark() {
    // Dark-family themes (dark, midnight) get the dark mermaid/hljs skins;
    // light-family themes (light, pastel, sepia) get the light ones.
    var t = document.documentElement.getAttribute("data-theme") || "light";
    return t === "dark" || t === "midnight";
  }

  // --- Mermaid ---
  function initMermaid() {
    // Only process mermaid divs that haven't been rendered yet (no SVG child).
    // This prevents re-rendering diagrams that are already shown, which would
    // cause a visible flash on live updates that don't change the diagram.
    var allDivs = document.querySelectorAll("div.mermaid");
    if (allDivs.length === 0) return;
    var mermaidDivs = Array.from(allDivs).filter(function (el) {
      return !el.querySelector("svg"); // skip already-rendered
    });
    if (mermaidDivs.length === 0) return; // nothing to render
    // Stamp the original source text as data-source BEFORE mermaid
    // replaces it with SVG output.
    mermaidDivs.forEach(function (el, i) {
      if (!el.getAttribute("data-source")) {
        el.setAttribute("data-source", el.textContent);
      }
      if (!el.id) el.id = "okf-mermaid-" + i;
    });
    // Mermaid v11 is ESM-only. Use dynamic import() so the module loads
    // asynchronously and we get the mermaid object as a named export.
    // CSP allows this because cdn.jsdelivr.net is in script-src.
    import(PINS.mermaidEsm)
      .then(function (mod) {
        var mermaid = mod.default || window.mermaid;
        if (!mermaid) return;
        mermaid.initialize({
          startOnLoad: false,
          theme: isDark() ? "dark" : "default",
        });
        return mermaid.run({ nodes: mermaidDivs });
      })
      .catch(function () {
        mermaidDivs.forEach(function (el) {
          el.classList.add("mermaid--fallback");
        });
      });
  }

  // --- Syntax highlighting (highlight.js) ---
  // The theme stylesheet follows the page theme. A data-theme mutation observer
  // swaps the sheet on toggle so light pages do not get dark code islands.
  var hljsThemeLink = null;
  function ensureHljsTheme() {
    var pin = isDark() ? PINS.hljsThemeDark : PINS.hljsThemeLight;
    if (hljsThemeLink && hljsThemeLink.href === pin.href) return;
    var old = hljsThemeLink;
    loadCSS(pin)
      .then(function (l) {
        hljsThemeLink = l;
        if (old && old.parentNode) old.parentNode.removeChild(old);
      })
      .catch(function () { /* theme optional; code stays readable */ });
  }

  var themeObserver = null;
  function watchThemeForHljs() {
    if (themeObserver) return;
    themeObserver = new MutationObserver(function () {
      if (hljsThemeLink) ensureHljsTheme();
    });
    themeObserver.observe(document.documentElement, {
      attributes: true, attributeFilter: ["data-theme"],
    });
  }

  function initHighlight() {
    // Only highlight code blocks that haven't been highlighted yet.
    var allBlocks = document.querySelectorAll("pre > code[class*='language-']");
    if (allBlocks.length === 0) return;
    var codeBlocks = Array.from(allBlocks).filter(function (el) {
      return !el.dataset.highlighted; // skip already-highlighted
    });
    if (codeBlocks.length === 0) return;
    ensureHljsTheme();
    watchThemeForHljs();
    // Load highlight.js via dynamic import() of the ESM build.
    // The old common.min.js is UMD/CommonJS (uses module.exports + require)
    // and throws "require is not defined" when loaded as a plain <script>.
    // The ESM build at /es/index.js exports a default `hljs` and runs
    // cleanly under CSP `script-src` that allows the CDN.
    import(PINS.hljsEsm)
      .then(function (mod) {
        // ESM default export is hljs; fall back to named for safety.
        var hljs = (mod && mod.default) || (mod && mod.hljs) || window.hljs;
        if (!hljs) return;
        codeBlocks.forEach(function (block) {
          try { hljs.highlightElement(block); } catch (e) { /* ignore */ }
          block.dataset.highlighted = "true"; // mark so we don't re-highlight
        });
      })
      .catch(function () {
        /* CDN unreachable: code blocks remain unstyled (readable). */
      });
  }

  // --- Math (KaTeX) ---
  function initMath() {
    // Skip blocks already rendered: re-running katex.render over rendered
    // output would typeset the MathML text, garbling it. Matters now that
    // okf-loom:bodyPatched re-scans can fire repeatedly (live patches, graph
    // detail panel).
    var allMath = document.querySelectorAll("div.math");
    if (allMath.length === 0) return;
    var mathDivs = Array.prototype.filter.call(allMath, function (el) {
      return !el.dataset.mathRendered;
    });
    if (mathDivs.length === 0) return;
    Promise.all([
      loadCSS(PINS.katexCss),
      loadScript(PINS.katexJs),
    ])
      .then(function () {
        mathDivs.forEach(function (el, i) {
          if (window.katex && !el.dataset.mathRendered) {
            var tex = el.textContent;
            el.id = el.id || ("okf-math-" + i);
            window.katex.render(tex, el, { throwOnError: false, displayMode: true });
            el.dataset.mathRendered = "true";
          }
        });
      })
      .catch(function () {
        /* CDN unreachable: raw LaTeX remains visible. */
      });
  }

  // ====================================================================
  // Local markdown UX enhancers (tables / code blocks / images)
  // ====================================================================
  // Scope: rendered concept bodies everywhere they appear — concept pages
  // (.okf-prose.okf-page__body), the index intro (.okf-prose), and the
  // graph / single-file detail panel (graph.js swaps the class list to
  // .okf-page__body). NOT the studio source pane (pre.okf-source lives
  // outside these containers) and NOT the frontmatter table.
  var PROSE_SELECTOR = ".okf-prose, .okf-page__body";
  // Show the filter/count/copy toolbar only when a table has enough rows
  // for scanning to hurt; sorting + resizing apply to every table.
  var TABLE_TOOLBAR_MIN_ROWS = 5;

  function proseRoots() {
    return Array.prototype.slice.call(document.querySelectorAll(PROSE_SELECTOR));
  }

  function collapseWs(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  // --- clipboard helper (shared by table CSV + code copy buttons) -------
  function copyText(text, btn, okLabel) {
    function flash(label) {
      var prev = btn.textContent;
      btn.textContent = label;
      btn.disabled = true;
      setTimeout(function () { btn.textContent = prev; btn.disabled = false; }, 1500);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { flash(okLabel); },
        function () { flash("Copy failed"); }
      );
      return;
    }
    // Legacy fallback (non-secure contexts): hidden textarea + execCommand.
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    flash(ok ? okLabel : "Copy failed");
  }

  // --- tables: sort / filter / resize / sticky / copy --------------------
  function initTables() {
    proseRoots().forEach(function (root) {
      Array.prototype.forEach.call(root.querySelectorAll("table"), function (t) {
        if (t.dataset.okfEnhanced) return;
        if (t.closest(".okf-tablewrap")) return; // already wrapped (nested scan)
        enhanceTable(t);
      });
    });
    updateTableFits();
  }

  function tbodyRows(table) {
    var tb = table.tBodies[0];
    return tb ? Array.prototype.slice.call(tb.rows) : [];
  }

  function enhanceTable(table) {
    var sourceHtml = table.outerHTML;
    table.dataset.okfEnhanced = "1";
    if (!table.tHead || !table.tHead.rows.length) return; // headerless: leave as-is

    // Wrapper carries the horizontal scroll (previously on the table itself)
    // plus data-source: the ORIGINAL whitespace-collapsed text, which
    // studio.js's blockSig uses so a user-applied sort/filter never makes an
    // unchanged table look "changed" to the live diff (and vice versa).
    var wrap = document.createElement("div");
    wrap.className = "okf-tablewrap";
    wrap.setAttribute("data-source", collapseWs(table.textContent));
    wrap.setAttribute("data-source-html", sourceHtml);
    table.parentNode.insertBefore(wrap, table);

    var rows = tbodyRows(table);
    if (rows.length >= TABLE_TOOLBAR_MIN_ROWS) {
      wrap.appendChild(buildTableToolbar(table));
    }
    wrap.appendChild(table);
    table.classList.add("okf-table--enhanced");

    // Original row order, for the third state of the sort cycle.
    var originalRows = rows.slice();

    var headRow = table.tHead.rows[0];
    Array.prototype.forEach.call(headRow.cells, function (th) {
      th.classList.add("okf-th-sortable");
      th.setAttribute("aria-sort", "none");
      th.tabIndex = 0;
      // The resizer is a drag handle on the column's right edge. It sits
      // inside the th but is aria-hidden decorative chrome; keyboard users
      // get horizontal scrolling instead (resize is a pointer nicety).
      var grip = document.createElement("span");
      grip.className = "okf-col-resizer";
      grip.setAttribute("aria-hidden", "true");
      th.appendChild(grip);
      wireColumnResize(table, wrap, th, grip);

      th.addEventListener("click", function (e) {
        if (e.target.closest(".okf-col-resizer")) return;
        cycleSort(table, th, originalRows);
      });
      th.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        cycleSort(table, th, originalRows);
      });
    });
  }

  function buildTableToolbar(table) {
    var bar = document.createElement("div");
    bar.className = "okf-table-toolbar";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Table tools");

    var filter = document.createElement("input");
    filter.type = "search";
    filter.className = "okf-table-filter";
    filter.placeholder = "Filter rows…";
    filter.setAttribute("aria-label", "Filter table rows");

    var count = document.createElement("span");
    count.className = "okf-table-count";
    count.setAttribute("aria-live", "polite");

    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "okf-table-copy";
    copy.textContent = "Copy CSV";
    copy.setAttribute("aria-label", "Copy table as CSV");

    function updateCount() {
      var rows = tbodyRows(table);
      var visible = rows.filter(function (r) { return !r.classList.contains("okf-row-hidden"); });
      count.textContent = filter.value
        ? visible.length + " of " + rows.length + " rows"
        : rows.length + " rows";
    }
    updateCount();

    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      tbodyRows(table).forEach(function (r) {
        var hit = !q || (r.textContent || "").toLowerCase().indexOf(q) >= 0;
        r.classList.toggle("okf-row-hidden", !hit);
      });
      updateCount();
    });
    filter.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && filter.value) {
        filter.value = "";
        filter.dispatchEvent(new Event("input"));
        e.stopPropagation(); // keep Esc from also closing studio panels
      }
    });
    copy.addEventListener("click", function () {
      copyText(tableToCsv(table), copy, "Copied ✓");
    });

    bar.appendChild(filter);
    bar.appendChild(count);
    bar.appendChild(copy);
    return bar;
  }

  function tableToCsv(table) {
    // Current sort order, ALL rows (a filtered view still copies the whole
    // table — partial exports silently masquerading as full ones are worse).
    var lines = [];
    function pushRow(cells) {
      lines.push(Array.prototype.map.call(cells, function (c) {
        var v = collapseWs(c.textContent);
        return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(","));
    }
    if (table.tHead && table.tHead.rows.length) pushRow(table.tHead.rows[0].cells);
    tbodyRows(table).forEach(function (r) { pushRow(r.cells); });
    return lines.join("\n");
  }

  // Sort comparator: numeric when every non-empty cell in the column parses
  // as a number (currency/percent/thousands tolerated), else natural-order
  // string compare (so v1.10 > v1.9). Empty cells always sort last.
  function numericValue(s) {
    if (!s) return null;
    var cleaned = s.replace(/[,$£€%\s]/g, "");
    if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(cleaned)) return null;
    return parseFloat(cleaned);
  }

  function cycleSort(table, th, originalRows) {
    var headRow = table.tHead.rows[0];
    var prev = th.getAttribute("aria-sort") || "none";
    Array.prototype.forEach.call(headRow.cells, function (h) {
      h.setAttribute("aria-sort", "none");
    });
    var next = prev === "none" ? "ascending" : prev === "ascending" ? "descending" : "none";
    th.setAttribute("aria-sort", next);

    var tb = table.tBodies[0];
    if (!tb) return;
    if (next === "none") {
      originalRows.forEach(function (r) { tb.appendChild(r); });
      return;
    }
    var idx = th.cellIndex;
    var rows = tbodyRows(table);
    var keys = rows.map(function (r) {
      var c = r.cells[idx];
      return collapseWs(c ? c.textContent : "");
    });
    var nonEmpty = keys.filter(function (k) { return k !== ""; });
    var numeric = nonEmpty.length > 0 && nonEmpty.every(function (k) { return numericValue(k) !== null; });
    var dir = next === "ascending" ? 1 : -1;
    var decorated = rows.map(function (r, i) { return { row: r, key: keys[i], i: i }; });
    decorated.sort(function (a, b) {
      if (a.key === "" || b.key === "") {
        if (a.key === b.key) return a.i - b.i;
        return a.key === "" ? 1 : -1; // empties last regardless of direction
      }
      var cmp;
      if (numeric) {
        cmp = numericValue(a.key) - numericValue(b.key);
      } else {
        cmp = a.key.localeCompare(b.key, undefined, { numeric: true, sensitivity: "base" });
      }
      return cmp !== 0 ? dir * cmp : a.i - b.i; // stable
    });
    decorated.forEach(function (d) { tb.appendChild(d.row); });
  }

  function wireColumnResize(table, wrap, th, grip) {
    var startX = 0;
    var startW = 0;
    function freezeWidths() {
      // First drag: pin every column at its rendered width and switch to
      // fixed layout so one column's change doesn't reflow the others.
      if (table.style.tableLayout === "fixed") return;
      Array.prototype.forEach.call(table.tHead.rows[0].cells, function (h) {
        h.style.width = h.offsetWidth + "px";
      });
      table.style.tableLayout = "fixed";
    }
    grip.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      freezeWidths();
      startX = e.clientX;
      startW = th.offsetWidth;
      grip.setPointerCapture(e.pointerId);
      grip.classList.add("okf-col-resizer--active");
    });
    grip.addEventListener("pointermove", function (e) {
      if (!grip.classList.contains("okf-col-resizer--active")) return;
      th.style.width = Math.max(48, startW + (e.clientX - startX)) + "px";
    });
    function endDrag() {
      if (!grip.classList.contains("okf-col-resizer--active")) return;
      grip.classList.remove("okf-col-resizer--active");
      updateTableFits();
    }
    grip.addEventListener("pointerup", endDrag);
    grip.addEventListener("pointercancel", endDrag);
    // Double-click a grip → back to automatic layout for the whole table.
    grip.addEventListener("dblclick", function (e) {
      e.stopPropagation();
      Array.prototype.forEach.call(table.tHead.rows[0].cells, function (h) {
        h.style.width = "";
      });
      table.style.tableLayout = "";
      updateTableFits();
    });
  }

  // Sticky headers only work when the wrapper is NOT a horizontal scroll
  // container (position:sticky pins to the nearest scrollport). When the
  // table fits, mark the wrapper so the stylesheet can lift the overflow
  // and let thead stick under the page topbar.
  function updateTableFits() {
    Array.prototype.forEach.call(document.querySelectorAll(".okf-tablewrap"), function (wrap) {
      var table = wrap.querySelector("table");
      if (!table) return;
      wrap.classList.toggle("okf-tablewrap--fit", table.scrollWidth <= wrap.clientWidth + 1);
    });
  }
  var fitTimer = null;
  window.addEventListener("resize", function () {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(updateTableFits, 150);
  });

  // --- code blocks: copy button + language badge -------------------------
  function initCodeBlocks() {
    proseRoots().forEach(function (root) {
      Array.prototype.forEach.call(root.querySelectorAll("pre"), function (pre) {
        if (pre.dataset.okfEnhanced) return;
        if (pre.closest(".okf-codewrap")) return;
        pre.dataset.okfEnhanced = "1";
        var wrap = document.createElement("div");
        wrap.className = "okf-codewrap";
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);

        var tools = document.createElement("div");
        tools.className = "okf-code-tools";
        var code = pre.querySelector("code");
        var m = code && /language-([\w+-]+)/.exec(code.className || "");
        if (m) {
          var badge = document.createElement("span");
          badge.className = "okf-code-lang";
          badge.setAttribute("aria-hidden", "true");
          badge.textContent = m[1];
          tools.appendChild(badge);
        }
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "okf-code-copy";
        btn.textContent = "Copy";
        btn.setAttribute("aria-label", "Copy code to clipboard");
        btn.addEventListener("click", function () {
          copyText((code || pre).textContent || "", btn, "Copied ✓");
        });
        tools.appendChild(btn);
        wrap.appendChild(tools);
      });
    });
  }

  // --- images: click-to-zoom lightbox ------------------------------------
  var lightbox = null;
  var lightboxReturnFocus = null;
  function closeLightbox() {
    if (!lightbox) return;
    lightbox.remove();
    lightbox = null;
    if (lightboxReturnFocus && typeof lightboxReturnFocus.focus === "function") {
      try { lightboxReturnFocus.focus({ preventScroll: true }); } catch (e) {}
    }
    lightboxReturnFocus = null;
  }
  function openLightbox(img) {
    closeLightbox();
    lightboxReturnFocus = img;
    lightbox = document.createElement("div");
    lightbox.className = "okf-lightbox";
    lightbox.setAttribute("role", "dialog");
    lightbox.setAttribute("aria-modal", "true");
    lightbox.setAttribute("aria-label", img.alt || "Image preview");

    var fig = document.createElement("figure");
    fig.className = "okf-lightbox__figure";
    var full = document.createElement("img");
    full.src = img.src;
    full.alt = img.alt || "";
    fig.appendChild(full);
    if (img.alt) {
      var cap = document.createElement("figcaption");
      cap.textContent = img.alt;
      fig.appendChild(cap);
    }
    var close = document.createElement("button");
    close.type = "button";
    close.className = "okf-lightbox__close";
    close.textContent = "×";
    close.setAttribute("aria-label", "Close image preview");
    close.addEventListener("click", closeLightbox);

    lightbox.appendChild(close);
    lightbox.appendChild(fig);
    lightbox.addEventListener("click", function (e) {
      if (e.target === lightbox) closeLightbox();
    });
    document.body.appendChild(lightbox);
    try { close.focus({ preventScroll: true }); } catch (e) {}
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeLightbox();
  });

  function initImages() {
    proseRoots().forEach(function (root) {
      Array.prototype.forEach.call(root.querySelectorAll("img"), function (img) {
        if (img.dataset.okfZoom) return;
        if (img.closest("a")) return; // linked images navigate, not zoom
        img.dataset.okfZoom = "1";
        img.classList.add("okf-zoomable");
        img.tabIndex = 0;
        img.setAttribute("role", "button");
        img.setAttribute("aria-label", "View full-size image" + (img.alt ? ": " + img.alt : ""));
        img.addEventListener("click", function () { openLightbox(img); });
        img.addEventListener("keydown", function (e) {
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          openLightbox(img);
        });
      });
    });
  }

  function initAll() {
    initMermaid();
    initHighlight();
    initMath();
    initTables();
    initCodeBlocks();
    initImages();
  }

  // Run after DOM is ready.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }

  // Re-run on live studio patches (when the concept body is re-rendered).
  window.addEventListener("okf-loom:bodyPatched", initAll);
})();
