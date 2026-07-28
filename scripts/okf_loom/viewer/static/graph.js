/* OKF viewer - graph view + detail panel logic.
 *
 * Used in TWO contexts:
 *   1. The single-file viewer (template inlines this + bundle data).
 *   2. The full-page /__graph view (fetches graph.json from the server).
 *
 * Mode detection:
 *   - If window.BUNDLE is set → single-file mode (data already inlined).
 *   - Else if window.OKF_LOOM_GRAPH_DATA_URL is set → fetch graph JSON.
 *
 * Optional window flags:
 *   - OKF_LOOM_INITIAL_LAYOUT (default "cose")
 *   - OKF_LOOM_INITIAL_THEME  (default "light")
 *   - OKF_CONCEPT_PAGE_PREFIX (default "/") - base URL for opening a concept
 *     page when "Open page" is clicked (graph view only).
 *
 * CDN deps: Cytoscape.js 3.x only. Markdown bodies are pre-rendered
 * server-side by okf-loom's escaping renderer, so no client-side
 * markdown parser (marked/DOMPurify) is needed. If Cytoscape fails to
 * load, the graph area shows a helpful error instead of throwing.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "okf-theme";

  // Theme cycle order + button glyphs. KEEP IN SYNC with the copies in
  // wiki.js / studio.js and render.py:_theme_button_html — this file must
  // stand alone in the single-file viewer, which has no wiki.js.
  var THEMES = ["light", "dark", "pastel", "sepia", "midnight"];
  var THEME_GLYPHS = { light: "☀", dark: "☾", pastel: "✿", sepia: "☕", midnight: "★" };

  // ---- Canvas colour constants (P2-5 iter-2) -------------------------------
  // Cytoscape canvas styles CANNOT read CSS custom properties directly, so
  // these JS literals MIRROR the design tokens declared in wiki.css :root
  // and the [data-theme="…"] blocks. KEEP IN SYNC WITH wiki.css TOKENS -
  // the CSS file is the authoritative source; this block exists so a token
  // change has ONE obvious place to update in JS. Per theme:
  //   nodeText     node label text. = --okf-fg.
  //   nodeBorder   node border. Light-family themes reuse --okf-fg; dark-
  //                family themes use --okf-bg so the border reads strong
  //                against the elevated canvas surface.
  //   bridgeBorder bridge/focus node border. = --okf-fg in every theme.
  //   edge         edge line + arrow. = --okf-border-strong.
  //   edgeLabel    edge label text. Review feedback: darkened well past
  //                --okf-fg-muted (light was #64748b ~4.6:1 → #334155
  //                ~10.4:1) so relationship labels stay legible when the
  //                auto-fit zooms the graph out. Each theme's value is a
  //                strengthened take on its fg-muted.
  //   edgeLabelBg  edge label background (opaque so labels stay readable
  //                over nodes). = --okf-bg-elev (light family) or --okf-bg
  //                (dark family).
  //   select       selection colour. = --okf-select (contrast ratios are
  //                documented in wiki.css).
  var GRAPH_COLORS = {
    light: {
      nodeText: "#0f172a", nodeBorder: "#0f172a", bridgeBorder: "#0f172a",
      edge: "#cbd5e1", edgeLabel: "#334155", edgeLabelBg: "#ffffff",
      select: "#0c7373",
    },
    dark: {
      nodeText: "#e2e8f0", nodeBorder: "#0b1220", bridgeBorder: "#e2e8f0",
      edge: "#334155", edgeLabel: "#cbd5e1", edgeLabelBg: "#0b1220",
      select: "#3ec9c9",
    },
    pastel: {
      nodeText: "#403a58", nodeBorder: "#403a58", bridgeBorder: "#403a58",
      edge: "#bbaed6", edgeLabel: "#4c4569", edgeLabelBg: "#f7f4fb",
      select: "#6d4fae",
    },
    sepia: {
      nodeText: "#3d3020", nodeBorder: "#3d3020", bridgeBorder: "#3d3020",
      edge: "#c6b28a", edgeLabel: "#54432c", edgeLabelBg: "#faf4e6",
      select: "#8a4a15",
    },
    midnight: {
      nodeText: "#dbe2f4", nodeBorder: "#050810", bridgeBorder: "#dbe2f4",
      edge: "#2a3352", edgeLabel: "#b8c1dd", edgeLabelBg: "#050810",
      select: "#52d8d8",
    },
  };

  // Resolve the palette for the CURRENT data-theme (light fallback).
  // On the full-page atlas graph shell, always use the deep-space atlas
  // canvas palette so the mockup look holds regardless of chrome theme.
  function graphPalette() {
    if (document.body && document.body.classList.contains("okf-viewer--graph")) {
      return {
        nodeText: "#e8eefc",
        nodeBorder: "rgba(255,255,255,0.22)",
        bridgeBorder: "#e8eefc",
        edge: "#3a4663",
        edgeLabel: "#b8c4e0",
        edgeLabelBg: "rgba(7,11,20,0.88)",
        select: "#3ec9c9",
      };
    }
    var t = document.documentElement.getAttribute("data-theme") || "light";
    return GRAPH_COLORS[t] || GRAPH_COLORS.light;
  }

  // ---- Config from data-* attributes (CSP-safe; no inline script) ------
  // The graph_page.html template passes config via data-* attributes on
  // #okf-graph instead of window.* globals, so the server's CSP
  // (script-src 'self' - no 'unsafe-inline') doesn't block initialisation.
  // MUST be read before the theme block below uses INITIAL_THEME.
  var graphEl = document.getElementById("okf-graph");
  var DATA_URL = (graphEl && graphEl.getAttribute("data-graph-url")) || window.OKF_LOOM_GRAPH_DATA_URL || null;
  var CONCEPT_PREFIX = (graphEl && graphEl.getAttribute("data-concept-prefix")) || window.OKF_CONCEPT_PAGE_PREFIX || "/";
  var INITIAL_LAYOUT = (graphEl && graphEl.getAttribute("data-initial-layout")) || window.OKF_LOOM_INITIAL_LAYOUT || "cose";
  var INITIAL_THEME = (graphEl && graphEl.getAttribute("data-initial-theme")) || window.OKF_LOOM_INITIAL_THEME || "light";
  // P1-3: build mode (serve|spa|static) is emitted on <body data-okf-mode>
  // by the graph_page template. In static mode the Open-page href needs a
  // .html suffix (concept pages are emitted as <id>.html; the bare path
  // 404s). Default to "serve" when the attribute is absent (single-file
    // viewers and older embedded pages).
  var MODE = (document.body && document.body.getAttribute("data-okf-mode")) || "serve";

  // ---- Theme (kept here so single-file viewers without wiki.js still work)
  var themeBtn = document.getElementById("okf-theme");
  var REDUCED_MOTION = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Layout parameters tuned for spread. The previous default
  // ({ name: 'cose', padding: 30 }) packed nodes too tightly — users
  // complained about everything being bunch up together. cose has many
  // spread knobs; this preset increases node repulsion, ideal edge
  // length, component spacing, and padding so the graph breathes.
  //
  // Returns the full layout options object for the given layout name.
  // Pass `randomize` for an initial layout (true) or a re-layout /
  // incremental add (false) — same as the original `randomize` semantics.
  //
  // Signal controls: every spacing/spread knob now accepts an
  // override via `opts` so the Signal-controls panel can drive node spacing,
  // cluster separation, and group strength from the UI. When an override is
  // absent the original tuned defaults apply, so behaviour is unchanged for
  // any caller that does not pass tuning (single-file viewer, presence focus).
  // `opts.idealEdgeLength` MAY be a function(edge) — that is how grouping pulls
  // same-group nodes together and pushes different-group nodes apart on cose.
  // Phase 3: prefer fCoSE (the constraint-capable successor to cose from
  // the same authors) whenever its CDN chain loaded. It spreads clusters
  // dramatically better, packs disconnected components, and TILES
  // degree-zero nodes into a tray instead of letting them drift (the
  // orphan shelf). cytoscape-fcose self-registers against
  // window.cytoscape; the global is our capability probe. Offline /
  // cdn:false keeps plain cose (identical option names, softer results).
  function fcoseAvailable() {
    return typeof window.cytoscapeFcose !== "undefined";
  }

  function layoutOpts(name, opts) {
    opts = opts || {};
    var randomize = opts.randomize !== false;
    var animate = opts.animate !== false && !REDUCED_MOTION;
    var pad = opts.padding || 60;
    // fit defaults to FALSE: graph.js owns fitting via overlayAwareFit() so it
    // can respect a MAX_ZOOM clamp + overlay insets (reviewer over-zoom blocker).
    var common = { name: name, animate: animate, padding: pad, fit: opts.fit === true };
    if (name === "cose" && fcoseAvailable()) {
      // The Signal-controls overrides map 1:1 (fcose shares cose's knob
      // names for repulsion/ideal-length/elasticity/gravity).
      return Object.assign(common, {
        name: "fcose",
        quality: "default",
        randomize: randomize,
        nodeRepulsion: opts.nodeRepulsion || 12000,
        idealEdgeLength: opts.idealEdgeLength || 120,
        edgeElasticity: opts.edgeElasticity || 0.45,
        gravity: opts.gravity != null ? opts.gravity : 0.25,
        gravityRange: 3.8,
        numIter: opts.numIter || 2500,
        nodeSeparation: 110,
        packComponents: true,
        tile: true,
      });
    }
    if (name === "cose") {
      // Spread tuned for ~30-500 node bundles. Higher repulsion + longer
      // ideal edges push clusters apart; component spacing adds breathing
      // room around disconnected subgraphs; more iterations let the
      // force-directed layout converge to a less-jittery stable state.
      return Object.assign(common, {
        randomize: randomize,
        nodeRepulsion: opts.nodeRepulsion || 12000,
        // nodeOverlap is cose's OVERLAPPING-node repulsion — the lever that
        // actually stops same-cluster nodes piling on top of each other (the
        // reviewer-1 collision). Default 4 is far too weak for our node sizes.
        nodeOverlap: opts.nodeOverlap || 24,
        idealEdgeLength: opts.idealEdgeLength || 140,
        edgeElasticity: opts.edgeElasticity || 0.45,
        gravity: opts.gravity != null ? opts.gravity : 0.25,
        numIter: opts.numIter || 3500,
        componentSpacing: opts.componentSpacing || 100,
        nestingFactor: 5,
        coolingFactor: 0.95,
        minTemp: 1.0,
      });
    }
    if (name === "dagre" && typeof window.dagre !== "undefined") {
      // Layered/Sugiyama layout for the Flow lens: reading direction =
      // dependency direction (left → right). The evidence-backed layout
      // for directed data (yFiles/Linkurious both ship it as the
      // dedicated "flow" layout).
      return Object.assign(common, {
        name: "dagre",
        rankDir: "LR",
        nodeSep: 26 + 24 * (opts.spacingFactor || 1),
        rankSep: 70 + 50 * (opts.spacingFactor || 1),
        edgeSep: 12,
      });
    }
    if (name === "dagre" || name === "breadthfirst") {
      // dagre requested but its CDN chain is absent → directed
      // breadthfirst approximates the layering.
      return Object.assign(common, {
        name: "breadthfirst",
        directed: name === "dagre" ? true : false,
        spacingFactor: opts.spacingFactor || 1.4,
        padding: pad,
      });
    }
    if (name === "concentric") {
      return Object.assign(common, {
        concentric: opts.concentric || function (n) { return n.degree(); },
        levelWidth: function () { return 2; },
        spacingFactor: opts.spacingFactor || 1.3,
        minNodeSpacing: opts.minNodeSpacing || 40,
        padding: pad,
      });
    }
    if (name === "circle") {
      return Object.assign(common, { radius: opts.radius || 280, padding: pad, spacingFactor: opts.spacingFactor || 1 });
    }
    if (name === "grid") {
      return Object.assign(common, { sort: opts.sort, padding: pad, avoidOverlap: true, spacingFactor: opts.spacingFactor || 1.25 });
    }
    // Fallback for any custom layout: hand back the common options.
    return common;
  }

  function applyTheme(t) {
    if (THEMES.indexOf(t) < 0) t = "light";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(STORAGE_KEY, t); } catch (e) {}
    if (themeBtn) {
      themeBtn.textContent = THEME_GLYPHS[t];
      themeBtn.setAttribute("title", "Theme: " + t + " — click to cycle");
      themeBtn.setAttribute("aria-label", "Change colour theme (current: " + t + ")");
      // Five-way cycle, not a two-state toggle — aria-pressed would lie.
      themeBtn.removeAttribute("aria-pressed");
    }
  }

  // ---- Luminance-aware chip foreground (P0-4 / P2-23) ------------------
  // Port of render.py:_chip_fg(): parses a CSS colour (hsl/hex/rgb/named)
  // and picks the foreground (black or white) with the higher WCAG
  // contrast ratio against the background. Previously the graph-view type
  // chip hardwired #fff (failed on yellow/green/cyan) and the threshold
  // approach had no margin near the luminance boundary; named CSS colours
  // bypassed the luminance check entirely.
  var _NAMED_CSS_COLORS = {
    aliceblue: [240, 248, 255], antiquewhite: [250, 235, 215],
    aqua: [0, 255, 255], aquamarine: [127, 255, 212], azure: [240, 255, 255],
    beige: [245, 245, 220], bisque: [255, 228, 196], black: [0, 0, 0],
    blanchedalmond: [255, 235, 205], blue: [0, 0, 255], blueviolet: [138, 43, 226],
    brown: [165, 42, 42], burlywood: [222, 184, 135], cadetblue: [95, 158, 160],
    chartreuse: [127, 255, 0], chocolate: [210, 105, 30], coral: [255, 127, 80],
    cornflowerblue: [100, 149, 237], cornsilk: [255, 248, 220], crimson: [220, 20, 60],
    cyan: [0, 255, 255], darkblue: [0, 0, 139], darkcyan: [0, 139, 139],
    darkgoldenrod: [184, 134, 11], darkgray: [169, 169, 169], darkgreen: [0, 100, 0],
    darkgrey: [169, 169, 169], darkkhaki: [189, 183, 107], darkmagenta: [139, 0, 139],
    darkolivegreen: [85, 107, 47], darkorange: [255, 140, 0], darkorchid: [153, 50, 204],
    darkred: [139, 0, 0], darksalmon: [233, 150, 122], darkseagreen: [143, 188, 143],
    darkslateblue: [72, 61, 139], darkslategray: [47, 79, 79], darkslategrey: [47, 79, 79],
    darkturquoise: [0, 206, 209], darkviolet: [148, 0, 211], deeppink: [255, 20, 147],
    deepskyblue: [0, 191, 255], dimgray: [105, 105, 105], dimgrey: [105, 105, 105],
    dodgerblue: [30, 144, 255], firebrick: [178, 34, 34], floralwhite: [255, 250, 240],
    forestgreen: [34, 139, 34], fuchsia: [255, 0, 255], gainsboro: [220, 220, 220],
    ghostwhite: [248, 248, 255], gold: [255, 215, 0], goldenrod: [218, 165, 32],
    gray: [128, 128, 128], green: [0, 128, 0], greenyellow: [173, 255, 47],
    grey: [128, 128, 128], honeydew: [240, 255, 240], hotpink: [255, 105, 180],
    indianred: [205, 92, 92], indigo: [75, 0, 130], ivory: [255, 255, 240],
    khaki: [240, 230, 140], lavender: [230, 230, 250], lavenderblush: [255, 240, 245],
    lawngreen: [124, 252, 0], lemonchiffon: [255, 250, 205], lightblue: [173, 216, 230],
    lightcoral: [240, 128, 128], lightcyan: [224, 255, 255],
    lightgoldenrodyellow: [250, 250, 210], lightgray: [211, 211, 211],
    lightgreen: [144, 238, 144], lightgrey: [211, 211, 211], lightpink: [255, 182, 193],
    lightsalmon: [255, 160, 122], lightseagreen: [32, 178, 170], lightskyblue: [135, 206, 250],
    lightslategray: [119, 136, 153], lightslategrey: [119, 136, 153],
    lightsteelblue: [176, 196, 222], lightyellow: [255, 255, 224], lime: [0, 255, 0],
    limegreen: [50, 205, 50], linen: [250, 240, 230], magenta: [255, 0, 255],
    maroon: [128, 0, 0], mediumaquamarine: [102, 205, 170], mediumblue: [0, 0, 205],
    mediumorchid: [186, 85, 211], mediumpurple: [147, 112, 219],
    mediumseagreen: [60, 179, 113], mediumslateblue: [123, 104, 238],
    mediumspringgreen: [0, 250, 154], mediumturquoise: [72, 209, 204],
    mediumvioletred: [199, 21, 133], midnightblue: [25, 25, 112], mintcream: [245, 255, 250],
    mistyrose: [255, 228, 225], moccasin: [255, 228, 181], navajowhite: [255, 222, 173],
    navy: [0, 0, 128], oldlace: [253, 245, 230], olive: [128, 128, 0],
    olivedrab: [107, 142, 35], orange: [255, 165, 0], orangered: [255, 69, 0],
    orchid: [218, 112, 214], palegoldenrod: [238, 232, 170], palegreen: [152, 251, 152],
    paleturquoise: [175, 238, 238], palevioletred: [219, 112, 147], papayawhip: [255, 239, 213],
    peachpuff: [255, 218, 185], peru: [205, 133, 63], pink: [255, 192, 203],
    plum: [221, 160, 221], powderblue: [176, 224, 230], purple: [128, 0, 128],
    rebeccapurple: [102, 51, 153], red: [255, 0, 0], rosybrown: [188, 143, 143],
    royalblue: [65, 105, 225], saddlebrown: [139, 69, 19], salmon: [250, 128, 114],
    sandybrown: [244, 164, 96], seagreen: [46, 139, 87], seashell: [255, 245, 238],
    sienna: [160, 82, 45], silver: [192, 192, 192], skyblue: [135, 206, 235],
    slateblue: [106, 90, 205], slategray: [112, 128, 144], slategrey: [112, 128, 144],
    snow: [255, 250, 250], springgreen: [0, 255, 127], steelblue: [70, 130, 180],
    tan: [210, 180, 140], teal: [0, 128, 128], thistle: [216, 191, 216],
    tomato: [255, 99, 71], turquoise: [64, 224, 208], violet: [238, 130, 238],
    wheat: [245, 222, 179], white: [255, 255, 255], whitesmoke: [245, 245, 245],
    yellow: [255, 255, 0], yellowgreen: [154, 205, 50]
  };
  function _chipFg(bgCss) {
    if (!bgCss) return "#ffffff";
    var s = String(bgCss).trim();
    var rgb = null;
    var mHsl = s.match(/^hsla?\(\s*(\d+(?:\.\d+)?)\s*(?:deg|rad|turn|grad)?\s*,\s*(\d+(?:\.\d+)?)%\s*,\s*(\d+(?:\.\d+)?)%/);
    var mRgb = s.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/);
    var mHex = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i);
    if (mHsl) {
      var h = parseFloat(mHsl[1]) % 360;
      var sat = parseFloat(mHsl[2]) / 100;
      var l = parseFloat(mHsl[3]) / 100;
      rgb = _hslToRgb(h, sat, l);
    } else if (mRgb) {
      rgb = [parseFloat(mRgb[1]) / 255, parseFloat(mRgb[2]) / 255, parseFloat(mRgb[3]) / 255];
    } else if (mHex) {
      var hex = mHex[1];
      if (hex.length === 3) {
        hex = hex.charAt(0) + hex.charAt(0) + hex.charAt(1) + hex.charAt(1) + hex.charAt(2) + hex.charAt(2);
      } else if (hex.length === 8) {
        // P1-7: strip alpha (#rrggbbaa) - luminance is RGB-only (parity
        // with the Python port in render.py:_chip_fg).
        hex = hex.slice(0, 6);
      }
      rgb = [
        parseInt(hex.slice(0, 2), 16) / 255,
        parseInt(hex.slice(2, 4), 16) / 255,
        parseInt(hex.slice(4, 6), 16) / 255,
      ];
    } else if (_NAMED_CSS_COLORS[s.toLowerCase()]) {
      var nc = _NAMED_CSS_COLORS[s.toLowerCase()];
      rgb = [nc[0] / 255, nc[1] / 255, nc[2] / 255];
    } else {
      return "#ffffff";
    }
    var lum = _relativeLuminance(rgb[0], rgb[1], rgb[2]);
    // P2-23: pick the higher-contrast foreground (black vs white) instead
    // of a brittle luminance threshold. Pure black has luminance 0, pure
    // white has luminance 1.
    var contrastBlack = (lum + 0.05) / 0.05;        // (lum_bg + 0.05) / (0 + 0.05)
    var contrastWhite = 1.05 / (lum + 0.05);        // (1 + 0.05) / (lum_bg + 0.05)
    return contrastBlack >= contrastWhite ? "#000000" : "#ffffff";
  }
  function _hslToRgb(h, s, l) {
    var c = (1 - Math.abs(2 * l - 1)) * s;
    var x = c * (1 - Math.abs((h / 60) % 2 - 1));
    var m_ = l - c / 2;
    var r, g, b;
    if (h < 60) { r = c; g = x; b = 0; }
    else if (h < 120) { r = x; g = c; b = 0; }
    else if (h < 180) { r = 0; g = c; b = x; }
    else if (h < 240) { r = 0; g = x; b = c; }
    else if (h < 300) { r = x; g = 0; b = c; }
    else { r = c; g = 0; b = x; }
    return [r + m_, g + m_, b + m_];
  }
  function _relativeLuminance(r, g, b) {
    function lin(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  }
  // Honour saved preference on load; fall back to OS pref, then INITIAL_THEME.
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved && THEMES.indexOf(saved) >= 0) applyTheme(saved);
    else {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      if (mq && mq.matches) applyTheme("dark");
      else applyTheme(INITIAL_THEME);
    }
  } catch (e) { applyTheme(INITIAL_THEME); }
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") || "light";
    applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
  });

  // ---- Bundle acquisition ------------------------------------------------
  function acquireBundle() {
    if (window.BUNDLE) return Promise.resolve(window.BUNDLE);
    var inline = document.getElementById("okf-graph-data");
    if (inline) {
      try {
        var raw = inline.content ? inline.content.textContent : inline.textContent;
        return Promise.resolve(JSON.parse(raw || "{}"));
      } catch (e) {
        return Promise.reject(new Error("invalid inline graph data: " + e.message));
      }
    }
    if (!DATA_URL) return Promise.reject(new Error("no bundle data"));
    return fetch(DATA_URL).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  acquireBundle().then(function (bundle) {
    // Read-only diagnostic seam used by file:// browser proofs. The graph's
    // live mutable API remains window.__okfLoomGraph after Cytoscape mounts.
    window.__okfLoomGraphData = bundle;
    init(bundle);
  }).catch(function (err) {
    // Surface the stack in the console — a swallowed init error is
    // undebuggable from the error banner alone.
    try { console.error("[okf-graph] init failed:", err && err.stack ? err.stack : err); } catch (e2) {}
    showLoadError(err && err.message ? err.message : String(err));
  });

  function showLoadError(msg) {
    var el = document.getElementById("okf-graph");
    if (!el) return;
    var notice = document.createElement("div");
    notice.className = "okf-graph-loading okf-graph-loading--error";
    notice.textContent = "Graph failed to load: " + msg;
    el.appendChild(notice);
  }

  function showWaiting(msg) {
    var el = document.getElementById("okf-graph");
    if (!el) return;
    var notice = document.createElement("div");
    notice.className = "okf-graph-loading";
    notice.textContent = msg;
    el.appendChild(notice);
    return notice;
  }

  function init(bundle) {
    if (typeof window.cytoscape !== "function") {
      showLoadError("Cytoscape.js failed to load (CDN unavailable?). " +
        "Reopen with network access or see config option {\"cdn\": false}.");
      return;
    }

    var layoutName = INITIAL_LAYOUT;
    // Sync the layout dropdown.
    // The raw layout dropdown is gone — each lens owns its
    // layout. The variable stays for the LOD grid switch below.
    var layoutSel = document.getElementById("okf-layout");

    var palette = bundle.palette || {};
    var bodies = bundle.bodies || {};
    var external = bundle.external || [];
    var conceptPagePrefix = CONCEPT_PREFIX;

    // Type filter dropdown.
    var typeSel = document.getElementById("okf-filter-type");
    (bundle.types || []).forEach(function (t) {
      if (!typeSel) return;
      var opt = document.createElement("option");
      opt.value = t; opt.textContent = t;
      typeSel.appendChild(opt);
    });

    // Lookup tables.
    var nodeIndex = {};
    bundle.nodes.forEach(function (n) { nodeIndex[n.data.id] = n.data; });
    function metaValue(d, key) {
      if (!d) return "";
      if (d[key]) return String(d[key]);
      if (d.metadata && d.metadata[key]) return String(d.metadata[key]);
      return "";
    }
    function hasMetaValue(key) {
      return Object.keys(nodeIndex).some(function (id) { return !!metaValue(nodeIndex[id], key); });
    }

    // Backlinks map (target → [source,...]). Computed if absent.
    var backlinks = bundle.backlinks || {};
    if (!bundle.backlinks) {
      backlinks = {};
      bundle.edges.forEach(function (e) {
        var s = e.data.source, t = e.data.target;
        if (!t) return;
        (backlinks[t] = backlinks[t] || []);
        if (backlinks[t].indexOf(s) < 0) backlinks[t].push(s);
      });
    }
    // Outgoing map (source → [target,...]).
    var outgoing = {};
    bundle.edges.forEach(function (e) {
      var s = e.data.source, t = e.data.target;
      if (!t) return;
      (outgoing[s] = outgoing[s] || []);
      if (outgoing[s].indexOf(t) < 0) outgoing[s].push(t);
    });

    // iter2 CRI2-006: level-of-detail stopgap for large graphs. A
    // real catalog bundle easily hits 200-500 concepts; rendering every node
    // through a cose layout multi-second jank on first paint and pan/zoom
    // stays sluggish. The stopgap: when the bundle has more than
    // GRAPH_LOD_THRESHOLD nodes, render only the top-N by degree (the
    // highest-connectivity hubs, which carry the structure) and surface a
    // "Showing N of M nodes. Show all" pill. Clicking the pill streams the
    // rest in + switches to a faster grid layout (cose on 500 nodes is the
    // jank source). The full set stays accessible via the keyboard node
    // index (buildNodeIndex lists every node regardless of LOD), and
    // showDetail/applyPresenceFocus add a focused node on demand so the
    // spatial-presence halo still works on a hidden node (decluster-on-focus).
    var GRAPH_LOD_THRESHOLD = window.OKF_LOOM_GRAPH_LOD_THRESHOLD || 100;
    var totalNodeCount = bundle.nodes.length;
    var degreeOf = {};
    bundle.nodes.forEach(function (n) {
      var id = n.data.id;
      degreeOf[id] = (outgoing[id] || []).length + (backlinks[id] || []).length;
    });
    // shownIds: the node ids currently in the Cytoscape canvas. Starts as the
    // full set when under threshold, or the top-N by degree when over.
    var shownIds = {};
    var lodActive = false;
    var lodPill = null;
    if (totalNodeCount > GRAPH_LOD_THRESHOLD) {
      lodActive = true;
      // Sort a copy of nodes by (degree desc, id asc) for determinism, take N.
      var ranked = bundle.nodes.slice().sort(function (a, b) {
        var da = degreeOf[a.data.id] || 0, db = degreeOf[b.data.id] || 0;
        if (da !== db) return db - da;
        var la = String(a.data.id), lb = String(b.data.id);
        return la < lb ? -1 : (la > lb ? 1 : 0);
      });
      // Keep at least the threshold; never more than totalNodeCount.
      var keep = Math.min(GRAPH_LOD_THRESHOLD, totalNodeCount);
      for (var si = 0; si < keep; si++) shownIds[ranked[si].data.id] = true;
    } else {
      bundle.nodes.forEach(function (n) { shownIds[n.data.id] = true; });
    }

    // elemFor(n): builds the Cytoscape node datum from a bundle node.
    // `baseColor`/`size` are the intrinsic type colour + body-length size;
    // `color`/`vizSize` are the CURRENT display values the Signal-controls
    // "Colour by" and "Scale nodes by importance" options drive (so the
    // intrinsic values are always restorable). Keeping both means a colour/
    // size override never destroys the original mapping (state-ownership).
    // Phase 3: node glyphs. The server ships icon_paths (icon-key → SVG
    // inner markup, one source of truth with the wiki) + a per-node icon
    // key. Each key becomes a white-stroke SVG data-URI drawn on the
    // type-coloured node. Shapes stay sparse — the icon is the real
    // differentiator; shape only marks the big structural families.
    var ICON_PATHS = bundle.icon_paths || {};
    var _iconUriCache = {};
    function iconDataUri(key) {
      if (!key || !ICON_PATHS[key]) key = "file";
      if (!ICON_PATHS[key]) return null;
      if (_iconUriCache[key]) return _iconUriCache[key];
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        + ' stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + ICON_PATHS[key] + '</svg>';
      var uri = "data:image/svg+xml;utf8," + encodeURIComponent(svg);
      _iconUriCache[key] = uri;
      return uri;
    }
    var SHAPE_FOR_ICON = {
      database: "barrel", table: "barrel",            // data at rest
      server: "round-hexagon",                          // running systems
      branch: "round-diamond",                          // decisions
    };
    function shapeForIcon(key) { return SHAPE_FOR_ICON[key] || "ellipse"; }

    function elemFor(n) {
      var base = n.data.color || palette[n.data.type] || "#94a3b8";
      // Review feedback: smaller base node sizes. With the MAX_ZOOM clamp the
      // old 32..80 range rendered as giant discs on small bundles, colliding
      // with labels/edges (reviewer blocker 1). 22..44 reads cleanly.
      var sz = n.data.size || (22 + Math.min(22, ((bodies[n.data.id] || "").length / 320)));
      var iconKey = n.data.icon || "file";
      return {
        data: {
          id: n.data.id,
          label: n.data.label || n.data.id,
          type: n.data.type,
          description: n.data.description || "",
          resource: n.data.resource || "",
          tags: n.data.tags || [],
          color: base,
          baseColor: base,
          size: sz,
          vizSize: sz,
          iconUrl: iconDataUri(iconKey),
          shape: shapeForIcon(iconKey),
        }
      };
    }
    // edgeVisible(e): true only when BOTH endpoints are currently shown.
    function edgeVisible(e) {
      return shownIds[e.data.source] && shownIds[e.data.target];
    }

    // Cytoscape elements: shown nodes + deduped edges between shown nodes.
    var elems = [];
    var seenEdge = {};
    bundle.nodes.forEach(function (n) {
      if (!shownIds[n.data.id]) return;
      elems.push(elemFor(n));
    });
    bundle.edges.forEach(function (e) {
      if (!edgeVisible(e)) return;
      var key = e.data.source + "__" + e.data.target;
      if (seenEdge[key]) return;
      seenEdge[key] = 1;
      var edgeData = { id: key, source: e.data.source, target: e.data.target, weight: 0.5 };
      if (e.data.label) edgeData.label = e.data.label;
      elems.push({ data: edgeData });
    });

    // ====================================================================
    // Signal controls — client-side metric engine
    // --------------------------------------------------------------------
    // Everything below is computed from fields ALREADY present in
    // graph.json (degree, backlinks, tags, entities, citations, typed
    // relations) — no backend contract change (contract-runtime-parity).
    // Scores normalise to 0..1 per graph with all-zero/empty-safe handling
    // so a uniform or empty bundle never divides by zero.
    // ====================================================================
    function arr(v) { return Array.isArray(v) ? v : []; }
    function citeKey(c) { return (c && (c.id || c.text)) || ""; }
    function setOf(list, keyFn) {
      var s = {};
      arr(list).forEach(function (x) {
        var k = keyFn ? keyFn(x) : x;
        if (k) s[String(k).toLowerCase()] = true;
      });
      return s;
    }
    function overlap(aSet, list, keyFn) {
      var n = 0;
      arr(list).forEach(function (x) {
        var k = keyFn ? keyFn(x) : x;
        if (k && aSet[String(k).toLowerCase()]) n++;
      });
      return n;
    }
    function entKey(e) { return e && (e.id || e.label); }

    // Per-node incident typed-relation count (resolved labelled edges) and
    // dominant incident edge label (for "Group by relation type").
    var incidentLabeled = {};
    var nodeRelLabel = {};
    var relationLabelSet = {};
    var relationLabelList = [];
    (function () {
      var counts = {};
      bundle.edges.forEach(function (e) {
        if (!e.data.label) return;
        if (!relationLabelSet[e.data.label]) { relationLabelSet[e.data.label] = true; relationLabelList.push(e.data.label); }
        [e.data.source, e.data.target].forEach(function (nid) {
          incidentLabeled[nid] = (incidentLabeled[nid] || 0) + 1;
          counts[nid] = counts[nid] || {};
          counts[nid][e.data.label] = (counts[nid][e.data.label] || 0) + 1;
        });
      });
      relationLabelList.sort();
      Object.keys(counts).forEach(function (nid) {
        var best = null, bestN = -1;
        Object.keys(counts[nid]).forEach(function (lab) {
          if (counts[nid][lab] > bestN) { bestN = counts[nid][lab]; best = lab; }
        });
        nodeRelLabel[nid] = best;
      });
    })();

    // Raw per-node scores.
    var raw = { links: {}, typed: {}, tags: {}, cites: {}, ents: {}, backl: {} };
    bundle.nodes.forEach(function (n) {
      var d = n.data, id = d.id;
      raw.links[id] = (outgoing[id] || []).length + (backlinks[id] || []).length;
      raw.typed[id] = arr(d.relations).length + (incidentLabeled[id] || 0);
      raw.cites[id] = arr(d.citations).length;
      raw.backl[id] = (backlinks[id] || []).length;
      var tagSet = setOf(d.tags);
      var entSet = setOf(d.entities, entKey);
      var nbrs = {};
      (outgoing[id] || []).forEach(function (t) { nbrs[t] = true; });
      (backlinks[id] || []).forEach(function (s) { nbrs[s] = true; });
      var tg = 0, en = 0;
      Object.keys(nbrs).forEach(function (nid) {
        var nd = nodeIndex[nid];
        if (!nd) return;
        tg += overlap(tagSet, nd.tags);
        en += overlap(entSet, nd.entities, entKey);
      });
      raw.tags[id] = tg;
      raw.ents[id] = en;
    });
    function maxOf(map) { var m = 0; Object.keys(map).forEach(function (k) { if (map[k] > m) m = map[k]; }); return m; }
    var rmax = {}, hasSignal = {};
    Object.keys(raw).forEach(function (k) { rmax[k] = maxOf(raw[k]); hasSignal[k] = rmax[k] > 0; });
    function normNode(metric, id) {
      var mx = rmax[metric];
      return mx ? (raw[metric][id] || 0) / mx : 0;
    }

    // Deduped edge list + per-signal raw/normalised edge scores.
    var edgeList = [], edgeSeenM = {}, edgeHasLabel = {};
    bundle.edges.forEach(function (e) {
      var s = e.data.source, t = e.data.target;
      if (!s || !t) return;
      var key = s + "__" + t;
      if (edgeSeenM[key]) { if (e.data.label) edgeHasLabel[key] = true; return; }
      edgeSeenM[key] = true;
      edgeList.push({ key: key, s: s, t: t });
      if (e.data.label) edgeHasLabel[key] = true;
    });
    var edgeRawMap = { links: {}, typed: {}, tags: {}, cites: {}, ents: {}, backl: {} };
    edgeList.forEach(function (e) {
      var s = e.s, t = e.t, k = e.key;
      var ds = nodeIndex[s] || {}, dt = nodeIndex[t] || {};
      edgeRawMap.links[k] = ((raw.links[s] || 0) + (raw.links[t] || 0)) / 2;
      edgeRawMap.typed[k] = edgeHasLabel[k] ? 1 : 0;
      edgeRawMap.tags[k] = overlap(setOf(ds.tags), dt.tags);
      edgeRawMap.ents[k] = overlap(setOf(ds.entities, entKey), dt.entities, entKey);
      edgeRawMap.cites[k] = overlap(setOf(ds.citations, citeKey), dt.citations, citeKey);
      edgeRawMap.backl[k] = (raw.backl[t] || 0);
    });
    var emax = {}, eHasSignal = {};
    Object.keys(edgeRawMap).forEach(function (k) { emax[k] = maxOf(edgeRawMap[k]); eHasSignal[k] = emax[k] > 0; });
    function normEdge(metric, key) {
      var mx = emax[metric];
      return mx ? (edgeRawMap[metric][key] || 0) / mx : 0;
    }

    // ---- Control state (single source of truth) -------------------------
    var SIGNAL_METRIC = { links: "links", typed: "typed", tags: "tags", cites: "cites", ents: "ents", backl: "backl" };
    var SIGNAL_LABELS = {
      mixed: "mixed signal", links: "link count", typed: "typed relations",
      tags: "shared tags", cites: "citations", ents: "entity overlap", backl: "backlinks"
    };
    var GROUP_LABELS = {
      community: "theme",
      none: "nothing", type: "type", tag: "tag", relation: "relation type",
      folder: "folder", neighborhood: "neighbourhood",
      graph_cluster: "graph cluster", source_system: "source system",
      project: "project", section: "section", import_batch: "import batch",
      redmine_project: "Redmine project"
    };
    var MIN_THRESH = [0, 0.25, 0.5, 0.72];
    var MIN_LABELS = ["Show all", "Weak and up", "Medium and up", "Strong only"];
    // Widened from 3 → 5 steps with a much higher ceiling so the user
    // can push nodes far apart / pull groups together hard (the 3-step range was
    // "too bunched up"). Index 1 (Balanced / Clear / Medium) stays the calm
    // default; presets sit in the low-mid band and the high end is reached
    // manually. Slider `max` is derived from these arrays' length (see rangeRow
    // calls) so index ↔ value ↔ label never drift out of lockstep.
    var SPACE_LABELS = ["Compact", "Balanced", "Open", "Wide", "Vast"];
    var SEP_LABELS = ["Soft", "Clear", "Wide", "Far", "Expanse"];
    var STR_LABELS = ["Low", "Medium", "High", "Strong", "Magnetic"];
    var SPACING = [0.55, 1.0, 1.7, 2.6, 4.0], SEPAR = [0.55, 1.0, 1.8, 2.8, 4.2], GROUPF = [0.3, 0.6, 0.9, 1.25, 1.6];
    var BRIDGE_CUT = 0.55;

    var controlState = {
      // Map is the default lens — community colors + PageRank
      // sizing on a force layout (the Gephi/InfraNodus composite; type is
      // carried by the node ICON, not color).
      preset: "map", lens: "map", layout: layoutName,
      spacing: 1, separation: 1, groupStrength: 1,
      groupBy: "community",
      colorMode: "community", sizeMode: "pagerank",
      // Compatibility signal-engine fields (edge weighting still uses them).
      colorBy: "type", primarySignal: "mixed", minLevel: 0,
      boostTyped: false, boostBacklinks: false,
      scaleByImportance: false, showBridges: false,
      focusEnabled: false, focusDepth: 1,
      // Review feedback: relation encoding lives on EDGES (colour + dash +
      // label), not node fills; edge labels are shown contextually to cut
      // clutter (reviewer blockers 6/8).
      relationEdges: false, showEdgeLabels: false,
      // Atlas calm default: hide weak untyped edges on Map unless the user
      // opts into full density.
      showAllEdges: false,
      // Phase 4: legend-chip type filter (type name → true when hidden).
      hiddenTypes: {},
      search: "", type: ""
    };
    var currentLayoutName = layoutName;
    var focusRoot = null;
    // Lens state — declared early: groupKeyFor('community') runs
    // during the FIRST applyGrouping pass, before the analytics block's
    // statements execute (var initializers do not hoist).
    var communityOf = {};
    var _lensCache = {};
    var panelEl = null;   // the Signal-controls <details>, for overlay-aware fit
    var GRAPH_EMPTY = totalNodeCount === 0;

    // Does the bundle expose a usable folder structure in its ids? Only then
    // do we offer "Group by folder" (advisor: do not advertise path grouping
    // unless it is reliably computable from ids).
    function folderOf(id) { var i = String(id).lastIndexOf("/"); return i > 0 ? id.slice(0, i) : "root"; }
    var HAS_FOLDERS = (function () {
      var seen = {}, n = 0;
      Object.keys(nodeIndex).forEach(function (id) { var f = folderOf(id); if (!seen[f]) { seen[f] = true; n++; } });
      return n > 1;
    })();
    var HAS_GRAPH_CLUSTER = hasMetaValue("graph_cluster");
    var HAS_SOURCE_SYSTEM = hasMetaValue("source_system");
    var HAS_PROJECT = hasMetaValue("project");
    var HAS_SECTION = hasMetaValue("section");
    var HAS_IMPORT_BATCH = hasMetaValue("import_batch");
    var HAS_REDMINE_PROJECT = hasMetaValue("redmine_project");

    // Connected components (for "Group by neighbourhood"); static per graph.
    var components = (function () {
      var comp = {}, cid = 0;
      Object.keys(nodeIndex).forEach(function (id) {
        if (comp[id] != null) return;
        var stack = [id]; comp[id] = cid;
        while (stack.length) {
          var cur = stack.pop();
          (outgoing[cur] || []).concat(backlinks[cur] || []).forEach(function (nb) {
            if (comp[nb] == null && nodeIndex[nb]) { comp[nb] = cid; stack.push(nb); }
          });
        }
        cid++;
      });
      return comp;
    })();

    // ---- Grouping ------------------------------------------------------
    var groupOf = {}, groupList = [], bridgeNorm = {};
    function groupKeyFor(id) {
      var d = nodeIndex[id] || {};
      switch (controlState.groupBy) {
        case "type": return d.type || "concept";
        case "tag": return arr(d.tags)[0] || "untagged";
        case "relation": return nodeRelLabel[id] || "untyped";
        case "folder": return folderOf(id);
        case "neighborhood": return "cluster " + (components[id] != null ? components[id] : 0);
        case "graph_cluster": return metaValue(d, "graph_cluster") || "unclustered";
        case "source_system": return metaValue(d, "source_system") || "unknown source";
        case "project": return metaValue(d, "project") || "unassigned project";
        case "section": return metaValue(d, "section") || "unassigned section";
        case "import_batch": return metaValue(d, "import_batch") || "unbatched";
        case "redmine_project": return metaValue(d, "redmine_project") || "unassigned Redmine project";
        // Detected communities drive the layout's
        // pull-together forces so themes read as spatial regions.
        case "community": return "theme " + (communityOf[id] != null ? communityOf[id] : 0);
        default: return "";
      }
    }
    function applyGrouping() {
      groupOf = {};
      var seen = {};
      Object.keys(nodeIndex).forEach(function (id) {
        var g = groupKeyFor(id); groupOf[id] = g; if (g) seen[g] = true;
      });
      groupList = Object.keys(seen).sort();
      // Bridge score: how many DISTINCT groups a node's neighbours span. A
      // node wiring together many groups is a structural bridge — a cheap,
      // dependency-free topological proxy (no betweenness plugin needed).
      var rawB = {}, mx = 0;
      Object.keys(nodeIndex).forEach(function (id) {
        var gs = {};
        (outgoing[id] || []).concat(backlinks[id] || []).forEach(function (nb) {
          var g = groupOf[nb]; if (g) gs[g] = true;
        });
        var c = Object.keys(gs).length;
        rawB[id] = c; if (c > mx) mx = c;
      });
      Object.keys(rawB).forEach(function (id) { bridgeNorm[id] = mx ? rawB[id] / mx : 0; });
    }

    // ---- Weighting (importance per chosen signal + boosts) -------------
    function applyBoost(base, typedN, backlN) {
      var num = base, denom = 1;
      if (controlState.boostTyped) { num += 0.5 * typedN; denom += 0.5; }
      if (controlState.boostBacklinks) { num += 0.5 * backlN; denom += 0.5; }
      var v = num / denom;
      return v < 0 ? 0 : (v > 1 ? 1 : v);
    }
    function nodeMixed(id) {
      var parts = ["links", "typed", "tags", "cites", "ents", "backl"], sum = 0, k = 0;
      parts.forEach(function (m) { if (hasSignal[m]) { sum += normNode(m, id); k++; } });
      return k ? sum / k : 0;
    }
    function nodeWeight(id) {
      var sig = controlState.primarySignal;
      var base = sig === "mixed" ? nodeMixed(id) : normNode(SIGNAL_METRIC[sig], id);
      return applyBoost(base, normNode("typed", id), normNode("backl", id));
    }
    function edgeMixed(key) {
      var parts = ["links", "typed", "tags", "cites", "ents", "backl"], sum = 0, k = 0;
      parts.forEach(function (m) { if (eHasSignal[m]) { sum += normEdge(m, key); k++; } });
      return k ? sum / k : 0;
    }
    function edgeWeight(key) {
      var sig = controlState.primarySignal;
      var base = sig === "mixed" ? edgeMixed(key) : normEdge(SIGNAL_METRIC[sig], key);
      return applyBoost(base, normEdge("typed", key), normEdge("backl", key));
    }

    // ---- Colour ramps --------------------------------------------------
    function hashHue(str) {
      var h = 0, s = String(str);
      for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
      return h;
    }
    function groupColor(g) {
      if (!g) return "#94a3b8";
      // Review feedback: when grouping BY TYPE, group colour === the stable
      // node palette so "Colour by: Group" with "Group by: Type" matches the
      // palette and never drifts between presets (reviewer blocker 5).
      if (controlState.groupBy === "type" && palette[g]) return palette[g];
      return "hsl(" + hashHue(g) + ", 58%, 55%)";
    }
    // Relation-type edge colour (slightly darker/saturated for line contrast).
    // Phase 3: typed edges are ALWAYS hue-coded, quietly — full saturation
    // is reserved for the explicit Relations/Focus intent (strong=true).
    // The muted variant keeps the default canvas calm while still letting
    // "same relationship = same colour" register peripherally.
    function relColor(label, strong) {
      return strong
        ? "hsl(" + hashHue(label) + ", 64%, 42%)"
        : "hsl(" + hashHue(label) + ", 38%, 56%)";
    }
    function relDashClass(label) { return "okf-reld-" + (hashHue(label) % 3 + 1); }
    function importanceColor(w) {
      // Muted slate (low) → saturated teal (high). Single hue so it reads as
      // one ordered scale, not a categorical palette (a11y: ordered ramp).
      return "hsl(186, " + (18 + 52 * w).toFixed(0) + "%, " + (78 - 34 * w).toFixed(0) + "%)";
    }

    // ---- Layout tuning derived from the spacing/separation/strength knobs
    function idealEdgeFn(spaceMul, sepMul, gf) {
      return function (edge) {
        var base = 140 * spaceMul;
        if (controlState.groupBy === "none") return base;
        var same = groupOf[edge.data("source")] === groupOf[edge.data("target")];
        // Review feedback: FLOOR same-group edge length so high Group strength
        // can never collapse members into an overlapping pile (blocker 1). The
        // floor scales with node size (~3× node diameter), so even at the widened
        // Magnetic group strength members stay separated (resolveOverlaps is the
        // hard backstop regardless).
        // Stronger inter-group coefficient (1.0 → 1.2) so high Group
        // strength + Cluster separation push distinct groups FAR apart while
        // members stay tightly clustered — the "into groups more" contrast.
        return same ? Math.max(95 * spaceMul, base * (1 - 0.30 * gf))
                    : base * (1 + 1.2 * gf * sepMul);
      };
    }
    function layoutTuning(state) {
      var sp = SPACING[state.spacing], se = SEPAR[state.separation], gf = GROUPF[state.groupStrength];
      // Review feedback: small/dense bundles (e.g. the 6-node demo, near-
      // complete) collapse into an overlapping pile under plain cose. Boost
      // the effective spacing + repulsion and cut gravity hard so the few
      // nodes push apart into a readable spread; tapers to 1× by ~16 nodes so
      // large bundles are unaffected.
      var nN = (typeof cy !== "undefined" && cy && cy.nodes) ? cy.nodes().length : 30;
      var smallBoost = nN <= 14 ? Math.min(3, 16 / Math.max(nN, 4)) : 1;
      var spE = sp * smallBoost;
      return {
        // For tiny/dense graphs, amplify repulsion AND loosen edge springs so
        // a near-complete cluster opens up instead of collapsing to a ball.
        nodeRepulsion: 18000 * (0.7 + 0.6 * spE) * smallBoost,
        // Strong overlap repulsion, scaled up for small/dense graphs so the
        // few nodes never sit on top of each other.
        nodeOverlap: 24 * smallBoost,
        idealEdgeLength: idealEdgeFn(spE, se, gf),
        edgeElasticity: smallBoost > 1 ? 0.12 : 0.45,
        numIter: smallBoost > 1 ? 5000 : 3500,
        componentSpacing: 120 * spE * se,
        gravity: (0.16 / se) / smallBoost,
        spacingFactor: 1.0 + 0.5 * spE + 0.3 * (se - 1),
        minNodeSpacing: 30 + 40 * spE,
        radius: 200 + 150 * spE,
        sort: (state.groupBy === "none") ? undefined : function (a, b) {
          var ga = groupOf[a.id()] || "", gb = groupOf[b.id()] || "";
          if (ga < gb) return -1;
          if (ga > gb) return 1;
          var la = String(a.data("label") || a.id()), lb = String(b.data("label") || b.id());
          return la < lb ? -1 : (la > lb ? 1 : 0);
        }
      };
    }

    // Compute the initial (default-preset) grouping so the first layout is
    // already grouped-by-type rather than a flat hairball.
    applyGrouping();

    var container = document.getElementById("okf-graph");
    var cy = window.cytoscape({
      container: container,
      elements: elems,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            // Phase 3 node glyphs: type icon (white stroke SVG data-URI)
            // centered on the coloured node; shape marks the structural
            // family (barrel = data, hexagon = service, diamond = decision).
            "shape": "data(shape)",
            "background-image": "data(iconUrl)",
            "background-width": "55%",
            "background-height": "55%",
            "background-position-x": "50%",
            "background-position-y": "50%",
            "background-clip": "node",
            "label": "data(label)",
            "color": GRAPH_COLORS.light.nodeText,
            // Review feedback: node names read "near-microscopic" at the
            // widened max spread (the auto-fit zooms the big graph out, so the
            // rendered size is font-size × zoom; measured max-range zoom is
            // ~0.47–0.71). A bump (11→13) plus a 600 weight keeps the identity
            // labels legible at the extreme end, and the min-zoomed floor is
            // lowered 7→6 so node IDENTITIES stay on-screen across that whole
            // zoom band (13 × 0.47 ≈ 6 ≥ 6) instead of vanishing in the wider
            // layouts — the reviewer's "users cannot identify nodes". Nodes keep
            // a lower floor than the secondary edge labels (which hide at 9), so
            // identity survives zoom-out while edge-label noise is dropped first.
            "font-size": 13,
            "font-weight": 600,
            "min-zoomed-font-size": 6,
            "text-valign": "bottom",
            "text-margin-y": 4,
            "text-wrap": "wrap",
            "text-max-width": 110,
            // Review feedback: a soft label plate keeps node names legible over
            // edges and neighbouring nodes (reviewer blocker 1 collisions); the
            // plate is a touch more opaque (0.72→0.85) so the bolder text stays
            // crisp over edges at high spread. Padding stays 2 so the plate does
            // not enlarge the label footprint at the compact default.
            "text-background-color": GRAPH_COLORS.light.edgeLabelBg,
            "text-background-opacity": 0.85,
            "text-background-padding": 2,
            "text-background-shape": "roundrectangle",
            "width": "data(vizSize)",
            "height": "data(vizSize)",
            "border-width": 2,
            "border-color": GRAPH_COLORS.light.nodeBorder,
            "border-opacity": 0.55,
          },
        },
        {
          selector: "node:selected",
          style: {
            "border-width": 4,
            "border-color": GRAPH_COLORS.light.select,
            "underlay-color": GRAPH_COLORS.light.select,
            "underlay-opacity": 0.55,
            "underlay-padding": 18,
            "z-index": 10,
          },
        },
        {
          selector: "node.okf-hub",
          style: {
            "underlay-color": "data(color)",
            "underlay-opacity": 0.32,
            "underlay-padding": 12,
          },
        },
        {
          selector: "edge",
          style: {
            "width": "mapData(weight, 0, 1, 1.1, 3.4)",
            "opacity": "mapData(weight, 0, 1, 0.22, 0.7)",
            "line-color": GRAPH_COLORS.light.edge,
            "target-arrow-color": GRAPH_COLORS.light.edge,
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "arrow-scale": "mapData(weight, 0, 1, 0.9, 1.4)",
            "label": "",
            "font-size": 11,
            "font-weight": 600,
            "color": GRAPH_COLORS.light.edgeLabel,
            "text-rotation": "autorotate",
            "text-background-color": GRAPH_COLORS.light.edgeLabelBg,
            "text-background-opacity": 0.9,
            "text-background-padding": 2,
            "text-background-shape": "roundrectangle",
            "min-zoomed-font-size": 9,
          },
        },
        // Contextual edge labels (Relations/Focus + selected-node edges).
        { selector: "edge.okf-show-label", style: { "label": "data(label)" } },
        // Relation-type encoding lives on the edge: colour + dash + (label).
        { selector: "edge.okf-rel-edge", style: { "line-color": "data(relColor)", "target-arrow-color": "data(relColor)" } },
        { selector: "edge.okf-reld-1", style: { "line-style": "dashed", "line-dash-pattern": [6, 3] } },
        { selector: "edge.okf-reld-2", style: { "line-style": "dashed", "line-dash-pattern": [2, 3] } },
        { selector: "edge.okf-reld-3", style: { "line-style": "dashed", "line-dash-pattern": [10, 3, 2, 3] } },
        // Atlas calm default: weak untyped edges stay off the Map until the
        // user asks for density (Advanced → Show all edges / Relations).
        { selector: "edge.okf-edge-calm-hide", style: { "display": "none" } },
        // Phase 3: degree-zero concepts read as "not yet linked" — dashed
        // outline + reduced opacity (fcose tiles them into a tray).
        { selector: "node.okf-orphan", style: {
            "border-style": "dashed",
            "border-width": 2.5,
            "border-color": GRAPH_COLORS.light.edge,
            "opacity": 0.7,
            "background-opacity": 0.85,
        } },
        // Phase 4 hover states: the pointed node's neighborhood stays at
        // full opacity while everything else fades; the node itself gets a
        // soft halo. Separate class from `.dim` so filters and hover
        // compose instead of fighting.
        { selector: ".okf-hover-dim", style: { "opacity": 0.18 } },
        { selector: "node.okf-hover", style: {
            "underlay-color": GRAPH_COLORS.light.select,
            "underlay-opacity": 0.18,
            "underlay-padding": 8,
        } },
        // Phase 4 path tracing (shift-click): the chain lights up in the
        // selection accent; everything off-path fades further than hover.
        { selector: ".okf-path-dim", style: { "opacity": 0.12 } },
        { selector: "edge.okf-path", style: {
            "width": 4.5,
            "line-color": GRAPH_COLORS.light.select,
            "target-arrow-color": GRAPH_COLORS.light.select,
            "opacity": 1,
            "arrow-scale": 1.6,
            "line-style": "dashed",
            "line-dash-pattern": [8, 4],
            "z-index": 9,
          } },
        { selector: "node.okf-path", style: {
            "border-width": 3,
            "border-color": GRAPH_COLORS.light.select,
            "underlay-color": GRAPH_COLORS.light.select,
            "underlay-opacity": 0.22,
            "underlay-padding": 8,
            "z-index": 9,
          } },
        {
          selector: "edge:selected",
          style: {
            "line-color": GRAPH_COLORS.light.select,
            "target-arrow-color": GRAPH_COLORS.light.select,
            "line-style": "solid",
            "width": 5,
            "opacity": 1,
          },
        },
        // Review feedback: bridge cue is now a CALM soft glow (underlay), not a
        // heavy double-black ring (reviewer blocker 1). Still a non-colour-only
        // SHAPE/halo cue; cross-group bridge edges stay dashed.
        { selector: "node.okf-bridge", style: {
            "underlay-color": GRAPH_COLORS.light.select,
            "underlay-opacity": 0.16,
            "underlay-padding": 6,
            "border-width": 2,
        } },
        { selector: "edge.okf-bridge-edge", style: { "line-style": "dashed" } },
        // Focus root: strong selection halo so the focused node is unmistakable.
        { selector: "node.okf-focus-root", style: {
            "underlay-color": GRAPH_COLORS.light.select,
            "underlay-opacity": 0.38,
            "underlay-padding": 14,
            "border-width": 4,
            "border-color": GRAPH_COLORS.light.select,
            "z-index": 30,
        } },
        // Non-neighbour dim during focus is stronger (~12%); reviewer asked ≤30%.
        { selector: ".dim", style: { opacity: 0.12 } },
        // iter1 CRI-004: presence halo. When the agent's presence.focus
        // points at a concept, graph.js adds this class to that node so the
        // agent is spatially visible on the canvas, not just in the status
        // chip. The border uses the selection (--okf-select) teal so the
        // halo is theme-aware and distinct from auto-generated node fills.
        { selector: ".okf-presence-halo", style: {
            "border-width": 4,
            "border-color": GRAPH_COLORS.light.select,
            "border-opacity": 0.9,
            "z-index": 20,
          } },
      ],
      // Seed with a cheap grid; the real tuned layout runs once via
      // runLayoutNow() AFTER the panel exists, so its overlay-aware fit can
      // measure the panel footprint and its layoutstop re-fits a settled graph
      // (fixes the init over-zoom + off-centre cluster).
      layout: layoutOpts("grid", { animate: false, fit: false }),
      wheelSensitivity: 0.2,
    });
    // Review feedback: clamp zoom. cy.fit on a small/collapsed cluster used to
    // over-zoom (font-size scales with zoom → giant labels). maxZoom caps both
    // user and programmatic zoom; overlayAwareFit() also respects it.
    var MAX_ZOOM = 1.6;
    try { cy.maxZoom(MAX_ZOOM); cy.minZoom(0.06); } catch (e) {}
    // Bridge/focus glow + bridge border mirror the theme select colour.
    function syncBridgeColour() {
      var pal = graphPalette();
      cy.style().selector("node.okf-bridge").style("underlay-color", pal.select).update();
      cy.style().selector("node.okf-bridge").style("border-color", pal.bridgeBorder).update();
      cy.style().selector("node.okf-focus-root").style("underlay-color", pal.select).update();
      cy.style().selector("node.okf-focus-root").style("border-color", pal.select).update();
    }

    // The theme tweaks the label colour.
    // P2-72: edge label text-background was hardcoded #ffffff (failed in
    //   dark mode). P3-11: also re-sync on prefers-color-scheme change so
    //   users with no explicit theme follow OS colour scheme toggles.
    // P2-5 (iter-2): all literals now read from the GRAPH_COLORS constants
    //   block (mirrors wiki.css theme tokens). Selection border + edge
    //   selection line/arrow are also re-synced here so a theme change
    //   updates them to the active theme's --okf-select value.
    function syncLabelColour() {
      var pal = graphPalette();
      cy.style().selector("node").style("color", pal.nodeText).update();
      cy.style().selector("node").style("border-color", pal.nodeBorder).update();
      cy.style().selector("node").style("text-background-color", pal.edgeLabelBg).update();
      cy.style().selector("edge").style("line-color", pal.edge).update();
      cy.style().selector("edge").style("target-arrow-color", pal.edge).update();
      cy.style().selector("edge").style("text-background-color", pal.edgeLabelBg).update();
      cy.style().selector("edge").style("color", pal.edgeLabel).update();
      cy.style().selector("node:selected").style("border-color", pal.select).update();
      cy.style().selector("node:selected").style("underlay-color", pal.select).update();
      cy.style().selector("node.okf-hub").style("underlay-color", pal.select).update();
      cy.style().selector("node.okf-path").style("border-color", pal.select).update();
      cy.style().selector("node.okf-path").style("underlay-color", pal.select).update();
      cy.style().selector("edge.okf-path").style("line-color", pal.select).update();
      cy.style().selector("edge.okf-path").style("target-arrow-color", pal.select).update();
      cy.style().selector("edge:selected").style("line-color", pal.select).update();
      cy.style().selector("edge:selected").style("target-arrow-color", pal.select).update();
      syncBridgeColour();
    }
    syncLabelColour();
    var themeBtn2 = document.getElementById("okf-theme");
    if (themeBtn2) themeBtn2.addEventListener("click", syncLabelColour);
    // P3-11: keep Cytoscape label colours in sync with OS colour-scheme.
    if (window.matchMedia) {
      var colourSchemeMq = window.matchMedia("(prefers-color-scheme: dark)");
      var colourSchemeHandler = function (e) {
        // Only follow OS pref when the user has not explicitly chosen.
        try {
          var saved = localStorage.getItem(STORAGE_KEY);
          if (saved && THEMES.indexOf(saved) >= 0) return;
        } catch (err) {}
        applyTheme(e.matches ? "dark" : "light");
        syncLabelColour();
      };
      if (colourSchemeMq.addEventListener) {
        colourSchemeMq.addEventListener("change", colourSchemeHandler);
      } else if (colourSchemeMq.addListener) {
        colourSchemeMq.addListener(colourSchemeHandler);
      }
    }

    // Legend overlay. Rebuilt by updateLegend() so it reflects the
    // active "Colour by" mode (type palette, per-group colours, or an
    // importance ramp) — never a stale key that disagrees with the canvas.
    var legend = document.createElement("div");
    legend.className = "okf-graph-legend";
    legend.setAttribute("aria-label", "Graph colour key");
    container.appendChild(legend);
    function legendRow(swatchBg, text) {
      var item = document.createElement("div");
      item.className = "okf-graph-legend__item";
      var sw = document.createElement("span");
      sw.className = "okf-graph-legend__swatch";
      sw.style.background = swatchBg;
      var label = document.createElement("span");
      label.textContent = text;
      item.appendChild(sw); item.appendChild(label);
      return item;
    }
    // Phase 4: type legend rows double as FILTER CHIPS — click toggles the
    // type's visibility, alt-click solos it (alt-click again shows all).
    // State lives in controlState.hiddenTypes; applyFilters consumes it.
    function legendTypeChip(swatchBg, t) {
      var item = legendRow(swatchBg, t);
      item.classList.add("okf-graph-legend__item--chip");
      var off = !!controlState.hiddenTypes[t];
      item.classList.toggle("okf-graph-legend__item--off", off);
      item.setAttribute("role", "button");
      item.setAttribute("tabindex", "0");
      item.setAttribute("aria-pressed", String(!off));
      item.title = "Click: show/hide " + t + " · Alt-click: only " + t;
      function toggle(alt) {
        var ht = controlState.hiddenTypes;
        if (alt) {
          var soloed = !ht[t] && Object.keys(palette).some(function (o) { return o !== t && ht[o]; })
            && Object.keys(palette).every(function (o) { return o === t ? !ht[o] : ht[o]; });
          controlState.hiddenTypes = {};
          if (!soloed) {
            Object.keys(palette).forEach(function (o) { if (o !== t) controlState.hiddenTypes[o] = true; });
          }
        } else {
          ht[t] = !ht[t];
        }
        applyFilters();
        updateLegend();
        updateStatus();
      }
      item.addEventListener("click", function (e) { toggle(e.altKey); });
      item.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e.altKey); }
      });
      return item;
    }
    // A legend row whose swatch is a coloured line segment (for edge keys),
    // with a dash variant matching the canvas dash class.
    function legendLineRow(color, dashClass, text) {
      var item = document.createElement("div");
      item.className = "okf-graph-legend__item";
      var line = document.createElement("span");
      line.className = "okf-graph-legend__line okf-legend-" + dashClass;
      line.style.borderTopColor = color;
      var label = document.createElement("span");
      label.textContent = text;
      item.appendChild(line); item.appendChild(label);
      return item;
    }
    function legendTitle(text) {
      var t = document.createElement("div");
      t.className = "okf-graph-legend__title";
      t.textContent = text;
      return t;
    }
    function updateLegend() {
      while (legend.firstChild) legend.removeChild(legend.firstChild);
      // Node colour key — driven by the active lens's colour mode.
      if (controlState.colorMode === "community") {
        var com = computeCommunities();
        var pr = computePageRank();
        legend.appendChild(legendTitle("Colour: themes"));
        com.list.slice(0, 8).forEach(function (c) {
          if (!c.ids.length) return;
          var ex = c.ids.slice().sort(function (a, b) { return (pr[b] || 0) - (pr[a] || 0); })[0];
          var d = nodeIndex[ex] || {};
          legend.appendChild(legendRow(communityColor(c.index),
            "Theme " + (c.index + 1) + " (" + (d.label || ex) + ")"));
        });
        if (com.list.length > 8) legend.appendChild(legendRow("transparent", "+ " + (com.list.length - 8) + " more"));
      } else if (controlState.colorMode === "recency") {
        legend.appendChild(legendTitle("Colour: freshness"));
        legend.appendChild(legendRow(recencyColor(7), "This month"));
        legend.appendChild(legendRow(recencyColor(90), "This quarter"));
        legend.appendChild(legendRow(recencyColor(365), "6 months +"));
        legend.appendChild(legendRow(recencyColor(Infinity), "No timestamp"));
      } else if (controlState.colorMode === "bridge") {
        legend.appendChild(legendTitle("Colour: bridging power"));
        legend.appendChild(legendRow(bridgeColor(0.1), "On few paths"));
        legend.appendChild(legendRow(bridgeColor(0.6), "Connective"));
        legend.appendChild(legendRow(bridgeColor(1), "Holds areas together"));
      } else if (controlState.colorBy === "group" && controlState.groupBy !== "type") {
        legend.appendChild(legendTitle("Colour: " + GROUP_LABELS[controlState.groupBy]));
        groupList.slice(0, 12).forEach(function (g) { legend.appendChild(legendRow(groupColor(g), g)); });
        if (groupList.length > 12) legend.appendChild(legendRow("transparent", "+ " + (groupList.length - 12) + " more"));
      } else if (controlState.colorBy === "importance") {
        legend.appendChild(legendTitle("Node colour: importance (" + SIGNAL_LABELS[controlState.primarySignal] + ")"));
        legend.appendChild(legendRow(importanceColor(0.1), "Low"));
        legend.appendChild(legendRow(importanceColor(0.5), "Medium"));
        legend.appendChild(legendRow(importanceColor(0.95), "High"));
      } else {
        legend.appendChild(legendTitle("Node colour: type"));
        Object.keys(palette).sort().forEach(function (t) { legend.appendChild(legendTypeChip(palette[t], t)); });
      }
      // Types are always filterable (icons carry type in every lens) —
      // keep the clickable chips available under the colour key.
      if (controlState.colorMode !== "type") {
        legend.appendChild(legendTitle("Filter types (icons)"));
        Object.keys(palette).sort().forEach(function (t) { legend.appendChild(legendTypeChip(palette[t], t)); });
      }
      // Relation-type EDGE key (reviewer blocker 6): when relation encoding is
      // on, map each relation label 1:1 to its edge colour + dashed line.
      if (controlState.relationEdges && relationLabelList.length) {
        legend.appendChild(legendTitle("Edge: relation type"));
        relationLabelList.slice(0, 10).forEach(function (lab) {
          legend.appendChild(legendLineRow(relColor(lab, true), relDashClass(lab), lab));
        });
      }
      legend.hidden = legend.childNodes.length === 0;
    }

    // iter2 CRI2-006: LOD "Show all" pill. Rendered only when the bundle
    // exceeded GRAPH_LOD_THRESHOLD and the hidden set is non-empty. Clicking
    // streams the remaining nodes + edges in one pass and switches to a grid
    // layout (cose on N>100 is the jank source). The pill is an accessible
    // <button> with an aria-label that names the full count.
    function refreshLodPill() {
      var hidden = 0;
      bundle.nodes.forEach(function (n) { if (!shownIds[n.data.id]) hidden++; });
      if (!lodActive || hidden === 0) {
        if (lodPill && lodPill.parentNode) lodPill.parentNode.removeChild(lodPill);
        lodPill = null;
        return;
      }
      if (!lodPill) {
        lodPill = document.createElement("button");
        lodPill.type = "button";
        lodPill.className = "okf-graph-lod-pill";
        if (container) container.appendChild(lodPill);
        lodPill.addEventListener("click", showAllGraph);
      }
      var shownCount = totalNodeCount - hidden;
      lodPill.textContent = "Showing " + shownCount + " of " + totalNodeCount + " nodes. Show all";
      lodPill.setAttribute("aria-label",
        "Showing " + shownCount + " of " + totalNodeCount + " nodes. Show every node (switches to a faster grid layout).");
    }
    // ensureNodeVisible(id): decluster-on-focus. If the requested node is not
    // currently in the canvas (LOD hid it), add it + any edges to already-
    // shown nodes, then re-run the layout so the new node is placed. Used by
    // showDetail (keyboard index + tap) and applyPresenceFocus (the spatial
    // halo) so the agent's focus is always visible even on a clustered graph.
    function ensureNodeVisible(id) {
      if (!id || shownIds[id]) return false;
      if (!nodeIndex[id]) return false;
      shownIds[id] = true;
      var nodeBundle = null;
      for (var i = 0; i < bundle.nodes.length; i++) {
        if (bundle.nodes[i].data.id === id) { nodeBundle = bundle.nodes[i]; break; }
      }
      if (nodeBundle) cy.add(elemFor(nodeBundle));
      // Add edges that now have both endpoints visible (either direction).
      bundle.edges.forEach(function (e) {
        if (!edgeVisible(e)) return;
        var key = e.data.source + "__" + e.data.target;
        if (seenEdge[key]) return;
        seenEdge[key] = 1;
        var edgeData = { id: key, source: e.data.source, target: e.data.target, weight: 0.5 };
        if (e.data.label) edgeData.label = e.data.label;
        cy.add({ data: edgeData });
      });
      // The new node/edges inherit the current weighting/colour/filter state.
      applyVisualEncoding();
      applyFilters();
      // Re-run the current layout THROUGH the single stale-safe owner so this
      // direct (non-debounced) path also stops any in-flight layout, bumps
      // layoutSeq, and fences its fit. Uses the EXPLICIT currentLayoutName
      // (cy.layoutName is not a guaranteed Cytoscape field). This replaces the
      // former direct cy.layout(...).run() + synchronous postLayout() that
      // bypassed the owner (async-lifecycle blocker).
      runLayoutNow(false);
      updateStatus();
      refreshLodPill();
      return true;
    }
    function showAllGraph() {
      // Stream every hidden node + edge in one pass, then switch to a faster
      // layout. Grid is O(n) and pan/zoom-friendly; cose on 500 nodes is the
      // jank source we are explicitly avoiding. Animate off under the reduced-
      // motion media query (graph.js honors REDUCED_MOTION globally).
      var added = [];
      bundle.nodes.forEach(function (n) {
        if (shownIds[n.data.id]) return;
        shownIds[n.data.id] = true;
        added.push(elemFor(n));
      });
      if (added.length) cy.add(added);
      bundle.edges.forEach(function (e) {
        if (!edgeVisible(e)) return;
        var key = e.data.source + "__" + e.data.target;
        if (seenEdge[key]) return;
        seenEdge[key] = 1;
        var edgeData = { id: key, source: e.data.source, target: e.data.target, weight: 0.5 };
        if (e.data.label) edgeData.label = e.data.label;
        cy.add({ data: edgeData });
      });
      // Switch to grid (fast, O(n)) and keep the control state in sync so the
      // topbar select + Signal status reflect the actual layout now on screen.
      currentLayoutName = "grid";
      controlState.layout = "grid";
      if (layoutSel) layoutSel.value = "grid";
      lodActive = false;  // user explicitly asked for all; no more auto-LOD
      // The full node/edge set inherits the current weighting/colour/filters.
      applyVisualEncoding();
      applyFilters();
      // Layout THROUGH the single stale-safe owner (was a direct cy.layout run
      // that bypassed layoutSeq/runningLayout — async-lifecycle blocker).
      runLayoutNow(false);
      updateStatus();
      refreshLodPill();
    }
    refreshLodPill();

    // P1-4 / P2-2 (iter-1): keyboard-accessible node index. Populates the
    // <details class="okf-node-index"> overlay with one <button> per node,
    // sorted by label (determinism §3.2). Each button calls showDetail(id)
    // - the same handler Cytoscape's tap uses - so keyboard users get full
    // graph access (WCAG 2.1.1).
    buildNodeIndex(bundle);

    // ====================================================================
    // Signal controls — apply layer (style / filter / layout / status)
    // ====================================================================

    // Re-derive node size/colour, edge weight, and bridge cues from the
    // current control state. Writes go through data() + class toggles inside
    // cy.batch() so the stylesheet's mapData()/class selectors keep ownership
    // (an inline ele.style bypass would clobber .dim and :selected).
    function applyVisualEncoding() {
      // Lens encodings — computed lazily by applyLens, read here.
      var pr = controlState.sizeMode === "pagerank" ? computePageRank() : null;
      var bt = (controlState.sizeMode === "betweenness" || controlState.colorMode === "bridge" ||
                controlState.showBridges) ? computeBetweenness() : null;
      if (controlState.colorMode === "community" || controlState.groupBy === "community") computeCommunities();
      // Hub threshold: top-decile degree among currently rendered nodes.
      var degrees = [];
      cy.nodes().forEach(function (n) { degrees.push(n.degree(false)); });
      degrees.sort(function (a, b) { return a - b; });
      var hubCut = degrees.length ? degrees[Math.max(0, Math.floor(degrees.length * 0.9))] : 999;
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          var id = n.id();
          var vs = n.data("size");
          if (controlState.sizeMode === "pagerank" && pr) vs = 24 + 44 * (pr[id] || 0);
          else if (controlState.sizeMode === "betweenness" && bt) vs = 22 + 50 * (bt[id] || 0);
          else if (controlState.scaleByImportance) vs = 26 + 44 * nodeWeight(id);
          n.data("vizSize", vs);
          var col = n.data("baseColor");
          if (controlState.colorMode === "community") col = communityColor(communityOf[id] || 0);
          else if (controlState.colorMode === "recency") col = recencyColor(recencyDays(id));
          else if (controlState.colorMode === "bridge" && bt) col = bridgeColor(bt[id] || 0);
          else if (controlState.colorBy === "group") col = groupColor(groupOf[id]);
          else if (controlState.colorBy === "importance") col = importanceColor(nodeWeight(id));
          n.data("color", col);
          // Bridges: the glow marks the TOP bridgers by real betweenness
          // (was: heuristic cross-group edge counting).
          n.toggleClass("okf-bridge", !!(controlState.showBridges && bt && (bt[id] || 0) >= BRIDGE_CUT));
          // Phase 3: degree-zero concepts carry the "not yet linked" cue.
          n.toggleClass("okf-orphan", n.degree(false) === 0);
          // Soft halo on hubs (top-decile degree) for the atlas look.
          var deg = n.degree(false);
          n.toggleClass("okf-hub", deg >= hubCut);
        });
        cy.edges().forEach(function (e) {
          e.data("weight", edgeWeight(e.id()));
          var cross = controlState.groupBy !== "none" && groupOf[e.source().id()] !== groupOf[e.target().id()];
          e.toggleClass("okf-bridge-edge", controlState.showBridges && cross);
          // Relation-type encoding on the edge (colour + dash). Phase 3:
          // typed edges keep their hue ALWAYS — muted by default, full
          // saturation + dash pattern under the explicit Relations intent.
          var lab = e.data("label");
          e.removeClass("okf-rel-edge okf-reld-1 okf-reld-2 okf-reld-3");
          if (lab) {
            e.data("relColor", relColor(lab, !!controlState.relationEdges));
            e.addClass("okf-rel-edge");
            if (controlState.relationEdges) e.addClass(relDashClass(lab));
          }
        });
      });
      applyEdgeLabels();
      updateLegend();
      updateNodeIndexSwatches();
    }

    // Toggle the per-edge `.okf-show-label` class. Labels show in Relations/
    // Focus presets (showEdgeLabels) and for edges touching the currently-
    // selected node, so context appears without global clutter.
    //
    // Review feedback: the IMPLICIT selected-node labels are suppressed once
    // Node spacing is high (index >= 3, Wide/Vast). At that spread the auto-fit
    // zooms the graph out so far that those rotated relationship labels render
    // sub-legible and read as faint noise (the reviewer's edge-label P1). Hiding
    // them at high spread keeps the canvas clean; they return as soon as spacing
    // drops or the user zooms/selects in. The EXPLICIT Relations/Focus path
    // (showEdgeLabels) is untouched — that is deliberate "show the relationships"
    // intent and stays on the existing min-zoomed-font-size LOD.
    function applyEdgeLabels() {
      var selId = null;
      var sel = cy.nodes(":selected");
      if (sel && sel.length) selId = sel.id();
      var contextualOk = controlState.spacing < 3;   // hide selected-node labels at Wide/Vast
      cy.batch(function () {
        cy.edges().forEach(function (e) {
          var show = false;
          if (e.data("label")) {
            if (controlState.showEdgeLabels) show = true;
            else if (contextualOk && selId && (e.source().id() === selId || e.target().id() === selId)) show = true;
          }
          e.toggleClass("okf-show-label", show);
        });
      });
    }

    // Ids within `depth` hops of `root`, over the CURRENTLY-rendered graph
    // (LOD-hidden neighbours are out of scope; documented in the status line).
    function neighborhoodIds(root, depth) {
      var start = cy.getElementById(root);
      if (!start || !start.length) return null;
      var acc = start;
      for (var i = 0; i < depth; i++) acc = acc.closedNeighborhood();
      var ids = {};
      acc.nodes().forEach(function (nn) { ids[nn.id()] = true; });
      return ids;
    }

    // ONE combined filter pass: search + type + focus-neighbourhood +
    // minimum-signal threshold, all expressed as `.dim`. Replaces the old
    // independent handlers that each cleared the others' dim state.
    function applyFilters() {
      var q = controlState.search, ty = controlState.type;
      var thresh = MIN_THRESH[controlState.minLevel] || 0;
      var focusSet = (controlState.focusEnabled && focusRoot) ? neighborhoodIds(focusRoot, controlState.focusDepth) : null;
      var calmMode = !controlState.showAllEdges && !controlState.relationEdges &&
        (controlState.lens === "map" || controlState.lens === "themes" ||
         controlState.lens === "bridges" || controlState.lens === "recent");
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          var d = n.data(), dim = false;
          if (q) {
            var hay = ((d.label || "") + " " + n.id() + " " + (d.tags || []).join(" ")).toLowerCase();
            dim = hay.indexOf(q) < 0;
          }
          if (!dim && ty) dim = d.type !== ty;
          if (!dim && controlState.hiddenTypes[d.type]) dim = true;  // legend chips (phase 4)
          if (!dim && focusSet) dim = !focusSet[n.id()];
          n.toggleClass("dim", dim);
        });
      });
      // After node dims settle, pick the sparse edge set for Map-like lenses.
      var calmKeep = calmMode ? computeCalmEdgeKeep() : null;
      cy.batch(function () {
        cy.edges().forEach(function (e) {
          var dim = e.source().hasClass("dim") || e.target().hasClass("dim");
          if (!dim && thresh > 0) {
            var keep = controlState.showBridges && e.hasClass("okf-bridge-edge");
            if (!keep && (e.data("weight") || 0) < thresh) dim = true;
          }
          e.toggleClass("dim", dim);
          // Mockup Map: sparse constellation — only strongest edges stay visible.
          var calmHide = !!(calmKeep && !dim && !calmKeep[e.id()]);
          e.toggleClass("okf-edge-calm-hide", calmHide);
        });
        // Focus root halo: unmistakable marker on the focused node so Focus
        // never looks like Relations (reviewer blocker 4).
        cy.nodes().removeClass("okf-focus-root");
        if (controlState.focusEnabled && focusRoot) {
          var fr = cy.getElementById(focusRoot);
          if (fr && fr.length) fr.addClass("okf-focus-root");
        }
      });
      updateOrphanShelf();
    }

    // Keep the top-K strongest undimmed edges per node (plus any high-weight
    // labeled relations). Mimics the sparse mockup constellation.
    function computeCalmEdgeKeep() {
      var K = 2;
      var keep = Object.create(null);
      cy.nodes().forEach(function (n) {
        if (n.hasClass("dim")) return;
        var eds = n.connectedEdges().filter(function (e) {
          return !e.source().hasClass("dim") && !e.target().hasClass("dim");
        }).toArray().sort(function (a, b) {
          return (b.data("weight") || 0) - (a.data("weight") || 0);
        });
        eds.slice(0, K).forEach(function (e) { keep[e.id()] = true; });
        eds.forEach(function (e) {
          var w = e.data("weight") || 0;
          var lab = e.data("label");
          if (lab && w >= 0.55) keep[e.id()] = true;
          else if (w >= 0.78) keep[e.id()] = true;
        });
      });
      return keep;
    }

    // Debounced, stale-safe layout rerun. The latest control values always
    // win: a newer schedule STOPS the previous (possibly still-running)
    // layout before starting a fresh one from current state, so a late layout
    // completion can never re-position nodes for a superseded config
    // (async-lifecycle: no stale-result override).
    // ---- Overlay-aware fit (reviewer blockers 1/2/3) -------------------
    // Custom fit that (a) never exceeds MAX_ZOOM, and (b) keeps content out of
    // the rectangle occupied by the Signal panel / node index / legend / LOD
    // pill by insetting the available area per side. When focusing, it fits the
    // selected neighbourhood so Focus visibly isolates it.
    function overlayInsets() {
      var crect = container.getBoundingClientRect();
      var cw = crect.width, ch = crect.height;
      // Review feedback: at high Node spacing / Cluster separation the graph
      // fills the canvas and its outermost nodes + labels can sit right on the
      // viewport edges (reviewer clipping risk). Grow the margin with the spread
      // knobs so the auto-fit frames the extreme spread with breathing room. The
      // extra is 0 at the default (index 1 ⇒ spread ≈ 1.0, so the default look is
      // unchanged) and CAPPED relative to the viewport's short side, so on a
      // narrow phone strip the inset never eats the canvas (mobile stays usable).
      var spc = SPACING[controlState.spacing] != null ? SPACING[controlState.spacing] : 1;
      var sepc = SEPAR[controlState.separation] != null ? SEPAR[controlState.separation] : 1;
      var spread = Math.max(spc, sepc);                          // ~0.55 … ~4.2
      // Keep the breathing room MODEST: too much inset zooms the (already large)
      // max-range graph out far enough to push labels under their LOD floor. ~24
      // px extra clears the corners without shrinking the labels, and the mobile
      // strip is capped tighter so the inset never eats the narrow viewport.
      var extra = Math.max(0, Math.min(24, Math.round(8 * (spread - 1))));
      if (Math.min(cw, ch) < 520) extra = Math.min(extra, 16);
      var base = 22 + extra;                                      // 22 default → ~46 desktop / ~38 mobile
      var ins = { top: base, right: base, bottom: base, left: base };
      function rel(elm) {
        if (!elm || elm.hidden || !elm.getBoundingClientRect) return null;
        var r = elm.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        return { l: r.left - crect.left, r: r.right - crect.left, t: r.top - crect.top, b: r.bottom - crect.top };
      }
      // The Signal bar lives in the detail pane (outside the
      // canvas), so it only insets the fit in the compatibility in-canvas
      // fallback mount.
      var pr = (panelEl && container.contains(panelEl)) ? rel(panelEl) : null;
      if (pr) {
        // Desktop: the panel is a tall LEFT rail — inset the left column. That
        // same inset also clears the bottom-left legend (both live left).
        // Mobile: the panel is a full-width TOP drawer — inset the top instead.
        if ((pr.r - pr.l) > cw * 0.6) ins.top = Math.max(ins.top, pr.b + 14);
        else ins.left = Math.max(ins.left, pr.r + 16);
      }
      // Atlas frosted overlays float above the full-bleed canvas — reserve
      // their footprints so fit/zoom keeps the constellation readable.
      var rail = document.getElementById("okf-graph-rail");
      var detail = document.getElementById("okf-detail");
      var rr = rel(rail);
      if (rr) ins.left = Math.max(ins.left, rr.r + 12);
      var dr = rel(detail);
      if (dr) ins.right = Math.max(ins.right, (cw - dr.l) + 12);
      // Node index is a small TOP-RIGHT box; push content below its row rather
      // than reserving the whole right column (which squeezed the graph).
      var nr = rel(container.querySelector(".okf-node-index"));
      if (nr) ins.top = Math.max(ins.top, nr.b + 12);
      var pillr = rel(lodPill);
      if (pillr) ins.top = Math.max(ins.top, pillr.b + 10);
      // Never let insets consume the whole canvas (small viewports).
      if (ins.left + ins.right > cw - 70) { ins.left = Math.min(ins.left, Math.max(base, cw * 0.45)); ins.right = base; }
      if (ins.top + ins.bottom > ch - 70) { ins.bottom = base; ins.top = Math.min(ins.top, Math.max(base, ch * 0.55)); }
      return ins;
    }
    function focusTargetEles() {
      if (!controlState.focusEnabled || !focusRoot) return null;
      var root = cy.getElementById(focusRoot);
      if (!root || !root.length) return null;
      var nb = root;
      for (var i = 0; i < controlState.focusDepth; i++) nb = nb.closedNeighborhood();
      return nb.nodes().length ? nb : root;
    }
    // Review feedback: layout-agnostic overlap removal. Some layouts (notably
    // the built-in cose on a small near-complete graph) leave nodes piled on
    // top of each other regardless of repulsion/nodeOverlap tuning. This is a
    // cheap force-based separation pass that GUARANTEES a minimum gap between
    // every node pair, for any layout / graph size (capped so the O(n²) loop
    // never runs on very large sets — LOD already caps the canvas at ~100).
    function resolveOverlaps() {
      var list = cy.nodes().toArray();
      if (list.length < 2 || list.length > 260) return;
      // The guaranteed gap is now driven by the Signal knobs, not a flat
      // 32px. On small/dense bundles plain cose collapses to a pile regardless of
      // idealEdgeLength, so THIS pass — not cose — sets the real spread; keying it
      // off the knobs is what gives Node spacing / Cluster separation visible
      // reach there (the "too bunched up" fix). Each overlap correction only
      // pushes nodes apart, so node collision safety is preserved. The Compact
      // end intentionally trades some of the old flat label-clearance pad for a
      // tighter view; defaults (Balanced spacing + Clear separation) still
      // resolve to ~32px, preserving the prior look.
      // basePad: the minimum gap between ANY two nodes (Node spacing).
      var spc = SPACING[controlState.spacing] != null ? SPACING[controlState.spacing] : 1;
      var sepc = SEPAR[controlState.separation] != null ? SEPAR[controlState.separation] : 1;
      var grouped = controlState.groupBy !== "none";
      var basePad = 2 + 30 * spc;                      // ~18 (Compact) … ~122 (Vast); 32 at Balanced
      // Different-group pairs get a WIDER guaranteed gap (Cluster separation), so
      // groups read as distinct clumps even when cose has piled them together.
      // Floored at basePad so a different group is never closer than a same-group
      // pair; ==basePad at Clear separation, preserving the default.
      var sepMul = Math.max(1, 0.3 + 0.7 * sepc);      // 1.0 at Clear … ~3.2 at Expanse
      var pos = [], grp = [], i, j;
      for (i = 0; i < list.length; i++) {
        var p = list[i].position();
        pos.push({ x: p.x, y: p.y, r: list[i].width() / 2 });
        grp.push(grouped ? groupOf[list[i].id()] : null);
      }
      for (var iter = 0; iter < 60; iter++) {
        var moved = false;
        for (i = 0; i < pos.length; i++) {
          for (j = i + 1; j < pos.length; j++) {
            var dx = pos[j].x - pos[i].x, dy = pos[j].y - pos[i].y;
            var dist = Math.sqrt(dx * dx + dy * dy);
            var pad = (grouped && grp[i] !== grp[j]) ? basePad * sepMul : basePad;
            var minD = pos[i].r + pos[j].r + pad;
            if (dist < minD) {
              if (dist < 0.01) { dx = (i % 2 ? 1 : -1) * (1 + i); dy = (j % 2 ? 1 : -1) * (1 + j); dist = Math.sqrt(dx * dx + dy * dy); }
              var push = (minD - dist) / 2, ux = dx / dist, uy = dy / dist;
              pos[i].x -= ux * push; pos[i].y -= uy * push;
              pos[j].x += ux * push; pos[j].y += uy * push;
              moved = true;
            }
          }
        }
        if (!moved) break;
      }
      cy.batch(function () { for (var k = 0; k < list.length; k++) list[k].position({ x: pos[k].x, y: pos[k].y }); });
    }
    // Separate overlaps, then frame the result. Used after every layout run.
    // Phase 3: label-collision pass. resolveOverlaps() separates the DISCS;
    // this pass separates the full visual footprint — disc PLUS the wrapped
    // label plate below it — so "Customer SLA Tiers" can no longer sit on
    // top of "Orders" (the reviewer pile-up). Geometry is approximated in
    // model coords from the stylesheet constants (font 13, text-max-width
    // 110, ~6.8px/char, 15px line height); a greedy axis-of-least-overlap
    // push converges in a few iterations on LOD-capped canvases.
    function resolveLabelCollisions() {
      var list = cy.nodes().toArray();
      if (list.length < 2 || list.length > 260) return;
      function footprint(n) {
        var p = n.position(), r = (n.width() || 24) / 2;
        var label = String(n.data("label") || "");
        var perLine = 16;
        var lines = Math.max(1, Math.ceil(Math.min(label.length, 48) / perLine));
        var w = Math.max(2 * r, Math.min(110, label.length * 6.8));
        var h = lines * 15;
        return {
          x1: p.x - w / 2, x2: p.x + w / 2,
          y1: p.y - r, y2: p.y + r + 4 + h,
          n: n,
        };
      }
      for (var iter = 0; iter < 24; iter++) {
        var moved = false;
        var boxes = list.map(footprint);
        for (var i = 0; i < boxes.length; i++) {
          for (var j = i + 1; j < boxes.length; j++) {
            var a = boxes[i], b = boxes[j];
            var ox = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
            var oy = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
            if (ox <= 0 || oy <= 0) continue;
            var pa = a.n.position(), pb = b.n.position();
            if (ox < oy) {
              var px = ox / 2 + 2;
              var leftFirst = pa.x <= pb.x ? 1 : -1;
              a.n.position({ x: pa.x - px * leftFirst, y: pa.y });
              b.n.position({ x: pb.x + px * leftFirst, y: pb.y });
            } else {
              var py = oy / 2 + 2;
              var topFirst = pa.y <= pb.y ? 1 : -1;
              a.n.position({ x: pa.x, y: pa.y - py * topFirst });
              b.n.position({ x: pb.x, y: pb.y + py * topFirst });
            }
            moved = true;
            boxes[i] = footprint(a.n);
            boxes[j] = footprint(b.n);
          }
        }
        if (!moved) break;
      }
    }

    function postLayout() { resolveOverlaps(); resolveLabelCollisions(); overlayAwareFit(); }

    function overlayAwareFit(eles) {
      if (!cy.nodes().length) return;
      var target = eles || focusTargetEles();
      var nodes = (target && target.nodes && target.nodes().length) ? target : cy.elements();
      var bb = nodes.boundingBox();
      if (!bb || !isFinite(bb.w) || !isFinite(bb.h)) return;
      var ins = overlayInsets();
      var cw = container.clientWidth, ch = container.clientHeight;
      var availW = Math.max(60, cw - ins.left - ins.right);
      var availH = Math.max(60, ch - ins.top - ins.bottom);
      var z = Math.min(availW / Math.max(bb.w, 1), availH / Math.max(bb.h, 1), MAX_ZOOM);
      if (z < 0.06) z = 0.06;
      cy.zoom(z);
      cy.pan({
        x: ins.left + availW / 2 - z * (bb.x1 + bb.w / 2),
        y: ins.top + availH / 2 - z * (bb.y1 + bb.h / 2)
      });
    }

    // ---- The SINGLE stale-safe layout owner ----------------------------
    // EVERY Cytoscape layout start (debounced controls, layout select, preset,
    // LOD show-all, node-index ensure-visible) goes through runLayoutNow so
    // there is exactly one owner of layout lifecycle. It: (1) BUMPS a monotonic
    // layoutSeq, (2) stops any in-flight layout, and (3) fences the post-layout
    // fit so a completion (layoutstop / timed fallback) from an OLDER run can
    // never re-position or re-fit after a newer one started.
    // layoutStats is exposed via window.__okfLoomGraph for durable async proof.
    var runningLayout = null, layoutTimer = null, layoutSeq = 0, lastAppliedSeq = 0;
    var layoutStats = { starts: 0, applied: 0, skippedStale: 0, lastAppliedSeq: 0 };

    // Apply a layout's settle result exactly ONCE, and only if it is still the
    // current layout. Stale (superseded) completions are counted + dropped.
    function applyLayoutResult(seq) {
      if (seq !== layoutSeq) { layoutStats.skippedStale++; return; }  // superseded → fenced, no fit
      if (seq === lastAppliedSeq) return;                             // one-shot per sequence
      lastAppliedSeq = seq;
      layoutStats.applied++;
      layoutStats.lastAppliedSeq = seq;
      postLayout();
    }

    function runLayoutNow(randomize) {
      // BUMP the owner token BEFORE stopping the previous layout. Cytoscape can
      // emit `layoutstop` SYNCHRONOUSLY inside stop(); bumping first guarantees
      // that stop-triggered completion sees mySeq !== layoutSeq for the OLD run
      // and is fenced. (Previously stop() ran before ++layoutSeq, so a sync
      // stop-triggered fit could pass as current — the async-lifecycle blocker.)
      var mySeq = ++layoutSeq;
      if (runningLayout) { try { runningLayout.stop(); } catch (e) {} }
      layoutStats.starts++;
      var l = cy.layout(layoutOpts(currentLayoutName,
        Object.assign({ animate: false, randomize: !!randomize, fit: false }, layoutTuning(controlState))));
      runningLayout = l;
      // `layoutstop` is the authoritative settle signal (final positions). The
      // single timed fallback is a one-shot SAFETY NET only if layoutstop never
      // fires; applyLayoutResult() dedups by sequence so the fit + the applied
      // counter run AT MOST ONCE per layout (no double-apply from layoutstop +
      // timer). 900ms is longer than a cose settle on the LOD-capped (<=100
      // node) canvas, so the net never preempts the real final positions.
      var done = function () { applyLayoutResult(mySeq); };
      try { l.on("layoutstop", done); } catch (e) {}
      try { l.run(); } catch (e) {}
      setTimeout(done, 900);
    }
    function scheduleLayout(randomize) {
      if (layoutTimer) clearTimeout(layoutTimer);
      layoutTimer = setTimeout(function () { layoutTimer = null; runLayoutNow(randomize); }, 220);
    }

    function visibleCount(coll) { var c = 0; coll.forEach(function (e) { if (!e.hasClass("dim")) c++; }); return c; }
    function statusText() {
      var totN = cy.nodes().length, totE = cy.edges().length;
      if (totN === 0) return "No concepts in this bundle — Signal controls are inactive.";
      var visN = visibleCount(cy.nodes()), visE = visibleCount(cy.edges());
      var s = "Showing " + visN + (visN !== totN ? " of " + totN : "") + " node" + (totN === 1 ? "" : "s");
      s += " and " + visE + (visE !== totE ? " of " + totE : "") + " edge" + (totE === 1 ? "" : "s");
      if (controlState.groupBy !== "none") s += ", grouped by " + GROUP_LABELS[controlState.groupBy];
      s += ", weighted by " + SIGNAL_LABELS[controlState.primarySignal];
      if ((MIN_THRESH[controlState.minLevel] || 0) > 0) s += ". Weak links dimmed";
      if (controlState.showBridges) {
        var bc = 0; Object.keys(bridgeNorm).forEach(function (id) { if (bridgeNorm[id] >= BRIDGE_CUT) bc++; });
        if (bc) s += ". " + bc + " bridge node" + (bc === 1 ? "" : "s") + " highlighted";
      }
      if (controlState.focusEnabled && focusRoot) {
        var fl = (nodeIndex[focusRoot] && nodeIndex[focusRoot].label) || focusRoot;
        s += ". Focused on " + fl + " (" + controlState.focusDepth + " hop" + (controlState.focusDepth === 1 ? "" : "s") + ")";
      } else if (controlState.focusEnabled && !focusRoot) {
        s += ". Select a node to focus its neighbourhood";
      }
      s += ".";
      var hidden = 0; bundle.nodes.forEach(function (n) { if (!shownIds[n.data.id]) hidden++; });
      if (hidden > 0) s += " " + hidden + " more available via Show all.";
      return s;
    }
    var statusEl = null, statusTimer = null;
    function updateStatus() {
      if (!statusEl) return;
      if (statusTimer) clearTimeout(statusTimer);
      // Debounced so the aria-live region coalesces rapid slider ticks into
      // one announcement instead of speaking on every step.
      statusTimer = setTimeout(function () { statusTimer = null; statusEl.textContent = statusText(); }, 200);
    }

    function updateNodeIndexSwatches() {
      var items = document.querySelectorAll("#okf-node-index-list .okf-node-index__item");
      Array.prototype.forEach.call(items, function (btn) {
        var id = btn.getAttribute("data-target");
        var sw = btn.querySelector(".okf-node-index__swatch");
        if (!sw || !id) return;
        var col = (nodeIndex[id] && nodeIndex[id].color) || "var(--okf-accent)";
        if (controlState.colorBy === "group") col = groupColor(groupOf[id]);
        else if (controlState.colorBy === "importance") col = importanceColor(nodeWeight(id));
        sw.style.background = col;
      });
    }

    // ====================================================================
    // Signal controls — presets, panel build, control wiring
    // ====================================================================
    // ====================================================================
    // Lens analytics — real algorithms behind each lens.
    // --------------------------------------------------------------------
    // Research-grounded (see docs/design/graph-lenses.md): communities are
    // the color dimension that reveals structure (Gephi/InfraNodus/Bloom
    // convergence), PageRank finds what matters better than raw degree,
    // betweenness finds the bridgers (Kumu's "top bridgers"), and
    // timestamps make freshness a first-class lens. All computed locally,
    // lazily, and cached until the graph changes.
    function invalidateLensCache() { _lensCache = {}; }

    // Deterministic label propagation: seeds by sorted id, iterates in
    // sorted order, ties break to the SMALLEST label — same input, same
    // communities, every load. Communities are then renumbered by size
    // (0 = biggest) so colors are stable and legends read biggest-first.
    function computeCommunities() {
      if (_lensCache.communities) return _lensCache.communities;
      // Deterministic single-level-iterated Louvain (modularity
      // optimization) — the Gephi-standard community method. Label
      // propagation was tried first and collapses densely cross-linked
      // bundles into one community; modularity splits them along real
      // link-density boundaries. Determinism: nodes processed in sorted
      // order, ties broken toward the smallest community id, fixed pass
      // cap — same bundle, same communities, every load.
      var ids = Object.keys(nodeIndex).sort();
      var idx = {};
      ids.forEach(function (id, i) { idx[id] = i; });
      var n = ids.length;
      // Undirected weighted adjacency (dedup multi-edges to weight).
      var adj = ids.map(function () { return {}; });
      var m2 = 0;  // 2m (sum of all degrees)
      cy.edges().forEach(function (e) {
        var a = idx[e.source().id()], b = idx[e.target().id()];
        if (a == null || b == null || a === b) return;
        adj[a][b] = (adj[a][b] || 0) + 1;
        adj[b][a] = (adj[b][a] || 0) + 1;
        m2 += 2;
      });
      var community = ids.map(function (_, i) { return i; });
      var degree = adj.map(function (nb) {
        var d = 0; Object.keys(nb).forEach(function (k) { d += nb[k]; }); return d;
      });
      if (m2 > 0) {
        var commTot = degree.slice();       // Σtot per community
        for (var pass = 0; pass < 10; pass++) {
          var moved = false;
          for (var i = 0; i < n; i++) {
            var current = community[i];
            // Links from i into each neighbouring community.
            var kIn = {};
            Object.keys(adj[i]).forEach(function (j) {
              var c = community[j];
              kIn[c] = (kIn[c] || 0) + adj[i][j];
            });
            // Remove i from its community.
            commTot[current] -= degree[i];
            var best = current;
            var bestGain = (kIn[current] || 0) - commTot[current] * degree[i] / m2;
            Object.keys(kIn).map(Number).sort(function (a, b) { return a - b; }).forEach(function (c) {
              if (c === current) return;
              var gain = kIn[c] - commTot[c] * degree[i] / m2;
              if (gain > bestGain + 1e-9) { bestGain = gain; best = c; }
            });
            commTot[best] += degree[i];
            if (best !== current) { community[i] = best; moved = true; }
          }
          if (!moved) break;
        }
      }
      // Renumber by community size (desc), tie → smallest member id.
      var members = {};
      ids.forEach(function (id, i) {
        (members[community[i]] = members[community[i]] || []).push(id);
      });
      var ordered = Object.keys(members).sort(function (a, b) {
        var d = members[b].length - members[a].length;
        return d !== 0 ? d : (members[a][0] < members[b][0] ? -1 : 1);
      });
      var out = {}, list = [];
      ordered.forEach(function (lab, k) {
        members[lab].forEach(function (id) { out[id] = k; });
        list.push({ index: k, ids: members[lab] });
      });
      _lensCache.communities = { of: out, list: list };
      communityOf = out;
      return _lensCache.communities;
    }
    function communityColor(i) {
      // Golden-angle hues (same technique as the type palette) at a calmer
      // saturation so regions read as areas, not warnings.
      return "hsl(" + Math.round((i * 137.508) % 360) + ", 52%, 52%)";
    }

    function computePageRank() {
      if (_lensCache.pagerank) return _lensCache.pagerank;
      var out = {}, max = 0;
      try {
        var pr = cy.elements().pageRank({ dampingFactor: 0.85, precision: 1e-6 });
        cy.nodes().forEach(function (n) {
          var r = pr.rank(n);
          out[n.id()] = r;
          if (r > max) max = r;
        });
        if (max > 0) Object.keys(out).forEach(function (k) { out[k] = out[k] / max; });
      } catch (e) { /* empty graph */ }
      _lensCache.pagerank = out;
      return out;
    }

    function computeBetweenness() {
      if (_lensCache.betweenness) return _lensCache.betweenness;
      var out = {}, max = 0;
      try {
        var bc = cy.elements().betweennessCentrality({ directed: false });
        cy.nodes().forEach(function (n) {
          var b = bc.betweenness(n);
          out[n.id()] = b;
          if (b > max) max = b;
        });
        if (max > 0) Object.keys(out).forEach(function (k) { out[k] = out[k] / max; });
      } catch (e) { /* empty graph */ }
      _lensCache.betweenness = out;
      return out;
    }

    // Recency in days (Infinity = undated). Uses the authored frontmatter
    // timestamp shipped per node.
    function recencyDays(id) {
      var d = nodeIndex[id] || {};
      if (!d.timestamp) return Infinity;
      var t = Date.parse(d.timestamp);
      if (isNaN(t)) return Infinity;
      return Math.max(0, (Date.now() - t) / 86400000);
    }
    function recencyColor(days) {
      if (!isFinite(days)) return "#94a3b8";               // undated: neutral slate
      // Fresh teal → aged amber → stale grey-brown over ~180 days.
      var t = Math.min(1, days / 180);
      var hue = 175 - 135 * t;                              // 175 (teal) → 40 (amber)
      var sat = 62 - 30 * t;
      var light = 46 + 10 * t;
      return "hsl(" + Math.round(hue) + ", " + Math.round(sat) + "%, " + Math.round(light) + "%)";
    }
    function bridgeColor(score) {
      // Muted slate for low scores → strong violet for the top bridgers
      // (distinct from the teal selection accent).
      var t = Math.max(0, Math.min(1, score));
      return "hsl(268, " + Math.round(12 + 60 * t) + "%, " + Math.round(62 - 20 * t) + "%)";
    }

    // ====================================================================
    // Lenses — each answers one question a user actually has.
    // Every lens = question + layout + computed encoding + ranked summary.
    // Naming follows the products users understand (Kumu, InfraNodus,
    // yFiles): the structure revealed, never the algorithm.
    // ====================================================================
    var LENSES = {
      map: {
        label: "Map", question: "How does this knowledge fit together?",
        state: { spacing: 1, separation: 1, groupStrength: 1, groupBy: "community",
                 colorMode: "community", sizeMode: "pagerank",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: false, layout: "cose" },
      },
      themes: {
        label: "Themes", question: "What are the main topic areas — and which barely touch?",
        state: { spacing: 1, separation: 2, groupStrength: 2, groupBy: "community",
                 colorMode: "community", sizeMode: "body",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: false, layout: "cose" },
      },
      flow: {
        label: "Flow", question: "What feeds into what? (reading left → right)",
        state: { spacing: 1, separation: 1, groupStrength: 0, groupBy: "none",
                 colorMode: "community", sizeMode: "body",
                 focusEnabled: false, relationEdges: true, showEdgeLabels: true,
                 showBridges: false, layout: "dagre" },
      },
      bridges: {
        label: "Bridges", question: "Which concepts hold the areas together?",
        state: { spacing: 2, separation: 2, groupStrength: 1, groupBy: "community",
                 colorMode: "bridge", sizeMode: "betweenness",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: true, layout: "cose" },
      },
      recent: {
        label: "Recent", question: "What is fresh, what is going stale?",
        state: { spacing: 1, separation: 1, groupStrength: 1, groupBy: "community",
                 colorMode: "recency", sizeMode: "body",
                 focusEnabled: false, relationEdges: false, showEdgeLabels: false,
                 showBridges: false, layout: "cose" },
      },
      focus: {
        label: "Focus", question: "What surrounds the selected concept?",
        state: { focusEnabled: true, focusDepth: 1, groupBy: "none",
                 colorMode: "community", sizeMode: "body",
                 relationEdges: false, showEdgeLabels: true, showBridges: false },
      },
    };
    var ui = {};

    function applyLens(name) {
      var lens = LENSES[name];
      if (!lens) return;
      var p = lens.state;
      Object.keys(p).forEach(function (k) { controlState[k] = p[k]; });
      controlState.preset = name;
      controlState.lens = name;
      if (p.layout) {
        currentLayoutName = p.layout;
        controlState.layout = p.layout;
      }
      if (name === "focus" && !focusRoot) {
        var selNode = cy.nodes(":selected");
        if (selNode && selNode.length) focusRoot = selNode.id();
      }
      // Compute what the lens needs BEFORE encoding paints it.
      if (p.colorMode === "community" || p.groupBy === "community") computeCommunities();
      if (p.sizeMode === "pagerank") computePageRank();
      if (p.sizeMode === "betweenness" || p.colorMode === "bridge" || p.showBridges) computeBetweenness();
      syncControlsFromState();
      applyGrouping();
      applyVisualEncoding();
      applyFilters();
      updateFocusControls();
      updateStatus();
      renderLensSummary();
      // randomize:false: the layout REFINES current positions, so lens
      // switches read as reprojections of the same map, not new worlds.
      scheduleLayout(false);
    }


    // ---- Lens summary: the ranked answers beside the picture -----------
    // Every analytic view ships a clickable ranked list (the pattern that
    // makes Kumu/InfraNodus/Graph-Analysis land where bare pictures
    // don't). Rendered into #detail-empty, i.e. visible whenever no node
    // is selected; click a row -> select that node.
    function renderLensSummary() {
      var empty = document.getElementById("detail-empty");
      if (!empty) return;
      while (empty.firstChild) empty.removeChild(empty.firstChild);
      empty.classList.add("okf-lens-summary");
      var lensKey = controlState.lens;
      var ids = Object.keys(nodeIndex);
      if (!ids.length) { empty.appendChild(el("p", {}, ["No concepts in this bundle."])); return; }
      var frag = document.createDocumentFragment();
      function heading(text) { frag.appendChild(el("h3", { class: "okf-lens-summary__title" }, [text])); }
      function verdict(text) { frag.appendChild(el("p", { class: "okf-lens-summary__verdict" }, [text])); }
      function row(id, note, score, barColor) {
        var d = nodeIndex[id] || {};
        var btn = el("button", { type: "button", class: "okf-lens-summary__row" });
        var bar = el("span", { class: "okf-lens-summary__bar" });
        bar.style.width = Math.round(Math.max(0.04, Math.min(1, score)) * 100) + "%";
        if (barColor) bar.style.background = barColor;
        btn.appendChild(bar);
        btn.appendChild(el("span", { class: "okf-lens-summary__label" }, [d.label || id]));
        btn.appendChild(el("span", { class: "okf-lens-summary__note" }, [note]));
        btn.addEventListener("click", function () { clearPath(); showDetail(id); });
        frag.appendChild(btn);
      }
      var orphans = ids.filter(function (id) {
        return ((outgoing[id] || []).length + (backlinks[id] || []).length) === 0;
      });

      if (lensKey === "bridges") {
        var bt = computeBetweenness();
        heading("Top bridgers");
        verdict("These concepts sit on the paths between themes; losing one disconnects areas.");
        ids.slice().sort(function (a, b) { return (bt[b] || 0) - (bt[a] || 0); })
          .slice(0, 8).forEach(function (id) {
            row(id, Math.round((bt[id] || 0) * 100) + "%", bt[id] || 0);
          });
      } else if (lensKey === "recent") {
        heading("Freshness");
        var dated = ids.filter(function (id) { return isFinite(recencyDays(id)); });
        var stale = dated.filter(function (id) { return recencyDays(id) > 180; });
        var undated = ids.length - dated.length;
        verdict(stale.length + " of " + ids.length + " concepts are older than ~6 months" +
          (undated ? "; " + undated + " carry no timestamp" : "") + ".");
        dated.sort(function (a, b) { return recencyDays(b) - recencyDays(a); })
          .slice(0, 8).forEach(function (id) {
            var d = recencyDays(id);
            row(id, d > 365 ? Math.round(d / 365) + "y old" : Math.round(d) + "d old",
                Math.min(1, d / 365));
          });
      } else if (lensKey === "flow") {
        heading("Sources and sinks");
        var roots = ids.filter(function (id) { return (backlinks[id] || []).length === 0 && (outgoing[id] || []).length > 0; });
        var leaves = ids.filter(function (id) { return (outgoing[id] || []).length === 0 && (backlinks[id] || []).length > 0; });
        verdict(roots.length + " sources feed the flow; " + leaves.length + " sinks end it. Arrows read left to right.");
        var maxOut = 1;
        ids.forEach(function (id) { maxOut = Math.max(maxOut, (outgoing[id] || []).length); });
        ids.slice().sort(function (a, b) { return (outgoing[b] || []).length - (outgoing[a] || []).length; })
          .slice(0, 8).forEach(function (id) {
            var n = (outgoing[id] || []).length;
            row(id, "feeds " + n, n / maxOut);
          });
      } else if (lensKey === "focus") {
        heading("Focus");
        verdict(focusRoot
          ? "Showing the " + controlState.focusDepth + "-hop neighbourhood of the focused concept. Depth lives in Advanced."
          : "Select any node to isolate its neighbourhood. Everything else fades but stays in place.");
      } else {
        // map / themes: mockup intelligence pane — verdict, themes, bridges.
        var com = computeCommunities();
        var pr = computePageRank();
        var top = com.list.slice(0, 6).filter(function (c) { return c.ids.length > 1; });
        var linked = {};
        cy.edges().forEach(function (e) {
          if (e.hasClass("okf-edge-calm-hide") || e.hasClass("dim")) return;
          var a = communityOf[e.source().id()], b = communityOf[e.target().id()];
          if (a != null && b != null && a !== b) linked[Math.min(a, b) + ":" + Math.max(a, b)] = true;
        });
        var gap = null;
        for (var i = 0; i < top.length && !gap; i++) {
          for (var j = i + 1; j < top.length && !gap; j++) {
            if (!linked[top[i].index + ":" + top[j].index]) gap = [top[i], top[j]];
          }
        }
        function exemplar(c) {
          return c.ids.slice().sort(function (a, b) { return (pr[b] || 0) - (pr[a] || 0); })[0];
        }
        var themes = com.list.filter(function (c) { return c.ids.length > 1; }).length;
        var verdictBits = themes + " theme" + (themes === 1 ? "" : "s");
        if (orphans.length) verdictBits += " \u00b7 " + orphans.length + " orphan" + (orphans.length === 1 ? "" : "s");
        var gapLabel = null;
        if (gap) {
          var ga = nodeIndex[exemplar(gap[0])] || {}, gb = nodeIndex[exemplar(gap[1])] || {};
          gapLabel = (ga.label || "?") + " and " + (gb.label || "?") + " share no links";
          verdictBits += " \u00b7 " + gapLabel;
        }

        var verdictBox = el("div", { class: "okf-lens-summary__verdict-card" });
        verdictBox.appendChild(el("div", { class: "okf-lens-summary__verdict-kicker" }, ["Lens verdict"]));
        verdictBox.appendChild(el("p", { class: "okf-lens-summary__verdict" }, [verdictBits]));
        frag.appendChild(verdictBox);

        heading("Top themes");
        com.list.slice(0, 6).forEach(function (c, idx) {
          if (c.ids.length < 2 && com.list.length > 3) return;
          var ex = exemplar(c);
          var d = nodeIndex[ex] || {};
          var btn = el("button", { type: "button", class: "okf-lens-summary__row" });
          var bar = el("span", { class: "okf-lens-summary__bar" });
          bar.style.width = Math.round(100 * c.ids.length / Math.max(1, com.list[0].ids.length)) + "%";
          bar.style.background = communityColor(c.index);
          btn.appendChild(bar);
          btn.appendChild(el("span", { class: "okf-lens-summary__label" },
            [(idx + 1) + ". " + (d.label || ex)]));
          btn.appendChild(el("span", { class: "okf-lens-summary__note" }, [c.ids.length + " nodes"]));
          btn.addEventListener("click", function () { clearPath(); showDetail(ex); });
          frag.appendChild(btn);
        });

        if (orphans.length || gap) {
          heading("Potential bridges");
          var bridgeHints = [];
          if (orphans.length) {
            var hub = ids.slice().sort(function (a, b) { return (pr[b] || 0) - (pr[a] || 0); })[0];
            bridgeHints.push({
              a: orphans[0],
              b: hub,
              note: "High value",
            });
            if (orphans[1]) bridgeHints.push({ a: orphans[1], b: hub, note: "Suggested" });
          }
          if (gap) {
            bridgeHints.push({ a: exemplar(gap[0]), b: exemplar(gap[1]), note: "Cross-theme" });
          }
          bridgeHints.slice(0, 3).forEach(function (h) {
            var da = nodeIndex[h.a] || {}, db = nodeIndex[h.b] || {};
            var row = el("div", { class: "okf-lens-summary__bridge" });
            row.appendChild(el("span", { class: "okf-lens-summary__bridge-label" },
              [(da.label || h.a) + " \u2194 " + (db.label || h.b)]));
            row.appendChild(el("span", { class: "okf-lens-summary__bridge-note" }, [h.note]));
            var plus = el("button", { type: "button", class: "okf-lens-summary__bridge-add", title: "Focus both ends" }, ["+"]);
            plus.addEventListener("click", function () {
              clearPath();
              showDetail(h.a);
              var nb = cy.getElementById(h.b);
              if (nb && nb.length) {
                try { showPathBetween(cy.getElementById(h.a), nb); } catch (err) {}
              }
            });
            row.appendChild(plus);
            frag.appendChild(row);
          });
        }

        if (window._okfRecentGraph && window._okfRecentGraph.length) {
          heading("Recently viewed");
          window._okfRecentGraph.slice(0, 5).forEach(function (rid) {
            var d = nodeIndex[rid] || {};
            var btn = el("button", { type: "button", class: "okf-lens-summary__row okf-lens-summary__row--recent" });
            btn.appendChild(el("span", { class: "okf-lens-summary__label" }, [d.label || rid]));
            btn.appendChild(el("span", { class: "okf-lens-summary__note" }, ["open"]));
            btn.addEventListener("click", function () { clearPath(); showDetail(rid); });
            frag.appendChild(btn);
          });
        }
      }
      frag.appendChild(el("p", { class: "okf-lens-summary__hint okf-muted" },
        ["Click a row to open it on the canvas. Hover nodes to preview; shift-click two nodes to trace the path between them."]));
      empty.appendChild(frag);
    }

    function markCustom() {
      controlState.preset = "custom";
      (ui.presets || []).forEach(function (r) { r.checked = false; });
    }

    function setRange(input, value, labels) {
      input.value = value;
      input.setAttribute("aria-valuetext", labels[value]);
      if (input.__valEl) input.__valEl.textContent = labels[value];
    }
    function syncControlsFromState() {
      if (!ui.spacing) return;
      setRange(ui.spacing, controlState.spacing, SPACE_LABELS);
      setRange(ui.separation, controlState.separation, SEP_LABELS);
      setRange(ui.groupStrength, controlState.groupStrength, STR_LABELS);
      if (ui.relLabels) ui.relLabels.checked = controlState.showEdgeLabels;
      if (ui.themeColors) ui.themeColors.checked = controlState.colorMode === "community";
      if (ui.groupBy) ui.groupBy.value = controlState.groupBy;
      if (ui.groupColors) ui.groupColors.checked = controlState.colorMode === "type" && controlState.colorBy === "group";
      if (ui.depth) ui.depth.value = String(controlState.focusDepth);
      (ui.presets || []).forEach(function (r) { r.checked = (r.value === controlState.lens); });
      if (ui.question) {
        var lens = LENSES[controlState.lens];
        ui.question.textContent = lens ? lens.question : "";
      }
    }

    function updateFocusControls() {
      if (!ui.depth) return;
      var active = controlState.focusEnabled;
      ui.depth.disabled = !active;
      ui.clearFocus.disabled = !(active && focusRoot);
      if (ui.focusHint) {
        if (active && !focusRoot) { ui.focusHint.hidden = false; }
        else { ui.focusHint.hidden = true; }
      }
    }

    function rangeRow(parent, labelText, max, value, labels, onChange) {
      var valEl = el("span", { class: "okf-signal__row-value" }, [labels[value]]);
      var head = el("span", { class: "okf-signal__row-label" }, [labelText + ": ", valEl]);
      var input = el("input", { type: "range", min: "0", max: String(max), step: "1", value: String(value), "aria-label": labelText });
      input.setAttribute("aria-valuetext", labels[value]);
      input.__valEl = valEl;
      input.addEventListener("input", function () {
        var v = parseInt(input.value, 10) || 0;
        valEl.textContent = labels[v];
        input.setAttribute("aria-valuetext", labels[v]);
        onChange(v);
      });
      parent.appendChild(el("label", { class: "okf-signal__row okf-signal__row--range" }, [head, input]));
      return input;
    }
    function selectRow(parent, labelText, options, value, onChange) {
      var sel = el("select", { "aria-label": labelText });
      options.forEach(function (o) {
        var opt = el("option", { value: o[0] }, [o[1]]);
        if (o[2] === false) opt.disabled = true;
        sel.appendChild(opt);
      });
      sel.value = value;
      sel.addEventListener("change", function () { onChange(sel.value); });
      parent.appendChild(el("label", { class: "okf-signal__row" }, [el("span", { class: "okf-signal__row-label" }, [labelText]), sel]));
      return sel;
    }
    function checkRow(parent, labelText, checked, onChange) {
      var input = el("input", { type: "checkbox" });
      input.checked = checked;
      input.addEventListener("change", function () { onChange(input.checked); });
      parent.appendChild(el("label", { class: "okf-signal__row okf-signal__row--check" }, [input, el("span", {}, [labelText])]));
      return input;
    }

    function buildSignalPanel() {
      // Atlas shell: lenses + advanced mount into the left rail; question +
      // status live at the top of the right intelligence pane. Fallback to
      // the legacy sticky bar in the detail pane when no rail exists
      // (single-file viewer).
      var railLenses = document.getElementById("okf-graph-rail-lenses");
      var railAdv = document.getElementById("okf-graph-rail-advanced");
      var lensHead = document.getElementById("okf-detail-lenshead");
      var atlas = !!(railLenses && railAdv);

      var panel = el("div", { class: "okf-signal okf-signal--bar" + (atlas ? " okf-signal--atlas" : "") });

      var lenses = el("div", { class: "okf-signal__lenses" });
      var seg = el("div", { class: "okf-signal__seg", role: "radiogroup", "aria-label": "Lens" });
      ui.presets = [];
      Object.keys(LENSES).forEach(function (key) {
        var lens = LENSES[key];
        var rid = "okf-lens-" + key;
        var input = el("input", { type: "radio", name: "okf-lens", id: rid, value: key });
        input.checked = controlState.lens === key;
        input.addEventListener("change", function () { if (input.checked) applyLens(key); });
        ui.presets.push(input);
        var item = el("label", { class: "okf-signal__seg-item", "for": rid, title: lens.question },
          [input, el("span", {}, [lens.label])]);
        seg.appendChild(item);
      });
      lenses.appendChild(seg);
      ui.clearFocus = el("button", { type: "button", class: "okf-signal__btn" }, ["Clear focus"]);
      ui.clearFocus.addEventListener("click", function () {
        controlState.focusEnabled = false; focusRoot = null;
        if (ui.focus) ui.focus.checked = false;
        applyFilters(); updateFocusControls(); updateStatus(); overlayAwareFit();
      });
      lenses.appendChild(ui.clearFocus);

      var adv = el("details", { class: "okf-signal__advanced" });
      adv.appendChild(el("summary", { class: "okf-signal__summary" }, ["Advanced"]));
      var body = el("div", { class: "okf-signal__body" });
      adv.appendChild(body);
      body.appendChild(el("p", { class: "okf-signal__hint okf-muted" },
        ["Lenses own the layout and colours. These knobs fine-tune the active lens."]));

      var vfs = el("fieldset", { class: "okf-signal__group" });
      vfs.appendChild(el("legend", {}, ["View"]));
      if (typeSel && !atlas) vfs.appendChild(el("label", { class: "okf-signal__row" }, [el("span", { class: "okf-signal__row-label" }, ["Type filter"]), typeSel]));
      var groupOpts = [["community", "Theme"], ["type", "Type"], ["tag", "First tag"], ["relation", "Relation type"], ["neighborhood", "Connected component"]];
      if (HAS_FOLDERS) groupOpts.push(["folder", "Folder"]);
      if (HAS_GRAPH_CLUSTER) groupOpts.push(["graph_cluster", "Graph cluster"]);
      if (HAS_SOURCE_SYSTEM) groupOpts.push(["source_system", "Source system"]);
      if (HAS_PROJECT) groupOpts.push(["project", "Project"]);
      if (HAS_SECTION) groupOpts.push(["section", "Section"]);
      if (HAS_IMPORT_BATCH) groupOpts.push(["import_batch", "Import batch"]);
      if (HAS_REDMINE_PROJECT) groupOpts.push(["redmine_project", "Redmine project"]);
      groupOpts.push(["none", "None"]);
      ui.groupBy = selectRow(vfs, "Group by", groupOpts, controlState.groupBy, function (v) {
        controlState.groupBy = v;
        if (v === "community") computeCommunities();
        markCustom(); applyGrouping(); applyVisualEncoding(); applyFilters(); updateLegend(); updateStatus(); scheduleLayout(false);
      });
      ui.relLabels = checkRow(vfs, "Show relationship labels", controlState.showEdgeLabels, function (c) {
        controlState.showEdgeLabels = c; controlState.relationEdges = c;
        markCustom(); applyVisualEncoding(); applyEdgeLabels(); applyFilters(); updateStatus();
      });
      ui.allEdges = checkRow(vfs, "Show all edges", controlState.showAllEdges, function (c) {
        controlState.showAllEdges = c;
        markCustom(); applyFilters(); updateStatus();
      });
      ui.themeColors = checkRow(vfs, "Colour by theme", controlState.colorMode === "community", function (c) {
        controlState.colorMode = c ? "community" : "type";
        if (c) controlState.colorBy = "type";
        if (ui.groupColors) ui.groupColors.checked = false;
        applyGrouping();
        applyVisualEncoding(); updateLegend(); updateStatus();
      });
      ui.groupColors = checkRow(vfs, "Colour by selected group", controlState.colorMode === "type" && controlState.colorBy === "group", function (c) {
        controlState.colorMode = "type";
        controlState.colorBy = c ? "group" : "type";
        if (ui.themeColors) ui.themeColors.checked = false;
        markCustom(); applyGrouping(); applyVisualEncoding(); updateLegend(); updateStatus();
      });
      body.appendChild(vfs);

      // Spacing.
      var sfs = el("fieldset", { class: "okf-signal__group" });
      sfs.appendChild(el("legend", {}, ["Spacing"]));
      ui.spacing = rangeRow(sfs, "Node spacing", SPACE_LABELS.length - 1, controlState.spacing, SPACE_LABELS, function (v) { controlState.spacing = v; markCustom(); applyEdgeLabels(); scheduleLayout(false); updateStatus(); });
      ui.separation = rangeRow(sfs, "Cluster separation", SEP_LABELS.length - 1, controlState.separation, SEP_LABELS, function (v) { controlState.separation = v; markCustom(); scheduleLayout(false); updateStatus(); });
      ui.groupStrength = rangeRow(sfs, "Group strength", STR_LABELS.length - 1, controlState.groupStrength, STR_LABELS, function (v) { controlState.groupStrength = v; markCustom(); scheduleLayout(false); updateStatus(); });
      body.appendChild(sfs);

      // Focus.
      var ffs = el("fieldset", { class: "okf-signal__group" });
      ffs.appendChild(el("legend", {}, ["Focus"]));
      ui.depth = selectRow(ffs, "Depth", [["1", "1 hop"], ["2", "2 hops"]], String(controlState.focusDepth), function (v) {
        controlState.focusDepth = parseInt(v, 10) || 1; markCustom(); applyFilters(); updateStatus(); overlayAwareFit();
      });
      ui.focusHint = el("p", { class: "okf-signal__row-hint okf-muted", hidden: "hidden" }, ["Select a node to focus its neighbourhood."]);
      ffs.appendChild(ui.focusHint);
      body.appendChild(ffs);

      body.appendChild(el("p", { class: "okf-signal__edgekey okf-muted" },
        ["Thicker links carry more connective weight. Hover any edge to read its relationship."]));

      ui.question = el("p", { class: "okf-signal__question" });
      statusEl = el("p", { class: "okf-signal__status", role: "status", "aria-live": "polite" });

      panelEl = panel;
      if (atlas) {
        // Left rail: vertical lenses + advanced. Right pane: question/status.
        railLenses.appendChild(lenses);
        railAdv.appendChild(adv);
        if (lensHead) {
          lensHead.appendChild(ui.question);
          lensHead.appendChild(statusEl);
        }
        buildTypeFilterRail();
        panelEl = document.getElementById("okf-graph-rail") || panel;
      } else {
        panel.appendChild(lenses);
        panel.appendChild(adv);
        panel.appendChild(ui.question);
        panel.appendChild(statusEl);
        var detailEl = document.getElementById("okf-detail");
        if (detailEl) {
          detailEl.insertBefore(panel, detailEl.firstChild);
        } else {
          ["mousedown", "touchstart", "pointerdown", "click", "dblclick", "wheel"].forEach(function (evName) {
            panel.addEventListener(evName, function (e) { e.stopPropagation(); });
          });
          container.insertBefore(panel, container.firstChild);
        }
      }
    }

    function buildTypeFilterRail() {
      var host = document.getElementById("okf-graph-rail-filters");
      if (!host) return;
      host.innerHTML = "";
      var counts = {};
      cy.nodes().forEach(function (n) {
        var t = n.data("type") || "concept";
        counts[t] = (counts[t] || 0) + 1;
      });
      Object.keys(counts).sort().forEach(function (t) {
        var btn = el("button", {
          type: "button",
          class: "okf-graph-rail__type" + (controlState.hiddenTypes[t] ? " is-hidden" : ""),
          "data-type": t,
        });
        var sw = el("span", { class: "okf-graph-rail__type-swatch" });
        sw.style.background = palette[t] || "#94a3b8";
        btn.appendChild(sw);
        btn.appendChild(el("span", {}, [t]));
        btn.appendChild(el("span", { class: "okf-graph-rail__type-count" }, [String(counts[t])]));
        btn.addEventListener("click", function (e) {
          if (e.altKey) {
            // Solo this type.
            Object.keys(counts).forEach(function (k) {
              controlState.hiddenTypes[k] = k !== t;
            });
          } else {
            controlState.hiddenTypes[t] = !controlState.hiddenTypes[t];
          }
          buildTypeFilterRail();
          applyFilters();
          updateLegend();
          updateStatus();
        });
        host.appendChild(btn);
      });
    }

    // Empty-graph hardening (reviewer #9): with zero nodes there is nothing to
    // tune, so every control except the panel disclosure is disabled and the
    // status explains why. Keeps the panel honest instead of offering inert
    // presets that silently do nothing.
    function setControlsDisabled(off) {
      if (!panelEl) return;
      var inputs = panelEl.querySelectorAll("input, select, button");
      Array.prototype.forEach.call(inputs, function (el2) { el2.disabled = off; });
      panelEl.classList.toggle("okf-signal--disabled", off);
    }

    // ---- Interaction wiring --------------------------------------------
    // Phase 4: tap selects (detail panel); shift-tap traces a path from
    // the selected node; double-tap opens the concept page; background tap
    // clears everything.
    cy.on("tap", "node", function (evt) {
      var oe = evt.originalEvent;
      var sel = cy.nodes(":selected");
      if (oe && oe.shiftKey && sel.length && sel.id() !== evt.target.id()) {
        showPathBetween(sel[0], evt.target);
        return;
      }
      clearPath();
      showDetail(evt.target.id());
    });
    cy.on("tap", function (evt) {
      if (evt.target === cy) { clearPath(); clearSelection(); }
    });
    cy.on("dbltap", "node", function (evt) {
      openConceptPage(evt.target.id());
    });

    function openConceptPage(conceptId) {
      var openSuffix = (MODE === "static") ? ".html" : "";
      window.location.href = conceptPagePrefix.replace(/\/$/, "") + "/" + conceptId + openSuffix;
    }

    // ---- Phase 4: hover = preview --------------------------------------
    // The pointed node's neighborhood keeps full opacity while the rest
    // fades, and a tooltip card (type, title, description, link counts)
    // follows the cursor. Filters/Focus own `.dim`; hover uses its own
    // class so the two dim sources never fight.
    var hoverTipEl = null;
    function ensureHoverTip() {
      if (hoverTipEl) return hoverTipEl;
      hoverTipEl = document.createElement("div");
      hoverTipEl.className = "okf-graph-tip";
      hoverTipEl.setAttribute("role", "tooltip");
      hoverTipEl.hidden = true;
      container.appendChild(hoverTipEl);
      return hoverTipEl;
    }
    function showHoverTip(node, rpos) {
      var tip = ensureHoverTip();
      var d = node.data();
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      var typeRow = document.createElement("div");
      typeRow.className = "okf-graph-tip__type";
      typeRow.textContent = d.type || "concept";
      typeRow.style.color = d.baseColor || d.color || "";
      tip.appendChild(typeRow);
      var title = document.createElement("div");
      title.className = "okf-graph-tip__title";
      title.textContent = d.label || node.id();
      tip.appendChild(title);
      if (d.description) {
        var desc = document.createElement("div");
        desc.className = "okf-graph-tip__desc";
        var text = String(d.description);
        desc.textContent = text.length > 140 ? text.slice(0, 140) + "…" : text;
        tip.appendChild(desc);
      }
      var meta = document.createElement("div");
      meta.className = "okf-graph-tip__meta";
      meta.textContent = "→ " + (outgoing[node.id()] || []).length +
        " · ← " + (backlinks[node.id()] || []).length + " · double-click opens";
      tip.appendChild(meta);
      tip.hidden = false;
      positionHoverTip(rpos);
    }
    function positionHoverTip(rpos) {
      if (!hoverTipEl || hoverTipEl.hidden || !rpos) return;
      var pad = 14;
      var w = hoverTipEl.offsetWidth, h = hoverTipEl.offsetHeight;
      var x = Math.min(Math.max(8, rpos.x + pad), Math.max(8, container.clientWidth - w - 8));
      var y = rpos.y - h - pad;
      if (y < 8) y = rpos.y + pad;
      hoverTipEl.style.left = x + "px";
      hoverTipEl.style.top = y + "px";
    }
    function hideHoverTip() { if (hoverTipEl) hoverTipEl.hidden = true; }

    cy.on("mouseover", "node", function (evt) {
      var n = evt.target;
      container.style.cursor = "pointer";
      var nb = n.closedNeighborhood();
      cy.batch(function () {
        cy.elements().not(nb).addClass("okf-hover-dim");
        n.addClass("okf-hover");
      });
      showHoverTip(n, evt.renderedPosition);
    });
    cy.on("mouseout", "node", function (evt) {
      container.style.cursor = "";
      cy.batch(function () {
        cy.elements().removeClass("okf-hover-dim");
        evt.target.removeClass("okf-hover");
      });
      hideHoverTip();
    });
    cy.on("mousemove", "node", function (evt) { positionHoverTip(evt.renderedPosition); });
    // Edge hover reveals its relationship label; mouseout restores the
    // canonical label state.
    cy.on("mouseover", "edge", function (evt) {
      if (evt.target.data("label")) evt.target.addClass("okf-show-label");
    });
    cy.on("mouseout", "edge", function () { applyEdgeLabels(); });
    // Pan/zoom/drag invalidate the tooltip's anchor.
    cy.on("pan zoom drag", function () { hideHoverTip(); });

    // ---- Phase 4: path tracing (shift-click) ----------------------------
    // Select node A, shift-click node B → the shortest chain lights up and
    // a path chip reads it out ("Orders → derived_from → Subscriptions").
    var pathEles = null;
    var pathChipEl = null;
    function ensurePathChip() {
      if (pathChipEl) return pathChipEl;
      pathChipEl = document.createElement("div");
      pathChipEl.className = "okf-path-chip";
      pathChipEl.setAttribute("role", "status");
      pathChipEl.setAttribute("aria-live", "polite");
      pathChipEl.hidden = true;
      var label = document.createElement("span");
      label.className = "okf-path-chip__label";
      var clear = document.createElement("button");
      clear.type = "button";
      clear.className = "okf-path-chip__clear";
      clear.setAttribute("aria-label", "Clear path");
      clear.textContent = "×";
      clear.addEventListener("click", function () { clearPath(); updateStatus(); });
      pathChipEl.appendChild(label);
      pathChipEl.appendChild(clear);
      container.appendChild(pathChipEl);
      return pathChipEl;
    }
    function setPathChip(text, parts) {
      var chip = ensurePathChip();
      var lab = chip.querySelector(".okf-path-chip__label");
      if (!text && !(parts && parts.length)) {
        lab.textContent = "";
        lab.innerHTML = "";
        chip.hidden = true;
        return;
      }
      lab.innerHTML = "";
      if (parts && parts.length) {
        parts.forEach(function (p, i) {
          var span = document.createElement("span");
          if (p.kind === "edge") {
            span.className = "okf-path-chip__edge";
            span.textContent = p.text;
          } else {
            span.className = "okf-path-chip__node";
            span.textContent = p.text;
          }
          lab.appendChild(span);
          if (i < parts.length - 1 && p.kind === "node" && parts[i + 1] && parts[i + 1].kind === "node") {
            var arrow = document.createElement("span");
            arrow.className = "okf-path-chip__edge";
            arrow.textContent = "→";
            lab.appendChild(arrow);
          }
        });
      } else {
        lab.textContent = text;
      }
      chip.hidden = false;
    }
    function setStatusNote(msg) {
      if (!statusEl) return;
      if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
      statusEl.textContent = msg;
    }
    function clearPath() {
      if (pathEles) { pathEles.removeClass("okf-path"); pathEles = null; }
      cy.elements().removeClass("okf-path-dim");
      setPathChip("");
    }
    function showPathBetween(a, b) {
      clearPath();
      var res = cy.elements().not(".dim").aStar({ root: a, goal: b, directed: false });
      if (!res || !res.found) res = cy.elements().aStar({ root: a, goal: b, directed: false });
      if (!res || !res.found) {
        setStatusNote("No path between “" + a.data("label") + "” and “" + b.data("label") + "”.");
        setPathChip("No path found");
        return;
      }
      pathEles = res.path;
      cy.elements().not(res.path).addClass("okf-path-dim");
      res.path.addClass("okf-path");
      var chain = [];
      var chipParts = [];
      res.path.forEach(function (ele) {
        if (ele.isNode()) {
          chain.push(ele.data("label") || ele.id());
          chipParts.push({ kind: "node", text: ele.data("label") || ele.id() });
        } else {
          var elab = ele.data("label");
          chain.push(elab ? "—" + elab + "→" : "→");
          chipParts.push({ kind: "edge", text: elab || "→" });
        }
      });
      setStatusNote("Path: " + chain.join(" "));
      setPathChip(chain.join(" "), chipParts);
    }

    // Orphan shelf — labelled tray of unlinked concepts at the canvas bottom.
    var orphanShelfEl = null;
    function updateOrphanShelf() {
      if (!orphanShelfEl) {
        orphanShelfEl = document.createElement("div");
        orphanShelfEl.className = "okf-orphan-shelf";
        orphanShelfEl.setAttribute("aria-label", "Not yet linked concepts");
        container.appendChild(orphanShelfEl);
      }
      var orphans = cy.nodes().filter(function (n) {
        return n.degree(false) === 0 && !n.hasClass("dim");
      });
      orphanShelfEl.innerHTML = "";
      if (!orphans.length) {
        orphanShelfEl.hidden = true;
        return;
      }
      orphanShelfEl.hidden = false;
      var title = document.createElement("div");
      title.className = "okf-orphan-shelf__title";
      title.textContent = "Not yet linked — " + orphans.length;
      orphanShelfEl.appendChild(title);
      var list = document.createElement("div");
      list.className = "okf-orphan-shelf__list";
      orphans.forEach(function (n) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "okf-orphan-shelf__item";
        b.textContent = n.data("label") || n.id();
        b.addEventListener("click", function () { showDetail(n.id()); });
        list.appendChild(b);
      });
      orphanShelfEl.appendChild(list);
    }

    // ---- Phase 4: keyboard ----------------------------------------------
    // f fits, / focuses search, Esc clears, Enter opens the selection,
    // arrows walk to the nearest neighbour in that direction.
    function isTypingTarget(t) {
      // Any interactive element owns its own keyboard semantics (the node
      // index buttons, preset radios, zoom cluster, links…) — the canvas
      // shortcuts only fire when focus rests on the page/canvas itself.
      if (!t || t === document.body) return false;
      var tag = (t.tagName || "").toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select" ||
        tag === "button" || tag === "a" || tag === "summary" || tag === "label" ||
        t.isContentEditable || t.getAttribute("role") === "button" ||
        t.hasAttribute("tabindex");
    }
    function walkSelection(dx, dy) {
      var sel = cy.nodes(":selected");
      if (!sel.length) return;
      var p = sel.position();
      var best = null, bestScore = Infinity;
      sel.closedNeighborhood().nodes().not(sel).forEach(function (n) {
        var q = n.position();
        var vx = q.x - p.x, vy = q.y - p.y;
        var along = vx * dx + vy * dy;             // progress in the pressed direction
        if (along <= 0) return;                     // behind us
        var ortho = Math.abs(vx * dy - vy * dx);    // sideways drift
        var score = ortho * 2 - along;
        if (score < bestScore) { bestScore = score; best = n; }
      });
      if (best) { clearPath(); showDetail(best.id()); }
    }
    document.addEventListener("keydown", function (e) {
      if (isTypingTarget(e.target)) return;
      if (e.key === "f" && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault(); overlayAwareFit();
      } else if (e.key === "/") {
        var si = document.getElementById("okf-search");
        if (si) { e.preventDefault(); si.focus(); }
      } else if (e.key === "Escape") {
        clearPath(); hideHoverTip(); clearSelection();
      } else if (e.key === "Enter") {
        var sel = cy.nodes(":selected");
        if (sel.length) { e.preventDefault(); openConceptPage(sel.id()); }
      } else if (e.key === "ArrowUp") { e.preventDefault(); walkSelection(0, -1); }
      else if (e.key === "ArrowDown") { e.preventDefault(); walkSelection(0, 1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); walkSelection(-1, 0); }
      else if (e.key === "ArrowRight") { e.preventDefault(); walkSelection(1, 0); }
    });

    // ---- Phase 4: zoom cluster (bottom-right) ---------------------------
    (function buildZoomCluster() {
      var wrap = document.createElement("div");
      wrap.className = "okf-graph-zoom";
      function zbtn(label, title, fn) {
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        b.title = title;
        b.setAttribute("aria-label", title);
        b.addEventListener("click", fn);
        wrap.appendChild(b);
        return b;
      }
      function zoomBy(f) {
        var z = cy.zoom() * f;
        cy.zoom({ level: Math.min(MAX_ZOOM, Math.max(0.06, z)),
                  renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 } });
      }
      zbtn("+", "Zoom in", function () { zoomBy(1.3); });
      zbtn("−", "Zoom out", function () { zoomBy(1 / 1.3); });
      zbtn("⤢", "Fit graph (f)", function () { overlayAwareFit(); });
      zbtn("↻", "Re-run layout", function () { runLayoutNow(true); });
      container.appendChild(wrap);
    })();

    var resetBtn = document.getElementById("okf-reset");
    if (resetBtn) resetBtn.addEventListener("click", function () {
      clearSelection();
      overlayAwareFit();
    });

    var searchInput = document.getElementById("okf-search");
    if (searchInput) {
      var runSearch = debounce(function () {
        controlState.search = searchInput.value.trim().toLowerCase();
        applyFilters();
        updateStatus();
      }, 120);
      searchInput.addEventListener("input", runSearch);
      document.addEventListener("keydown", function (e) {
        if ((e.metaKey || e.ctrlKey) && String(e.key || "").toLowerCase() === "k") {
          e.preventDefault();
          searchInput.focus();
          searchInput.select();
        }
      });
    }

    if (typeSel) typeSel.addEventListener("change", function (e) {
      controlState.type = e.target.value;
      applyFilters();
      updateStatus();
    });

    // Build the panel + apply the default lens. Initial positions
    // are seeded DETERMINISTICALLY (sorted ids on a circle) before the
    // force layout refines them with randomize:false — the same bundle
    // lays out the same way on every load (the top Obsidian complaint was
    // position churn destroying spatial memory).
    buildSignalPanel();
    ensureMinimap();
    // Reposition floating card on pan/zoom so it stays anchored to the node.
    cy.on("pan zoom", function () {
      var sel = cy.nodes(":selected");
      if (sel.length) showGraphCard(sel.id());
      else hideGraphCard();
    });
    if (GRAPH_EMPTY) {
      applyVisualEncoding();
      applyFilters();
      updateFocusControls();
      updateStatus();
      setControlsDisabled(true);
      renderLensSummary();
      overlayAwareFit();
    } else {
      (function seedPositions() {
        var ns = cy.nodes().sort(function (a, b) { return a.id() < b.id() ? -1 : 1; });
        var R = Math.max(200, ns.length * 18);
        ns.forEach(function (n, i) {
          var a = (2 * Math.PI * i) / Math.max(1, ns.length);
          n.position({ x: Math.cos(a) * R, y: Math.sin(a) * R });
        });
      })();
      applyLens(controlState.lens);
    }
    var fitDebounced = debounce(function () { overlayAwareFit(); }, 160);
    if (window.addEventListener) window.addEventListener("resize", fitDebounced);
    // The Advanced disclosure lives in the detail pane, so its
    // toggle only affects the canvas fit in the compatibility in-canvas mount.
    if (panelEl && container.contains(panelEl)) {
      var advEl = panelEl.querySelector(".okf-signal__advanced");
      if (advEl) advEl.addEventListener("toggle", function () { fitDebounced(); });
    }

    // ---- Detail-panel relationship explanation -------------------------
    // All figures are read from the SAME precomputed client-side metrics that
    // drive the canvas, so the panel never advertises a signal that is not
    // actually present (contract-runtime-parity for the copy).
    function topSharedTags(id) {
      var d = nodeIndex[id] || {};
      var mine = setOf(d.tags);
      if (!Object.keys(mine).length) return [];
      var counts = {};
      (outgoing[id] || []).concat(backlinks[id] || []).forEach(function (nb) {
        var nd = nodeIndex[nb]; if (!nd) return;
        arr(nd.tags).forEach(function (t) { var k = String(t).toLowerCase(); if (mine[k]) counts[t] = (counts[t] || 0) + 1; });
      });
      return Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; }).slice(0, 3);
    }
    function standoutText(id) {
      var defs = [["links", "link count"], ["backl", "backlinks"], ["typed", "typed relations"], ["tags", "shared tags"], ["cites", "citations"], ["ents", "shared entities"]];
      var best = null, bestV = -1;
      defs.forEach(function (dd) {
        if (!hasSignal[dd[0]]) return;
        var v = normNode(dd[0], id);
        if (v > bestV) { bestV = v; best = dd; }
      });
      var parts = [];
      if (best && bestV >= 0.5) parts.push("top-tier " + best[1] + " (" + Math.round(bestV * 100) + "% of the strongest)");
      else if (best && bestV > 0) parts.push("moderate " + best[1]);
      var gs = {};
      (outgoing[id] || []).concat(backlinks[id] || []).forEach(function (nb) { var g = groupOf[nb]; if (g) gs[g] = true; });
      if (Object.keys(gs).length >= 3) parts.push("bridges " + Object.keys(gs).length + " groups");
      if (!parts.length) return "No standout signal yet";
      var joined = parts.join("; ");
      return joined.charAt(0).toUpperCase() + joined.slice(1);
    }
    function renderSignalExplain(id) {
      var content = document.getElementById("detail-content");
      if (!content) return;
      var box = document.getElementById("okf-detail-signals");
      var dl;
      if (!box) {
        box = el("section", { id: "okf-detail-signals", class: "okf-detail__signals" });
        box.appendChild(el("h3", { class: "okf-detail__signals-head" }, ["Why this connects"]));
        dl = el("dl", { class: "okf-detail__signals-list" });
        box.appendChild(dl);
        var hr = content.querySelector("hr");
        if (hr) content.insertBefore(box, hr); else content.appendChild(box);
      } else {
        dl = box.querySelector("dl");
        while (dl.firstChild) dl.removeChild(dl.firstChild);
      }
      var og = (outgoing[id] || []).length, bl = (backlinks[id] || []).length;
      var rows = [["Connected to", og + " outgoing · " + bl + " incoming"]];
      var tt = topSharedTags(id);
      if (tt.length) rows.push(["Shared tags", tt.join(", ")]);
      if ((raw.typed[id] || 0) > 0) rows.push(["Typed relations", String(raw.typed[id])]);
      if ((raw.cites[id] || 0) > 0) rows.push(["Citations", String(raw.cites[id])]);
      rows.push(["Stands out by", standoutText(id)]);
      rows.forEach(function (r) {
        dl.appendChild(el("dt", {}, [r[0]]));
        dl.appendChild(el("dd", {}, [r[1]]));
      });
      box.hidden = false;
    }

    function clearSelection() {
      cy.elements().unselect();
      focusRoot = null;
      applyFilters();
      applyEdgeLabels();
      hideGraphCard();
      updateFocusControls();
      updateStatus();
      var empty = document.getElementById("detail-empty");
      var content = document.getElementById("detail-content");
      if (empty) empty.hidden = false;
      if (content) content.hidden = true;
      if (typeof renderLensSummary === "function") {
        try { renderLensSummary(controlState.lens || "map"); } catch (e) {}
      }
    }

    // Floating on-canvas selection card (mockup process card).
    var graphCardEl = null;
    function ensureGraphCard() {
      if (graphCardEl) return graphCardEl;
      graphCardEl = document.createElement("div");
      graphCardEl.className = "okf-graph-card";
      graphCardEl.hidden = true;
      container.appendChild(graphCardEl);
      return graphCardEl;
    }
    function hideGraphCard() {
      if (graphCardEl) graphCardEl.hidden = true;
    }
    function showGraphCard(conceptId) {
      var data = nodeIndex[conceptId];
      var node = cy.getElementById(conceptId);
      if (!data || !node || !node.length) { hideGraphCard(); return; }
      var card = ensureGraphCard();
      var out = 0, inn = 0;
      try {
        out = node.outgoers("node").length;
        inn = node.incomers("node").length;
      } catch (e) {}
      var strong = Math.max(out, inn);
      var weak = Math.min(out, inn);
      var openSuffix = (MODE === "static") ? ".html" : "";
      var href = conceptPagePrefix.replace(/\/$/, "") + "/" + conceptId + openSuffix;
      card.innerHTML = "";
      var typeEl = document.createElement("div");
      typeEl.className = "okf-graph-card__type";
      typeEl.textContent = data.type || "concept";
      var titleEl = document.createElement("h3");
      titleEl.className = "okf-graph-card__title";
      titleEl.textContent = data.label || conceptId;
      var descEl = document.createElement("p");
      descEl.className = "okf-graph-card__desc";
      descEl.textContent = (data.description || "").slice(0, 140) || "No description.";
      var stats = document.createElement("div");
      stats.className = "okf-graph-card__stats";
      stats.innerHTML = "<span><strong>" + strong + "</strong> strong</span>" +
        "<span><strong>" + weak + "</strong> weak</span>" +
        "<span><strong>" + (out + inn) + "</strong> total</span>";
      var open = document.createElement("a");
      open.className = "okf-graph-card__open";
      open.href = href;
      open.textContent = "Open page →";
      card.appendChild(typeEl);
      card.appendChild(titleEl);
      card.appendChild(descEl);
      card.appendChild(stats);
      card.appendChild(open);
      var rp = node.renderedPosition();
      var crect = container.getBoundingClientRect();
      var left = rp.x + 18;
      var top = rp.y - 20;
      var cardW = 280, cardH = 160;
      if (left + cardW > crect.width - 12) left = rp.x - cardW - 18;
      if (top + cardH > crect.height - 12) top = crect.height - cardH - 12;
      if (top < 12) top = 12;
      if (left < 12) left = 12;
      card.style.left = left + "px";
      card.style.top = top + "px";
      card.hidden = false;
    }

    // Simple minimap of node positions.
    var minimapEl = null, minimapCtx = null;
    function ensureMinimap() {
      if (minimapEl) return;
      if (!document.body.classList.contains("okf-viewer--graph")) return;
      minimapEl = document.createElement("div");
      minimapEl.className = "okf-graph-minimap";
      minimapEl.setAttribute("aria-hidden", "true");
      var tools = document.createElement("div");
      tools.className = "okf-graph-minimap__tools";
      [
        ["in", "+", "Zoom in"],
        ["out", "−", "Zoom out"],
        ["fit", "⊡", "Fit"],
        ["center", "◎", "Recenter"],
      ].forEach(function (spec) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.setAttribute("data-mm", spec[0]);
        btn.title = spec[2];
        btn.textContent = spec[1];
        btn.addEventListener("click", function () {
          var a = btn.getAttribute("data-mm");
          if (a === "in") cy.zoom({ level: cy.zoom() * 1.2, renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 } });
          else if (a === "out") cy.zoom({ level: cy.zoom() / 1.2, renderedPosition: { x: container.clientWidth / 2, y: container.clientHeight / 2 } });
          else if (a === "fit") cy.fit(undefined, 48);
          else if (a === "center") cy.center();
          drawMinimap();
        });
        tools.appendChild(btn);
      });
      var view = document.createElement("div");
      view.className = "okf-graph-minimap__view";
      var canvas = document.createElement("canvas");
      canvas.width = 132; canvas.height = 88;
      view.appendChild(canvas);
      minimapEl.appendChild(tools);
      minimapEl.appendChild(view);
      container.appendChild(minimapEl);
      minimapCtx = canvas.getContext("2d");
      cy.on("pan zoom position dragfree", drawMinimap);
      drawMinimap();
    }
    function drawMinimap() {
      if (!minimapCtx || !minimapEl) return;
      var w = 132, h = 88;
      minimapCtx.clearRect(0, 0, w, h);
      minimapCtx.fillStyle = "rgba(7,11,20,0.9)";
      minimapCtx.fillRect(0, 0, w, h);
      var bb = cy.elements().boundingBox();
      if (!bb || !isFinite(bb.w) || bb.w < 1 || bb.h < 1) return;
      var pad = 8;
      var sx = (w - pad * 2) / bb.w;
      var sy = (h - pad * 2) / bb.h;
      var s = Math.min(sx, sy);
      cy.nodes().forEach(function (n) {
        if (n.hasClass("dim")) return;
        var p = n.position();
        var x = pad + (p.x - bb.x1) * s;
        var y = pad + (p.y - bb.y1) * s;
        minimapCtx.beginPath();
        minimapCtx.fillStyle = n.data("color") || "#64748b";
        minimapCtx.arc(x, y, n.selected() ? 3.2 : 2, 0, Math.PI * 2);
        minimapCtx.fill();
      });
      var ext = cy.extent();
      var vx = pad + (ext.x1 - bb.x1) * s;
      var vy = pad + (ext.y1 - bb.y1) * s;
      var vw = (ext.x2 - ext.x1) * s;
      var vh = (ext.y2 - ext.y1) * s;
      minimapCtx.strokeStyle = "rgba(62,201,201,0.7)";
      minimapCtx.lineWidth = 1;
      minimapCtx.strokeRect(vx, vy, vw, vh);
    }

    function showDetail(conceptId) {
      var data = nodeIndex[conceptId];
      if (!data) return;
      // iter2 CRI2-006: decluster-on-focus. A keyboard-index or tap on a
      // node the LOD hid must still resolve: add it to the canvas first.
      ensureNodeVisible(conceptId);
      cy.elements().unselect();
      var node = cy.getElementById(conceptId);
      if (node && node.length) node.select();

      // Show edge labels for the selected node's edges (context without
      // global clutter — reviewer #8).
      applyEdgeLabels();
      // Selected-neighbourhood focus tracks the selected node: re-run the
      // combined filter so the focus dim follows the new selection, then zoom
      // the neighbourhood into view so Focus visibly isolates it (reviewer #4).
      if (controlState.focusEnabled) {
        focusRoot = conceptId;
        applyFilters();
        updateFocusControls();
        updateStatus();
        overlayAwareFit();
      }

      var empty = document.getElementById("detail-empty");
      var content = document.getElementById("detail-content");
      if (empty) empty.hidden = true;
      if (content) content.hidden = false;
      showGraphCard(conceptId);
      if (typeof drawMinimap === "function") drawMinimap();
      try {
        window._okfRecentGraph = window._okfRecentGraph || [];
        window._okfRecentGraph = [conceptId].concat(
          window._okfRecentGraph.filter(function (x) { return x !== conceptId; })
        ).slice(0, 8);
      } catch (err) {}

      var chip = document.getElementById("detail-type");
      if (chip) {
        chip.textContent = data.type || "concept";
        var bg = data.color || palette[data.type] || "#94a3b8";
        chip.style.background = bg;
        // P0-4: per-chip foreground via luminance (was hardwired #fff via
        // --okf-chip-fg, which failed on yellow/green/cyan palette colours).
        chip.style.color = _chipFg(bg);
      }
      setText("detail-title", data.label || conceptId);
      setText("detail-id", conceptId);
      setText("detail-description", data.description || "Not set");

      // Resource.
      var resourceEl = document.getElementById("detail-resource");
      if (resourceEl) {
        resourceEl.innerHTML = "";
        if (data.resource) {
          var a = document.createElement("a");
          a.href = data.resource; a.textContent = data.resource;
          a.target = "_blank"; a.rel = "noopener"; a.className = "okf-external";
          resourceEl.appendChild(a);
        } else {
          // iter1 CRI-009: em dash replaced with a screen-reader-friendly
          // "Not set" so an empty field reads as content, not a glyph.
          resourceEl.textContent = "Not set";
        }
      }

      // Tags.
      var tagsEl = document.getElementById("detail-tags");
      if (tagsEl) {
        tagsEl.innerHTML = "";
        if (data.tags && data.tags.length) {
          data.tags.forEach(function (t) {
            var span = document.createElement("span");
            span.className = "okf-tag"; span.textContent = t;
            tagsEl.appendChild(span);
          });
        } else {
          tagsEl.textContent = "Not set";
        }
      }

      // §7 governed keys (P1-3 iter-1): aliases / entities / provenance /
      // citations / relations - mirrors the concept page. Hidden when empty.
      renderGoverned(document.getElementById("detail-governed"), data);

      // Computed relationship explanation — why this node connects and
      // stands out. Every figure comes from the SAME client-side metrics that
      // drive the canvas, so the copy never claims a signal that is not real.
      renderSignalExplain(conceptId);

      // Body: pre-rendered server-side by okf-loom's HTML-escaping
      // markdown renderer (see build_graph_data). Assign directly - no
      // client-side markdown parser is loaded, which removes the entire
      // client-side XSS surface (no marked.parse, no DOMPurify needed).
      var bodyEl = document.getElementById("detail-body");
      if (bodyEl) {
        bodyEl.innerHTML = bodies[conceptId] || "";
        // Stamp the concept id so studio.js's comment affordance can
        // anchor comments to the right concept when the user selects
        // text in the graph detail panel.
        bodyEl.setAttribute("data-concept-id", conceptId);
        bodyEl.className = "okf-page__body okf-graph-detail__body";
        rewriteInternalLinks(bodyEl);
        // User feedback: the detail panel shows the SAME
        // server-rendered concept HTML as the wiki pages, so the same
        // progressive renderers must run over it. renderers.js listens
        // for okf-loom:bodyPatched (the event the studio dispatches after live
        // patches) and re-scans for mermaid/code/math blocks.
        try { window.dispatchEvent(new Event("okf-loom:bodyPatched")); } catch (e) {}
      }

      // Open-page link (graph view only).
      // P1-3: in static mode the concept page is emitted at <id>.html, so
      // the href needs the .html suffix (the bare path 404s).
      var openLink = document.getElementById("detail-open-link");
      if (openLink) {
        var openSuffix = (MODE === "static") ? ".html" : "";
        openLink.href = conceptPagePrefix.replace(/\/$/, "") + "/" + conceptId + openSuffix;
      }

      // Outgoing.
      renderLinkList("outgoing-list", "detail-outgoing", outgoing[conceptId] || []);
      // Backlinks.
      renderLinkList("backlinks-list", "detail-backlinks", backlinks[conceptId] || []);

      // Pan to node. P2-75: skip animation when user prefers reduced motion.
      // Skip entirely under focus — overlayAwareFit() already framed the whole
      // neighbourhood, and a competing pan-to would undo that isolation.
      if (node && node.length && !controlState.focusEnabled) {
        var lvl = Math.min(Math.max(cy.zoom(), 0.9), MAX_ZOOM);
        if (REDUCED_MOTION) {
          cy.center(node);
          cy.zoom({ eles: node, level: lvl });
        } else {
          cy.animate({ center: { eles: node }, zoom: lvl }, { duration: 200 });
        }
      }
    }

    function renderLinkList(listId, sectionId, ids) {
      var list = document.getElementById(listId);
      var section = document.getElementById(sectionId);
      if (!list || !section) return;
      list.innerHTML = "";
      if (!ids.length) { section.hidden = true; return; }
      section.hidden = false;
      ids.forEach(function (src) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.textContent = (nodeIndex[src] && nodeIndex[src].label) || src;
        a.href = "#node-" + src;
        a.className = "okf-internal";
        a.addEventListener("click", function (e) {
          e.preventDefault(); showDetail(src);
        });
        li.appendChild(a);
        var muted = document.createElement("span");
        muted.className = "okf-muted";
        muted.textContent = " " + src;
        li.appendChild(muted);
        list.appendChild(li);
      });
    }

    // P1-4 / P2-2 (iter-1): build the keyboard-accessible node index.
    // One <button> per node, sorted by label. Click / Enter / Space →
    // showDetail(id). Reuses the concept-page local-graph pill pattern.
    function buildNodeIndex(bundleData) {
      var list = document.getElementById("okf-node-index-list");
      var countEl = document.getElementById("okf-node-index-count");
      if (!list) return;
      while (list.firstChild) list.removeChild(list.firstChild);
      var sorted = (bundleData.nodes || []).slice().sort(function (a, b) {
        var la = String(a.data.label || a.data.id || "").toLowerCase();
        var lb = String(b.data.label || b.data.id || "").toLowerCase();
        if (la < lb) return -1;
        if (la > lb) return 1;
        return 0;
      });
      if (countEl) countEl.textContent = "(" + sorted.length + ")";
      sorted.forEach(function (n) {
        var id = n.data.id;
        var label = n.data.label || id;
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "okf-node-index__item";
        btn.setAttribute("data-target", id);
        btn.setAttribute("aria-label", "Focus " + label);
        var sw = document.createElement("span");
        sw.className = "okf-node-index__swatch";
        sw.style.background = n.data.color || "var(--okf-accent)";
        btn.appendChild(sw);
        var lbl = document.createElement("span");
        lbl.className = "okf-node-index__label";
        lbl.textContent = label;
        btn.appendChild(lbl);
        btn.addEventListener("click", function (e) {
          e.preventDefault();
          showDetail(id);
          // Move focus into the detail panel so keyboard users land on the
          // freshly-populated content.
          var dc = document.getElementById("detail-content");
          if (dc) dc.setAttribute("tabindex", "-1"), dc.focus({ preventScroll: false });
        });
        list.appendChild(btn);
      });
    }

    function rewriteInternalLinks(root) {      if (!root) return;
      var links = root.querySelectorAll("a[href]");
      Array.prototype.forEach.call(links, function (a) {
        var href = a.getAttribute("href") || "";
        // Internal: /foo/bar.md or ./baz.md or foo.md - strip .md, look up.
        var target = null;
        if (href.charAt(0) === "/" && href.slice(-3) === ".md") {
          target = href.slice(1, -3);
        } else if (href.indexOf("./") === 0 && href.slice(-3) === ".md") {
          // Best-effort: strip leading ./ and hope the path resolves.
          target = href.slice(2, -3);
        } else if (href.slice(-3) === ".md" && href.indexOf("/") < 0) {
          target = href.slice(0, -3);
        }
        if (target && nodeIndex[target]) {
          a.setAttribute("href", "#node-" + target);
          a.className = "okf-internal";
          a.addEventListener("click", function (e) {
            e.preventDefault(); showDetail(target);
          });
          return;
        }
        if (href.charAt(0) !== "#" && href.indexOf("://") < 0 && href.charAt(0) !== "/" && href.indexOf("mailto:") !== 0) {
          // Unknown internal form - leave as plain external link.
        }
        a.className = "okf-external";
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener");
      });
    }

    // iter1 CRI-004: subscribe to presence so the agent's focus shows up as
    // a halo + gentle pan-to on the canvas. When presence goes idle (no
    // focus), every halo clears so a stale cue never lingers. This makes
    // the agent feel spatially present in the graph, not just a badge.
    var presenceFocus = null;
    function applyPresenceFocus(focus) {
      cy.nodes().removeClass("okf-presence-halo");
      presenceFocus = focus || null;
      if (!focus) return;
      // iter2 CRI2-006: the spatial-presence halo must still highlight the
      // focused node even when LOD hid it. Decluster-on-focus adds the node
      // (and any now-visible edges) before applying the halo class.
      ensureNodeVisible(focus);
      var node = cy.getElementById(focus);
      if (!node || !node.length) return;
      node.addClass("okf-presence-halo");
      if (REDUCED_MOTION) {
        cy.center(node);
      } else {
        cy.animate({ center: { eles: node } }, { duration: 240 });
      }
    }
    if (window.okfLoomLive && typeof window.okfLoomLive.on === "function") {
      window.okfLoomLive.on("presence", function (p) {
        var focus = p && p.focus ? p.focus : null;
        if (focus === presenceFocus) return;
        applyPresenceFocus(focus);
      });

      // ---- Phase 5: the canvas consumes live graph events ---
      // A `graph` SSE event means the bundle's structure changed (create /
      // remove / link edits via CLI mutators or the studio). Refetch
      // graph.json, DIFF it against the canvas, animate additions in and
      // removals out — and NEVER yank the viewport: a chip offers the
      // re-layout instead. Signal-engine normalisations recompute on the
      // next full load; the visual encoding refresh below keeps weights,
      // orphan cues, and typed-edge hues consistent for the delta.
      var _graphRefreshTimer = null;
      // Latest-wins fence for overlapping/late DATA_URL refreshes: a `graph`
      // burst (or a slow response) can leave two fetches in flight. Only the
      // response whose token is still current may mutate graph data / detail /
      // status — a stale earlier response is dropped (same ownership model as
      // runLayoutNow's layoutSeq). `refreshStats` mirrors `layoutStats` for
      // tests/diagnostics only.
      var _graphRefreshSeq = 0;
      var refreshStats = { started: 0, applied: 0, skippedStale: 0 };
      var _graphChipEl = null;
      function showGraphChip(text) {
        if (!_graphChipEl) {
          _graphChipEl = document.createElement("div");
          _graphChipEl.className = "okf-graph-updated";
          var span = document.createElement("span");
          _graphChipEl.appendChild(span);
          var btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = "Re-run layout";
          btn.addEventListener("click", function () {
            hideGraphChip();
            runLayoutNow(false);
          });
          _graphChipEl.appendChild(btn);
          var x = document.createElement("button");
          x.type = "button";
          x.textContent = "×";
          x.setAttribute("aria-label", "Dismiss");
          x.addEventListener("click", hideGraphChip);
          _graphChipEl.appendChild(x);
          container.appendChild(_graphChipEl);
        }
        _graphChipEl.firstChild.textContent = text;
        _graphChipEl.hidden = false;
      }
      function hideGraphChip() { if (_graphChipEl) _graphChipEl.hidden = true; }

      function refreshGraphFromServer() {
        if (!DATA_URL) return;
        var seq = ++_graphRefreshSeq;
        refreshStats.started++;
        fetch(DATA_URL, { headers: { Accept: "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (fresh) {
            // A newer refresh started while this one was in flight → this
            // response is stale; never let it mutate graph/detail/status.
            if (seq !== _graphRefreshSeq) { refreshStats.skippedStale++; return; }
            refreshStats.applied++;
            var freshIds = {};
            var added = 0, removed = 0, changed = 0;
            // Update the shared lookups the detail panel + tooltips read.
            bodies = fresh.bodies || bodies;
            backlinks = fresh.backlinks || backlinks;
            outgoing = {};
            (fresh.edges || []).forEach(function (e) {
              (outgoing[e.data.source] = outgoing[e.data.source] || []).push(e.data.target);
            });
            (fresh.nodes || []).forEach(function (n) {
              var id = n.data.id;
              freshIds[id] = true;
              nodeIndex[id] = n.data;
              var existing = cy.getElementById(id);
              if (existing && existing.length) {
                // Live-edited metadata: refresh label/colour/description.
                var el2 = elemFor(n);
                ["label", "type", "description", "resource", "tags", "baseColor", "iconUrl", "shape"].forEach(function (k) {
                  if (JSON.stringify(existing.data(k)) !== JSON.stringify(el2.data[k])) {
                    existing.data(k, el2.data[k]);
                    changed++;
                  }
                });
              } else if (!lodActive || shownIds[id]) {
                shownIds[id] = true;
                var ne = cy.add(elemFor(n));
                // Place near a linked neighbour so the node appears to grow
                // out of its context rather than teleporting in.
                var nb = (outgoing[id] || []).concat(backlinks[id] || []);
                for (var i = 0; i < nb.length; i++) {
                  var anchor = cy.getElementById(nb[i]);
                  if (anchor && anchor.length) {
                    var ap = anchor.position();
                    ne.position({ x: ap.x + 40 + Math.floor(60 * ((id.length % 7) / 7)), y: ap.y + 40 });
                    break;
                  }
                }
                if (!REDUCED_MOTION) {
                  ne.style("opacity", 0);
                  ne.animate({ style: { opacity: 1 } }, { duration: 350 });
                  setTimeout(function () { ne.removeStyle("opacity"); }, 400);
                }
                added++;
              }
            });
            cy.nodes().forEach(function (n) {
              if (freshIds[n.id()]) return;
              removed++;
              delete shownIds[n.id()];
              if (REDUCED_MOTION) { cy.remove(n); }
              else {
                n.animate({ style: { opacity: 0 } }, { duration: 250, complete: function () { try { cy.remove(n); } catch (e) {} } });
              }
            });
            // Edge delta between currently-shown nodes.
            var haveEdge = {};
            cy.edges().forEach(function (e) { haveEdge[e.id()] = e; });
            var freshEdgeIds = {};
            (fresh.edges || []).forEach(function (e) {
              if (!shownIds[e.data.source] || !shownIds[e.data.target]) return;
              var key = e.data.source + "__" + e.data.target;
              if (freshEdgeIds[key]) return;
              freshEdgeIds[key] = true;
              if (!haveEdge[key]) {
                var ed = { id: key, source: e.data.source, target: e.data.target, weight: 0.5 };
                if (e.data.label) ed.label = e.data.label;
                try { cy.add({ data: ed }); added++; } catch (err) {}
              }
            });
            Object.keys(haveEdge).forEach(function (key) {
              if (!freshEdgeIds[key]) { try { cy.remove(haveEdge[key]); removed++; } catch (err) {} }
            });
            if (added || removed || changed) {
              invalidateLensCache();
              computeCommunities();
              applyVisualEncoding();
              applyFilters();
              updateStatus();
              renderLensSummary();
              var bits = [];
              if (added) bits.push(added + " added");
              if (removed) bits.push(removed + " removed");
              if (changed) bits.push("metadata updated");
              showGraphChip("Graph updated (" + bits.join(", ") + ")");
            }
          })
          .catch(function () { /* next event retries */ });
      }
      window.okfLoomLive.on("graph", function () {
        if (_graphRefreshTimer) clearTimeout(_graphRefreshTimer);
        _graphRefreshTimer = setTimeout(function () { _graphRefreshTimer = null; refreshGraphFromServer(); }, 700);
      });
    }

    // No auto-selected node at boot — the first thing the right
    // pane shows is the ACTIVE LENS SUMMARY (the ranked answers), which
    // the old highest-degree auto-select used to bury. Clicking any node
    // or summary row opens its page as before.

    // ---- Phase 5: first-visit micro-tour --------------------------------
    // Three coach marks (lenses / hover+path / open), shown once per
    // browser and dismissed forever via localStorage. Zero-dependency: a
    // single repositioned card, Esc or ✕ skips.
    (function microTour() {
      var KEY = "okfGraphTourDone";
      try { if (window.localStorage.getItem(KEY)) return; } catch (e) { return; }
      if (GRAPH_EMPTY) return;
      var steps = [
        { title: "Lenses", text: "Each lens answers one question: Map lays the land out, Themes finds the topic areas, Flow reads dependencies left-to-right, Bridges shows what holds it together, Recent shows what's going stale. The panel below the bar ranks the answers.", at: "right" },
        { title: "Explore", text: "Hover a node to preview it and light up its neighbourhood. Shift-click a second node to trace the path between them.", at: "center" },
        { title: "Open", text: "Double-click any node (or press Enter) to open its page. The panel on the right always shows the selection in full.", at: "right" },
      ];
      var idx = 0;
      // Remember what had focus so the tour can restore it on close (a11y:
      // dialogs must move focus in on open and hand it back on dismiss).
      var previousFocus = document.activeElement;
      var card = document.createElement("div");
      card.className = "okf-graph-tour";
      card.setAttribute("role", "dialog");
      // Focus is trapped inside the tour, so it behaves as a modal dialog.
      card.setAttribute("aria-modal", "true");
      card.setAttribute("aria-label", "Graph tour");
      card.setAttribute("tabindex", "-1");
      var h = document.createElement("strong");
      h.id = "okf-graph-tour__title";
      var p = document.createElement("p");
      p.id = "okf-graph-tour__desc";
      // Name stays "Graph tour"; the step title + body are the description.
      card.setAttribute("aria-describedby", "okf-graph-tour__title okf-graph-tour__desc");
      var nav = document.createElement("div");
      nav.className = "okf-graph-tour__nav";
      var dots = document.createElement("span");
      dots.className = "okf-graph-tour__dots";
      var next = document.createElement("button");
      next.type = "button";
      next.className = "okf-graph-tour__next";
      var skip = document.createElement("button");
      skip.type = "button";
      skip.className = "okf-graph-tour__skip";
      skip.textContent = "✕";
      skip.setAttribute("aria-label", "Dismiss tour");
      nav.appendChild(dots); nav.appendChild(next);
      card.appendChild(skip); card.appendChild(h); card.appendChild(p); card.appendChild(nav);
      // Focusable controls, in DOM/tab order: dismiss (✕) then the Next/Got-it
      // button. Tab/Shift+Tab cycle within these two — nothing else.
      var focusables = [skip, next];
      function restoreFocus() {
        // Hand focus back to the opener when it is still a real, connected
        // control; otherwise land on a stable graph control (search/reset).
        var target = (previousFocus && previousFocus !== document.body &&
                      document.contains(previousFocus) &&
                      typeof previousFocus.focus === "function")
          ? previousFocus
          : (document.getElementById("okf-search") ||
             document.getElementById("okf-reset") || container);
        if (target && typeof target.focus === "function") {
          try { target.focus({ preventScroll: true }); }
          catch (e) { try { target.focus(); } catch (e2) {} }
        }
      }
      function finish() {
        try { window.localStorage.setItem(KEY, "1"); } catch (e) {}
        card.removeEventListener("keydown", onKey);
        if (card.parentNode) card.parentNode.removeChild(card);
        restoreFocus();
      }
      function render() {
        var s = steps[idx];
        h.textContent = s.title;
        p.textContent = s.text;
        dots.textContent = (idx + 1) + " / " + steps.length;
        next.textContent = idx === steps.length - 1 ? "Got it" : "Next";
        card.setAttribute("data-at", s.at);
      }
      // The tour OWNS Tab (focus trap) and Escape while open. The handler lives
      // on the card and stops Escape from bubbling to the graph's global
      // keydown handler, so Escape closes ONLY the tour (it does not also clear
      // the current selection / path the way a bare graph Escape would).
      function onKey(e) {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          finish();
          return;
        }
        if (e.key === "Tab") {
          var first = focusables[0], last = focusables[focusables.length - 1];
          var active = document.activeElement;
          if (e.shiftKey) {
            if (active === first || active === card) { e.preventDefault(); last.focus(); }
          } else {
            if (active === last) { e.preventDefault(); first.focus(); }
          }
        }
      }
      next.addEventListener("click", function () {
        if (idx >= steps.length - 1) { finish(); return; }
        idx++; render();
      });
      skip.addEventListener("click", finish);
      card.addEventListener("keydown", onKey);
      render();
      container.appendChild(card);
      // Move focus into the dialog (the primary action) so keyboard + screen
      // reader users land inside the trapped tour rather than behind it.
      try { next.focus({ preventScroll: true }); }
      catch (e) { try { next.focus(); } catch (e2) {} }
    })();

    // ---- Diagnostic / test hook (non-visual; NOT a public API) ---------
    // This is a TEST/DIAGNOSTIC hook, not a stable read-only runtime API. It
    // exposes the LIVE, MUTABLE Cytoscape instance (`cy`) plus read-only helpers
    // — a control-state snapshot (`getState()`), the current `focusRoot()`, and
    // the async-layout lifecycle counters (`layoutStats`, incl. `lastAppliedSeq`)
    // — so durable browser tests can assert REAL graph-state mutations
    // (positions, data, classes) and that stale layout completions are fenced
    // (`layoutStats.skippedStale`). The tests only READ through it; callers must
    // not treat `cy` as immutable. It adds no inline script (CSP-safe) and does
    // not affect rendering. Field shape may change with the implementation.
    try {
      window.__okfLoomGraph = {
        cy: cy,   // live, mutable Cytoscape core — for tests/diagnostics only
        getState: function () { try { return JSON.parse(JSON.stringify(controlState)); } catch (e) { return null; } },
        applyLens: applyLens,
        communityOf: function () { try { return JSON.parse(JSON.stringify(communityOf)); } catch (e) { return {}; } },
        focusRoot: function () { return focusRoot; },
        layoutStats: layoutStats,
        // Live-refresh fence counters (present only when the live layer wired
        // the DATA_URL refresh). Used to prove stale responses are dropped.
        refreshStats: (typeof refreshStats !== "undefined") ? refreshStats : null
      };
    } catch (e) {}
  }

  // ---- helpers ----------------------------------------------------------
  function setText(id, txt) {
    var el = document.getElementById(id);
    if (el) el.textContent = txt;
  }
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var ctx = this, args = arguments;
      if (t) clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }
  // Tiny createElement helper for the Signal-controls panel. Children are
  // appended as text nodes (strings) or elements — never innerHTML, so no
  // untrusted markup path is introduced (CSP-safe, no inline HTML).
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null) return;
      if (k === "class") node.className = v;
      else node.setAttribute(k, v);
    });
    (kids || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  // ---- Governed keys (P1-3 iter-1) -------------------------------------
  // Renders §7 governed keys (aliases / entities / provenance / citations /
  // relations) into #detail-governed, mirroring the concept page's
  // _render_governed_keys treatment (contract-runtime-parity). Built with
  // createElement + textContent so untrusted alias/entity/note text cannot
  // XSS the panel (the data ships as JSON from the server; URLs were
  // already _safe_url-sanitized server-side).
  function makeLabel(text) {
    var s = document.createElement("span");
    s.className = "okf-governed-label";
    s.textContent = text;
    return s;
  }

  function isWebUrl(u) {
    return typeof u === "string" && /^https?:\/\//i.test(u);
  }

  function renderGoverned(container, data) {
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);
    var sections = [];

    // aliases - pills
    var aliases = (data && data.aliases) || [];
    if (aliases.length) {
      var div = document.createElement("div");
      div.className = "okf-governed okf-aliases";
      div.appendChild(makeLabel("Also known as:"));
      aliases.forEach(function (a) {
        var pill = document.createElement("span");
        pill.className = "okf-alias-pill";
        pill.textContent = a;
        div.appendChild(document.createTextNode(" "));
        div.appendChild(pill);
      });
      sections.push(div);
    }

    // entities - <dl> (term = label + kind chip; dd = aliases)
    var entities = (data && data.entities) || [];
    if (entities.length) {
      var wrap = document.createElement("div");
      wrap.className = "okf-governed okf-governed--block okf-entities";
      wrap.appendChild(makeLabel("Entities:"));
      var dl = document.createElement("dl");
      dl.className = "okf-entity-list";
      entities.forEach(function (ent) {
        var dt = document.createElement("dt");
        var lbl = document.createElement("span");
        lbl.className = "okf-entity";
        lbl.textContent = ent.label || ent.id || "";
        dt.appendChild(lbl);
        if (ent.kind) {
          var kind = document.createElement("span");
          kind.className = "okf-entity-kind";
          kind.textContent = ent.kind;
          dt.appendChild(kind);
        }
        var dd = document.createElement("dd");
        if (ent.aliases && ent.aliases.length) {
          var al = document.createElement("span");
          al.className = "okf-entity-aliases";
          al.textContent = "(" + ent.aliases.join(", ") + ")";
          dd.appendChild(al);
        }
        dl.appendChild(dt);
        dl.appendChild(dd);
      });
      wrap.appendChild(dl);
      sections.push(wrap);
    }

    // provenance - stacked <ul> (source link / note / time)
    var provenance = (data && data.provenance) || [];
    if (provenance.length) {
      var pwrap = document.createElement("div");
      pwrap.className = "okf-governed okf-governed--block okf-provenance";
      pwrap.appendChild(makeLabel("Sources:"));
      var ul = document.createElement("ul");
      ul.className = "okf-provenance-list";
      provenance.forEach(function (p) {
        var li = document.createElement("li");
        li.className = "okf-provenance-item";
        if (p.source) {
          if (isWebUrl(p.source)) {
            var a = document.createElement("a");
            a.href = p.source; a.rel = "noopener";
            a.textContent = p.source;
            li.appendChild(a);
          } else {
            var s = document.createElement("span");
            s.className = "okf-provenance-source";
            s.textContent = p.source;
            li.appendChild(s);
          }
        }
        if (p.note) {
          var n = document.createElement("span");
          n.className = "okf-provenance-note";
          n.textContent = p.note;
          li.appendChild(n);
        }
        if (p.timestamp) {
          var t = document.createElement("time");
          t.className = "okf-provenance-time";
          t.textContent = p.timestamp;
          li.appendChild(t);
        }
        if (li.childNodes.length) ul.appendChild(li);
      });
      if (ul.childNodes.length) { pwrap.appendChild(ul); sections.push(pwrap); }
    }

    // citations - numbered <ol>
    var citations = (data && data.citations) || [];
    if (citations.length) {
      var cwrap = document.createElement("div");
      cwrap.className = "okf-governed okf-governed--block okf-citations";
      cwrap.appendChild(makeLabel("Citations:"));
      var ol = document.createElement("ol");
      ol.className = "okf-citation-list";
      citations.forEach(function (c) {
        var li = document.createElement("li");
        li.className = "okf-citation";
        if (c.id) {
          var id = document.createElement("span");
          id.className = "okf-cite-id";
          id.textContent = "[" + c.id + "]";
          li.appendChild(id);
        }
        if (c.text) {
          var tx = document.createElement("span");
          tx.className = "okf-cite-text";
          tx.textContent = c.text;
          li.appendChild(tx);
        }
        if (isWebUrl(c.url)) {
          var ua = document.createElement("a");
          ua.href = c.url; ua.rel = "noopener";
          ua.textContent = c.url;
          li.appendChild(ua);
        }
        if (li.childNodes.length) ol.appendChild(li);
      });
      if (ol.childNodes.length) { cwrap.appendChild(ol); sections.push(cwrap); }
    }

    // relations - typed chips
    var relations = (data && data.relations) || [];
    if (relations.length) {
      var rwrap = document.createElement("div");
      rwrap.className = "okf-governed okf-governed--block okf-relations-fm";
      rwrap.appendChild(makeLabel("Relations:"));
      relations.forEach(function (r, i) {
        if (i > 0) rwrap.appendChild(document.createTextNode(", "));
        var rel = document.createElement("span");
        rel.className = "okf-relation";
        var chip = document.createElement("span");
        chip.className = "okf-rel-type";
        chip.textContent = r.type || "related";
        rel.appendChild(chip);
        rel.appendChild(document.createTextNode(" → "));
        var tgt = document.createElement("span");
        tgt.textContent = r.target || "";
        rel.appendChild(tgt);
        if (r.via) {
          var det = document.createElement("span");
          det.className = "okf-rel-detail";
          det.textContent = " " + r.via;
          rel.appendChild(det);
        }
        rwrap.appendChild(rel);
      });
      sections.push(rwrap);
    }

    sections.forEach(function (s) { container.appendChild(s); });
    container.hidden = sections.length === 0;
  }
})();
