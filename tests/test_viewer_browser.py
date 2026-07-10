"""Browser e2e proof for the OKF viewer (current spec §9).

Requires ``python -m pip install playwright`` + ``playwright install chromium``.
Skips cleanly otherwise.

This module is gated three ways so the default ``pytest`` run never fails
because of a missing optional dependency or browser binary:

1. ``pytest.importorskip("playwright")`` skips collection of the whole
   module when Playwright is not importable (optional browser-proof dependencies
   were not installed).
2. ``pytestmark = pytest.mark.browser`` registers/labels every test with
   the ``browser`` marker (see ``pyproject.toml``); CI can deselect via
   ``-m "not browser"`` if desired.
3. The ``page`` fixture skips the individual test if the Chromium binary
   itself is not installed (``playwright install chromium`` not run), and
   the ``server_url`` fixture skips if the live server cannot start.

The tests stand up ``okf serve samples/demo_bundle`` on an ephemeral port
and assert the browser-proof contracts: graph renders, concept page loads,
search returns results, and backlinks show on a referenced concept.
"""
from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request
import os
from pathlib import Path

import pytest

# Gate 1: skip the entire module cleanly when optional browser-proof dependencies are absent.
pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from conftest import okf_module_argv, okf_subprocess_env

# Gate 2: every test in this module is marked as a browser test.
pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resolved repo root (the directory containing pyproject.toml). We do NOT
# depend on the process's CWD because pytest can be invoked from anywhere.
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"

# How long to wait for `okf serve` to bind + answer 200 on /, and how often
# to poll. The server boots in well under a second on a warm machine; this
# budget covers slow CI runners.
_SERVER_STARTUP_TIMEOUT = 15.0
_SERVER_POLL_INTERVAL = 0.15

# Per-request HTTP timeout used during readiness polling.
_POLL_HTTP_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Reserve and immediately release an ephemeral port for the test server.

    Mirrors ``tests/test_server_smoke.py`` so we never collide with the
    default ``okf serve`` port (8787) or any concurrently running service.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(proc: subprocess.Popen, base: str) -> None:
    """Poll ``base`` until ``GET /`` returns 200; skip on failure.

    Skip (not fail) when:
      * the ``okf serve`` subprocess exits before becoming ready, or
      * the readiness deadline elapses without a 200.
    """
    deadline = time.monotonic() + _SERVER_STARTUP_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            pytest.skip(f"okf serve exited before becoming ready (rc={rc})")
        try:
            with urllib.request.urlopen(f"{base}/", timeout=_POLL_HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
        time.sleep(_SERVER_POLL_INTERVAL)
    pytest.skip(
        f"okf serve did not become ready within {_SERVER_STARTUP_TIMEOUT:g}s "
        f"(last error: {last_error!r})"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def server_url():
    """Start ``okf serve samples/demo_bundle`` on an ephemeral port once.

    Returns the base URL (e.g. ``http://127.0.0.1:54321``). Always tears
    the server down via SIGTERM (and SIGKILL on timeout) in ``finally``.
    """
    if not DEMO_BUNDLE.is_dir():
        pytest.skip(f"demo bundle not found at {DEMO_BUNDLE}")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # --no-watch: file watcher is unnecessary for a fixed bundle and adds a
    #   background thread that complicates clean teardown.
    # --no-open: never pop a browser window from the test process.
    proc = subprocess.Popen(
        okf_module_argv(
            "serve", str(DEMO_BUNDLE), "--host", "127.0.0.1", "--port", str(port),
            "--no-watch", "--no-open",
        ),
        cwd=str(TOOLKIT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=okf_subprocess_env(),
    )
    try:
        _wait_for_server(proc, base)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def page():
    """Yield a fresh Playwright ``Page`` backed by a per-test Chromium.

    Gate 3: skip the individual test if the Chromium binary is missing
    (``playwright install chromium`` not run) instead of erroring out.

    Tries (1) the system Chrome at ``$AIC_PLAYWRIGHT_CHROME_PATH`` if set,
    (2) ``channel="chrome"``, (3) the bundled Chromium. The first one that
    launches wins; all are skip-on-failure rather than error.
    """
    chrome_path = os.environ.get("AIC_PLAYWRIGHT_CHROME_PATH") or ""
    attempts: list[dict] = []
    if chrome_path:
        attempts.append({"executable_path": chrome_path, "args": ["--no-sandbox"]})
    attempts.append({"channel": "chrome"})
    attempts.append({})  # default: bundled chromium
    with sync_playwright() as p:
        browser = None
        last_exc: Exception | None = None
        for kwargs in attempts:
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception as exc:
                last_exc = exc
                continue
        if browser is None:
            pytest.skip(
                "chromium binary not installed and no system Chrome available: "
                "run `playwright install chromium` or install Google Chrome. "
                f"({last_exc})"
            )
        try:
            context = browser.new_context()
            pg = context.new_page()
            yield pg
            context.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Browser-proof contracts
# ---------------------------------------------------------------------------


def test_graph_renders(server_url: str, page) -> None:
    """The full-page graph view mounts the Cytoscape canvas.

    Previously the canvas check was ``try: wait_for_selector(canvas) except:
    pass``, which SWALLOWED a render failure, and only a non-zero container
    bounding box was asserted (which passes with zero nodes — the graph
    section mounts even when Cytoscape never paints). Now:

      * the Cytoscape ``<canvas>`` MUST be visible (hard assertion — fails if
        the CDN script is blocked or Cytoscape fails to initialise); and
      * the graph carries a non-zero node count, proved by reading the same
        graph.json the page consumed (a non-zero bbox alone cannot distinguish
        a rendered graph from an empty one).
    """
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    # The graph container is always present in the template; visibility +
    # non-zero bounding box prove it actually rendered rather than being
    # hidden by a CSS / load failure.
    graph = page.locator("#okf-graph.okf-graph")
    graph.wait_for(state="visible")
    box = graph.bounding_box()
    assert box is not None and box["width"] > 0 and box["height"] > 0, (
        "graph container is not laid out (zero bounding box)"
    )
    # HARD assertion: the Cytoscape <canvas> must be visible. If Cytoscape.js
    # failed to load (CDN blocked) or failed to initialise, no canvas is
    # created and graph.js shows a load-error notice instead — so this fails,
    # rather than being swallowed by the old try/except.
    # Cytoscape stacks multiple <canvas> elements (one per layer); the bottom
    # layer is always present once Cytoscape initialises, so .first is the
    # stable visibility probe.
    canvas = page.locator("#okf-graph canvas").first
    expect(canvas).to_be_visible()
    # Non-zero node count: fetch the same graph.json the page fed to Cytoscape
    # and assert it carries real concepts. A non-zero bbox passes with zero
    # nodes, so this is the proof that actual concept nodes were rendered.
    node_count = page.evaluate("""async () => {
        const el = document.getElementById('okf-graph');
        const url = el.getAttribute('data-graph-url');
        const resp = await fetch(url);
        const data = await resp.json();
        return (data.nodes || []).length;
    }""")
    assert node_count > 0, (
        "graph rendered with zero nodes — data path delivered no concepts"
    )


def test_concept_page_loads(server_url: str, page) -> None:
    """A concept page renders its H1 title and a non-empty body."""
    page.goto(f"{server_url}/tables/orders", wait_until="domcontentloaded")
    h1 = page.locator("h1.okf-page__title")
    h1.wait_for(state="visible")
    title = (h1.text_content() or "").strip()
    assert title, "concept page H1 is empty"
    # The Orders concept title is "Orders"; tolerate case but require text.
    assert "order" in title.lower(), f"unexpected concept title: {title!r}"
    body = page.locator(".okf-page__body")
    body.wait_for(state="visible")
    assert (body.inner_text() or "").strip(), "concept body is empty"


def test_search_returns_results(server_url: str, page) -> None:
    """The live search endpoint returns at least one hit for ``orders``.

    Uses the live server (the static build has no search backend and the
    capture script covers that path separately).
    """
    page.goto(server_url + "/", wait_until="domcontentloaded")
    # The index page exposes a topbar search form whose input is named ``q``.
    search_input = page.locator('input[type="search"][name="q"]').first
    search_input.wait_for(state="visible")
    search_input.fill("orders")
    # Submitting the form navigates to /__search?q=orders on the live server.
    with page.expect_navigation(wait_until="domcontentloaded") as nav_info:
        search_input.press("Enter")
    resp = nav_info.value
    assert resp.ok, f"search navigation failed: status={resp.status}"
    assert "/__search" in page.url, f"did not navigate to search: {page.url}"
    results = page.locator(".okf-search__results")
    results.wait_for(state="visible")
    # At least one result element should be rendered (link or list item).
    result_count = results.locator("a, li").count()
    assert result_count >= 1, "search returned zero results for 'orders'"


def test_static_file_graph_and_search_load_without_fetch(tmp_path: Path, page) -> None:
    """A multi-file static build works when opened directly via ``file://``.

    This is the browser-level contract that file-existence unit tests cannot
    prove: adjacent JSON fetches are blocked by normal browser security.
    """
    from okf_loom import Bundle
    from okf_loom.render import build_site

    out_dir = tmp_path / "static-file-site"
    bundle = Bundle.load(DEMO_BUNDLE)
    build_site(bundle, out_dir, target="static")

    page.goto((out_dir / "__graph.html").as_uri(), wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraphData && "
        "Array.isArray(window.__okfLoomGraphData.nodes)",
        timeout=10000,
    )
    node_count = page.evaluate("() => window.__okfLoomGraphData.nodes.length")
    assert node_count == len(bundle.concepts)
    assert "Failed to fetch" not in page.locator("body").inner_text()

    page.goto(
        (out_dir / "__search.html").as_uri() + "?q=orders",
        wait_until="domcontentloaded",
    )
    results = page.locator(".okf-search-result")
    results.first.wait_for(state="visible", timeout=10000)
    assert results.count() >= 1
    assert "Search corpus not available" not in page.locator("body").inner_text()


def test_backlinks_show(server_url: str, page) -> None:
    """A referenced concept shows a non-empty 'Cited by' section.

    ``tables/customers`` is referenced by ``tables/orders`` (both a body
    markdown link and a typed ``references`` relation), so the backlinks
    block on its page must be present and non-empty.
    """
    page.goto(f"{server_url}/tables/customers", wait_until="domcontentloaded")
    backlinks = page.locator("#okf-backlinks")
    backlinks.wait_for(state="attached")
    assert backlinks.is_visible(), "backlinks section is hidden"
    # The section is always rendered (template emits the heading); the
    # contract is that it contains at least one outbound link.
    link_count = backlinks.locator("a").count()
    assert link_count >= 1, (
        "'Cited by' section is empty; expected a link back to tables/orders"
    )


# ---------------------------------------------------------------------------
# Signal controls — durable behaviour + async-lifecycle proof
# ---------------------------------------------------------------------------
#
# These assert REAL Cytoscape/graph-state mutations (node positions, data,
# classes; edge weights/classes; focus/dim classes; layout lifecycle counters)
# through the ``window.__okfLoomGraph`` test/diagnostic hook (which exposes the LIVE,
# mutable Cytoscape instance for tests) — not screenshots or status strings.
# They prove each control changes graph state, that keyboard operation works,
# and that the SINGLE stale-safe layout owner fences late completions so the
# latest control/LOD/focus state always wins.
#
# Lifecycle owner contract (runLayoutNow): the owner token (layoutSeq) is BUMPED
# BEFORE the previous layout is stopped, so even a SYNCHRONOUS stop-triggered
# `layoutstop` from the old run sees `mySeq != layoutSeq` and is fenced. The
# settle result is applied exactly ONCE per layout (one-shot by sequence), so a
# layout's `layoutstop` + its safety-net timer never double-apply. Readiness is
# keyed on the exact `layoutStats.lastAppliedSeq`.
#
# ASYNC_LIFECYCLE_MATRIX (proven by test_signal_async_lifecycle_latest_wins and
# test_signal_lod_lifecycle below):
#   scenario                          | owner / fence                  | proof
#   debounced control relayout        | runLayoutNow (++seq, then stop)| latest preset wins; positions valid
#   layout-select rapid restart       | runLayoutNow (++seq, then stop)| skippedStale increases; last layout wins
#   stop-triggered SYNC layoutstop     | ++layoutSeq BEFORE stop()      | old run sees mySeq!=layoutSeq → skippedStale; never applied
#   one-shot apply per layout         | lastAppliedSeq dedup           | applied delta <= starts delta (no double-apply)
#   LOD show-all during/after layout  | runLayoutNow (routed)          | all nodes shown, grid, no overlap
#   node-index ensure-visible         | runLayoutNow (routed)          | hidden node added; latest state intact
#   late layoutstop / timed fallback  | mySeq === layoutSeq gate       | skippedStale increments; no stale fit
#   selection/focus preserved         | identity kept on relayout      | selected node unchanged after rapid changes


def _open_graph(page, server_url, init_script: str | None = None):
    """Navigate to /__graph and wait until the graph + diagnostic hook are
    ready and the initial layout has been applied at least once.

    Readiness keys on ``layoutStats.lastAppliedSeq`` — the EXACT sequence of the
    most recently applied (settled, fenced-current) layout — which is one-shot
    per layout, instead of the looser ``applied`` counter."""
    # Returning-visitor state: suppress the first-visit micro-tour so these
    # graph-state tests drive a clean canvas. The tour moves focus into itself
    # and traps Tab on open; test_graph_micro_tour_focus_trap drives it
    # deliberately WITHOUT this flag.
    page.add_init_script(
        "try { window.localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}"
    )
    if init_script:
        page.add_init_script(init_script)
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.locator("#okf-graph canvas").first.wait_for(state="visible")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1",
        timeout=10000,
    )


def _state(page):
    return page.evaluate("() => window.__okfLoomGraph.getState()")


def _applied_seq(page) -> int:
    """The exact sequence of the last applied layout (one-shot per layout)."""
    return page.evaluate("() => window.__okfLoomGraph.layoutStats.lastAppliedSeq")


def _wait_applied_after(page, prev_seq: int, timeout: int = 10000) -> None:
    """Wait until a NEW layout (sequence > prev_seq) has actually settled and
    been applied — a precise readiness signal, not a blind sleep."""
    page.wait_for_function(
        "prev => window.__okfLoomGraph.layoutStats.lastAppliedSeq > prev",
        arg=prev_seq, timeout=timeout,
    )


_SNAP_JS = """() => {
  const cy = window.__okfLoomGraph.cy;
  const nodes = {}, edges = {};
  cy.nodes().forEach(n => { const p = n.position(); nodes[n.id()] = {
    x: Math.round(p.x), y: Math.round(p.y), color: n.data('color'),
    vizSize: Math.round((n.data('vizSize')||0)*10)/10,
    bridge: n.hasClass('okf-bridge'), focus: n.hasClass('okf-focus-root'),
    dim: n.hasClass('dim') }; });
  cy.edges().forEach(e => { edges[e.id()] = {
    weight: Math.round((e.data('weight')||0)*1000)/1000,
    rel: e.hasClass('okf-rel-edge'), relColor: e.data('relColor')||null,
    showLabel: e.hasClass('okf-show-label'), dim: e.hasClass('dim') }; });
  return { nodes, edges, n: cy.nodes().length, e: cy.edges().length,
           dimNodes: cy.nodes('.dim').length, dimEdges: cy.edges('.dim').length,
           bridgeNodes: cy.nodes('.okf-bridge').length,
           relEdges: cy.edges('.okf-rel-edge').length,
           focusNodes: cy.nodes('.okf-focus-root').length,
           zoom: Math.round(cy.zoom()*100)/100 };
}"""


def _snap(page):
    return page.evaluate(_SNAP_JS)


def _pos_moved(a, b) -> int:
    """Total |dx|+|dy| of nodes present in both snapshots."""
    tot = 0
    for nid, na in a["nodes"].items():
        nb = b["nodes"].get(nid)
        if nb:
            tot += abs(na["x"] - nb["x"]) + abs(na["y"] - nb["y"])
    return tot


def _min_gap(page) -> float:
    return page.evaluate(
        """() => { const cy = window.__okfLoomGraph.cy; const ns = cy.nodes();
           let m = 1e9; for (let i=0;i<ns.length;i++) for (let j=i+1;j<ns.length;j++){
             const a=ns[i].position(), b=ns[j].position();
             const d=Math.hypot(a.x-b.x,a.y-b.y)-ns[i].width()/2-ns[j].width()/2;
             if (d<m) m=d; } return ns.length<2 ? 999 : Math.round(m); }"""
    )


def _bbox_diag(page) -> float:
    """Model-space bounding-box diagonal of all nodes — a spread metric that is
    independent of zoom/fit (node.position() is in Cytoscape model coordinates,
    which overlayAwareFit never mutates)."""
    return page.evaluate(
        "() => { const cy = window.__okfLoomGraph.cy; const bb = cy.nodes().boundingBox();"
        " return Math.round(Math.hypot(bb.w, bb.h)); }"
    )


def _range_valuetext(page, aria_label: str):
    """The aria-valuetext (high-end label) currently exposed by a named range."""
    return page.evaluate(
        """(label) => { const i = Array.from(document.querySelectorAll('.okf-signal input[type=range]'))
             .find(x => (x.getAttribute('aria-label')||'') === label);
           return i ? i.getAttribute('aria-valuetext') : null; }""",
        arg=aria_label,
    )


def _group_distances(page):
    """Center-to-center model-space distances between every node pair at the
    current layout, split by whether the pair shares a ``type`` (i.e. is the same
    group under Group by = Type). Returns
    ``{'same': [...], 'diff': [...], 'same_pairs': [[idA, idB], ...]}``.

    Model coordinates (``node.position()``) are zoom/fit independent, so this is a
    durable grouping metric, not a screenshot heuristic."""
    return page.evaluate(
        """() => {
          const cy = window.__okfLoomGraph.cy;
          const ns = cy.nodes().toArray();
          const same = [], diff = [], same_pairs = [];
          for (let i = 0; i < ns.length; i++) {
            for (let j = i + 1; j < ns.length; j++) {
              const a = ns[i].position(), b = ns[j].position();
              const d = Math.round(Math.hypot(a.x - b.x, a.y - b.y));
              const ta = ns[i].data('type'), tb = ns[j].data('type');
              if (ta != null && ta === tb) { same.push(d); same_pairs.push([ns[i].id(), ns[j].id()]); }
              else diff.push(d);
            }
          }
          return { same, diff, same_pairs };
        }"""
    )


def _preset(page, name: str) -> None:
    page.click(f'label[for="okf-lens-{name}"]')


def _set_range(page, aria_label: str, value: int) -> None:
    page.evaluate(
        """([label, val]) => {
          const inp = Array.from(document.querySelectorAll('.okf-signal input[type=range]'))
            .find(i => (i.getAttribute('aria-label')||'') === label);
          inp.value = String(val); inp.dispatchEvent(new Event('input', {bubbles:true})); }""",
        arg=[aria_label, value],
    )


def _set_select(page, aria_label: str, value: str) -> None:
    page.evaluate(
        """([label, val]) => {
          const s = Array.from(document.querySelectorAll('.okf-signal select'))
            .find(x => (x.getAttribute('aria-label')||'') === label);
          s.value = val; s.dispatchEvent(new Event('change', {bubbles:true})); }""",
        arg=[aria_label, value],
    )


def _toggle_check(page, text: str) -> None:
    page.evaluate(
        """(text) => { const l = Array.from(document.querySelectorAll('.okf-signal__row--check'))
             .find(x => x.textContent.trim().indexOf(text) >= 0);
           l.querySelector('input').click(); }""",
        arg=text,
    )


def test_lens_switching_mutates_graph_state(server_url: str, page) -> None:
    """Lenses change real Cytoscape state — computed colours, computed sizes,
    bridge classes, edge labels — not just chrome. (Evidence-based
    lens engine: communities/PageRank/betweenness/recency.)"""
    _open_graph(page, server_url)
    base = _snap(page)
    assert base["n"] == 6 and base["e"] >= 1
    st = _state(page)
    assert st["lens"] == "map" and st["colorMode"] == "community" and st["sizeMode"] == "pagerank"
    # Map: PageRank sizing differentiates nodes.
    sizes = sorted(v["vizSize"] for v in base["nodes"].values())
    assert sizes[-1] > sizes[0], "PageRank sizing produced uniform node sizes"

    # Bridges: betweenness sizing + bridge colour ramp + glow class.
    prev = _applied_seq(page)
    _preset(page, "bridges")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    st = _state(page)
    assert st["lens"] == "bridges" and st["colorMode"] == "bridge" and st["sizeMode"] == "betweenness"
    br = _snap(page)
    assert br["bridgeNodes"] >= 1, "Bridges lens set no okf-bridge node"
    assert any(br["nodes"][k]["color"] != base["nodes"][k]["color"] for k in base["nodes"]), \
        "Bridges colour ramp did not recolour any node"

    # Flow: relationship labels on; typed edges carry colour + labels.
    prev = _applied_seq(page)
    _preset(page, "flow")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    st = _state(page)
    assert st["relationEdges"] and st["showEdgeLabels"]
    fl = _snap(page)
    assert fl["relEdges"] >= 1, "Flow lens set no okf-rel-edge"
    assert any(e["showLabel"] for e in fl["edges"].values()), "no edge label shown in Flow"

    # Recent: recency colouring is applied (demo nodes lack timestamps →
    # the neutral undated colour is fine; just prove the mode switched).
    prev = _applied_seq(page)
    _preset(page, "recent")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    assert _state(page)["colorMode"] == "recency"

    # Back to Map: bridge glow cleared, dash encodings gone.
    prev = _applied_seq(page)
    _preset(page, "map")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(400)
    mp = _snap(page)
    assert mp["bridgeNodes"] == 0
    dash_count = page.evaluate(
        "window.__okfLoomGraph.cy.edges('.okf-reld-1, .okf-reld-2, .okf-reld-3').length"
    )
    assert dash_count == 0, "dash encoding must be an explicit-labels state"
    assert _state(page)["lens"] == "map"


def test_lens_summary_panel_ranks_and_navigates(server_url: str, page) -> None:
    """Every lens renders a ranked summary in the detail pane; clicking a row
    selects that node on the canvas (the 'answers beside the picture'
    pattern)."""
    _open_graph(page, server_url)
    page.wait_for_selector(".okf-lens-summary .okf-lens-summary__verdict", timeout=5000)
    # Map: community digest with at least one theme row.
    rows = page.locator(".okf-lens-summary__row")
    assert rows.count() >= 1, "Map summary rendered no theme rows"
    # Bridges: ranked bridgers; clicking the top row selects a node.
    prev = _applied_seq(page)
    _preset(page, "bridges")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(300)
    page.wait_for_selector(".okf-lens-summary__row", timeout=5000)
    page.locator(".okf-lens-summary__row").first.click()
    page.wait_for_timeout(400)
    assert page.evaluate(
        "() => window.__okfLoomGraph.cy.nodes(':selected').length"
    ) == 1, "clicking a summary row did not select the node"
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True, \
        "detail panel did not open from the summary row"


def test_signal_spacing_and_lens_relayout(server_url: str, page) -> None:
    """Node-spacing changes and lens switches trigger a real relayout
    (positions change), not a no-op."""
    _open_graph(page, server_url)
    _preset(page, "map")
    page.wait_for_timeout(700)

    base = _snap(page)
    prev = _applied_seq(page)
    _set_range(page, "Node spacing", 2)  # Open (mid of the 5-step range)
    _wait_applied_after(page, prev)
    page.wait_for_timeout(600)
    assert _state(page)["spacing"] == 2
    spaced = _snap(page)
    assert _pos_moved(base, spaced) > 0, "Node spacing change did not relayout"

    base2 = _snap(page)
    prev = _applied_seq(page)
    _preset(page, "themes")
    _wait_applied_after(page, prev)
    page.wait_for_timeout(600)
    assert _state(page)["lens"] == "themes"
    assert _pos_moved(base2, _snap(page)) > 0, "Lens switch did not relayout"


def test_signal_expanded_range_spreads_further(server_url: str, page) -> None:
    """The widened Signal range (5 steps: Compact…Vast / Soft…Expanse /
    Low…Magnetic) pushes nodes substantially farther than the previous 3-step
    ceiling. The model-space spread at the NEW maximum is much larger than at the
    OLD reachable top (index 2 ≈ the previous ceiling), overlap stays safe via the
    resolver, and aria-valuetext reflects the high-end labels."""
    _open_graph(page, server_url)
    _preset(page, "map")
    page.wait_for_timeout(700)

    def _push(spacing: int, sep: int, strength: int) -> None:
        # Set each range and wait on a real applied-layout signal (the debounced
        # owner coalesces, so wait per step) — no blind sleeps as the proof.
        for label, val in (("Node spacing", spacing),
                           ("Cluster separation", sep),
                           ("Group strength", strength)):
            prev = _applied_seq(page)
            _set_range(page, label, val)
            _wait_applied_after(page, prev)
            page.wait_for_timeout(350)

    # Old reachable ceiling: the previous range topped out at index 2.
    _push(2, 2, 2)
    old_ceiling = _bbox_diag(page)
    snap_old = _snap(page)
    assert _min_gap(page) > 0, "overlap at the mid range"

    # New maximum (Vast / Expanse / Magnetic): spreads much farther.
    _push(4, 4, 4)
    st = _state(page)
    assert st["spacing"] == 4 and st["separation"] == 4 and st["groupStrength"] == 4, \
        f"controls did not reach the new maximum index: {st}"
    new_max = _bbox_diag(page)

    # The widened ceiling must produce a substantially larger spread than the old
    # top-of-range could — proves the range now has real reach, not a token bump.
    assert new_max > old_ceiling * 1.4, (
        f"expanded range did not spread substantially farther than the old ceiling "
        f"(bbox diagonal {old_ceiling} -> {new_max})"
    )
    assert _pos_moved(snap_old, _snap(page)) > 0, "pushing to the new maximum did not relayout"
    # Overlap invariant holds even at the extreme end (resolver guarantees a gap).
    assert _min_gap(page) > 0, "overlap at maximum spacing/separation/strength"

    # aria-valuetext exposes the accessible high-end label for each control.
    assert _range_valuetext(page, "Node spacing") == "Vast"
    assert _range_valuetext(page, "Cluster separation") == "Expanse"
    assert _range_valuetext(page, "Group strength") == "Magnetic"


def test_lens_communities_deterministic_and_painted(server_url: str, page) -> None:
    """Community detection is deterministic (same bundle → same assignment on
    every load — the anti-position-churn requirement) and the Map lens paints
    nodes by community: same community ⇒ same colour, and the colour comes
    from the community ramp, not the type palette."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    com1 = page.evaluate("() => window.__okfLoomGraph.communityOf()")
    assert com1 and len(com1) == 6, f"expected 6 community assignments, got {com1}"
    snap = _snap(page)
    colours = {}
    for nid, c in com1.items():
        colour = snap["nodes"][nid]["color"]
        if c in colours:
            assert colours[c] == colour, (
                f"nodes in community {c} got different colours: {colours[c]} vs {colour}"
            )
        else:
            colours[c] = colour
    # Distinct communities get distinct colours.
    assert len(set(colours.values())) == len(colours), f"community colours collide: {colours}"

    # Reload → identical assignment (deterministic label propagation).
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1",
        timeout=10000,
    )
    page.wait_for_timeout(300)
    com2 = page.evaluate("() => window.__okfLoomGraph.communityOf()")
    assert com1 == com2, f"community assignment changed across reloads: {com1} vs {com2}"


def test_lens_encoding_toggles(server_url: str, page) -> None:
    """Advanced toggles: 'Colour by theme' off reverts nodes to the type
    palette (the Saket et al. evidence-mandated escape hatch); 'Show
    relationship labels' reveals typed-edge labels in any lens."""
    _open_graph(page, server_url)
    page.wait_for_timeout(400)
    themed = _snap(page)
    _toggle_check(page, "Colour by theme")
    page.wait_for_timeout(300)
    assert _state(page)["colorMode"] == "type"
    typed = _snap(page)
    assert any(typed["nodes"][k]["color"] != themed["nodes"][k]["color"] for k in themed["nodes"]), \
        "disabling theme colours changed nothing"
    # The type palette equals each node's baseColor.
    base_matches = page.evaluate(
        """() => window.__okfLoomGraph.cy.nodes().every(
             n => n.data('color') === n.data('baseColor'))"""
    )
    assert base_matches, "type colouring does not match the node type palette"
    _toggle_check(page, "Colour by theme")
    page.wait_for_timeout(300)
    assert _state(page)["colorMode"] == "community"

    # Relationship labels toggle (unselect first: a selected node shows
    # its own contextual edge labels by design).
    page.evaluate("() => window.__okfLoomGraph.cy.elements().unselect()")
    page.wait_for_timeout(200)
    before = _snap(page)
    assert not any(e["showLabel"] for e in before["edges"].values()), \
        "labels visible before the toggle with nothing selected"
    _toggle_check(page, "Show relationship labels")
    page.wait_for_timeout(300)
    st = _state(page)
    assert st["showEdgeLabels"] and st["relationEdges"]
    after = _snap(page)
    assert any(e["showLabel"] for e in after["edges"].values()), \
        "relationship-label toggle revealed no labels"


def test_signal_focus_depth_and_clear(server_url: str, page) -> None:
    """Selecting a node + enabling focus marks the focus root; Clear focus
    removes the focus class/state while keeping the selection/detail."""
    _open_graph(page, server_url)
    _preset(page, "map")
    page.wait_for_timeout(500)

    # Select a concept via the keyboard-accessible node index.
    page.click(".okf-node-index > summary")
    page.locator(".okf-node-index__item").first.click()
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True, "detail not shown after index click"

    _preset(page, "focus")
    page.wait_for_timeout(400)
    st = _state(page)
    assert st["focusEnabled"] is True
    assert page.evaluate("() => window.__okfLoomGraph.focusRoot()") is not None
    assert _snap(page)["focusNodes"] == 1, "focus root halo class not applied to exactly one node"

    _set_select(page, "Depth", "2")
    page.wait_for_timeout(300)
    assert _state(page)["focusDepth"] == 2

    # Clear focus: class + state cleared, but the selection/detail remains.
    page.click(".okf-signal__btn")  # Clear focus
    page.wait_for_timeout(300)
    assert _state(page)["focusEnabled"] is False
    assert _snap(page)["focusNodes"] == 0, "focus root halo not cleared"
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True, "detail lost after clear focus"


def test_signal_keyboard_and_node_index(server_url: str, page) -> None:
    """Keyboard operation: preset radios via Arrow keys apply a preset; the
    node index selects via Enter; Reset clears; controls show a focus ring."""
    _open_graph(page, server_url)

    # Lens radio group is arrow-navigable AND applies on change.
    page.eval_on_selector("#okf-lens-map", "e => e.focus()")
    assert page.evaluate("() => document.activeElement.id") == "okf-lens-map"
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(600)
    assert page.evaluate("() => document.activeElement.id") == "okf-lens-themes"
    assert page.evaluate("() => document.getElementById('okf-lens-themes').checked") is True
    assert _state(page)["lens"] == "themes"

    # A panel select shows a visible focus ring (focus-visible outline).
    # Selects live inside the Advanced disclosure (collapsed by
    # default) — open it first so the select is focusable.
    page.eval_on_selector(".okf-signal__advanced", "e => { e.open = true; }")
    outline = page.eval_on_selector(
        ".okf-signal select", "e => { e.focus(); return getComputedStyle(e).outlineStyle; }"
    )
    assert outline == "solid", f"control focus outline not visible: {outline!r}"

    # Node index: keyboard activate (Enter) selects the concept + shows detail.
    page.click(".okf-node-index > summary")
    btn = page.locator(".okf-node-index__item").nth(2)
    btn.focus()
    target = btn.get_attribute("data-target")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is True
    assert page.evaluate("() => { const s = window.__okfLoomGraph.cy.nodes(':selected'); return s.length ? s.id() : null; }") == target

    # Reset button clears selection from the keyboard.
    page.eval_on_selector("#okf-reset", "e => e.focus()")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#detail-empty", "e => e.hidden") is False, "Reset did not clear detail"


def test_signal_async_lifecycle_latest_wins(server_url: str, page) -> None:
    """The single layout owner fences stale completions: overlapping layout
    starts increment layoutStats.skippedStale, the LAST layout wins, the graph
    stays overlap-free, the fit is applied ONCE per layout, and the selection is
    preserved. Covers the stop-triggered-stale path (owner token bumped before
    stop) and the late layoutstop / timed-fallback path."""
    _open_graph(page, server_url)
    selected = page.evaluate("() => { const s = window.__okfLoomGraph.cy.nodes(':selected'); return s.length ? s.id() : null; }")
    base = page.evaluate("() => Object.assign({}, window.__okfLoomGraph.layoutStats)")

    # Fire THREE overlapping layout starts by applying lenses back-to-back
    # (each applyLens schedules through the single owner; the debounce
    # coalesces same-tick calls, so space them just past it). Each new
    # start bumps layoutSeq BEFORE stopping the prior layout, so the
    # prior's completion (synchronous stop-triggered layoutstop AND/OR its
    # later timed fallback) sees mySeq != layoutSeq and is fenced →
    # skippedStale rises; the last lens wins.
    for lens in ("themes", "bridges", "map"):
        page.evaluate(f"() => window.__okfLoomGraph.applyLens({lens!r})")
        page.wait_for_timeout(260)
    # Wait past the 900ms one-shot fallback so every superseded completion fired.
    page.wait_for_timeout(1300)
    stats = page.evaluate("() => Object.assign({}, window.__okfLoomGraph.layoutStats)")
    assert stats["skippedStale"] > base["skippedStale"], (
        f"stale layout completions were not fenced "
        f"(skippedStale {base['skippedStale']} -> {stats['skippedStale']})"
    )
    # One-shot: 3 starts apply at most 3 layouts even though each layout has BOTH
    # a layoutstop handler and a timed fallback (no double-apply per sequence).
    started = stats["starts"] - base["starts"]
    applied = stats["applied"] - base["applied"]
    assert started >= 3, f"expected >=3 layout starts, got {started}"
    assert applied <= started, f"fit applied more times ({applied}) than layouts started ({started})"
    assert stats["lastAppliedSeq"] == stats["starts"], "lastAppliedSeq is not the most recent layout"
    assert _state(page)["lens"] == "map", "the latest lens did not win"
    assert _min_gap(page) > 0, "overlap after rapid layout restarts"
    assert _snap(page)["zoom"] <= 1.6 + 1e-6, "zoom exceeded clamp after rapid restarts"

    # Rapid lens spam: the latest lens wins, still overlap-free, selection kept.
    for pr in ["themes", "bridges", "flow", "recent", "focus", "map", "bridges"]:
        _preset(page, pr)
        page.wait_for_timeout(35)
    page.wait_for_timeout(1300)
    assert _state(page)["preset"] == "bridges", "latest lens did not win after spam"
    assert _min_gap(page) > 0, "overlap after rapid preset spam"
    now_sel = page.evaluate("() => { const s = window.__okfLoomGraph.cy.nodes(':selected'); return s.length ? s.id() : null; }")
    assert now_sel == selected, f"selection identity lost across rapid changes ({selected} -> {now_sel})"


def test_signal_lod_lifecycle(server_url: str, page) -> None:
    """With a low LOD threshold, node-index ensure-visible and Show-all both
    route through the stale-safe owner: hidden nodes are added, Show-all reveals
    every node on a grid, and the graph stays overlap-free."""
    _open_graph(page, server_url, init_script="window.OKF_LOOM_GRAPH_LOD_THRESHOLD = 3;")
    assert _snap(page)["n"] == 3, "LOD did not cap the initial canvas to the top-3 nodes"
    assert page.locator(".okf-graph-lod-pill").count() == 1, "LOD pill missing"

    # ensure-visible: pick a node the LOD hid and focus it from the index.
    hidden = page.evaluate(
        """() => { const cy = window.__okfLoomGraph.cy;
           const shown = new Set(cy.nodes().map(n => n.id()));
           const items = Array.from(document.querySelectorAll('#okf-node-index-list .okf-node-index__item'));
           const it = items.find(b => !shown.has(b.getAttribute('data-target')));
           return it ? it.getAttribute('data-target') : null; }"""
    )
    assert hidden, "no hidden node found to ensure-visible"
    starts0 = page.evaluate("() => window.__okfLoomGraph.layoutStats.starts")
    page.click(".okf-node-index > summary")
    page.click(f'.okf-node-index__item[data-target="{hidden}"]')
    page.wait_for_timeout(700)
    assert page.evaluate(f"() => !!window.__okfLoomGraph.cy.getElementById({hidden!r}).length"), \
        "ensure-visible did not add the hidden node"
    assert _snap(page)["n"] >= 4
    assert page.evaluate("() => window.__okfLoomGraph.layoutStats.starts") > starts0, \
        "ensure-visible did not run its layout through the owner"

    # Show all: every node revealed, grid layout, overlap-free.
    page.click(".okf-graph-lod-pill")
    page.wait_for_timeout(900)
    assert _snap(page)["n"] == 6, "Show all did not reveal every node"
    assert _state(page)["layout"] == "grid", "Show all did not switch to grid"
    assert _min_gap(page) > 0, "overlap after Show all"


# ---------------------------------------------------------------------------
# First-visit micro-tour — dialog focus management (a11y hardening)
# ---------------------------------------------------------------------------


def test_graph_micro_tour_focus_trap(server_url: str, page) -> None:
    """The first-visit graph tour is a real dialog: focus moves in on open,
    Tab/Shift+Tab are trapped, Escape closes ONLY the tour (it does not clear
    the graph selection the way a bare graph Escape would), and focus is
    restored to a stable graph control on close.

    A fresh browser context has no ``okfGraphTourDone`` flag, so the tour shows
    on first visit (``_open_graph`` sets that flag to suppress it elsewhere).
    """
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    tour = page.locator(".okf-graph-tour")
    tour.wait_for(state="visible")
    # Named modal dialog.
    expect(tour).to_have_attribute("role", "dialog")
    expect(tour).to_have_attribute("aria-modal", "true")
    expect(tour).to_have_attribute("aria-label", "Graph tour")
    # Focus moved INTO the dialog on open (its primary action).
    page.wait_for_function(
        "() => { const c = document.querySelector('.okf-graph-tour');"
        " return c && c.contains(document.activeElement); }",
        timeout=4000,
    )

    # Give the graph a selection so we can later prove Escape did NOT reach the
    # global graph handler (which would clear it).
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0",
        timeout=10000,
    )
    page.evaluate("() => { window.__okfLoomGraph.cy.nodes().first().select(); }")
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes(':selected').length") == 1

    dismiss = tour.locator(".okf-graph-tour__skip")
    nxt = tour.locator(".okf-graph-tour__next")
    # Tab from the last control (Next) wraps to the first (Dismiss).
    nxt.focus()
    page.keyboard.press("Tab")
    expect(dismiss).to_be_focused()
    # Shift+Tab from the first control wraps back to the last.
    page.keyboard.press("Shift+Tab")
    expect(nxt).to_be_focused()

    # Focus is inside the tour; Escape must close ONLY the tour.
    assert page.evaluate(
        "() => document.querySelector('.okf-graph-tour').contains(document.activeElement)"
    )
    page.keyboard.press("Escape")
    expect(tour).to_have_count(0)
    # Global Escape (clear selection/path) was NOT triggered.
    assert page.evaluate("() => window.__okfLoomGraph.cy.nodes(':selected').length") == 1, (
        "Escape leaked to the graph's global handler and cleared the selection"
    )
    # Focus restored to a stable graph control (the search input).
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.id"
    ) == "okf-search"


# ---------------------------------------------------------------------------
# Live graph refresh — stale-fetch latest-wins fence (async lifecycle)
# ---------------------------------------------------------------------------


def test_graph_refresh_latest_wins(server_url: str, page) -> None:
    """Overlapping/late live-refresh (DATA_URL) responses are latest-wins: a
    stale earlier response cannot mutate the graph after a newer one applied.

    Gates the graph-JSON fetch, drives two ``graph`` live events so two
    refreshes are in flight, resolves the NEWER one first (adds one node) then
    the STALE one (adds a different node), and proves only the newer node landed
    while the fence counted the stale drop. Deterministic — the test releases
    the responses; nothing is timed against the network.
    """
    gate = """
    (() => {
      const realFetch = window.fetch.bind(window);
      window.__graphGate = { on: false, pending: [], real: realFetch };
      window.fetch = function (input, init) {
        const url = (typeof input === 'string') ? input : (input && input.url) || '';
        const el = document.getElementById('okf-graph');
        const dataUrl = el && el.getAttribute('data-graph-url');
        if (window.__graphGate.on && dataUrl && url.indexOf(dataUrl) !== -1) {
          return new Promise((resolve) => { window.__graphGate.pending.push(resolve); });
        }
        return realFetch(input, init);
      };
    })();
    """
    page.add_init_script(gate)
    page.add_init_script(
        "try { window.localStorage.setItem('okfGraphTourDone', '1'); } catch (e) {}"
    )
    page.goto(f"{server_url}/__graph", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.__okfLoomGraph && window.__okfLoomGraph.cy "
        "&& window.__okfLoomGraph.cy.nodes().length > 0 "
        "&& window.__okfLoomGraph.refreshStats",
        timeout=10000,
    )
    # Read the real graph JSON so the two refresh bodies are realistic (base + 1
    # distinguishing node each → a clean single-node delta, no mass removals).
    base = page.evaluate(
        """async () => {
            const el = document.getElementById('okf-graph');
            const dataUrl = el.getAttribute('data-graph-url');
            const r = await window.__graphGate.real(dataUrl, { headers: { Accept: 'application/json' } });
            return r.json();
        }"""
    )
    assert base.get("nodes"), "graph JSON has no nodes to base the refresh bodies on"
    # Turn the gate on and drive two overlapping refreshes via `graph` events.
    page.evaluate("() => { window.__graphGate.on = true; }")
    page.evaluate("() => window.okfLoomLive.emit('graph', {})")
    page.wait_for_function("() => window.__graphGate.pending.length >= 1", timeout=5000)
    page.evaluate("() => window.okfLoomLive.emit('graph', {})")
    page.wait_for_function("() => window.__graphGate.pending.length >= 2", timeout=5000)
    # Resolve the NEWER refresh (pending[1]) first, then the STALE one (pending[0]).
    page.evaluate(
        """(base) => {
            const mk = (extraId) => {
                const b = JSON.parse(JSON.stringify(base));
                b.nodes.push({ data: { id: extraId, label: extraId, type: 'concept' } });
                return b;
            };
            const R = (body) => new Response(
                JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
            window.__graphGate.pending[1](R(mk('okf-fence-new')));
            return new Promise((done) => setTimeout(() => {
                window.__graphGate.pending[0](R(mk('okf-fence-stale')));
                setTimeout(done, 80);
            }, 80));
        }""",
        base,
    )
    res = page.evaluate(
        """() => ({
            hasNew: window.__okfLoomGraph.cy.getElementById('okf-fence-new').length > 0,
            hasStale: window.__okfLoomGraph.cy.getElementById('okf-fence-stale').length > 0,
            skippedStale: window.__okfLoomGraph.refreshStats.skippedStale,
        })"""
    )
    assert res["hasNew"], "the newer refresh response did not apply"
    assert not res["hasStale"], "a STALE refresh response mutated the graph (fence failed)"
    assert res["skippedStale"] >= 1, "the stale refresh was not counted by the fence"


# ---------------------------------------------------------------------------
# Wiki search suggestions — honest list/link semantics (a11y hardening)
# ---------------------------------------------------------------------------


def test_search_suggestions_use_plain_link_semantics(server_url: str, page) -> None:
    """The live search-suggestion dropdown uses honest list/link semantics, not
    an unimplemented composite listbox.

    The suggestions are plain navigation links you Tab through (Escape to
    dismiss), so: the container is ``role=list`` (NOT listbox), items are
    ``role=listitem`` wrapping real ``<a href>`` links (NOT ``role=option``),
    and the input carries no ``aria-autocomplete``/``aria-controls`` combobox
    promise. Escape hides the panel.
    """
    page.goto(server_url + "/", wait_until="domcontentloaded")
    search = page.locator('input[type="search"][name="q"]').first
    search.wait_for(state="visible")
    # No composite-combobox ARIA on the input.
    assert search.get_attribute("aria-autocomplete") is None, "input still claims aria-autocomplete"
    assert search.get_attribute("aria-controls") is None, "input still claims aria-controls"
    search.fill("orders")
    live = page.locator(".okf-search-live")
    live.wait_for(state="visible")
    # Honest list semantics — not a composite listbox.
    expect(live).to_have_attribute("role", "list")
    assert live.get_attribute("role") != "listbox"
    assert live.locator('[role="option"]').count() == 0, "suggestions still use role=option"
    links = live.locator('[role="listitem"] > a[href]')
    assert links.count() >= 1, "no suggestion links rendered as list-item links"
    # Escape dismisses (and clears) the panel.
    search.press("Escape")
    expect(live).to_be_hidden()
