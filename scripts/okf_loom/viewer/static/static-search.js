/* OKF static-build client-side search (current spec §9).
 *
 * Loaded only on the static search page (__search.html) when the bundle was
 * built with `--target static`. Reads `?q=` from the URL, consumes the inert
 * corpus in the page (file:// safe), with __data/search.json as a fallback
 * for older/hosted builds, tokenizes the
 * query, scores each corpus entry by term frequency with field weights, and
 * renders the results into the existing results container.
 *
 * Zero dependencies (vanilla JS). CSP compliant: script-src 'self' +
 * connect-src 'self' - both satisfied because the script and the corpus are
 * same-origin. No inline handlers, no eval, no dynamic imports.
 *
 * Determinism: the corpus is sorted by concept_id at build time; results are
 * sorted by (-score, id), so identical (query, corpus) ⇒ identical result
 * order across browsers and runs.
 *
 * The live `serve` search path is UNCHANGED - it still hits /__search
 * server-side. This script is a no-op outside static mode.
 */
(function () {
  "use strict";

  // Only run on the static search page. The <body> carries
  // data-okf-mode="static" in static builds; serve/spa keep using the live
  // /__search backend. Also require the results container so we never run on
  // a concept page that happens to share data-okf-mode.
  var body = document.body;
  if (!body) return;
  if (body.getAttribute("data-okf-mode") !== "static") return;
  var resultsContainer = document.querySelector(".okf-search__results");
  if (!resultsContainer) return;

  // P2-3 (iter-2): the search h1 is no longer wrapped in .okf-muted, so we
  // update the heading element directly. The prior code queried a child
  // `.okf-muted` span (countSpan) + a `.okf-search__query` span that the
  // template never emitted (querySpan was always null). Now we rebuild the
  // heading text with proper pluralisation, mirroring the server-side
  // _render_search_page heading strings.
  var titleEl = document.querySelector(".okf-search__title");
  var inputEl = document.querySelector('input[type="search"][name="q"]');

  // Announce result updates to assistive tech (results replace the empty
  // server-rendered state without a full navigation).
  resultsContainer.setAttribute("aria-live", "polite");
  resultsContainer.setAttribute("aria-busy", "true");

  // ---------------------------------------------------------------------
  // Hide the now-stale "search unavailable" notice that wiki.js appends to
  // the topbar search input on every static page. On the search page itself
  // the client-side searcher is live, so the notice would be misleading.
  // (wiki.js is owned by a different workstream; we hide its artifact here
  // rather than edit wiki.js. This is the only cross-file coupling and is
  // keyed off the stable `.okf-search-note` class + the literal "unavailable"
  // substring, so it degrades safely if wiki.js's note text changes.)
  // ---------------------------------------------------------------------
  function hideStaleNote() {
    var notes = document.querySelectorAll(".okf-search-note");
    for (var i = 0; i < notes.length; i++) {
      var t = notes[i].textContent || "";
      if (t.indexOf("unavailable") >= 0) {
        notes[i].setAttribute("hidden", "");
      }
    }
  }
  hideStaleNote();
  // wiki.js is `defer` and runs before us in document order, but defensively
  // re-check after a microtask in case future ordering changes.
  Promise.resolve().then(hideStaleNote);

  // ---------------------------------------------------------------------
  // URL query parsing (?q=...).
  // ---------------------------------------------------------------------
  function getQuery() {
    var qs = window.location.search || "";
    if (qs.charAt(0) === "?") qs = qs.slice(1);
    if (!qs) return "";
    var pairs = qs.split("&");
    for (var i = 0; i < pairs.length; i++) {
      var eq = pairs[i].indexOf("=");
      if (eq < 0) continue;
      var k = decodeURIComponent(pairs[i].slice(0, eq).replace(/\+/g, " "));
      if (k === "q") {
        return decodeURIComponent(pairs[i].slice(eq + 1).replace(/\+/g, " "));
      }
    }
    return "";
  }

  // ---------------------------------------------------------------------
  // Tokenizer: lowercase, split on Unicode word runs, drop stopwords.
  // APPROXIMATES the Python `search.tokenize` for the static-build use case
  // (no external deps), but is NOT a byte-identical mirror (iter2 P3-1):
  //   - The stopword set is a pragmatic subset; the Python `_STOPWORDS` list
  //     differs (it includes more function words like been/being/having/
  //     into/through/under/until/up/off/once/other/out/over/some/their/
  //     there/what/when/where/which/while/who/whom/why/would; the JS set
  //     includes a few short forms like don/now/d/ll/m/o/re/ve/y).
  //   - The scorer uses field-weighted term-frequency (title×3, aliases×2,
  //     description×2, tags×1, body×1) - simpler than the live BM25+IDF
  //     backend (which uses title×5, headings×3, description×2, body×1,
  //     tags×1 with IDF normalization). The corpus does not carry `headings`
  //     (a live-only field); aliases/type are additive corpus-only fields.
  //   - The scorer uses raw substring indexOf (matches inside other words),
  //     unlike the Python token-set matching.
  // These divergences are acceptable: the static search is a convenience
  // fallback for static-hosted bundles; it is NOT required to produce
  // byte-identical rankings to the live `/__search` backend (which uses a
  // different algorithm). The current browser-proof contract does not mandate parity.
  // `\p{L}`/`\p{N}` require the ES2018 `u` flag (Chrome ≥64, FF ≥78,
  // Safari ≥12 - well within the static-build browser matrix).
  // ---------------------------------------------------------------------
  var STOP = new Set((
    "a an and are as at be but by for from has have in is it its of on or " +
    "that the to was were will with this these those your yours you your we " +
    "our ours us me my mine i he she they them his her hers do does did doing " +
    "can could should shall may might must also via per within without about " +
    "above after again against all because before below between during each " +
    "few further down not no nor if then than so such only own same very don " +
    "now d ll m o re ve y"
  ).split(" "));

  function tokenize(text) {
    if (!text) return [];
    var lowered = String(text).toLowerCase();
    var raw = lowered.match(/[\p{L}\p{N}_]+/gu) || [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var tok = raw[i];
      if (!tok) continue;
      // CJK/ideographic fallback: a token with NO ASCII letter/digit is
      // split per character (so 比特币 indexes as 比, 特, 币).
      if (!/[a-z0-9]/.test(tok)) {
        for (var j = 0; j < tok.length; j++) {
          var ch = tok[j];
          if (ch && !STOP.has(ch)) out.push(ch);
        }
        continue;
      }
      if (STOP.has(tok)) continue;
      out.push(tok);
    }
    return out;
  }

  function countMatches(haystack, needle) {
    if (!haystack || !needle) return 0;
    var count = 0;
    var idx = 0;
    while ((idx = haystack.indexOf(needle, idx)) !== -1) {
      count++;
      idx += needle.length;
    }
    return count;
  }

  // ---------------------------------------------------------------------
  // Scorer: term frequency across field-weighted fields.
  //   title×3 + aliases×2 + description×2 + tags×1 + body_excerpt×1
  // Matches the contract documented at the build-time emitter
  // (render.py:_search_corpus_json) so live and static results stay
  // semantically close.
  // ---------------------------------------------------------------------
  function scoreEntry(entry, queryTokens) {
    if (!queryTokens.length) return 0;
    var title = String(entry.title || "").toLowerCase();
    var aliases = (entry.aliases || []).join(" ").toLowerCase();
    var desc = String(entry.description || "").toLowerCase();
    var tags = (entry.tags || []).join(" ").toLowerCase();
    var bodyExcerpt = String(entry.body_excerpt || "").toLowerCase();
    var score = 0;
    for (var i = 0; i < queryTokens.length; i++) {
      var t = queryTokens[i];
      score += countMatches(title, t) * 3;
      score += countMatches(aliases, t) * 2;
      score += countMatches(desc, t) * 2;
      score += countMatches(tags, t);
      score += countMatches(bodyExcerpt, t);
    }
    return score;
  }

  function escapeHtml(s) {
    // iter2 P3-6: escape single-quote too (defense-in-depth). All current
    // call sites use double-quoted attribute context, but a future edit
    // putting an escaped value into a single-quoted attribute or JS string
    // literal would silently introduce an attribute-breakout hole without
    // this. CSP 'self' caps the blast radius regardless.
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function makeSnippet(entry) {
    // Prefer the curated description; fall back to the build-time body
    // excerpt. Truncate to 200 chars to match the live search page.
    var desc = entry.description || "";
    if (desc) return String(desc).slice(0, 200);
    var body = entry.body_excerpt || "";
    if (body) return String(body).slice(0, 200);
    return "";
  }

  function setStatus(count, query) {
    if (titleEl) {
      var q = query || "";
      // Match the server-side pluralisation (render.py _render_search_page):
      // "No results for ...", "1 result for ...", "N results for ...".
      if (count === 0) {
        titleEl.textContent = "No results for \u201C" + q + "\u201D";
      } else if (count === 1) {
        titleEl.textContent = "1 result for \u201C" + q + "\u201D";
      } else {
        titleEl.textContent = count + " results for \u201C" + q + "\u201D";
      }
    }
    // Reflect the URL query into the topbar input so re-searching works.
    if (inputEl && !inputEl.value) inputEl.value = query || "";
  }

  // ---------------------------------------------------------------------
  // Renderers. The result shape mirrors the live search page
  // (`<article class="okf-search-result"><h3><a>title</a> <span>id</span></h3>
  //   <div class="okf-search-snippet">…</div></article>`).
  // ---------------------------------------------------------------------
  function renderResults(query, entries) {
    var tokens = tokenize(query);
    var scored = [];
    for (var i = 0; i < entries.length; i++) {
      var s = scoreEntry(entries[i], tokens);
      if (s > 0) scored.push({ entry: entries[i], score: s });
    }
    // Deterministic sort: (-score, id). Corpus is pre-sorted by id, so this
    // is reproducible across runs/browsers.
    scored.sort(function (a, b) {
      if (a.score !== b.score) return b.score - a.score;
      var ai = a.entry.id || "";
      var bi = b.entry.id || "";
      return ai < bi ? -1 : ai > bi ? 1 : 0;
    });

    setStatus(scored.length, query);
    resultsContainer.setAttribute("aria-busy", "false");

    if (!scored.length) {
      resultsContainer.innerHTML =
        '<p class="okf-search-empty">No results for "' +
        escapeHtml(query) + '".</p>';
      return;
    }
    var html = "";
    for (var j = 0; j < scored.length; j++) {
      var e = scored[j].entry;
      var cid = e.id || "";
      // Static concept pages live at <id>.html at the bundle root.
      var url = cid + ".html";
      var snippet = makeSnippet(e);
      html +=
        '<article class="okf-search-result">' +
        '<h3><a href="' + escapeHtml(url) + '" class="okf-internal">' +
        escapeHtml(e.title || cid) + '</a>' +
        ' <span class="okf-muted">' + escapeHtml(cid) + '</span></h3>' +
        '<div class="okf-search-snippet">' + escapeHtml(snippet) + '</div>' +
        '</article>';
    }
    resultsContainer.innerHTML = html;
  }

  function renderUnavailable(message) {
    // Older build without __data/search.json, or fetch failure: fail closed
    // with a graceful message pointing to the index. No partial state.
    setStatus(0, getQuery());
    resultsContainer.setAttribute("aria-busy", "false");
    resultsContainer.innerHTML =
      '<p class="okf-search-empty">' + escapeHtml(message) + '</p>';
  }

  // ---------------------------------------------------------------------
  // Corpus URL resolution. The static search page is emitted at the bundle
  // root (__search.html), so __data/search.json is a sibling directory. We
  // resolve the root prefix from the search form's action attribute so the
  // path stays correct if the site is hosted under a sub-path or if the
  // search page is ever nested.
  // ---------------------------------------------------------------------
  function corpusUrl() {
    var form = document.querySelector("form.okf-search-form");
    var root = "./";
    if (form) {
      var act = form.getAttribute("action") || "";
      var idx = act.indexOf("__search");
      if (idx >= 0) root = act.slice(0, idx) || "./";
    }
    return root + "__data/search.json";
  }

  function acquireCorpus() {
    var inline = document.getElementById("okf-search-data");
    if (inline) {
      try {
        var raw = inline.content ? inline.content.textContent : inline.textContent;
        return Promise.resolve(JSON.parse(raw || "[]"));
      } catch (e) {
        return Promise.reject(e);
      }
    }
    return fetch(corpusUrl(), { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  // ---------------------------------------------------------------------
  // Initial render from ?q=. No query yet → friendly prompt (the form is
  // already visible and the user just hasn't typed). Otherwise fetch the
  // corpus and render. Network/HTTP/parse failures all funnel to the same
  // graceful unavailable message - no unhandled rejections.
  // ---------------------------------------------------------------------
  var initialQuery = getQuery();
  if (!initialQuery) {
    setStatus(0, "");
    resultsContainer.setAttribute("aria-busy", "false");
    resultsContainer.innerHTML =
      '<p class="okf-search-empty">Type a query above and press Enter.</p>';
    return;
  }

  acquireCorpus()
    .then(function (data) {
      // Accept either a bare array (current build) or a wrapper
      // {entries: [...]} (forward-compat). Anything else → empty.
      var entries = Array.isArray(data)
        ? data
        : (data && Array.isArray(data.entries) ? data.entries : []);
      renderResults(initialQuery, entries);
    })
    .catch(function () {
      renderUnavailable(
        "Search corpus not available. This may be an older build; " +
        "use the index to browse concepts."
      );
    });
})();
