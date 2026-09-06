/* OKF Studio - live client (SSE + in-place DOM patching).
 *
 * The no-refresh contract (spec §7.3): when the bundle changes, every open
 * surface updates in place. We swap the SMALLEST affected region and never
 * re-render the whole page. Across every patch we preserve scroll position,
 * focus, active view mode, expanded/collapsed panels, open comment threads
 * + any in-flight comment draft, and graph zoom/selection.
 *
 * Responsibilities:
 *   - EventSource('/__events') with exponential-backoff reconnect; a subtle
 *     connection indicator; on reconnect, resync.
 *   - changed/created on the open concept → fetch /__data/doc, hand the doc
 *     to studio.js (okfLoomStudio.applyDoc) which renders it in the current view
 *     mode, then pulse-highlight the body.
 *   - graph/removed → delegate to studio.js.
 *   - presence/activity/comment → fan out via the hub so studio.js updates
 *     its panels.
 *   - resync → re-fetch open doc + tell studio to reload change list/comments.
 *
 * Security: untrusted strings never go through innerHTML here. The only
 * innerHTML assignment is the server-rendered concept body (.html field of
 * /__data/doc), which is produced by the SAME escaped markdown renderer the
 * concept page itself uses on initial load - identical trust level (§7.3).
 * All mutating requests in studio.js carry X-OKF-Token; live.js only reads.
 *
 * No framework, no bundler. Vanilla ES module. Degrades gracefully: if the
 * studio isn't enabled (window.__OKF_LOOM_STUDIO__ absent), this module is a no-op.
 */
(function () {
  "use strict";

  // CSP-safe bootstrap read: prefer the JSON data block the server embeds
  // (a <script type="application/json" id="okf-studio-bootstrap">, which
  // script-src does not govern), falling back to window.__OKF_LOOM_STUDIO__ for
  // parity with the documented API if a host page sets it directly.
  function readBoot() {
    if (window.__OKF_LOOM_STUDIO__ && typeof window.__OKF_LOOM_STUDIO__ === "object") {
      return window.__OKF_LOOM_STUDIO__;
    }
    const node = document.getElementById("okf-studio-bootstrap");
    if (node) {
      try {
        const cfg = JSON.parse(node.textContent || "{}");
        window.__OKF_LOOM_STUDIO__ = cfg; // publish for parity + studio.js
        return cfg;
      } catch (e) { /* fall through */ }
    }
    return null;
  }
  const BOOT = readBoot();
  if (!BOOT || !BOOT.live) return; // live updates disabled or studio absent

  const REDUCED_MOTION =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- tiny pub/sub hub (window.okfLoomLive) -------------------------------
  // studio.js subscribes; live.js is the only publisher. Typed channels
  // mirror the SSE event types plus internal lifecycle signals.
  const listeners = Object.create(null);
  function on(type, fn) {
    (listeners[type] || (listeners[type] = [])).push(fn);
    return () => off(type, fn);
  }
  function off(type, fn) {
    const arr = listeners[type];
    if (!arr) return;
    const i = arr.indexOf(fn);
    if (i >= 0) arr.splice(i, 1);
  }
  function emit(type, data) {
    const arr = listeners[type];
    if (!arr) return;
    // Copy so a handler unsubscribing mid-dispatch doesn't skip entries.
    arr.slice().forEach((fn) => {
      try { fn(data); } catch (e) { console.error("[okf-live] handler error", type, e); }
    });
  }
  window.okfLoomLive = { on, off, emit, get state() { return conn.state; }, get rev() { return rev; } };

  // ---- connection state ------------------------------------------------
  // `state` ∈ "online" | "reconnecting" | "offline". rev tracks the latest
  // bundle rev we've seen; a gap between events triggers a resync.
  const conn = { state: "connecting", backoff: 800, timer: null, source: null };
  let rev = BOOT.rev || 0;
  // Track the last event id we've processed (across SSE + polling) so the
  // poller can resume with ?since=<id>, and the last time SSE delivered
  // anything - used by the starvation watchdog to decide whether SSE is
  // being buffered by an intermediary proxy (Cloudflare tunnels, corporate
  // proxies, some CDNs) and we should fall back to polling.
  let lastEventId = null;
  let sseLastReceived = 0;
  let pollingTimer = null;
  let pollingActive = false;
  // Bundle C / INTENT-004 / §7.3 fix: the starvation watchdog's grace
  // window MUST be longer than the heartbeat interval (server-side 15s).
  // A grace shorter than the heartbeat arms false-positive polling on
  // every healthy idle stream. Default grace = 3 * heartbeat = 45s; both
  // the heartbeat interval and the grace are configurable via the
  // ``__OKF_LOOM_STUDIO__`` bootstrap so an operator can tune for unusual
  // proxies (a very aggressive proxy that buffers for 60s+ can extend
  // the grace; a tight localhost deployment can shorten it).
  const HEARTBEAT_INTERVAL_MS =
    (BOOT.sse_heartbeat_ms && Number.isFinite(BOOT.sse_heartbeat_ms) && BOOT.sse_heartbeat_ms > 0)
      ? Math.max(5000, BOOT.sse_heartbeat_ms)
      : 15000;
  const SSE_GRACE_MS =
    (BOOT.sse_grace_ms && Number.isFinite(BOOT.sse_grace_ms) && BOOT.sse_grace_ms > 0)
      ? Math.max(HEARTBEAT_INTERVAL_MS * 2, BOOT.sse_grace_ms)
      : HEARTBEAT_INTERVAL_MS * 3;
  const POLL_MS = 3000; // polling interval when SSE is starved.

  // iter1 CRI-001/INTENT-006: event-id guard. SSE and the polling fallback
  // both arm during the boot window before `ready`, and a buffered proxy can
  // deliver the same logical change through both paths. Without a guard the
  // body patches (and pulses) twice for one edit. We key on
  // per-append event_id (legacy: type/rev/ids). Process-local revision
  // counters can collide across CLI edits and HTTP undo.
  // The set is capped so a long session doesn't grow unbounded.
  const appliedKeys = new Set();
  const APPLIED_KEYS_CAP = 256;
  function eventKey(d) {
    if (!d || !d.type) return null;
    if (d.event_id) return d.event_id;
    const ids = Array.isArray(d.ids) ? d.ids.join(",") : "";
    return d.type + ":" + (d.rev != null ? d.rev : "") + ":" + ids;
  }
  function markApplied(key) {
    if (!key) return;
    if (appliedKeys.size >= APPLIED_KEYS_CAP) {
      // Drop the oldest ~25% to amortise the cap check.
      const drop = Array.from(appliedKeys).slice(0, Math.ceil(APPLIED_KEYS_CAP / 4));
      drop.forEach((k) => appliedKeys.delete(k));
    }
    appliedKeys.add(key);
  }
  function wasApplied(key) { return key ? appliedKeys.has(key) : false; }
  // Tracks the latest content-rev we've rendered per concept (for the
  // ready/doc_revs resync path above).
  const lastDocRev = Object.create(null);

  function setState(state) {
    conn.state = state;
    emit("conn", { state });
  }

  function currentConceptId() {
    // Authoritative for serve mode: /tables/orders → "tables/orders".
    let p = window.location.pathname.replace(/^\/+/, "").replace(/\/+$/, "");
    if (p.endsWith(".md")) p = p.slice(0, -3);
    return p;
  }

  // ---- SSE connect with exponential backoff ----------------------------
  function connect() {
    if (conn.source) { try { conn.source.close(); } catch (e) {} }
    let source;
    try {
      source = new EventSource("/__events");
    } catch (e) {
      // EventSource unsupported - go offline and stop retrying.
      setState("offline");
      return;
    }
    conn.source = source;

    // While in CONNECTING, show reconnecting; flips to online on first event.
    setState("reconnecting");

    source.addEventListener("ready", (e) => {
      const d = safeJson(e.data);
      markSseReceived();
      if (!d) return;
      rev = d.rev || rev;
      setState("online");
      conn.backoff = 800;
      scheduleBackoffClear();
      // iter1 CRI-001/INTENT-006: consume the Bundle-A `doc_revs` map (P1-6)
      // so a reconnect knows which docs changed while we were offline. If the
      // open concept's rev advanced, force a patch (the polling fallback may
      // have already delivered it, but the event-id guard in onChange makes
      // that idempotent). Older servers without doc_revs are unaffected.
      if (d.doc_revs && typeof d.doc_revs === "object") {
        const open = currentConceptId();
        const seen = open ? lastDocRev[open] : null;
        const fresh = d.doc_revs[open];
        if (open && typeof fresh === "number" && fresh !== seen) {
          patchOpenConcept(open, /*pulse*/ true).catch((err) => console.error("[okf-live] ready-resync patch failed", err));
        }
      }
    });

    // All data events route through handleEvent (shared with the polling
    // fallback) and mark SSE as alive so the watchdog knows it's delivering.
    source.addEventListener("changed", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("created", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("removed", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("graph", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("presence", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    source.addEventListener("activity", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     source.addEventListener("comment", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
     // INTENT4-001: listen for comment_link events so the change-list
     // back-link renders live (not just after page reload).
     source.addEventListener("comment_link", (e) => { markSseReceived(); handleEvent(safeJson(e.data)); });
    // Bundle C / INTENT-004 / §7.3 fix: the server's heartbeat now ships
    // BOTH a ``: ping`` comment (HTTP-level keep-alive) AND a real
    // ``event: ping`` frame (app-level heartbeat the EventSource
    // dispatches). Listening for ``ping`` lets the watchdog count the
    // heartbeat as "stream alive" so a HEALTHY idle stream (no real
    // changes, only heartbeats) is NOT misclassified as buffered/starved
    // and never falls back to polling.
    source.addEventListener("ping", () => { markSseReceived(); });
    source.addEventListener("resync", (e) => {
      markSseReceived();
      const d = safeJson(e.data);
      if (!d) return;
      noteRev(d.rev);
      doResync();
    });

    source.onopen = () => {
      markSseReceived();
      // "ready" sets online; keep this as a fallback for browsers that
      // don't fire addEventListener('ready') before onopen.
      if (conn.state !== "online") setState("online");
    };
    source.onerror = () => {
      // EventSource auto-reconnects, but we also drive our own backoff so
      // we can show a honest indicator + resync after a long drop.
      setState("reconnecting");
      scheduleReconnect();
    };
  }

  // ---- polling fallback (when SSE is buffered by a proxy) --------------
  // Some intermediaries (Cloudflare quick tunnels, corporate proxies, certain
  // CDNs) buffer the SSE text/event-stream response, so the browser's
  // EventSource connects but never receives frames. We detect that (no SSE
  // event within SSE_GRACE_MS) and fall back to polling /__data/events,
  // dispatching through the SAME handleEvent so the page patches identically.
  // This keeps the no-refresh contract alive behind any proxy.
  async function pollOnce() {
    try {
      // P2-1 (QUA3-004) + QUA4-005: when using a `since` cursor, pass
      // order=asc so the server honors the cursor (the desc default
      // ignores `since`). On boot (no cursor), use desc so the newest
      // events surface first.
      const order = lastEventId ? "asc" : "desc";
      const url = "/__data/events?limit=50&order=" + order + (lastEventId ? "&since=" + encodeURIComponent(lastEventId) : "");
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      const data = await res.json();
      const events = (data && Array.isArray(data.events)) ? data.events : [];
      if (events.length) {
        // Reconcile rev from the feed header even if no rows match.
        if (typeof data.rev === "number") noteRev(data.rev);
        if (order === "desc") events.reverse();
        for (const ev of events) handleEvent(ev);
        // We're getting live data over polling - show as online.
        if (conn.state !== "online") setState("online");
      }
    } catch (e) { /* transient fetch error; next tick retries */ }
  }

  function startPolling() {
    if (pollingActive) return;
    pollingActive = true;
    // First poll immediately so a buffered client catches up at once, then
    // settle into the interval.
    pollOnce();
    pollingTimer = setInterval(pollOnce, POLL_MS);
  }

  function stopPolling() {
    pollingActive = false;
    if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
  }

  // Watchdog: if SSE hasn't delivered ANY event within the grace window,
  // it's almost certainly being buffered - start polling. Re-checked on an
  // interval; if SSE recovers, markSseReceived() stops the poller.
  function starvationCheck() {
    if (pollingActive) return;
    const idle = Date.now() - (sseLastReceived || 0);
    if (idle >= SSE_GRACE_MS) startPolling();
  }

  function scheduleReconnect() {
    if (conn.timer) return;
    // The native EventSource may reconnect on its own; we additionally
    // probe. After several failed native retries the source closes; we
    // reopen with backoff. Cap at ~30s.
    const delay = Math.min(30000, conn.backoff);
    conn.timer = setTimeout(() => {
      conn.timer = null;
      conn.backoff = Math.min(30000, conn.backoff * 2);
      // If the native source is still open, leave it; otherwise reopen.
      if (conn.source && conn.source.readyState === EventSource.CLOSED) {
        connect();
      }
    }, delay);
  }
  function scheduleBackoffClear() {
    // Reset backoff once we're stably online for a moment.
    setTimeout(() => { if (conn.state === "online") conn.backoff = 800; }, 2000);
  }

  function safeJson(raw) {
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }

  function noteRev(r) {
    if (typeof r === "number" && r > rev) rev = r;
  }

  // ---- unified event dispatch (shared by SSE + polling fallback) -------
  // Both delivery paths route events through here so the page patches
  // identically regardless of transport. noteRev is idempotent.
  function handleEvent(d) {
    if (!d || !d.type) return;
    noteRev(d.rev);
    if (d.event_id || d.id) lastEventId = d.event_id || d.id;
    switch (d.type) {
      case "changed": onChange(d, "changed"); break;
      case "created": onChange(d, "created"); break;
      case "removed": onRemoved(d); break;
      case "graph":
        emit("graph", d);
        if (window.okfLoomStudio && typeof window.okfLoomStudio.refreshGraph === "function") {
          try { window.okfLoomStudio.refreshGraph(); } catch (e) { console.error(e); }
        }
        break;
      case "presence": emit("presence", d); break;
      case "activity": emit("activity", d); break;
      case "comment": emit("comment", d); break;
      case "comment_link": emit("comment_link", d); break;
      case "agent_conflict": emit("agent_conflict", d); break;
      case "suggestions": emit("suggestions", d); break;
      // 'ready'/'resync' are SSE lifecycle control frames, not feed events.
    }
  }

  function markSseReceived() {
    sseLastReceived = Date.now();
    // SSE delivered - if we'd fallen back to polling, stop it (SSE is cheaper).
    if (pollingActive) stopPolling();
  }

  // ---- changed / created handler ---------------------------------------
  // For the open concept: fetch the doc and apply it in place. Preserve
  // scroll/focus/drafts (studio.js.applyDoc owns the view-aware swap; the
  // comment composer + open panels are in separate DOM, untouched).
  function onChange(d, kind) {
    if (!d) return;
    noteRev(d.rev);
    const open = currentConceptId();
    const ids = Array.isArray(d.ids) ? d.ids : [];
    const affectsOpen = ids.indexOf(open) >= 0;

    // Always fan out so the change list / comments / presence update.
    emit(kind, d);

    if (!affectsOpen) return;
    // iter1 CRI-001: dedupe across SSE + polling so the same change can't
    // trigger two body patches / two pulses. The panels still update via
    // the emit() above (idempotent upserts), so the change list and
    // comments stay correct; only the expensive DOM patch is gated.
    const key = eventKey(d);
    if (wasApplied(key)) return;
    markApplied(key);
    patchOpenConcept(open, /*pulse*/ true).catch((e) => {
      appliedKeys.delete(key);
      console.error("[okf-live] patch failed", e);
    });
  }

  function onRemoved(d) {
    if (!d) return;
    noteRev(d.rev);
    emit("removed", d);
    const open = currentConceptId();
    if ((Array.isArray(d.ids) ? d.ids : []).indexOf(open) >= 0) {
      // The open concept was deleted. Surface honestly without a hard
      // reload; the user can navigate away.
      emit("open-removed", { id: open });
    }
  }

  // ---- the in-place patch (sacred state-preservation contract) ---------
  // Per-concept fetch sequence: two rapid `changed` events fire two
  // concurrent /__data/doc fetches, and without a fence the OLDER response
  // can resolve last and overwrite the newer body. Each call
  // takes a ticket; only the holder of the latest ticket may apply.
  const _docFetchSeq = {};
  async function patchOpenConcept(conceptId, pulse) {
    const seq = (_docFetchSeq[conceptId] = (_docFetchSeq[conceptId] || 0) + 1);
    const res = await fetch("/__data/doc?id=" + encodeURIComponent(conceptId), {
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error("doc fetch " + res.status);
    const doc = await res.json();
    if (_docFetchSeq[conceptId] !== seq) return; // a newer fetch superseded us

    // Capture the bits we promise to preserve BEFORE the DOM mutates.
    const savedScrollX = window.scrollX;
    const savedScrollY = window.scrollY;
    const active = document.activeElement;
    const activeId = active && active.id ? active.id : null;

    // Delegate the view-aware swap to studio.js when present; else fall
    // back to a plain rendered-body swap so live.js is self-sufficient.
    let studioHandledPulse = false;
    if (window.okfLoomStudio && typeof window.okfLoomStudio.applyDoc === "function") {
      // studio.js.applyDoc owns the block-level diff (iter1 CRI-001) and
      // preserves in-body selection across the patch. It also re-applies
      // comment marks (CRI-002), repositions margin markers, AND pulses
      // only the changed blocks (scoped pulse) - so live.js must NOT also
      // pulse the whole body. applyDoc returns true when it handled the
      // pulse itself.
      try { studioHandledPulse = !!window.okfLoomStudio.applyDoc(doc, { pulse: pulse }); }
      catch (e) { console.error("[okf-live] applyDoc threw", e); studioHandledPulse = false; }
    } else {
      // Fallback (no studio): preserve selection across the whole-body swap.
      const selSnapshot = saveBodySelection();
      const body = document.querySelector(".okf-page__body");
      if (body) body.innerHTML = doc.html || "";
      restoreBodySelection(selSnapshot);
      // studio.js dispatches this after its block diff; the fallback swap
      // must too, or renderers.js never re-runs (mermaid/hljs/KaTeX plus
      // the table/code/image enhancers) over the fresh body.
      try { window.dispatchEvent(new CustomEvent("okf-loom:bodyPatched")); } catch (e) {}
    }

    // Record what we rendered so the ready/doc_revs path can detect drift.
    if (conceptId && typeof doc.rev !== "undefined") lastDocRev[conceptId] = doc.rev;

    // Restore scroll exactly (the page header/sidebar are unchanged, so
    // pixel-identical scroll reads as "no jump").
    window.scrollTo(savedScrollX, savedScrollY);

    // Restore focus if the previously-focused element still exists. If it
    // lived inside the swapped body it's gone - move focus to the body
    // container so keyboard users aren't dropped.
    if (activeId) {
      const restored = document.getElementById(activeId);
      if (restored && typeof restored.focus === "function") {
        try { restored.focus({ preventScroll: true }); } catch (e) {}
      } else {
        const body = document.querySelector(".okf-page__body");
        if (body) { body.setAttribute("tabindex", "-1"); try { body.focus({ preventScroll: true }); } catch (e) {} }
      }
    }

    // iter1 CRI-001: only the fallback path pulses the whole body. When
    // studio.js ran, it already pulse-highlighted the individual changed
    // blocks (scoped pulse), so a body-wide flash would double up.
    if (pulse && !studioHandledPulse) pulseHighlight();
    emit("patched", { id: conceptId, rev: doc.rev });
  }

  // ---- selection save/restore (fallback path) -------------------------
  // Captures the selected text + its character offsets within the body so a
  // whole-body innerHTML swap can attempt to restore the same selection.
  // Best-effort: if the selected text no longer exists after the swap, the
  // selection is simply cleared (no false match). The studio.js path uses a
  // richer block-aware restore (see studio.js saveSelectionAcrossPatch).
  function saveBodySelection() {
    const sel = window.getSelection && window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    const body = document.querySelector(".okf-page__body");
    if (!body || !body.contains(sel.anchorNode)) return null;
    return { text: sel.toString() };
  }
  function restoreBodySelection(snapshot) {
    if (!snapshot || !snapshot.text) return;
    const body = document.querySelector(".okf-page__body");
    if (!body) return;
    const found = findTextNode(body, snapshot.text);
    if (!found) return;
    const range = document.createRange();
    range.setStart(found.node, found.start);
    range.setEnd(found.node, found.end);
    const sel = window.getSelection();
    if (!sel) return;
    sel.removeAllRanges();
    try { sel.addRange(range); } catch (e) {}
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

  function pulseHighlight() {
    if (REDUCED_MOTION) return; // respect preference: no flash
    const body = document.querySelector(".okf-page__body");
    if (!body) return;
    body.classList.remove("okf-pulse");
    // Force reflow so the animation restarts on consecutive patches.
    void body.offsetWidth;
    body.classList.add("okf-pulse");
  }

  // ---- resync ----------------------------------------------------------
  async function doResync() {
    emit("resync", {});
    const open = currentConceptId();
    if (open && document.querySelector(".okf-page__body")) {
      try { await patchOpenConcept(open, false); } catch (e) { console.error(e); }
    }
  }

  // Expose for studio.js / debugging / the command palette.
  window.okfLoomLive.patchNow = () => patchOpenConcept(currentConceptId(), true);
  window.okfLoomLive.resync = doResync;
  window.okfLoomLive.currentConceptId = currentConceptId;
  // Bundle C / INTENT-004: minimal debug accessors so the browser test
  // (tests/test_studio_iter1_browser.py::test_sse_watchdog_quiet_bundle_*)
  // can verify that a HEALTHY idle stream (heartbeat-only) never arms the
  // polling fallback. Read-only — no production behaviour depends on these.
  Object.defineProperty(window.okfLoomLive, "_debug", {
    get() {
      return {
        pollingActive,
        sseLastReceived,
        graceMs: SSE_GRACE_MS,
        heartbeatMs: HEARTBEAT_INTERVAL_MS,
        connState: conn.state,
      };
    },
    configurable: true,
  });

  // ---- boot ------------------------------------------------------------
  // Connect once DOM is ready (head wiring is before </head>; the body may
  // not be parsed yet). We also re-run presence/highlight wiring after load.
  // The starvation watchdog runs every 2s: if SSE hasn't delivered in the
  // grace window (proxy buffering), it arms the polling fallback.
  let watchdogTimer = null;
  function boot() {
    connect();
    watchdogTimer = setInterval(starvationCheck, 2000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  // On tab refocus, if we've been disconnected, nudge a resync.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && conn.state !== "online") {
      connect();
    }
  });
})();
