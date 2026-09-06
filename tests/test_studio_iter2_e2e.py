"""Live studio iter 2 — Bundle H1: full-loop Playwright e2e (P3-1 / INTENT-009).

End-to-end proof of the **entire agent loop** as defined in §18 (Definition of
Done) and current spec §12 (the agent's comment loop):

    user comment in the browser
      → ``scripts/okf-loom wait`` returns the comment (cross-process, file bus)
      → ``scripts/okf-loom comment-claim``
      → mutator (``scripts/okf-loom link-add --group-id E2E1``)
    → live page patch + change-list entry + graph patch (all visible in the
      browser; iter-3 ARCH3-001 + INTENT3-003 fixed the cross-process SSE
      tail so the documented CLI loop now delivers live updates. This e2e
      observes normal live delivery without calling internal patch helpers.)
      → ``scripts/okf-loom comment-resolve --activity <id>``
      → ``POST /__undo {group_id}`` reverts the whole pass with one click
      → live body reverts + a new ``undo_restore`` change-list entry appears

Non-negotiables (from the assignment + ``e2e-testing-playwright`` skill):

  * **No** ``waitForTimeout``. Every wait is a web-first assertion
    (``expect(locator).to_be_visible()``) or a ``expect_response`` /
    ``wait_for_function`` tied to a real contract.
  * Locators via ``getByRole`` / ``getByLabel`` / ``getByText`` /
    ``getByTestId`` only — no CSS/XPath locators for **interactions**. The
    one structural exception is asserting on the ``<mark>`` element that
    wraps the commented text (it has no role); we use ``page.evaluate``
    for that single DOM-structural check, mirroring the iter-1 browser
    tests' pattern for layout/CSS-contract assertions.
  * **Independent + parallel-safe.** The fixture owns its OWN bundle copy
    under ``tmp_path_factory`` (module-scoped so the slow ``okf serve``
    boot happens once); the in-tree ``samples/demo_bundle`` is never
    mutated in place. A unique ``--group-id E2E1`` keeps undo scoped to
    this test's pass.
  * CI evidence: trace on first retry, screenshots on failure (via the
    ``page`` fixture's context options).

The test SKIPS one assertion per the assignment's escape hatch: the
"presence chip flips to 'Agent: editing tables/orders'" check after
``comment-claim`` depends on Bundle F's P2-15 (claim auto-flips presence),
which has not landed yet. The skip is documented inline and the test stays
meaningful end-to-end without it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Gate 1: skip the whole module when Playwright is absent (browser-proof
# dependencies are optional; the default suite stays zero-dep beyond PyYAML).
pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from conftest import okf_module_argv, okf_subprocess_env

pytestmark = pytest.mark.browser

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEMO_BUNDLE = TOOLKIT_ROOT / "samples" / "demo_bundle"

_SERVER_STARTUP_TIMEOUT = 30.0
# Where the e2e inserts the selectable lead paragraph that the assignment
# steps select ("An orders table"). The demo bundle's orders.md begins with
# ``# Schema``; we prepend one short paragraph so the selection is
# unambiguous and durable across demo-bundle content drift.
_LEAD_PARA = "An orders table."


# ---------------------------------------------------------------------------
# Module-scoped fixtures: own bundle copy + long-lived server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_server(proc: subprocess.Popen, base: str) -> None:
    """Block until ``okf serve`` answers GET / with 200 (fail on early exit).

    No ``time.sleep`` pollutes the test assertions; this is a readiness gate
    for the test's own subprocess, not an assertion about app behavior.
    """
    deadline = time.monotonic() + _SERVER_STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"okf serve exited early (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(f"{base}/", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.15)
    pytest.fail(f"okf serve not ready within {_SERVER_STARTUP_TIMEOUT:g}s")


@pytest.fixture
def e2e_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An isolated COPY of samples/demo_bundle the e2e can mutate freely.

    The lead paragraph ``_LEAD_PARA`` is prepended to ``tables/orders.md`` so
    the assignment's literal step "select the body text 'An orders table'"
    has a stable anchor. The demo bundle on disk is never touched.
    """
    if not DEMO_BUNDLE.is_dir():
        pytest.skip(f"demo bundle missing: {DEMO_BUNDLE}")
    dst = tmp_path_factory.mktemp("e2e_bundle") / "bundle"
    # CRITICAL: exclude .okf-loom/ so the copy starts with NO leftover session
    # state (events.jsonl, directives.jsonl, token, history). The demo
    # bundle on disk accumulates .okf-loom/session/ from local ``okf serve``
    # runs; including it would pollute the e2e with stale comments, a
    # flooded events feed, and a pre-existing token that doesn't match
    # the new server. Excluding .okf-loom/ gives the e2e a pristine bundle.
    shutil.copytree(
        DEMO_BUNDLE, dst,
        ignore=shutil.ignore_patterns(".okf-loom"),
    )
    orders = dst / "tables" / "orders.md"
    raw = orders.read_text(encoding="utf-8")
    # Insert the lead paragraph immediately after the frontmatter, before
    # the first body line. The frontmatter ends at the second ``---``.
    parts = raw.split("---\n", 2)
    if len(parts) == 3:
        new_raw = "---\n" + parts[1] + "---\n\n" + _LEAD_PARA + "\n\n" + parts[2].lstrip()
    else:
        # Defensive fallback: prepend at the very top.
        new_raw = _LEAD_PARA + "\n\n" + raw
    orders.write_text(new_raw, encoding="utf-8")
    return dst


@pytest.fixture
def server_url(e2e_bundle: Path) -> str:
    """Start ``okf serve <e2e_bundle>`` on a free port; yield the base URL.

    Function-scoped so collaboration tests never share comment or history state. Teardown terminates the subprocess cleanly.
    """
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        okf_module_argv(
            "serve", str(e2e_bundle), "--host", "127.0.0.1", "--port", str(port),
            "--no-open",
        ),
        cwd=str(TOOLKIT_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
def page(request):
    """A Chrome-backed Playwright page.

    Uses the system Chrome channel (``AIC_PLAYWRIGHT_CHROME_PATH`` is already
    configured in the test environment). Captures trace on first retry +
    screenshots on failure for CI evidence (``e2e-testing-playwright`` skill
    CI evidence rule). Skips the individual test if Chrome is unavailable.
    """
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch() if os.environ.get("CI") else p.chromium.launch(channel="chrome")
        except Exception as exc:
            if os.environ.get("CI"):
                pytest.fail(f"required Chromium unavailable: {exc}")
            pytest.skip(f"chrome channel unavailable: {exc}")
        try:
            context = browser.new_context(
                # CI evidence: trace on first retry + screenshot on failure.
                # These are no-ops on a passing test; they only fire when a
                # flake happens, so a future debugger has the trace.
                viewport={"width": 1280, "height": 900},
                reduced_motion="reduce",  # deterministic: no animation timing
            )
            context.tracing.start(
                screenshots=True, snapshots=True, sources=True,
            )
            pg = context.new_page()
            try:
                yield pg
            finally:
                evidence = TOOLKIT_ROOT / 'test-results' / request.node.name
                evidence.mkdir(parents=True, exist_ok=True)
                try:
                    pg.screenshot(path=str(evidence / 'final.png'), full_page=True)
                    context.tracing.stop(path=str(evidence / 'trace.zip'))
                finally:
                    context.close()

        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_reading_context_and_toolbar_reflow(page, server_url):
    page.goto(server_url + '/tables/orders')
    _wait_for_studio(page)
    context = page.locator('.okf-context')
    assert not context.evaluate('(el) => el.open')
    assert page.locator('.okf-page__body').bounding_box()['y'] < 650
    evidence = TOOLKIT_ROOT / 'test-results' / 'reading-layout'
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / 'desktop.png'))
    context.locator('summary').first.click()
    expect(page.locator('.okf-page__sidebar')).to_be_visible()
    context.locator('summary').first.click()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.locator('.okf-studio-tools > summary').click()
    expect(page.get_by_role('button',name='Source',exact=True)).to_be_visible()
    page.get_by_role('button',name='Source',exact=True).click()
    page.get_by_role('button',name='Rendered',exact=True).click()
    for state in ('watching', 'idle'):
        with page.expect_response(lambda r: r.url.endswith('/__presence')) as response:
            page.locator('.okf-watch-toggle').click()
        assert response.value.ok
        assert response.value.json()['presence']['state'] == state
    page.locator('.okf-palettebtn').click()
    expect(page.locator('.okf-palette-overlay')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('.okf-palettebtn')).to_be_focused()
    page.locator('.okf-studio-tools > summary').click()
    for control in page.locator('.okf-studio-bar button').all():
        if control.is_visible():
            bounds = control.bounding_box()
            assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 391
    page.screenshot(path=str(evidence / 'mobile.png'))


def test_meridian_workspace_with_real_loom_api(page, server_url, e2e_bundle, tmp_path):
    """Real React + browser + Loom; explicit Meridian host double, not host E2E."""
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    bundle_js = tmp_path / 'meridian-harness.js'
    subprocess.run(['node','tests/build-meridian-harness.mjs',str(bundle_js)],
                   cwd=str(TOOLKIT_ROOT), check=True, timeout=30)
    def relay(request):
        url = urlsplit(request['url'])
        assert url.scheme == 'https' and url.hostname == 'loom.example.com'
        local = server_url + url.path + ('?' + url.query if url.query else '')
        body = request.get('body')
        req = Request(local, data=body.encode() if body is not None else None,
                      headers=request.get('headers',{}), method=request.get('method','GET'))
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return {'status':response.status,'statusText':'','headers':{},'body':response.read().decode()}
    page.expose_function('loomTestFetch',relay)
    page.route('https://plugin.example.test/**', lambda route: route.fulfill(
        status=200, content_type='text/javascript' if route.request.url.endswith('.js') else 'text/html',
        body=bundle_js.read_text() if route.request.url.endswith('.js') else
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0"><div id="root"></div><script type="module" src="/app.js"></script></body></html>'))
    page.goto('https://plugin.example.test/')
    expect(page.get_by_role('heading',name='Orders',exact=True)).to_be_visible(timeout=15000)
    page.get_by_role('button',name='Connect editing',exact=True).click()
    token = _run_okl(e2e_bundle,'token',str(e2e_bundle)).stdout.strip()
    assert token
    page.get_by_label('Loom session token').fill(token)
    page.get_by_role('button',name='Connect',exact=True).click()
    expect(page.get_by_role('button',name='Editing connected')).to_be_visible()
    page.get_by_role('button',name='Edit source',exact=True).click()
    source = page.get_by_label('Markdown source')
    original = source.input_value()
    source.fill(original + '\nMeridian browser acceptance passage.\n')
    page.get_by_role('button',name='Save source',exact=True).click()
    expect(page.locator('.loom-prose')).to_contain_text('Meridian browser acceptance passage.')
    assert 'Meridian browser acceptance passage.' in (e2e_bundle/'tables/orders.md').read_text()
    page.get_by_role('button',name='Comments',exact=True).click()
    page.get_by_label('Instruction or comment').fill('Check the Meridian integration.')
    page.get_by_role('button',name='Post comment',exact=True).click()
    expect(page.get_by_label('Instruction or comment')).to_have_value('')
    expect(page.get_by_text('Check the Meridian integration.',exact=True)).to_be_visible()
    thread = page.locator('.loom-card').filter(has=page.get_by_text('Check the Meridian integration.',exact=True))
    thread.get_by_label('Resolution summary').fill('Integration checked.')
    thread.get_by_role('button',name='Resolve',exact=True).click()
    expect(thread.get_by_role('button',name='Reopen',exact=True)).to_be_visible()
    page.get_by_role('button',name='Changes',exact=True).click()
    page.get_by_role('button',name='Undo',exact=True).first.click()
    page.get_by_role('button',name='Read',exact=True).click()
    expect(page.locator('.loom-prose')).not_to_contain_text('Meridian browser acceptance passage.')
    evidence = TOOLKIT_ROOT/'test-results'/'meridian-workspace'
    evidence.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(evidence/'desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    expect(page.locator('.loom-catalog')).not_to_be_visible()
    page.get_by_role('button',name='Browse & search',exact=False).click()
    expect(page.get_by_role('searchbox',name='Search concepts')).to_be_visible()
    page.get_by_role('button',name='Browse & search',exact=False).click()
    page.screenshot(path=str(evidence/'mobile.png'))
    page.evaluate('window.loomTestUnmount()')
    assert page.evaluate('window.loomTestDisposed === true')


def _wait_for_studio(pg) -> None:
    """Block until studio.js has booted (window.okfLoomStudio defined).

    Web-first: uses ``wait_for_function``, not a timeout.
    """
    pg.wait_for_function(
        "typeof window.okfLoomStudio === 'object'", timeout=15_000,
    )


def _run_okl(bundle: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run an ``okf`` CLI command against ``bundle`` and return the completed
    process. Raises on timeout so a hung wait never freezes the suite."""
    return subprocess.run(
        okf_module_argv(*args),
        cwd=str(TOOLKIT_ROOT),
        env=okf_subprocess_env(),
        capture_output=True, text=True, timeout=timeout,
    )


def _select_lead_paragraph(pg) -> None:
    """Programmatically select the lead paragraph text in the page body.

    The comment affordance listens to ``selectionchange`` (debounced 120ms),
    so we dispatch that event after building the Range. The selection itself
    uses the DOM Range API because Playwright has no high-level "select this
    text" verb — this is the same pattern the iter-1 browser tests use.
    """
    pg.evaluate(
        """() => {
            const body = document.querySelector('.okf-page__body');
            if (!body) return;
            // The lead paragraph is the first <p> in the body.
            const p = body.querySelector('p');
            if (!p) return;
            let txt = p;
            while (txt && txt.nodeType !== 3) txt = txt.firstChild;
            if (!txt) return;
            const range = document.createRange();
            range.setStart(txt, 0);
            range.setEnd(txt, txt.nodeValue.length);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            // selectionchange is debounced 120ms; nudge it now.
            document.dispatchEvent(new Event('selectionchange'));
        }"""
    )


def _read_event_ids(bundle: Path) -> list[dict]:
    """Read all rows from <bundle>/.okf-loom/session/events.jsonl (best effort)."""
    path = bundle / ".okf-loom" / "session" / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# The full loop (P3-1 / INTENT-009 / §18 Definition of Done)
# ---------------------------------------------------------------------------


def test_full_agent_loop_e2e(server_url: str, page, e2e_bundle: Path) -> None:
    """Drive the full agent loop end-to-end: user comment → agent claim →
    mutator → live patch + change list + graph + undo, all visible in the
    browser with no full refresh."""

    # ------------------------------------------------------------------
    # Step 1-2: navigate to /tables/orders; verify the page loads + boots.
    # ------------------------------------------------------------------
    page.goto(f"{server_url}/tables/orders", wait_until="load")
    _wait_for_studio(page)
    # The lead paragraph we injected must be present (sanity).
    expect(page.get_by_text(_LEAD_PARA, exact=True)).to_be_visible(timeout=5_000)

    # ------------------------------------------------------------------
    # Pre-spawn the ``scripts/okf-loom wait`` subprocess BEFORE posting the comment.
    # ``scripts/okf-loom wait`` is the agent's foreground block-once primitive: it
    # establishes its "new work" baseline at call time and returns only
    # items that arrive AFTER that baseline. So we spawn it before the
    # comment exists; the comment post (step 3) is what unblocks it.
    # This is the actual agent-loop ordering (``scripts/okf-loom wait`` → user posts).
    # The subprocess startup (Python import + baseline computation, ~200ms)
    # always finishes during the subsequent UI interaction steps (select →
    # click → fill → send), so there is no startup race.
    # ------------------------------------------------------------------
    wait_proc = subprocess.Popen(
        okf_module_argv(
            "wait", str(e2e_bundle), "--for", "comment", "--timeout", "10",
        ),
        cwd=str(TOOLKIT_ROOT),
        env=okf_subprocess_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    # ------------------------------------------------------------------
    # Step 3: post a comment via the comment affordance.
    #   select "An orders table" → click affordance → type → submit.
    # ------------------------------------------------------------------
    _select_lead_paragraph(page)
    # The affordance button is added to document.body and un-hidden when a
    # selection inside .okf-page__body exists. Use role+name (no CSS).
    affordance_btn = page.get_by_role(
        "button", name="Comment for agent on the selected text",
    )
    expect(affordance_btn).to_be_visible(timeout=5_000)
    # Set up the response wait BEFORE the click (race-free per the skill).
    with page.expect_response(
        lambda r: r.url.endswith("/__comment"), timeout=10_000,
    ) as resp_info:
        affordance_btn.click()
        # The composer opens focused; fill + submit. Use role=textbox +
        # exact name to disambiguate from the affordance button whose
        # aria-label ("Comment for agent on the selected text") is a
        # substring match for "Comment for agent".
        composer = page.get_by_role("textbox", name="Comment for agent")
        expect(composer).to_be_visible(timeout=5_000)
        comment_body = "Add a depends_on link to Customers"
        composer.fill(comment_body)
        page.get_by_role("button", name="Send", exact=True).click()
    resp = resp_info.value
    assert resp.ok, f"/__comment failed: HTTP {resp.status}"
    comment_json = resp.json()
    assert comment_json.get("ok") is True, f"/__comment body: {comment_json}"
    server_comment_id = (comment_json.get("comment") or {}).get("id")
    assert server_comment_id, f"/__comment returned no comment id: {comment_json}"

    # ------------------------------------------------------------------
    # Step 4: the comment marker (<mark data-comment-id>) wraps the
    # selection. There is no ARIA role for <mark>, so we evaluate the DOM
    # directly (same pattern the iter-1 browser tests use for layout
    # contracts). We assert the mark carries the SERVER-confirmed id (not
    # the optimistic local- id), proving the round-trip.
    # ------------------------------------------------------------------
    page.wait_for_function(
        """(cid) => {
            const m = document.querySelector(
              '.okf-page__body mark.okf-comment-mark[data-comment-id="' + CSS.escape(cid) + '"]'
            );
            return !!m;
        }""",
        arg=server_comment_id,
        timeout=8_000,
    )

    # ------------------------------------------------------------------
    # Step 5: the new comment event is visible in the studio UI. The
    # change list panel explicitly excludes comment events (studio.js
    # renderChangeList filters them); comments surface in the COMMENTS
    # panel, which auto-opened on affordance click. Assert the comment
    # card with our body text is visible there.
    # ------------------------------------------------------------------
    expect(page.get_by_text(comment_body, exact=True)).to_be_visible(timeout=5_000)

    # ------------------------------------------------------------------
    # Step 6: the pre-spawned ``scripts/okf-loom wait`` subprocess should now have
    # detected the just-posted comment (cross-process, via the file bus)
    # and exited 0 with the comment JSON. ``communicate`` with a timeout
    # so a hung wait never freezes the suite.
    # ------------------------------------------------------------------
    try:
        wait_stdout, wait_stderr = wait_proc.communicate(timeout=15.0)
    except subprocess.TimeoutExpired:
        wait_proc.kill()
        wait_stdout, wait_stderr = wait_proc.communicate()
        pytest.fail(
            f"okf wait did not exit within 15s; stdout={wait_stdout!r} "
            f"stderr={wait_stderr!r}"
        )
    assert wait_proc.returncode == 0, (
        f"okf wait exit={wait_proc.returncode}; "
        f"stdout={wait_stdout!r} stderr={wait_stderr!r}"
    )
    wait_item = json.loads(wait_stdout)
    assert wait_item.get("id") == server_comment_id, (
        f"okf wait returned id={wait_item.get('id')!r}, "
        f"expected {server_comment_id!r}"
    )
    assert wait_item.get("state") == "open"

    # ------------------------------------------------------------------
    # Step 7: claim the comment via the CLI. Exits 0.
    # ------------------------------------------------------------------
    claim_proc = _run_okl(
        e2e_bundle, "comment-claim", str(e2e_bundle), server_comment_id,
    )
    assert claim_proc.returncode == 0, (
        f"comment-claim exit={claim_proc.returncode}; "
        f"stdout={claim_proc.stdout!r} stderr={claim_proc.stderr!r}"
    )

    # ------------------------------------------------------------------
    # Step 8: a session-only CLI claim reaches the live UI without a
    # document edit, manual fetch, or forced patch.
    expect(page.locator('.okf-presence')).to_have_attribute('data-state', 'editing', timeout=5_000)
    expect(page.locator('.okf-presence')).to_contain_text('tables/orders')
    # ------------------------------------------------------------------
    # Durable-state: the comment's latest record in directives.jsonl is
    # state=claimed, and a comment event with state=claimed is in
    # events.jsonl.
    claim_seen = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        for ev in _read_event_ids(e2e_bundle):
            if (
                ev.get("type") == "comment"
                and ev.get("id") == server_comment_id
                and ev.get("state") == "claimed"
            ):
                claim_seen = True
                break
        if claim_seen:
            break
        time.sleep(0.2)
    assert claim_seen, (
        f"no 'claimed' comment event for {server_comment_id} in events.jsonl"
    )

    # ------------------------------------------------------------------
    # Step 9: add a NEW link via the mutator, grouped for one-click undo.
    # We link orders → playbooks/refund_flow (a concept orders does NOT
    # already link to in the demo bundle, so the link really lands and the
    # body patches). --group-id E2E1 ties this into one undo group.
    # ------------------------------------------------------------------
    link_target = "playbooks/refund_flow"
    link_label = "Refund flow"
    link_proc = _run_okl(
        e2e_bundle, "link-add",
        "--bundle", str(e2e_bundle),
        "--source", "tables/orders",
        "--target", link_target,
        "--label", link_label,
        "--group-id", "E2E1",
    )
    assert link_proc.returncode == 0, (
        f"link-add exit={link_proc.returncode}; "
        f"stdout={link_proc.stdout!r} stderr={link_proc.stderr!r}"
    )

    # ------------------------------------------------------------------
    # Step 10: browser assertions — body patches, change list gains an
    # attributed activity entry, graph patches.
    #
    # The UI must update through normal live delivery (SSE or its real polling
    # fallback). Do not invoke internal patch functions or refresh the document.
    # (a) Concept body patches: the new "Refund flow" link appears in the
    #     body (block-level patch via applyDoc, no full refresh).
    expect(page.get_by_role("link", name=link_label, exact=True)).to_be_visible(
        timeout=10_000,
    )

    # (b) The change list (activity panel) gains an attributed entry. The
    #     SSE `activity` event for the link-add carries actor=agent,
    #     action=add_link, ids=["tables/orders"], origin=mutator,
    #     group_id="E2E1". Verify durable state via events.jsonl on disk
    #     AND that the change-list panel surfaces it. First, capture the
    #     activity id from events.jsonl (used again in step 12).
    activity_id: str | None = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        for ev in _read_event_ids(e2e_bundle):
            if (
                ev.get("type") == "activity"
                and ev.get("action") == "add_link"
                and "tables/orders" in (ev.get("ids") or [])
                and ev.get("group_id") == "E2E1"
            ):
                activity_id = ev.get("id")
                break
        if activity_id:
            break
        time.sleep(0.2)
    assert activity_id, (
        "no attributed add_link activity event with group_id=E2E1 in events.jsonl"
    )

    # (c) Inspect the visible activity panel as well as its durable record.
    page.get_by_role('button', name='Show changes', exact=True).click()
    expect(page.get_by_role('button', name='Undo group', exact=True)).to_be_visible(timeout=5_000)
    page.get_by_role('button', name='Show comments', exact=True).click()
    filtered = page.evaluate(
        """async () => {
            const r = await fetch('/__data/events?concept=tables/orders&limit=50');
            const d = await r.json();
            return d.events || [];
        }"""
    )
    matched = [
        e for e in filtered
        if e.get("type") == "activity"
        and e.get("action") == "add_link"
        and e.get("origin") == "mutator"
        and e.get("group_id") == "E2E1"
        and "tables/orders" in (e.get("ids") or [])
    ]
    assert matched, (
        f"no attributed add_link activity in concept-filtered events; "
        f"got {len(filtered)} events for tables/orders"
    )

    # (d) Graph patches: the link-add emitted a `graph` SSE event (add_link
    #     is in _GRAPH_ACTIONS). Verify durable state — the orders →
    #     refund_flow edge now exists in the bundle graph JSON. This is the
    #     same data the graph view would re-fetch on the graph event.
    #     build_graph_data nests edges under {data: {source, target, ...}},
    #     so unwrap before matching.
    graph_resp = page.evaluate(
        """async () => {
            const r = await fetch('/__data/graph.json');
            return r.ok ? await r.json() : null;
        }"""
    )
    assert graph_resp is not None, "graph.json fetch failed"
    edges = graph_resp.get("edges", [])
    has_new_edge = False
    for e in edges:
        d = e.get("data") or e  # unwrap cytoscape-style {data: {...}}
        if d.get("source") == "tables/orders" and (
            d.get("target") == link_target
            or str(d.get("target_raw", "")).endswith("refund_flow.md")
        ):
            has_new_edge = True
            break
    assert has_new_edge, (
        f"orders → {link_target} edge missing from graph after link-add; "
        f"edges sample: {edges[:3]}"
    )

    # ------------------------------------------------------------------
    # Step 11: activity id captured above (events.jsonl is the durable
    # source; DOM scraping would be more brittle).
    # ------------------------------------------------------------------
    assert activity_id

    # ------------------------------------------------------------------
    # Step 12: resolve the comment with reply + activity link. NOTE: the
    # reply text uses a regular hyphen, not an em dash, per the impeccable
    # copy ban (CRI-009) — em dashes are prohibited in user-visible copy.
    # ------------------------------------------------------------------
    resolve_proc = _run_okl(
        e2e_bundle, "comment-resolve", str(e2e_bundle), server_comment_id,
        "--activity", activity_id,
        "--reply", "Done - added the link",
    )
    assert resolve_proc.returncode == 0, (
        f"comment-resolve exit={resolve_proc.returncode}; "
        f"stdout={resolve_proc.stdout!r} stderr={resolve_proc.stderr!r}"
    )

    # ------------------------------------------------------------------
    # Step 13: resolution appears in the open comments panel and durable feed.
    expect(page.locator('.okf-comment__reply')).to_contain_text('Done - added the link', timeout=5_000)
    # ------------------------------------------------------------------
    resolve_seen = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        for ev in _read_event_ids(e2e_bundle):
            if (
                ev.get("type") == "comment"
                and ev.get("id") == server_comment_id
                and ev.get("state") == "resolved"
            ):
                resolve_seen = True
                # The reply text must be present in the resolved record.
                assert "Done - added the link" in (ev.get("reply") or ""), (
                    f"reply text missing; reply={ev.get('reply')!r}"
                )
                # The activity link must be present.
                assert activity_id in (ev.get("resolved_activity") or []), (
                    f"activity {activity_id} not in resolved_activity="
                    f"{ev.get('resolved_activity')!r}"
                )
                break
        if resolve_seen:
            break
        time.sleep(0.2)
    assert resolve_seen, (
        f"no 'resolved' comment event for {server_comment_id} in events.jsonl"
    )

    # ------------------------------------------------------------------
    # Step 14: POST /__undo {group_id:"E2E1"} with the CSRF token from
    # `okf token`. The token is read by the same CLI the agent uses, not
    # extracted from the page, so this exercises the documented contract.
    # ------------------------------------------------------------------
    token_proc = _run_okl(e2e_bundle, "token", str(e2e_bundle))
    assert token_proc.returncode == 0, (
        f"okf token exit={token_proc.returncode}; stderr={token_proc.stderr!r}"
    )
    csrf_token = token_proc.stdout.strip()
    assert csrf_token, "okf token returned empty"

    page.get_by_role('button', name='Show changes', exact=True).click()
    with page.expect_response(lambda r: r.url.endswith('/__undo'), timeout=10_000) as undo_response:
        page.get_by_role('button', name='Undo group', exact=True).click()
    response = undo_response.value
    undo_result = {"status": response.status, "body": response.json()}
    undo_status = undo_result["status"]
    undo_body = undo_result["body"]
    assert undo_status == 200, (
        f"/__undo returned HTTP {undo_status}; body={undo_body!r}"
    )

    # ------------------------------------------------------------------
    # Step 15: browser — concept body reverts (block-level patch back),
    # change list gains an undo_restore activity entry.
    # ------------------------------------------------------------------
    # Observe normal live delivery for undo as well.
    # (a) The "Refund flow" link is gone from the body.
    #     Use web-first: the link element should become detached/hidden
    #     via the applyDoc block-level patch. Wait for it to leave the
    #     body. Playwright's auto-retry handles the SSE timing.
    page.wait_for_function(
        """() => {
            const body = document.querySelector('.okf-page__body');
            if (!body) return false;
            // The link is gone if no anchor in the body points at a path
            // ending with refund_flow.md.
            return !Array.from(body.querySelectorAll('a')).some((a) => {
                const href = a.getAttribute('href') || '';
                return href.indexOf('refund_flow') >= 0;
            });
        }""",
        timeout=10_000,
    )

    # (b) The change list gains an undo_restore activity entry. Verified
    #     via the concept-filtered endpoint (same read_events oldest-N
    #     caveat as step 10c). The restore_snapshot routes through
    #     save_concept with action="undo_restore", so the event carries
    #     origin="undo".
    undo_activity_id: str | None = None
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        for ev in _read_event_ids(e2e_bundle):
            if (
                ev.get("type") == "activity"
                and ev.get("action") == "undo_restore"
                and "tables/orders" in (ev.get("ids") or [])
            ):
                undo_activity_id = ev.get("id")
                break
        if undo_activity_id:
            break
        time.sleep(0.2)
    assert undo_activity_id, (
        "no undo_restore activity event after POST /__undo {group_id:E2E1}"
    )
    # Confirm the server serves it via the concept-filtered endpoint.
    undo_filtered = page.evaluate(
        """async () => {
            const r = await fetch('/__data/events?concept=tables/orders&limit=50');
            const d = await r.json();
            return d.events || [];
        }"""
    )
    undo_matched = [
        e for e in undo_filtered
        if e.get("type") == "activity"
        and e.get("action") == "undo_restore"
        and "tables/orders" in (e.get("ids") or [])
    ]
    assert undo_matched, (
        f"no undo_restore activity in concept-filtered events; "
        f"got {len(undo_filtered)} events for tables/orders"
    )

    # ------------------------------------------------------------------
    # Step 16: teardown is the server_url fixture's responsibility.
    # ------------------------------------------------------------------
