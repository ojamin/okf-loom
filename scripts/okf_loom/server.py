"""Live HTTP wiki server for OKF bundles.

Public API:
    run_server(bundle_root, *, host, port, watch, open_browser) -> None

The request handler ``OKFWikiHandler`` is exposed for harness embedding:
mount it on any ``http.server`` server subclass and attach a ``bundle``
attribute (plus optional ``config`` / ``state``) before serving.

Stdlib-only (``http.server``, ``socketserver``, ``threading``, ``urllib``).
No Flask/FastAPI/Jinja2/Markdown/watchdog.

Routes:
    /                       root index (top-level concepts + subdirs)
    /<concept_id>           concept page (rendered markdown + panels)
    /<dir>/index.md         directory index page
    /index.md               root index page (alias for /)
    /__graph                full-page Cytoscape.js graph view
    /__search?q=...         search results page (HTML or JSON if ?format=json)
    /__raw/<concept_id>     raw markdown body
    /__data/graph.json      graph JSON (for external tools / harness embed)
    /__data/content.json    content index JSON
    /__static/<file>        static asset (CSS / JS)
    /<path>.<media ext>     bundle-local media file (png/jpg/webp/gif/svg/
                            mp4/webm/pdf …) — §5-visible + contained only
"""
from __future__ import annotations

import json
import os
import posixpath
import queue
import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

from .model import Bundle, Concept
from .paths import ConceptId, ConceptIdError, concept_id_from_str, concept_id_to_str
from .studio import Studio, rev_of
from .lifecycle import StudioServer
from .render import (
    build_graph_data,
    _render_concept_page,
    _render_index_page,
    _render_search_page,
    _render_graph_page,
    _content_index_json,
)
from .viewer.assets import (
    BUNDLE_MEDIA_EXTENSIONS,
    list_builtin_static,
    load_config,
    load_static,
    resolve_palette,
    effective_allow_active_code,
    clear_overrides_cache,
    operator_scope,
    isolated_server_factory,
)
from .viewer.markdown import markdown_to_html, rewrite_internal_links, url_for_concept

# ---------------------------------------------------------------------------
# DoS size caps (P2-55). The authoritative YAML/body size cap lives in
# ``parse.py`` (NOT this file — REPORTED: ``safe_load`` alias expansion /
# billion-laughs must be capped at the parser boundary). As defence in
# depth, the server caps the size of bodies it ships over HTTP so a single
# huge concept body cannot saturate a request handler. The cap is generous
# (16 MiB) — it is a backstop, not the primary control.
# ---------------------------------------------------------------------------
MAX_RAW_RESPONSE_BYTES: int = 16 * 1024 * 1024  # 16 MiB cap on /__raw bodies.

# Defense-in-depth cap on the length of a ``/__search?q=`` query string
# (iter1 P3-5). The lexical/semantic-lite backends tokenize + score the query
# in-process; an unbounded multi-MB query could pin a ThreadingHTTPServer
# worker. 4096 chars is far beyond any realistic knowledge-base query (and
# beyond what the URL-length-aware browsers accept in a querystring anyway).
MAX_SEARCH_QUERY_CHARS: int = 4096

# Studio caps (current spec §13). max_sse_clients bounds the
# worker threads tied up by long-lived SSE streams under ThreadingHTTPServer;
# MAX_EVENT_BODY_BYTES caps a POST /__comment / /__apply / /__undo body so a
# huge payload cannot pin a handler thread (defence in depth — the real size
# guard for rendered markdown lives in parse.py / markdown.py).
DEFAULT_MAX_SSE_CLIENTS: int = 32
MAX_EVENT_BODY_BYTES: int = 1 * 1024 * 1024  # 1 MiB cap on studio POST bodies.

# CSP for bundle-served media (extensions in
# ``viewer.assets.BUNDLE_MEDIA_EXTENSIONS`` — the shared serve/build
# allowlist). Bundle files are USER CONTENT: a bundle SVG
# can carry <script>, and navigating to it directly would otherwise execute
# in the studio origin — where the per-session token is embedded in every
# HTML page. ``sandbox`` gives direct navigation a unique opaque origin with
# scripting disabled; <img>/<video> embedding is unaffected by the response
# CSP. img/media/style allowances keep the browsers' built-in direct-view
# pages (which wrap the file in a tiny document) rendering.
_BUNDLE_ASSET_CSP: str = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "style-src 'unsafe-inline'; "
    "sandbox"
)

# ---------------------------------------------------------------------------
# Plugin hook (entry-point loader in current spec §15; see okf_loom.viewer.plugins)
# ---------------------------------------------------------------------------

from typing import Protocol, runtime_checkable


@runtime_checkable
class ViewerPlugin(Protocol):
    """Hook points for viewer plugins.

    The current loader discovers concrete plugins via Python entry points in the group
    ``"okf_loom.viewer_plugins"`` (see
    :mod:`okf_loom.viewer.plugins`). The hook contract below is the
    minimum surface; concrete plugins MUST implement ``on_concept_render``
    and MAY implement ``on_index_render``. The composite returned by
    ``build_viewer_plugin`` honours the §11 active-code gate and skips
    plugins that raise (log to stderr, never crash the viewer).

    Embedders can still attach a custom plugin instance via
    ``server.plugin`` after construction; that bypasses entry-point
    discovery.
    """

    def on_concept_render(self, concept: Concept, html: str) -> str:
        """Called after a concept page body is rendered. Return possibly
        modified HTML (e.g. to inject a widget, badges, or telemetry)."""
        ...

    def on_index_render(self, html: str) -> str:
        """Called after an index page is rendered."""
        ...


class NoOpPlugin:
    """Default plugin: passes values through unchanged."""

    def on_concept_render(self, concept: Concept, html: str) -> str:
        return html

    def on_index_render(self, html: str) -> str:
        return html


# ---------------------------------------------------------------------------
# Bundle watcher
# ---------------------------------------------------------------------------

from .watcher import BundleWatcher as _BundleWatcher  # compatibility alias

# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class OKFWikiHandler(BaseHTTPRequestHandler):
    """HTTP request handler. Reads ``self.server.bundle`` for the live bundle.

    A harness may also set ``self.server.config``, ``self.server.palette``,
    ``self.server.name``, and ``self.server.plugin``. If unset, they are
    lazily computed on first request.

    Reload safety (P2-46): ``run_server`` attaches a ``server._state_lock``
    (a :class:`threading.Lock`). The watcher's reload mutates ``bundle``,
    ``_palette``, and ``plugin`` under that lock, and handlers snapshot all
    three once per request via :meth:`_state_snapshot` (under the same lock)
    so a concurrent reload cannot hand the handler a torn view (e.g. the new
    bundle with the old palette). Harness-embedded servers without the lock
    fall back to direct-attribute reads (single-threaded embedding is safe).
    """

    server_version = "OKFWiki/0.1"
    # HTTP/1.1 enables keep-alive (Connection: keep-alive) and is required for
    # the /__events SSE stream to hold a connection open without a
    # Content-Length. Every non-streaming response sends Content-Length, so
    # keep-alive is safe for them; SSE streams omit it and hold the thread.
    protocol_version = "HTTP/1.1"

    # --- Convenience accessors --------------------------------------------
    @property
    def studio(self) -> Studio | None:
        return getattr(self.server, "studio", None)

    @property
    def csrf_token(self) -> str | None:
        return getattr(self.server, "csrf_token", None)

    @property
    def bundle(self) -> Bundle:
        return self.server.bundle  # type: ignore[attr-defined]

    @property
    def display_name(self) -> str:
        name = getattr(self.server, "name", None)
        if name:
            return name
        cfg = self._config()
        return cfg.get("name") or self.bundle.name

    @property
    def palette(self) -> dict[str, str]:
        if not hasattr(self.server, "_palette"):
            self.server._palette = resolve_palette(self.bundle)  # type: ignore[attr-defined]
        return self.server._palette  # type: ignore[attr-defined]

    def _config(self) -> dict[str, Any]:
        return getattr(self.server, "config", None) or load_config(self.bundle)

    def _plugin(self):
        return getattr(self.server, "plugin", None) or NoOpPlugin()

    def _state_lock(self):
        """Return the server's reload-state lock, or None if not attached.

        Harness-embedded servers (tests, custom embeddings) may not attach
        ``_state_lock``; in that case we fall back to unlocked access. The
        lock is only required when a watcher thread can mutate the bundle
        concurrently with request handling, which is the ``run_server``
        contract.
        """
        return getattr(self.server, "_state_lock", None)

    def _state_snapshot(self) -> tuple[Bundle, dict[str, str], Any, dict[str, Any]]:
        """Atomically snapshot (bundle, palette, plugin, config) under the lock.

        Used by handlers that read more than one of these attributes, so a
        concurrent watcher reload cannot produce a torn view (P2-46).
        Palette is lazily resolved inside the lock (so the cached palette
        always matches the snapshot's bundle).
        """
        lock = self._state_lock()
        if lock is None:
            bundle = self.bundle
            palette = (
                getattr(self.server, "_palette", None)
                if hasattr(self.server, "_palette")
                else resolve_palette(bundle)
            )
            return bundle, palette, self._plugin(), self._config()
        with lock:
            bundle = self.server.bundle  # type: ignore[attr-defined]
            if not hasattr(self.server, "_palette"):
                self.server._palette = resolve_palette(bundle)  # type: ignore[attr-defined]
            palette = self.server._palette  # type: ignore[attr-defined]
            plugin = self.server.plugin  # type: ignore[attr-defined]
            config = getattr(self.server, "config", None) or load_config(bundle)
            return bundle, palette, plugin, config

    # --- Routing ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server convention)
        try:
            with operator_scope(getattr(self.server, "operator_consent", None)):
                self._route()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            # Log the full exception server-side (operator can debug); return
            # a generic message to the client to avoid information disclosure
            # (filesystem paths, library internals, partial state).
            import traceback
            traceback.print_exc()
            self._send_text(
                500,
                "Internal Server Error\n\nThe server encountered an unexpected "
                "error. Details have been logged to the server's stderr.",
                content_type="text/plain; charset=utf-8",
            )

    def do_HEAD(self) -> None:  # noqa: N802
        """Handle HEAD requests by routing like GET but discarding the body."""
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    # --- Live studio: mutating endpoints (§6, §9, §12, §15) -----------------
    def do_POST(self) -> None:  # noqa: N802
        try:
            with operator_scope(getattr(self.server, "operator_consent", None)):
                self._route_post()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            import traceback
            traceback.print_exc()
            self._send_text(
                500, "Internal Server Error", content_type="text/plain; charset=utf-8"
            )

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        # Cap body size (defence in depth) BEFORE reading, so a huge body is
        # rejected without buffering it. The body is read here (not later) so
        # that EVERY early-return path below has consumed Content-Length bytes
        # — otherwise an unconsumed POST body desyncs the next keep-alive
        # request on the same connection (the leftover bytes get parsed as the
        # next request's method line). This matters concretely for the studio:
        # a 403 on a stale token (e.g. after a server restart) must still drain
        # the body, or every subsequent GET/module fetch on that connection 501s.
        # F4: a malformed (non-integer) Content-Length must NOT raise a 500
        # here. Default to 0 so the body is treated as empty (→ 400/403 below)
        # and the connection stays in sync (nothing to drain).
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (ValueError, TypeError):
            length = 0
        if length > MAX_EVENT_BODY_BYTES:
            # The oversized body can be arbitrarily large — draining it would
            # pin this worker thread, which is worse than a clean reconnect.
            # Instead CLOSE the connection: set close_connection + send
            # ``Connection: close`` so the client reconnects cleanly, and we
            # never parse leftover body bytes as the next request (the
            # keep-alive desync the body-drain invariant above prevents).
            return self._send_text(
                413, "Request entity too large.",
                content_type="text/plain; charset=utf-8", close=True,
            )
        raw_body = self.rfile.read(length) if length > 0 else b""
        # Current spec §14 invisible cross-origin guard: every mutating endpoint requires
        # a valid per-session token AND (when an Origin is present) a host in
        # allowed_hosts. The studio's own JS carries the token automatically
        # (embedded in the page); a foreign page / DNS-rebinding attack lacks
        # it. A non-browser client (curl, okf CLI, tests) with no Origin and a
        # valid token is allowed.
        if not self._check_write_auth():
            return self._send_text(
                403, "Forbidden: invalid or missing cross-origin token.",
                content_type="text/plain; charset=utf-8",
            )
        studio = self.studio
        if studio is None:
            return self._send_text(
                503, "Studio not enabled on this server.",
                content_type="text/plain; charset=utf-8",
            )
        # --no-edit kiosk (current spec §9/§14): live reads stay on, but no
        # mutating writes. ``/__tunnel`` is exempt: it is server admin (start
        # or stop the share tunnel), not a bundle mutation — sharing a
        # read-only kiosk is precisely the kiosk use case. It still sits
        # behind the token + Origin/Host guard above.
        if not getattr(self.server, "studio_edit", True) and path != "/__tunnel":
            return self._send_text(
                403, "Read-only kiosk: commenting/directing is disabled (--no-edit).",
                content_type="text/plain; charset=utf-8",
            )
        try:
            data: dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send_text(
                400, "Bad request: invalid JSON body.",
                content_type="text/plain; charset=utf-8",
            )

        if not isinstance(data, dict):
            return self._send_json(400, {"ok": False, "error": "JSON body must be an object"})
        routes = {
            "/__tunnel": self._handle_tunnel,
            "/__comment": self._handle_comment,
            "/__comment-update": self._handle_comment_update,
            "/__presence": self._handle_presence,
            "/__claim": self._handle_claim,
            "/__resolve": self._handle_resolve,
            "/__apply": self._handle_apply,
            "/__undo": self._handle_undo,
            "/__preview": self._handle_preview,
        }
        handler = routes.get(path)
        if handler is None:
            return self._send_text(404, "Not found", content_type="text/plain; charset=utf-8")
        # One boundary covers read/check/modify/write, idempotency and group undo.
        # Tunnel lifecycle is independent of document writes and can be slow.
        if path == "/__tunnel":
            return handler(data)
        with studio.bus.batch(), studio.transaction():
            return handler(data)

    def _check_write_auth(self) -> bool:
        """Current spec §14 cross-origin guard: valid token + Origin/Host ∈ allowed_hosts.

        The token is embedded in every served HTML page (``_inject_studio``);
        the studio's own JS attaches it as ``X-OKF-Token`` on writes. A foreign
        page cannot read it (same-origin policy), and DNS-rebinding cannot
        forge it. ``--no-edit`` disables the studio entirely (no mutating
        endpoints). When ``studio`` is not attached (plain server), all
        writes are refused here.

        P1-8 (SEC-004): the spec names BOTH Origin and Host. We check
        Origin when present, AND fall back to / additionally verify Host
        against ``allowed_hosts`` so a DNS-rebinding attack that strips
        Origin is still contained. ``allowed_hosts`` defaults to loopback
        (P1-16) and is never empty.
        """
        token = self.csrf_token
        if token is None:
            return False
        sent = self.headers.get("X-OKF-Token", "")
        if not secrets.compare_digest(sent, token):
            return False
        # P1-16: never fail open on an unset allowed_hosts. Default to
        # loopback if the server somehow shipped without it set.
        allowed = getattr(self.server, "allowed_hosts", None)
        if not allowed:
            allowed = ("127.0.0.1", "localhost")
        origin = self.headers.get("Origin")
        if origin:
            # Origin is like "http://127.0.0.1:8787"; compare the host part.
            try:
                host = urlparse(origin).hostname or ""
            except ValueError:
                return False
            if host not in allowed:
                return False
        # P1-8: also check Host (defense against DNS-rebinding that strips
        # Origin). Host is like "127.0.0.1:8787"; compare the host part.
        raw_host = self.headers.get("Host", "")
        if raw_host:
            host_only = raw_host.rsplit(":", 1)[0]
            # A missing/empty Host (rare; some clients omit it) is treated
            # as "trust Origin" — we already required a valid token above.
            if host_only and host_only not in allowed:
                return False
        return True

    def _handle_comment(self, data: dict[str, Any]) -> None:
        concept = str(data.get("concept", "")).strip()
        body = str(data.get("body", "")).strip()
        if not concept or not body:
            return self._send_json(400, {"ok": False, "error": "concept and body required"})
        # P1-11 (QUA idempotency-retry): an ``Idempotency-Key`` header
        # deduplicates a re-POST within a 10-minute window. The body
        # fingerprint is part of the cache key (same key + same body =
        # replay; same key + different body = fresh call). Returns the
        # ORIGINAL response so a client retry after a network glitch does
        # not produce a duplicate comment directive.
        idem_key = self.headers.get("Idempotency-Key")
        if idem_key:
            cached = self.studio.idempotency_lookup(idem_key, body=data)
            if cached is not None:
                # Re-emit the cached response (same status code + payload).
                return self._send_json(cached.get("status", 201), cached.get("body", {}))
        anchor = data.get("anchor") if isinstance(data.get("anchor"), dict) else {}
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        actor = str(data.get("actor", "user")) or "user"
        # QUA2-002 + QUA2-011 fix: hold the studio's in-process lock for the
        # critical section (lookup → post_comment → store) so two concurrent
        # same-key POSTs don't both miss the cache and both append. The
        # irreversible post_comment happens exactly once per key. The store
        # is best-effort (a disk failure here surfaces as 500 BEFORE
        # post_comment, so the client retries against the same key with no
        # duplicate directive). Without this lock, two concurrent same-key
        # POSTs race past the lookup and double-append.
        with self.studio.transaction():
            # Re-check under the lock in case a concurrent same-key POST
            # already populated the cache between the unlocked lookup above
            # and now.
            if idem_key:
                cached = self.studio.idempotency_lookup(idem_key, body=data)
                if cached is not None:
                    return self._send_json(cached.get("status", 201), cached.get("body", {}))
            directive = self.studio.post_comment(
                concept=concept, body=body, anchor=anchor, actor=actor, detail=detail,
                parent_id=data.get("parent_id"),
            )
            response_body = {"ok": True, "comment": directive}
            if idem_key:
                # Best-effort store: if it fails (disk full), we DO NOT
                # surface a 500 because post_comment already happened —
                # instead log + continue, accepting that a retry of the
                # same key may double-emit. This is the lesser evil.
                try:
                    self.studio.idempotency_store(
                        idem_key, body=data,
                        response={"status": 201, "body": response_body},
                    )
                except OSError:
                    import sys as _sys
                    print(
                        "[okf-idempotency] WARNING: cache store failed for "
                        f"key {idem_key!r}; client retry may emit a duplicate.",
                        file=_sys.stderr,
                    )
        self._send_json(201, response_body)

    def _handle_comment_update(self, data: dict[str, Any]) -> None:
        """Update a comment's state or archive flag — user-facing (§9.1).

        Archive is a SEPARATE track from lifecycle state.

        - ``state`` transitions (open / dismissed, plus compatibility claimed /
          resolved round-trips) do NOT touch the archive flag.
        - ``archived: true/false`` flips only the archive flag; the comment
          keeps its current state (open / resolved / dismissed).
        - Archive is only allowed on ROOT comments, and only when EVERY
          comment in the thread (root + all replies) is ``resolved``.
          Replies to an archived thread auto-unarchive the parent
          (enforced in ``Studio.post_comment``).
        - Dismiss is still rejected for claimed comments (agent is
          mid-flight); archive follows the resolved-thread rule instead.
        """
        comment_id = str(data.get("id", "")).strip()
        if not comment_id:
            return self._send_json(400, {"ok": False, "error": "id required"})
        new_state = str(data.get("state", "")).strip()
        has_archived = "archived" in data
        archived_flag = None
        if has_archived:
            archived_flag = bool(data.get("archived"))
        # Body edit (the enter-too-soon fix): the comment author can amend
        # the text of a not-yet-resolved comment in place. The edit bumps
        # updated_at, so an agent blocked in ``wait`` re-receives the
        # corrected ask.
        new_body = None
        if "body" in data:
            new_body = str(data.get("body") or "").strip()
            if not new_body:
                return self._send_json(400, {"ok": False, "error": "body must be non-empty"})
        if not new_state and not has_archived and new_body is None:
            return self._send_json(400, {"ok": False, "error": "state, archived, or body required"})

        current = self.studio.get_comment(comment_id)
        if current is None:
            return self._send_json(404, {"ok": False, "error": "comment not found"})

        # Body edits are only meaningful while the ask is still actionable.
        if new_body is not None and current.get("state") in ("resolved", "dismissed"):
            return self._send_json(
                409, {"ok": False,
                      "error": "cannot edit a resolved/dismissed comment "
                               "(reopen it first)"}
            )

        # State transitions: only dismiss is blocked on claimed comments.
        if new_state == "dismissed" and current.get("claimed_by"):
            return self._send_json(
                409, {"ok": False, "error": "cannot dismiss a claimed comment"}
            )

        # Archive guard: only on roots, only when whole thread is resolved.
        if has_archived and archived_flag:
            if current.get("parent_id"):
                return self._send_json(
                    400, {"ok": False, "error": "archive is only available on the top-level comment of a thread"}
                )
            # Build the thread and check every comment is resolved.
            all_comments = self.studio.list_comments()
            thread = [c for c in all_comments
                      if c.get("id") == comment_id or c.get("parent_id") == comment_id]
            if not thread:
                thread = [current]
            unresolved = [c for c in thread if c.get("state") != "resolved"]
            if unresolved:
                return self._send_json(
                    409, {
                        "ok": False,
                        "error": "archive requires every comment in the thread to be resolved",
                        "unresolved_ids": [c.get("id") for c in unresolved],
                    }
                )

        # Apply. State and archived are independent tracks.
        kwargs: dict[str, Any] = {}
        if new_state:
            kwargs["state"] = new_state
        if has_archived:
            kwargs["archived"] = archived_flag
        if new_body is not None:
            kwargs["body"] = new_body
        updated = self.studio.update_comment(comment_id, **kwargs)
        self._send_json(200, {"ok": True, "comment": updated})

    def _handle_claim(self, data: dict[str, Any]) -> None:
        comment_id, actor = data.get("id"), data.get("actor")
        if not isinstance(comment_id, str) or not isinstance(actor, str) or not actor.strip():
            return self._send_json(400, {"ok": False, "error": "id and actor required"})
        summary = data.get("summary")
        if summary is not None and not isinstance(summary, str):
            return self._send_json(400, {"ok": False, "error": "summary must be a string"})
        try:
            comment = self.studio.update_comment(comment_id, state="claimed",
                claimed_by=actor, request_summary=summary)
        except ValueError as exc:
            return self._send_json(409, {"ok": False, "error": str(exc)})
        if comment is None:
            return self._send_json(404, {"ok": False, "error": "comment not found"})
        self.studio.set_presence(actor=actor, state="editing", focus=comment.get("concept"),
                                 message=summary)
        return self._send_json(200, {"ok": True, "comment": comment})

    def _handle_resolve(self, data: dict[str, Any]) -> None:
        comment_id = data.get("id")
        activity = data.get("activity", [])
        if (not isinstance(comment_id, str) or not isinstance(activity, list)
                or not all(isinstance(v, str) for v in activity)
                or any(data.get(k) is not None and not isinstance(data[k], str)
                       for k in ("summary", "reply"))):
            return self._send_json(400, {"ok": False, "error": "invalid resolve request"})
        comment = self.studio.resolve_comment(comment_id, reply=data.get("reply"),
            summary=data.get("summary"), activity_ids=activity)
        if comment is None:
            return self._send_json(404, {"ok": False, "error": "comment not found"})
        return self._send_json(200, {"ok": True, "comment": comment})

    def _handle_presence(self, data: dict[str, Any]) -> None:
        state = str(data.get("state", "idle"))
        focus = data.get("focus")
        message = data.get("message")
        if message is not None:
            message = str(message)
        actor = str(data.get("actor", "agent")) or "agent"
        # INTENT2-002 fix: don't let a browser boot POST clobber the agent's
        # presence. The studio client used to POST
        # ``{actor:"user", state:"idle"}`` on every boot, silently overwriting
        # "Agent: editing tables/orders". The fix: a ``user`` actor's write
        # only takes effect when the current agent presence is already idle
        # (i.e. there's no active agent state to preserve). The agent's own
        # writes (and explicit ``okf presence`` CLI calls) always go through.
        if actor == "user":
            current = self.studio.get_presence()
            current_state = (current.get("state") or "idle") if current.get("actor") == "agent" else "idle"
            if current_state not in ("idle", "watching"):
                # The agent is actively doing something — keep its presence.
                self._send_json(200, {"ok": True, "presence": current, "preserved": True})
                return
        presence = self.studio.set_presence(actor=actor, state=state, focus=focus,
                                            message=message)
        self._send_json(200, {"ok": True, "presence": presence})

    def _handle_tunnel(self, data: dict[str, Any]) -> None:
        """Attach/detach a cloudflared quick tunnel to the RUNNING server.

        ``okf tunnel <bundle>`` drives this so going public no longer means
        killing and restarting ``serve`` (losing the session token, open
        SSE clients, and undo history). Actions:

        * ``start`` — spawn cloudflared against this server's port, add the
          tunnel hostname to ``allowed_hosts`` (checked per-request, so it
          takes effect immediately), record it in ``server.json``, and
          return the public URL. Idempotent: a second start returns the
          existing URL with ``already_running: true``.
        * ``stop`` — terminate the tunnel process, restore the base
          allowlist, clear ``server.json``'s tunnel_url. Idempotent.
        * ``status`` — report the current URL (or null).

        The tunnel process belongs to THIS server process (it dies with it
        — same lifecycle as ``serve --tunnel``). Exempt from --no-edit (it
        is server admin, not a bundle write) but never from the token +
        Origin/Host guard.
        """
        action = str(data.get("action", "start") or "start").strip()
        if action not in ("start", "stop", "status"):
            return self._send_json(
                400, {"ok": False, "error": "action must be start|stop|status"})
        server = self.server
        lock = getattr(server, "_tunnel_lock", None)
        if lock is None:
            return self._send_json(
                503, {"ok": False, "error": "tunnel control not available on this server"})
        with lock:
            current_url = getattr(server, "tunnel_url", None)
            if action == "status":
                return self._send_json(200, {"ok": True, "url": current_url})
            if action == "start":
                if current_url:
                    return self._send_json(
                        200, {"ok": True, "url": current_url, "already_running": True})
                port = server.server_address[1]
                try:
                    proc, url = start_quick_tunnel(port)
                except RuntimeError as e:
                    return self._send_json(502, {"ok": False, "error": str(e)})
                server.tunnel_proc = proc  # type: ignore[attr-defined]
                server.tunnel_url = url  # type: ignore[attr-defined]
                tunnel_host = url.split("://", 1)[1]
                base = getattr(server, "base_allowed_hosts", None) or ("127.0.0.1", "localhost")
                server.allowed_hosts = tuple(base) + (tunnel_host,)  # type: ignore[attr-defined]
                write_server_state(server, self.studio)
                self._emit_tunnel_activity("tunnel_attach",
                                           f"public tunnel attached: {url}")
                return self._send_json(200, {"ok": True, "url": url})
            # stop
            proc = getattr(server, "tunnel_proc", None)
            if proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            server.tunnel_proc = None  # type: ignore[attr-defined]
            server.tunnel_url = None  # type: ignore[attr-defined]
            base = getattr(server, "base_allowed_hosts", None) or ("127.0.0.1", "localhost")
            server.allowed_hosts = tuple(base)  # type: ignore[attr-defined]
            write_server_state(server, self.studio)
            if current_url:
                self._emit_tunnel_activity("tunnel_detach",
                                           f"public tunnel detached: {current_url}")
            return self._send_json(200, {"ok": True, "url": None,
                                         "stopped": current_url is not None})

    def _emit_tunnel_activity(self, action: str, summary: str) -> None:
        """Best-effort change-list entry so the share/unshare is visible."""
        try:
            self.studio.append_event({
                "type": "activity", "actor": "agent", "origin": "tunnel",
                "action": action, "ids": [], "summary": summary,
                "undoable": False,
            })
        except Exception:
            pass

    def _handle_apply(self, data: dict[str, Any]) -> None:
        """Run a whitelisted UpdateOp via the studio write funnel (§9.5/§12.3).

        Routes through ``update.apply_plan(..., studio=self.studio)`` so every
        write lands through :meth:`Studio.save_concept` — the single internal
        write path that:

        * validates the concept id (path containment, §9.5);
        * runs the §9.3 collision guard (``expected_rev`` mismatch → 409);
        * atomically writes (AGENTS.md hard rule #8);
        * snapshots prior bytes for one-click Undo (§12.5);
        * records an attributed activity event with `before/snap_rev` in
          detail (§12.2);
        * marks the rev as logged so the watcher's disk event de-dupes it
          (§10.5); and
        * broadcasts `changed` + `graph` (for graph-affecting ops) over SSE.

        The endpoint NEVER accepts a shell/argv string — only a known
        UpdateOp kind + args (§15.5). Returns 409 with a conflict payload
        when a concurrent on-disk edit moved the rev out from under us
        (§9.3/§9.4 — never silent-clobber).
        """
        from .update import UpdateOp, UpdatePlan, apply_plan
        from .paths import concept_id_from_str
        from .studio import validate_apply_args, _assert_safe_path_token

        kind = str(data.get("kind", "")).strip()
        target_str = str(data.get("target", "")).strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        group_id = data.get("group_id")
        expected_rev = data.get("expected_rev")
        if expected_rev is not None:
            expected_rev = str(expected_rev)
        if group_id is not None:
            group_id = str(group_id)
        if not kind or not target_str:
            return self._send_json(400, {"ok": False, "error": "kind and target required"})
        # P2-17 (§12.3 / §15.5): fail-closed per-kind args validation on the
        # public HTTP surface. Returns 400 (with a stable machine-readable
        # reason prefix) for missing / wrongly-typed / unknown args BEFORE
        # the concept lookup or any write attempt. Library handlers
        # (``update._h_*``) stay lenient for in-process / CLI callers.
        valid, reason = validate_apply_args(kind, args)
        if not valid:
            return self._send_json(400, {"ok": False, "error": reason})
        # P2-15 (SEC-005 / §9.5): reject path-traversal-shaped group_id and
        # expected_rev values BEFORE they reach the studio's filesystem ops.
        # ``_assert_safe_path_token`` raises ValueError on a value containing
        # path separators, leading dots, or chars outside ``[A-Za-z0-9._-]``;
        # we surface that as 400 (not 500) so the contract reads cleanly.
        for label, value in (("group_id", group_id), ("expected_rev", expected_rev)):
            if value is None:
                continue
            try:
                _assert_safe_path_token(label, value)
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": f"unsafe {label}: {e}"})
        live, _, _, _ = self._state_snapshot()
        try:
            cid = concept_id_from_str(target_str)
        except ConceptIdError:
            return self._send_json(400, {"ok": False, "error": f"bad concept id: {target_str}"})
        # P1-1: short-circuit collision check when the caller passed an
        # explicit expected_rev. The caller (an agent that read the doc at
        # reasoning time, possibly seconds or minutes earlier) wants the
        # write to FAIL rather than clobber if the on-disk bytes have moved
        # since. This catches the §9.3/§9.4 case before apply_plan runs.
        if expected_rev is not None:
            from .studio import rev_of
            target_path = live.root / f"{target_str}.md"
            try:
                current_bytes = target_path.read_bytes() if target_path.is_file() else b""
            except OSError:
                current_bytes = b""
            current_rev = rev_of(current_bytes) if current_bytes else None
            if current_rev != expected_rev:
                return self._send_json(409, {
                    "ok": False, "conflict": True,
                    "concept": target_str,
                    "expected_rev": expected_rev,
                    "current_rev": current_rev,
                })
        # F1: operate on a PRIVATE fresh bundle so the live bundle concurrent
        # GET handlers read is never mutated in place (no torn concept reads).
        fresh = Bundle.load(live.root)
        op = UpdateOp(kind=kind, target=cid, args=args)
        plan = UpdatePlan(bundle_root=str(fresh.root), description="studio-apply",
                          ops=[op], plan_kind="studio-apply")
        # When the caller passed expected_rev, prime the studio's collision
        # guard so the FIRST write attempt surfaces the conflict.
        result = apply_plan(
            fresh, plan, apply=True,
            studio=self.studio,
            actor=str(data.get("actor", "agent")),
            origin="http-apply",
            group_id=group_id,
        )
        applied = bool(result.get("applied"))
        # Detect a §9.3 collision surfacing from the funnel.
        conflict = None
        for _op_dict, op_result in result.get("results", []):
            if op_result.get("reason") == "rev_conflict":
                conflict = op_result.get("conflict")
                break
        if conflict is not None:
            # §9.4 conflict UX — return 409 with current vs expected rev so
            # the client can show "Keep mine / Take the agent's / View diff".
            return self._send_json(409, {
                "ok": False, "conflict": True,
                "concept": target_str,
                "expected_rev": conflict.get("expected_rev"),
                "current_rev": conflict.get("current_rev"),
            })
        if applied:
            # F1: swap the private fresh bundle in atomically under the state
            # lock; clear the cached palette so it recomputes from the new
            # bundle. Concurrent handlers that snapshot under the same lock see
            # either the old or the new bundle, never a half-mutation.
            state_lock = self._state_lock()

            def _swap() -> None:
                self.server.bundle = fresh  # type: ignore[attr-defined]
                if hasattr(self.server, "_palette"):
                    del self.server._palette  # type: ignore[attr-defined]

            if state_lock is not None:
                with state_lock:
                    _swap()
            else:
                _swap()
            # Note: graph + changed events are emitted by the funnel
            # (Studio.save_concept → record_activity → emit_change), so we
            # do not duplicate them here.
        self._send_json(200, {"ok": True, "applied": applied, "result": result})

    def _handle_undo(self, data: dict[str, Any]) -> None:
        """Restore a prior snapshot via :meth:`Studio.restore_snapshot` (§12.5).

        Routes through the studio write funnel so the restore is itself an
        attributed, undoable write (you can redo by undoing the undo).
        """
        from .studio import _assert_safe_path_token
        from .paths import concept_id_from_str, ConceptIdError

        concept_id = str(data.get("concept", "")).strip()
        rev = str(data.get("rev", "")).strip()
        group_id = data.get("group_id")
        if group_id is not None:
            group_id = str(group_id)
        if not concept_id and not group_id:
            return self._send_json(400, {"ok": False, "error": "concept+rev or group_id required"})
        # Validate the concept id lexically, symmetric with /__apply, so a
        # malformed value is a 400 before any filesystem op (defense in
        # depth; the history dir lookup hex-encodes the id anyway).
        if concept_id:
            try:
                concept_id_from_str(concept_id)
            except ConceptIdError as e:
                return self._send_json(400, {"ok": False, "error": f"bad concept id: {e}"})
        # P2-15 (SEC-005 / §9.5): reject path-traversal-shaped rev / group_id
        # values BEFORE they reach the studio's filesystem ops so the
        # contract returns 400 (not 500). rev and group_id are joined into
        # history paths; an unsanitised value could escape the session dir.
        for label, value in (("rev", rev), ("group_id", group_id)):
            if not value:
                continue
            try:
                _assert_safe_path_token(label, value)
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": f"unsafe {label}: {e}"})
        targets: list[tuple[str, str]] = []
        if group_id:
            members = self.studio.undo_group(str(group_id))
            if not members:
                return self._send_json(404, {"ok": False, "error": "unknown group"})
            targets = [(m["concept"], m["rev"]) for m in members]
        else:
            targets = [(concept_id, rev)]
        # P1-11 (QUA idempotency-retry): an undo of an already-undone
        # activity returns 409 with the current state, not a fresh apply.
        # The snapshot's rev is a content hash; if every target's CURRENT
        # on-disk bytes already hash to the requested rev, the undo has
        # already happened (replay). This catches the "client retry after
        # network glitch" case without re-running the restore (which would
        # be a no-op anyway, but the contract wants an explicit 409 so the
        # caller can tell a replay from a fresh undo).
        from .studio import rev_of as _rev_of_undo
        already_undone: list[dict[str, Any]] = []
        for cid, r in targets:
            try:
                cur_path = self.server.bundle.root / f"{cid}.md"  # type: ignore[attr-defined]
                cur_bytes = cur_path.read_bytes() if cur_path.is_file() else b""
            except OSError:
                cur_bytes = b""
            cur_rev = _rev_of_undo(cur_bytes) if cur_bytes else None
            if cur_rev == r:
                already_undone.append({"concept": cid, "rev": r, "current_rev": cur_rev})
        if already_undone and len(already_undone) == len(targets):
            # Every target is already at its snapshot rev → fully replayed.
            return self._send_json(409, {
                "ok": False, "conflict": True,
                "error": "already_undone",
                "targets": already_undone,
                "current_state": already_undone,
            })
        restored = 0
        missing: list[dict[str, str]] = []
        # Reload bundle after each restore so subsequent restores observe
        # the prior restore's effect.
        for cid, r in targets:
            res = self.studio.restore_snapshot(
                concept_id=cid, rev=r, group_id=group_id,
            )
            if res.ok:
                restored += 1
            elif not res.conflict:
                # INTENT2-003 fix: snapshot was pruned by the history ring
                # cap. Surface as a per-target missing entry so the caller
                # knows the undo is PARTIAL, not silently successful.
                missing.append({"concept": cid, "rev": r, "error": res.error or "missing"})
        # INTENT2-003: if every target was missing (ring cap), return 404 so
        # the browser shows an error toast instead of a success toast while
        # the file is unchanged. If only SOME targets were missing, return 409
        # (partial) — the change-list entry remains for audit either way.
        if missing and not restored:
            return self._send_json(404, {
                "ok": False, "error": "snapshots_pruned",
                "missing": missing,
                "message": "Cannot undo — the snapshot(s) for this change were "
                           "pruned by the history ring cap. The change-list "
                           "entry remains for audit.",
            })
        # Swap in a freshly-loaded bundle so GET handlers see the restored
        # content (the restore wrote through save_concept which mutated the
        # underlying file; reload to refresh caches).
        if restored:
            _, _, _, _ = self._state_snapshot()
            live_root = self.server.bundle.root  # type: ignore[attr-defined]
            fresh = Bundle.load(live_root)
            state_lock = self._state_lock()

            def _swap() -> None:
                self.server.bundle = fresh  # type: ignore[attr-defined]
                if hasattr(self.server, "_palette"):
                    del self.server._palette  # type: ignore[attr-defined]

            if state_lock is not None:
                with state_lock:
                    _swap()
            else:
                _swap()
        if missing:
            return self._send_json(409, {
                "ok": True, "restored": restored, "partial": True,
                "missing": missing,
                "message": f"Partial undo: {restored} restored, "
                           f"{len(missing)} snapshot(s) pruned.",
            })
        self._send_json(200, {"ok": True, "restored": restored})

    def _handle_diff(self, query: dict[str, list[str]]) -> None:
        """Line diff between two revs of one concept for the §9.4 conflict UI.

        Query params: ``concept``, ``from`` (a content-hash rev), ``to`` (a
        content-hash rev). Returns ``{concept, from, to, diff: [...]}`` where
        ``diff`` is a list of ``{kind: "ctx"|"add"|"del", text, num}`` rows
        for the client to render. Reads snapshots from
        ``history/<hex>/<rev>.md``; if either is missing, returns 404.

        SEC2-004 + SEC2-005: the concept id is validated via
        ``concept_id_from_str`` (defense-in-depth; the studio's hex encoding
        already defuses traversal), and the endpoint requires the per-session
        CSRF token because it exposes prior snapshot bytes (a same-origin
        attacker driving the browser can't read it without the token embedded
        in the served page).
        """
        from .studio import rev_of as _rev_of
        import difflib

        # SEC2-005: require the token. Same-origin GET protects against
        # cross-origin reads (CORS blocks the response), but a same-origin
        # attacker (e.g. an XSS in the bundle content) could fetch this
        # without the token. The studio's own JS carries the token on every
        # fetch via ``tokenFetch``.
        if not self._check_write_auth():
            return self._send_text(
                403, "Forbidden: /__diff requires the studio token.",
                content_type="text/plain; charset=utf-8",
            )

        concept = (query.get("concept", [""])[0] or "").strip()
        from_rev = (query.get("from", [""])[0] or "").strip()
        to_rev = (query.get("to", [""])[0] or "").strip()
        if not concept or not from_rev or not to_rev:
            return self._send_json(400, {"ok": False, "error": "concept, from, to required"})
        # SEC2-004: validate concept as a real concept id (defense-in-depth).
        try:
            concept_id_from_str(concept)
        except ConceptIdError:
            return self._send_json(400, {"ok": False, "error": f"bad concept id: {concept}"})
        from_raw = self.studio.undo_snapshot(concept_id=concept, rev=from_rev)
        to_raw = self.studio.undo_snapshot(concept_id=concept, rev=to_rev)
        if from_raw is None or to_raw is None:
            return self._send_json(404, {"ok": False, "error": "snapshot missing"})
        from_lines = from_raw.splitlines(keepends=False)
        to_lines = to_raw.splitlines(keepends=False)
        diff_rows: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, from_lines, to_lines, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                for n, line in enumerate(from_lines[i1:i2]):
                    diff_rows.append({"kind": "ctx", "num": i1 + n + 1, "text": line})
            elif tag in ("replace", "delete"):
                for n, line in enumerate(from_lines[i1:i2]):
                    diff_rows.append({"kind": "del", "num": i1 + n + 1, "text": line})
                if tag == "replace":
                    for n, line in enumerate(to_lines[j1:j2]):
                        diff_rows.append({"kind": "add", "num": j1 + n + 1, "text": line})
            elif tag == "insert":
                for n, line in enumerate(to_lines[j1:j2]):
                    diff_rows.append({"kind": "add", "num": j1 + n + 1, "text": line})
        self._send_json(200, {
            "ok": True, "concept": concept,
            "from": from_rev, "to": to_rev, "diff": diff_rows,
        })

    def _handle_preview(self, data: dict[str, Any]) -> None:
        """Render arbitrary in-flight markdown for preview (§9.5 DoS caps)."""
        md = str(data.get("markdown", ""))
        html = markdown_to_html(md)
        self._send_json(200, {"ok": True, "html": html})

    def _route(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        # Normalize trailing slash (except for root).
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        # Normalize trailing .md → extensionless concept route.
        # SPEC §5 links use /tables/users.md or ./users.md; the server's
        # canonical route is /tables/users (no extension). Stripping .md
        # at the route level makes every valid OKF link reachable.
        # BUT: do NOT strip /index.md or <dir>/index.md — those are
        # reserved-filename routes handled separately below.
        if path.endswith(".md") and not path.startswith("/__") and not path.endswith("/index.md") and path != "/index.md":
            path = path[:-3]

        # Stable integration discovery contains no token or absolute disk path.
        if path == "/__api/v1":
            from .contracts import discovery
            return self._send_json(200, discovery(
                editing=getattr(self.server, "studio_edit", False),
                live=getattr(self.server, "studio_live", False)))
        if path == "/__health":
            return self._send_json(200, {
                "ok": not bool(getattr(self.server, "_reload_error", None)),
                "api_version": "1", "concepts": len(self.bundle.concepts),
                "reload": "error" if getattr(self.server, "_reload_error", None) else "ready",
            })
        # Internal endpoints first.
        if path == "/":
            return self._handle_root()
        if path == "/__events":
            return self._handle_events()
        if path == "/__data/doc":
            return self._handle_data_doc(query)
        if path == "/__data/events":
            return self._handle_data_events(query)
        if path == "/__comments":
            return self._handle_comments(query)
        if path == "/__diff":
            return self._handle_diff(query)
        if path in ("/__graph", "/__graph.html"):
            return self._handle_graph()
        if path == "/__search":
            return self._handle_search(query)
        if path == "/__data/graph.json":
            return self._handle_graph_json()
        if path == "/__data/content.json":
            return self._handle_content_json()
        if path.startswith("/__raw/"):
            return self._handle_raw(path[len("/__raw/"):])
        if path.startswith("/__static/"):
            return self._handle_static(path[len("/__static/"):])
        if path in ("/index.md", "/index.html"):
            return self._handle_root()
        if path.endswith("/index.md"):
            return self._handle_dir_index(path[:-len("/index.md")])
        # Bundle-local media: any path with an allowlisted media extension
        # is served from the bundle tree (visibility + containment checks
        # live in the handler). Everything else stays a concept path.
        if posixpath.splitext(path)[1].lower() in BUNDLE_MEDIA_EXTENSIONS:
            return self._handle_bundle_asset(path)
        # Concept page.
        return self._handle_concept(path)

    # --- Route handlers ---------------------------------------------------
    def _handle_root(self) -> None:
        html = _render_index_page(
            self.bundle, mode="serve", name=self.display_name,
            palette=self.palette, config=self._config(), sub="",
            index_file=self.bundle.root_index(),
        )
        html = self._plugin().on_index_render(html)
        self._send_text(200, html)

    def _handle_dir_index(self, sub: str) -> None:
        sub = sub.lstrip("/")
        # Validate sub path segments to prevent traversal / weird inputs.
        # bundle.indexes.get() is already safe (returns None for unknown
        # keys), but we validate explicitly as defence in depth: any future
        # change that reads from disk using ``sub`` would silently introduce
        # traversal if we didn't pin the contract here.
        if sub:
            for segment in sub.split("/"):
                if not segment:
                    continue
                # Reject obvious traversal attempts.
                if segment in ("..", ".") or "\x00" in segment:
                    return self._send_text(
                        404, "Not found", content_type="text/plain; charset=utf-8"
                    )
        rel_index_md = Path(sub) / "index.md" if sub else Path("index.md")
        index_file = self.bundle.indexes.get(rel_index_md)
        # P2-54 server-side defense: ``index.md`` / ``log.md`` are reserved
        # files NOT covered by the concept-level symlink containment in
        # Bundle.load (model.py — REPORTED). If the loaded index file's
        # resolved path escapes the bundle root (e.g. a symlinked
        # ``subdir/index.md`` pointing at ``/etc/passwd``), refuse to serve.
        if index_file is not None and getattr(index_file, "path", None) is not None:
            if not _path_within(index_file.path, self.bundle.root):
                return self._send_text(
                    404, "Not found", content_type="text/plain; charset=utf-8"
                )
        html = _render_index_page(
            self.bundle, mode="serve", name=self.display_name,
            palette=self.palette, config=self._config(), sub=sub,
            index_file=index_file,
        )
        html = self._plugin().on_index_render(html)
        self._send_text(200, html)

    def _handle_concept(self, cid_str: str) -> None:
        cid_str = cid_str.lstrip("/")
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_text(404, f"Not a concept id: {cid_str}", content_type="text/plain")
        concept = self.bundle.concepts.get(cid)
        if concept is None:
            # Friendly fallback: if the path matches a directory containing
            # concepts, serve the directory index page instead of a 404.
            if any(c.id[:len(cid)] == cid for c in self.bundle.concepts.values()):
                return self._handle_dir_index("/".join(cid))
            return self._send_text(404, f"Unknown concept: {cid_str}", content_type="text/plain")
        graph = self.bundle.graph()
        html = _render_concept_page(
            concept, self.bundle, graph, mode="serve",
            name=self.display_name, palette=self.palette,
            config=self._config(),
        )
        html = self._plugin().on_concept_render(concept, html)
        self._send_text(200, html)

    def _handle_graph(self) -> None:
        html = _render_graph_page(
            self.bundle, mode="serve", name=self.display_name,
            config=self._config(),
        )
        self._send_text(200, html)

    def _handle_search(self, query: dict[str, list[str]]) -> None:
        q = (query.get("q", [""])[0] or "").strip()
        # Defense-in-depth (iter1 P3-5): cap query length before dispatch so a
        # pathological/hostile multi-MB query cannot pin the lexical/semantic
        # tokenizer+scorer. Truncating (rather than 413) keeps search usable
        # for the (already unrealistic) over-long case while bounding work.
        if len(q) > MAX_SEARCH_QUERY_CHARS:
            q = q[:MAX_SEARCH_QUERY_CHARS]
        want_json = query.get("format", ["html"])[0] == "json"
        from .search import SearchMode, search_bundle
        mode = query.get("mode", ["lexical"])[0]
        try:
            search_mode = SearchMode(mode)
        except ValueError:
            return self._send_json(400, {"ok": False, "error": "unknown search mode"})
        if search_mode == SearchMode.LEXICAL:
            results = self._run_search(q, limit=30)
        else:
            results = [r.as_dict() for r in search_bundle(self.bundle, q, mode=search_mode, limit=30)]
        if want_json:
            return self._send_json(200, results)
        html = _render_search_page(
            self.bundle, mode="serve", name=self.display_name,
            config=self._config(), query=q, results=results, search_mode=mode,
        )
        self._send_text(200, html)

    def _run_search(self, q: str, *, limit: int) -> list[dict[str, Any]]:
        """Run the lexical search backend and return rendered result rows.

        Security/determinism (P1-35): there is NO silent substring fallback.
        The lexical backend (``okf_loom.search``) is always-on; if it
        raises, the exception propagates to :meth:`do_GET`, which logs the
        full traceback to stderr and returns a clean 500 to the client. We
        deliberately do NOT fall back to a different ranking algorithm: two
        identical ``(bundle, query)`` inputs MUST rank identically, and a
        hidden fallback that scored by a different algorithm (different
        fields, score=1.0, different snippets) would silently violate that
        contract whenever a backend threw — with no log.
        """
        if not q:
            return []
        # The lexical backend is part of this package; the import always
        # succeeds. If it ever failed (e.g. a broken install), the
        # ImportError propagates to do_GET → 500, which is correct: search
        # is unavailable, not silently degraded.
        from .search import search_bundle, SearchMode  # type: ignore
        rs = search_bundle(self.bundle, q, mode=SearchMode.LEXICAL, limit=limit)
        return [
            {
                "concept_id": concept_id_to_str(r.concept_id),
                "title": r.title,
                "score": r.score,
                "description": getattr(r, "description", "") or "",
                "snippets": list(getattr(r, "snippets", []) or []),
            }
            for r in rs
        ]

    def _handle_raw(self, cid_str: str) -> None:
        cid_str = cid_str.lstrip("/")
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_text(404, f"Not a concept id: {cid_str}", content_type="text/plain")
        concept = self.bundle.concepts.get(cid)
        if concept is None:
            return self._send_text(404, f"Unknown concept: {cid_str}", content_type="text/plain")
        # P2-55: DoS backstop. The authoritative body-size cap belongs in
        # parse.py (REPORTED); the server additionally refuses to ship a
        # single raw body larger than MAX_RAW_RESPONSE_BYTES so one huge
        # concept cannot saturate a handler. 413 is the right status for
        # "entity too large"; Content-Type is plain text to avoid rendering.
        body = concept.body
        if len(body.encode("utf-8", errors="replace")) > MAX_RAW_RESPONSE_BYTES:
            return self._send_text(
                413,
                f"Request entity too large: raw body exceeds "
                f"{MAX_RAW_RESPONSE_BYTES} bytes.",
                content_type="text/plain; charset=utf-8",
            )
        self._send_text(200, body, content_type="text/markdown; charset=utf-8")

    def _handle_graph_json(self) -> None:
        data = build_graph_data(self.bundle, name=self.display_name)
        self._send_json(200, data)

    def _handle_content_json(self) -> None:
        data = _content_index_json(self.bundle)
        data["name"] = self.display_name
        self._send_json(200, data)

    # --- Live studio: live push + per-doc + events feed (§7, §9) -----------
    def _handle_events(self) -> None:
        """SSE stream of change/presence/activity/comment events (§7.1).

        Registers a bounded queue with the EventBus and streams events until
        the client disconnects, with a 15s heartbeat. Capped at
        ``max_sse_clients`` concurrent streams (503 when full). The handler
        thread blocks for the connection lifetime (ThreadingHTTPServer), so
        the cap bounds resource use.

        P2-14: the max-clients check + the subscribe run under
        ``_state_lock`` so concurrent bursts cannot over-admit (TOCTOU).
        P1-6: the ``ready`` frame carries ``doc_revs`` (cid → rev) so the
        client can detect which docs moved during reconnect.
        """
        # F12: a HEAD request must NOT enter the SSE loop — it has no body to
        # read and no client reading the stream, so it would block this worker
        # thread forever. Return an empty body immediately.
        if getattr(self, "_head_only", False):
            return self._send_text(200, "")
        studio = self.studio
        if studio is None:
            return self._send_text(
                503, "Studio (live updates) not enabled.", content_type="text/plain"
            )
        if not getattr(self.server, "studio_live", True):
            # P1-15: --no-watch-ui disables SSE live push entirely. The
            # endpoint returns 503 so a client knows to fall back to reads.
            return self._send_text(
                503, "Live push disabled (--no-watch-ui).",
                content_type="text/plain",
            )
        max_clients = getattr(self.server, "max_sse_clients", DEFAULT_MAX_SSE_CLIENTS)
        # P2-14: take the state lock for check-then-subscribe so concurrent
        # SSE bursts cannot over-admit beyond max_sse_clients.
        state_lock = self._state_lock()
        q: queue.Queue | None = None
        try:
            if state_lock is not None:
                with state_lock:
                    if studio.bus.client_count() >= max_clients:
                        return self._send_text(
                            503, "Too many live clients.",
                            content_type="text/plain",
                        )
                    q = studio.bus.subscribe()
            else:
                if studio.bus.client_count() >= max_clients:
                    return self._send_text(
                        503, "Too many live clients.", content_type="text/plain"
                    )
                q = studio.bus.subscribe()
        except Exception:
            if q is not None:
                studio.bus.unsubscribe(q)
            raise
        # Send the SSE response headers (no Content-Length → stream stays open).
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # P1-6: initial 'ready' event carries the current rev AND a per-doc
        # rev map (cid → content-hash) so the client can detect which docs
        # moved during a reconnect and refetch just those.
        ready_payload: dict[str, Any] = {
            "type": "ready", "rev": studio.current_rev(),
            "doc_revs": self._compute_doc_revs(),
        }
        self._sse_write(ready_payload)
        last_heartbeat = time.monotonic()
        stop_event = getattr(self.server, "stop_event", threading.Event())
        try:
            while not stop_event.is_set():
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    # Heartbeat keeps proxies from timing the idle stream out.
                    # Naming the heartbeat ``type=ping`` lets the client count
                    # it as "stream alive" for its polling-watchdog grace
                    # (§7.3 INTENT-004 fix).
                    if time.monotonic() - last_heartbeat >= 15:
                        self._sse_write({"type": "ping"}, comment="ping")
                        last_heartbeat = time.monotonic()
                    continue
                if event.get("type") == "resync":
                    self._sse_write({
                        "type": "resync", "rev": studio.current_rev(),
                        "doc_revs": self._compute_doc_revs(),
                    })
                else:
                    self._sse_write(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            studio.bus.unsubscribe(q)

    def _compute_doc_revs(self) -> dict[str, str]:
        """Snapshot of every concept's current content-hash rev (P1-6).

        The bundle is loaded under ``_state_lock`` so the snapshot is
        consistent with the latest swap. Used in the SSE ``ready`` and
        ``resync`` frames so the client can detect exactly which docs
        changed during a reconnect.
        """
        from .studio import rev_of
        bundle, _, _, _ = self._state_snapshot()
        out: dict[str, str] = {}
        try:
            for cid, concept in bundle.concepts.items():
                try:
                    out[concept_id_to_str(cid)] = rev_of(concept.path.read_bytes())
                except OSError:
                    continue
        except Exception:
            return out
        return out

    def _sse_write(self, event: dict[str, Any] | None, *, comment: str = "") -> None:
        """Write one SSE frame and flush.

        ``comment`` is the SSE comment line (``: <c>``) — HTTP-level
        keep-alive that proxies/intermediaries see as stream progress.
        ``event`` is a real SSE event frame (``event: <type>\\ndata:
        <json>``) the client's EventSource dispatches.

        Bundle C fix (INTENT-004 / §7.3): when BOTH ``event`` and
        ``comment`` are provided, emit BOTH frames (comment first so a
        buffering proxy sees traffic immediately, then the dispatchable
        event so the client's app-level watchdog can count it). The
        previous implementation dropped the event when ``comment`` was
        set, so the heartbeat's ``type=ping`` event was lost in transit
        and the client's ``markSseReceived`` was never called for
        heartbeats — false-positive starvation → spurious polling.
        """
        try:
            if comment:
                self.wfile.write(f": {comment}\n\n".encode("utf-8"))
            if event is not None:
                etype = str(event.get("type", "message"))
                payload = json.dumps(event, default=str)
                self.wfile.write(f"event: {etype}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise

    def _handle_data_doc(self, query: dict[str, list[str]]) -> None:
        """One concept's rendered+raw+meta JSON for in-place re-render (§6)."""
        from .render import _render_link_map

        cid_str = (query.get("id", [""])[0] or "").strip()
        if not cid_str:
            return self._send_json(400, {"error": "id required"})
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_json(404, {"error": f"bad concept id: {cid_str}"})
        bundle, _, _, _ = self._state_snapshot()
        concept = bundle.concepts.get(cid)
        if concept is None:
            return self._send_json(404, {"error": f"unknown concept: {cid_str}"})
        graph = bundle.graph()
        # Render the concept body HTML the same way the concept page does, so a
        # live `changed` patch swaps in identical markup (§7.3 no-refresh).
        body_html = markdown_to_html(concept.body)
        link_map = _render_link_map(bundle, concept, "serve")
        body_html = rewrite_internal_links(body_html, link_map)
        # F13: apply the plugin hook so a plugin's body injection survives the
        # first live patch — parity with _handle_concept's render path, which
        # routes the page through ``on_concept_render``. Use ``self._plugin()``
        # (NoOpPlugin fallback) for the same crash-safety the concept page has;
        # the snapshot's ``plugin`` may be None when an embedder sets
        # ``server.plugin = None`` (the test harness does exactly that).
        body_html = self._plugin().on_concept_render(concept, body_html)
        backlinks = sorted({
            concept_id_to_str(link.source)
            for link in graph.in_edges.get(cid, []) if link.source is not None
        })
        outgoing = sorted({
            concept_id_to_str(link.target)
            for link in graph.out_edges.get(cid, []) if link.target is not None
        })
        self._send_json(200, {
            "id": cid_str,
            "rev": rev_of(concept.raw_text),
            "title": concept.title,
            "type": concept.type,
            "html": body_html,
            "raw": concept.body,
            "frontmatter": concept.frontmatter,
            "headings": [{"level": h.level, "text": h.text} for h in concept.headings],
            "backlinks": backlinks,
            "outgoing": outgoing,
        })

    def _handle_data_events(self, query: dict[str, list[str]]) -> None:
        """Filtered read of the events feed for the change list (§10.5/§12.2).

        Defaults to ``order=desc`` (newest-first) so a busy session's recent
        activity is surfaced in the change list. Callers can pass
        ``?order=asc&since=<id>`` explicitly for the older replay use case.
        """
        studio = self.studio
        if studio is None:
            return self._send_json(200, {"events": []})
        since = (query.get("since", [""])[0] or "").strip() or None
        # F17: a non-integer limit must NOT raise a 500; default to 200.
        try:
            limit = int(query.get("limit", ["200"])[0] or 200)
        except (ValueError, TypeError):
            limit = 200
        actor = (query.get("actor", [""])[0] or "").strip() or None
        concept = (query.get("concept", [""])[0] or "").strip() or None
        # Default to newest-first; ``?order=asc`` opts into oldest-first
        # (used by okf watch --since replay).
        order = (query.get("order", ["desc"])[0] or "desc").strip().lower()
        if order not in ("asc", "desc"):
            order = "desc"
        events = studio.read_events(
            since=since, limit=limit, actor=actor, concept=concept, order=order,
        )
        self._send_json(200, {"events": events, "rev": studio.current_rev()})

    def _handle_comments(self, query: dict[str, list[str]]) -> None:
        """Canonical comment state from directives.jsonl (§9.1, P1-6 QUA3-002).

        Returns the latest record per comment id (last-write-wins), optionally
        filtered by ``state`` or ``concept``. This is the authoritative source
        for the comment panel — the client should NOT reconstruct state from
        the events feed (which had a desc-order + last-iteration inversion bug
        that showed claimed/resolved comments as ``open`` after reload).
        """
        studio = self.studio
        if studio is None:
            return self._send_json(200, {"comments": []})
        state = (query.get("state", [""])[0] or "").strip() or None
        concept = (query.get("concept", [""])[0] or "").strip() or None
        comments = studio.list_comments(state=state, concept=concept)
        self._send_json(200, {"comments": comments})

    def _handle_static(self, name: str) -> None:
        # Prevent path traversal. Normalize, then reject anything still
        # containing a separator or a traversal token after normalization.
        # ``unquote`` already ran in ``_route`` (we parse the decoded path),
        # so this catches both ``..`` and the post-decode form of ``%2e%2e``.
        # NUL bytes are rejected outright (they break C-string path handling
        # downstream and have no legitimate use in an asset name).
        if "\x00" in name:
            return self._send_text(404, "Not found", content_type="text/plain")
        name = posixpath.normpath("/" + name).lstrip("/")
        if "/" in name or ".." in name or not name:
            return self._send_text(404, "Not found", content_type="text/plain")
        # Bundle override first — gated on the effective active-code gate
        # (security: overrides can carry arbitrary JS/HTML, same trust
        # boundary as plugins/templates). The operator must consent via
        # OKF_LOOM_ALLOW_ACTIVE_CODE / --allow-active-code; the bundle alone
        # cannot enable its own overrides (P1-40).
        from .viewer.assets import _overrides_allowed
        if _overrides_allowed(self.bundle):
            override = self.bundle.root / ".okf-loom" / "viewer" / "static" / name
            if override.is_file():
                # P2-54 defense in depth: refuse to serve an override file
                # whose resolved path escapes the bundle root (e.g. a
                # symlinked static asset pointing at /etc/passwd). Concepts
                # are protected at the model layer; override files are not,
                # so we check here.
                if not _path_within(override, self.bundle.root):
                    return self._send_text(
                        404, "Not found", content_type="text/plain"
                    )
                body = override.read_bytes()
                ctype = _content_type_for(name)
                return self._send_bytes(200, body, content_type=ctype)
        if name in list_builtin_static():
            body = load_static(name, self.bundle).encode("utf-8")
            ctype = _content_type_for(name)
            return self._send_bytes(200, body, content_type=ctype)
        return self._send_text(404, f"Unknown static asset: {name}", content_type="text/plain")

    def _handle_bundle_asset(self, rel: str) -> None:
        """Serve a bundle-local media file (screenshots, diagrams, video, PDF).

        Only ``BUNDLE_MEDIA_EXTENSIONS`` paths route here, and a file is
        served only when ALL of these hold:

        * the path is clean relative segments — no ``..``/``.``/NUL/backslash
          (rejected lexically; never resolved);
        * the §5 scan would reach it (:func:`okf_loom.ignore.is_path_visible`
          — so ``.okf-loom`` session state, ``.git``, ``node_modules`` and
          gitignored paths stay unreachable, and ``bundle.include`` revives
          work the same as for concepts);
        * its resolved location stays inside the bundle root (``_path_within``
          symlink defence, P2-54);
        * it fits the response size cap (``MAX_RAW_RESPONSE_BYTES`` backstop).

        Responses carry the sandboxed ``_BUNDLE_ASSET_CSP`` (bundle media is
        user content — see the constant) and a weak mtime+size ETag so
        image-heavy pages revalidate with 304s instead of re-downloading
        every screenshot on each visit (Cache-Control stays no-cache).

        A path that names no existing file falls through to concept routing
        (``shot.png.md`` is a legal concept file rendering at ``/shot.png``);
        an existing file that fails visibility/containment is refused with
        404, never fallen through.
        """
        rel = rel.lstrip("/")
        segments = rel.split("/")
        if any(s in ("", ".", "..") or "\x00" in s or "\\" in s for s in segments):
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        bundle = self.bundle
        target = bundle.root / rel
        try:
            if not target.is_file():
                raise OSError(rel)
            st = target.stat()
        except OSError:
            # No such FILE → fall through to concept routing: a concept id
            # may legally contain a media-looking suffix (``shot.png.md``
            # renders at ``/shot.png``). When both exist, the file wins —
            # an <img src> needs bytes, not an HTML page.
            return self._handle_concept(rel)
        if not _path_within(target, bundle.root):
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        # Same exclusion semantics as Bundle.load / the watcher (spec §5):
        # defaults + .gitignore + bundle.exclude, include add-backs on top.
        # Config is re-read per request (cheap small file) so a lock-down
        # edit takes effect without a restart, mirroring the watcher.
        from .config import OkfConfig, OkfConfigError
        try:
            bundle_cfg = OkfConfig.load(bundle.root).bundle
        except OkfConfigError:
            from .config import BundleConfig
            bundle_cfg = BundleConfig()
        from .ignore import is_path_visible
        if not is_path_visible(
            bundle.root, rel,
            exclude=bundle_cfg.exclude,
            include=bundle_cfg.include,
            respect_gitignore=bundle_cfg.respect_gitignore,
        ):
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        if st.st_size > MAX_RAW_RESPONSE_BYTES:
            return self._send_text(
                413,
                f"Request entity too large: bundle file exceeds "
                f"{MAX_RAW_RESPONSE_BYTES} bytes.",
                content_type="text/plain; charset=utf-8",
            )
        ctype = _content_type_for(rel)
        etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            return self._send_bytes(
                304, b"", content_type=ctype,
                csp=_BUNDLE_ASSET_CSP, extra_headers={"ETag": etag},
            )
        try:
            body = target.read_bytes()
        except OSError:
            return self._send_text(
                404, f"No such bundle file: {rel}",
                content_type="text/plain; charset=utf-8",
            )
        self._send_bytes(
            200, body, content_type=ctype,
            csp=_BUNDLE_ASSET_CSP, extra_headers={"ETag": etag},
        )

    # --- Response writers -------------------------------------------------
    def _studio_bootstrap(self) -> str | None:
        """Inline ``<script>``/``<link>`` bootstrap for the live studio (§7/§13).

        Injected into every served HTML page so the studio client has the
        per-session CSRF token (current spec §14), the studio flags, and the initial
        bundle rev. Returns ``None`` when the studio is not attached
        (plain read-only server) — then nothing is injected.
        """
        studio = self.studio
        if studio is None:
            return None
        token = self.csrf_token or ""
        cfg = {
            "token": token,
            "live": getattr(self.server, "studio_live", True),
            "edit": getattr(self.server, "studio_edit", True),
            "rev": studio.current_rev(),
            "theme": getattr(self.server, "studio_theme", "auto"),
        }
        payload = json.dumps(cfg, default=str)
        bits = [
            # CSP-safe bootstrap: a non-executable JSON data block. The
            # served CSP is ``script-src 'self' ...`` with NO 'unsafe-inline',
            # so an inline executable <script> is blocked (and the binding
            # studio contract says "no inline <script> bodies"). A
            # ``<script type="application/json">`` data block is NOT executed
            # by the browser, so ``script-src`` does not govern it; the studio
            # modules (live.js/studio.js) read it on boot and expose it as
            # ``window.__OKF_LOOM_STUDIO__`` for parity with the documented API.
            f'<script type="application/json" id="okf-studio-bootstrap">{payload}</script>',
            '<link rel="stylesheet" href="/__static/studio.css">',
            '<script type="module" src="/__static/live.js"></script>',
            '<script type="module" src="/__static/studio.js"></script>',
        ]
        return "\n".join(bits)

    def _send_text(self, code: int, body: str, *, content_type: str = "text/html; charset=utf-8",
                   close: bool = False) -> None:
        # Inject the studio bootstrap into HTML pages so the live client boots
        # with the CSRF token + asset links (no-op when studio is absent).
        if content_type.startswith("text/html") and "</head>" in body:
            boot = self._studio_bootstrap()
            if boot:
                body = body.replace("</head>", boot + "\n</head>", 1)
        self._send_bytes(code, body.encode("utf-8"), content_type=content_type, close=close)

    def _send_bytes(self, code: int, body: bytes, *, content_type: str,
                    close: bool = False, csp: str | None = None,
                    extra_headers: dict[str, str] | None = None) -> None:
        if close:
            # Signal both the server loop (stop reading after this response)
            # and the client (reconnect cleanly) that this connection closes.
            # Used by the 413 oversized-body path where draining an unbounded
            # body is worse than a clean reconnect (F4).
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-cache")
        # Security response headers (defence in depth). The default CSP
        # restricts script sources to 'self' and the pinned CDN; everything
        # else (inline event handlers, remote img/style, etc.) is blocked or
        # restricted. ``csp`` overrides it per response — used by the bundle
        # media handler to sandbox user content. Adjust via bundle overrides
        # if needed.
        #
        # ``style-src`` and ``connect-src`` also allow
        # https://cdn.jsdelivr.net because highlight.js loads its theme
        # stylesheet and source map from the CDN. ``img-src`` allows
        # ``data:`` (inline SVG icons) plus the CDN for any image asset
        # a bundle might embed from jsdelivr. ``font-src`` allows the CDN
        # because KaTeX's stylesheet references its webfonts relative to
        # the CDN. Keep this list in sync with the per-page
        # <meta> CSPs in viewer/templates/ — browsers enforce the
        # INTERSECTION of header + meta, so a directive missing here or
        # there silently wins.
        self.send_header(
            "Content-Security-Policy",
            csp or (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://cdn.jsdelivr.net; "
                "connect-src 'self' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
        )
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-XSS-Protection", "0")  # disabled in favour of CSP
        self.end_headers()
        try:
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self._send_bytes(code, body, content_type="application/json; charset=utf-8")

    # --- Logging (quieter than default) ----------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Suppress default noisy stderr access log; keep error log.
        # Guard: send_error passes HTTPStatus enum as args[0], not str.
        if args and isinstance(args[0], str) and args[0].startswith(("GET ", "POST ")):
            return
        super().log_message(format, *args)


# Extension → Content-Type. Every response also carries
# ``X-Content-Type-Options: nosniff``, so a type missing here does not
# degrade to browser sniffing — it ships as ``application/octet-stream``
# (download, never render). That makes this table the effective allowlist
# of what the viewer can DISPLAY, for built-in static, override-dir, and
# bundle media files alike.
_EXT_CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
}


def _content_type_for(name: str) -> str:
    ext = posixpath.splitext(name.lower())[1]
    return _EXT_CONTENT_TYPES.get(ext, "application/octet-stream")


def _path_within(candidate: Path, root: Path) -> bool:
    """True iff ``candidate.resolve()`` is ``root`` or lives beneath it.

    Symlink defense (P2-54): used for static-override files, bundle media,
    and (via the handler) for reserved index/log files so a symlinked
    ``index.md`` / ``log.md`` / override asset pointing outside the bundle
    root is refused at serve time even if :meth:`Bundle.load` followed the
    link at load time. Canonical implementation lives in
    :func:`okf_loom.ignore.path_within` (shared with the static build's
    media copy).
    """
    from .ignore import path_within
    return path_within(candidate, root)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Quick tunnel: one-command public sharing via cloudflared
# ---------------------------------------------------------------------------
# ``okf serve <bundle> --tunnel`` starts a Cloudflare quick tunnel next to
# the loopback server and prints the public URL. The tunnel hostname is
# appended to the running server's allowed_hosts so the cross-origin write
# guard accepts studio traffic arriving through the tunnel — no manual
# okf-loom.config.yaml edit needed. The server itself STAYS on loopback;
# cloudflared is the only network-facing process.

_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def parse_quick_tunnel_url(line: str) -> str | None:
    """Extract a trycloudflare quick-tunnel URL from a cloudflared log line.

    Returns the full ``https://…`` URL, or ``None`` when the line carries
    no tunnel URL. Pure + deterministic (unit-tested separately from the
    subprocess plumbing).
    """
    m = _TUNNEL_URL_RE.search(line or "")
    return m.group(0) if m else None


def start_quick_tunnel(port: int, *, timeout_s: float = 45.0):
    """Start ``cloudflared tunnel --url http://127.0.0.1:<port>``.

    Returns ``(process, public_url)``. Raises ``RuntimeError`` with an
    actionable message when the cloudflared binary is missing or the
    public URL does not appear within ``timeout_s`` (the process is
    terminated in that case).
    """
    import shutil
    import subprocess

    exe = shutil.which("cloudflared")
    if not exe:
        raise RuntimeError(
            "cloudflared not found on PATH. Install it "
            "(https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) "
            "or share the server another way."
        )
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    found: list[str] = []
    got_url = threading.Event()

    def _scan() -> None:
        try:
            for line in proc.stdout or ():
                url = parse_quick_tunnel_url(line)
                if url and not found:
                    found.append(url)
                    got_url.set()
                    # Keep draining so cloudflared never blocks on a full pipe.
        except (OSError, ValueError):
            pass
        got_url.set()  # EOF: unblock the waiter (found stays empty on failure)

    threading.Thread(target=_scan, daemon=True).start()
    if not got_url.wait(timeout_s) or not found:
        try:
            proc.terminate()
        except OSError:
            pass
        raise RuntimeError(
            f"cloudflared started but no tunnel URL appeared within {timeout_s:.0f}s."
        )
    return proc, found[0]


def write_server_state(server: Any, studio: "Studio | None") -> None:
    """Persist the running server's address to ``<session>/server.json``.

    Best-effort: session-aware CLI verbs (``okf tunnel``) read this to find
    the live server's port; everything else keeps working without it.
    """
    if studio is None:
        return
    addr = getattr(server, "server_address", None)
    if not addr:
        return  # embedding stubs without a bound socket have no port to record
    try:
        from .io_utils import atomic_write_text
        studio.ensure_session()
        host, port = addr[:2]
        state = {
            "host": host,
            "port": port,
            "pid": os.getpid(),
            "started": getattr(server, "started_iso", None),
            "url": f"http://{host}:{port}/",
            "tunnel_url": getattr(server, "tunnel_url", None),
        }
        atomic_write_text(studio.server_state_path,
                          json.dumps(state, indent=2) + "\n")
    except OSError:
        pass


@isolated_server_factory
def create_server(
    bundle_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    watch: bool = True,
    open_browser: bool = False,
    name: str | None = None,
    allow_active_code: bool | None = None,
    studio_edit: bool = True,
    studio_live: bool = True,
    tunnel: bool = False,
    allow_network: bool = False,
) -> StudioServer:
    """Create a bound, unstarted studio for an embedding host.

    Use as a context manager or call start()/close(). Port zero selects an
    available port. The factory opens no browser and owns no agent process.
    ``open_browser`` is accepted for source compatibility but has no effect.

    current studio (docs-bundle/reference/spec.md): by default the full live collaborative
    studio is on — SSE push, commenting/directing, agent enrichment.
    ``studio_edit=False`` (--no-edit) makes a read-only kiosk (no comments);
    ``studio_live=False`` disables SSE push. Both read the ``studio:``
    section of ``okf-loom.config.yaml``; explicit args override config.

    Active-code gate (P1-40): ``allow_active_code`` is the operator-level
    override. ``None`` (default) defers to :func:`operator_consent` (env var
    ``OKF_LOOM_ALLOW_ACTIVE_CODE``). An explicit ``True`` / ``False`` from the
    CLI ``--allow-active-code`` / ``--no-allow-active-code`` flag (cli.py —
    REPORTED) wins. The effective gate is
    ``bundle_cfg.allow_active_code AND operator_consent``; the bundle alone
    cannot enable viewer overrides / plugins.

    Reload safety (P2-46): the server is reload-safe — a watcher reload
    mutates ``bundle`` / ``_palette`` / ``plugin`` under a lock, and every
    handler that needs more than one of those snapshots them once under the
    same lock (``_state_snapshot``).
    """
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_network:
        raise ValueError("non-loopback embedding requires allow_network=True")
    bundle_root = Path(bundle_root).resolve()

    # effective_allow drives the warning + the plugin build.
    effective = effective_allow_active_code(bundle_root)

    bundle = Bundle.load(bundle_root)
    config = load_config(bundle)

    # --- Live studio setup -------------------------------------------------
    from .config import OkfConfig
    okf_cfg = OkfConfig.load(bundle_root)
    sc = okf_cfg.studio
    studio_edit = studio_edit and sc.edit
    studio_live = studio_live and sc.live
    studio: Studio | None = None
    if studio_live or studio_edit:
        studio = Studio.for_configured_bundle(
            bundle_root,
            bundle_name=name or config.get("name") or bundle.name,
            max_queue=64,
        )
        studio.ensure_session()
        # Seed the bundle rev so the first 'ready' event is coherent.
        studio.set_presence(actor="agent", state="idle")

    # Current spec §15 + §14: load viewer plugins at server start, gated on
    # the EFFECTIVE active-code gate (bundle cfg AND operator consent). We
    # pass the precomputed ``allow_active_code=effective`` so the plugin
    # builder does NOT re-read the bundle cfg alone (which would ignore
    # operator consent). When the gate is closed, no entry-point discovery
    # happens and the composite is a complete no-op.
    from .viewer.plugins import build_viewer_plugin
    plugin = build_viewer_plugin(bundle_root, allow_active_code=effective)

    # P1-40: prominent stderr WARNING whenever the effective gate is open.
    # Mirrors the --host warning style. Active code means viewer overrides
    # AND plugins execute attacker-controllable JS/Python; the operator
    # must understand the trust implication.
    if effective:
        print(
            f"WARNING: active code (viewer overrides + plugins) is ENABLED "
            f"for bundle {bundle_root}.\n"
            f"WARNING: Only run this for bundles whose override/plugin "
            f"sources you trust.\n"
            f"WARNING: A malicious override can execute arbitrary "
            f"JavaScript in every visitor's browser.\n"
            f"WARNING: (gate opened by OKF_LOOM_ALLOW_ACTIVE_CODE env var or "
            f"--allow-active-code)",
            file=sys.stderr,
        )

    # Warn if binding to a non-loopback address (the server has no auth,
    # no rate-limiting, and serves raw bundle bodies — exposure is a footgun).
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {host} — the viewer has NO authentication and\n"
            f"WARNING: will be reachable from the network. Anyone who can reach\n"
            f"WARNING: this host can read the full bundle content. Use\n"
            f"WARNING: 127.0.0.1 (the default) for local-only access.",
            file=sys.stderr,
        )

    server = StudioServer((host, port), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = config  # type: ignore[attr-defined]
    server.name = name or config.get("name") or bundle.name  # type: ignore[attr-defined]
    server.plugin = plugin  # type: ignore[attr-defined]
    # P2-46: lock that serializes watcher reloads with handler snapshots.
    # Handlers detect its presence via ``_state_lock`` and snapshot bundle /
    # palette / plugin once per request under it; the reload path holds it
    # while mutating all three.
    server._state_lock = threading.Lock()  # type: ignore[attr-defined]
    # P1-37: last reload error (if any), surfaced for a transient banner.
    # Best-effort display state; set under the same lock.
    server._reload_error = None  # type: ignore[attr-defined]
    # Palette is lazily computed on first request via the handler accessor.
    # Live studio state on the server object (§6 ``server.state``).
    server.studio = studio  # type: ignore[attr-defined]
    # P1-16: allowed_hosts MUST default to loopback and never be empty —
    # the cross-origin guard fails closed against an unset/empty list.
    allowed_hosts_final = sc.allowed_hosts or ("127.0.0.1", "localhost")
    csrf_token = secrets.token_hex(16) if studio else None
    server.csrf_token = csrf_token  # type: ignore[attr-defined]
    server.allowed_hosts = allowed_hosts_final  # type: ignore[attr-defined]
    server.max_sse_clients = sc.max_sse_clients  # type: ignore[attr-defined]
    server.studio_live = studio_live  # type: ignore[attr-defined]
    server.studio_edit = studio_edit  # type: ignore[attr-defined]
    server.studio_theme = sc.theme  # type: ignore[attr-defined]
    # P0-2: persist the CSRF token to <session>/.token (0o600) so an
    # external agent can read it via ``okf token <bundle>`` (or the library)
    # and then drive /__apply /__presence /__comment /__undo over HTTP.
    # The file is owned by the user running ``okf serve``; the per-session
    # token rotates on each serve start.
    if studio is not None and csrf_token is not None:
        try:
            studio.ensure_session()
            token_path = studio.token_path
            # mkstemp + chmod + rename for atomicity + correct perms.
            import tempfile as _tempfile
            token_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = _tempfile.mkstemp(
                prefix=".token-", suffix=".tmp", dir=str(token_path.parent)
            )
            try:
                os.write(fd, csrf_token.encode("utf-8"))
                os.close(fd)
                os.chmod(tmp, 0o600)
                os.replace(tmp, token_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    display_name = server.name  # type: ignore[attr-defined]

    def _concept_revs(b: Bundle) -> dict[str, str]:
        return {
            concept_id_to_str(cid): rev_of(c.raw_text)
            for cid, c in b.concepts.items()
        }

    watcher: _BundleWatcher | None = None
    # P1-15: --no-watch-ui / studio_live=False disables SSE live push, but
    # the watcher STILL runs so a headless ``okf watch`` consumer (or the
    # ``/__data/events`` polling endpoint) keeps working. When both
    # ``studio_live`` and ``studio_edit`` are False (plain kiosk), we skip
    # the watcher too — there is no consumer for the events.
    if watch and (studio_live or studio_edit):
        state_lock = server._state_lock  # type: ignore[attr-defined]

        def _reload() -> None:
            # Reload the bundle and rebuild the plugin composite atomically
            # under the state lock so a concurrent request cannot see a
            # torn view (new bundle + old palette + old plugin). On failure
            # we re-raise so the watcher logs the error AND does not advance
            # its mtime snapshot (next tick retries — P1-37).
            try:
                old_bundle = server.bundle  # type: ignore[attr-defined]
                old_revs = _concept_revs(old_bundle) if studio else None
                fresh = Bundle.load(bundle_root)
                fresh_plugin = build_viewer_plugin(
                    bundle_root,
                    allow_active_code=effective_allow_active_code(bundle_root),
                )
                with state_lock:
                    server.bundle = fresh  # type: ignore[attr-defined]
                    # Reset cached palette so it recomputes from the new bundle.
                    if hasattr(server, "_palette"):
                        del server._palette  # type: ignore[attr-defined]
                    server.plugin = fresh_plugin  # type: ignore[attr-defined]
                    server._reload_error = None  # type: ignore[attr-defined]
                # Invalidate the per-root override cache so an okf-loom.config.yaml
                # edit (e.g. toggling allow_active_code) made alongside a
                # concept edit is picked up on the next request.
                clear_overrides_cache(bundle_root)
                # Watcher change-diff (§7): publish created/changed/removed
                # + graph events so open tabs patch in place with no refresh.
                if studio is not None and old_revs is not None:
                    new_revs = _concept_revs(fresh)
                    created = [i for i in new_revs if i not in old_revs]
                    removed = [i for i in old_revs if i not in new_revs]
                    changed = [
                        i for i in new_revs
                        if i in old_revs and new_revs[i] != old_revs[i]
                    ]
                    if created:
                        studio.emit_change(kind="created", ids=created, origin="disk")
                    if changed:
                        studio.emit_change(kind="changed", ids=changed, origin="disk")
                    if removed:
                        studio.emit_change(kind="removed", ids=removed, origin="disk")
                    if created or removed or changed:
                        studio.emit_change(kind="graph", ids=[], origin="disk", graph=True)
                    # Cross-process SSE tail (INTENT2-001 / Bundle H finding):
                    # when a CLI mutator in a SEPARATE process wrote via
                    # save_concept, its activity/change/graph events landed in
                    # events.jsonl but were never broadcast to THIS process's
                    # SSE subscribers (the CLI's bus has no subscribers; this
                    # process's bus never saw the append). The dedup map
                    # (.last-logged.json) correctly suppresses a duplicate
                    # disk-origin ``changed`` for the same .md write, but
                    # that leaves the browser with NO signal at all. Tail
                    # events.jsonl for entries newer than the last one this
                    # process published + broadcast them. The bounded
                    # ``_recently_published_events`` set prevents double-
                    # broadcast of this process's own writes.
                    if created or removed or changed:
                        studio.tail_and_broadcast_cross_process_events()
                    # ARCH4-001 fix: the events.jsonl mtime check + staleness
                    # sweep are now handled by the watcher's _tick() callback
                    # (fires every tick, not just when .md changed). They were
                    # previously inside _reload() which is gated by the .md
                    # snapshot diff — so they never fired for session-only
                    # writes (claim/resolve/presence). Removed from here to
                    # avoid duplication; _tick() handles both the mtime→tail
                    # path AND the staleness sweep unconditionally.
            except Exception as e:
                # Record the failure for a transient banner; re-raise so the
                # watcher logs to stderr and does NOT advance its snapshot
                # (the watcher's own except handles logging + retry).
                with state_lock:
                    server._reload_error = str(e)  # type: ignore[attr-defined]
                raise
        # ARCH4-001 fix: the tick_fn fires every watcher tick (1s) regardless
        # of .md activity, handling cross-process session-feed events
        # (events.jsonl mtime → tail broadcast) + the staleness sweep
        # (§13.8). These must fire even when the bundle is idle.
        def _tick() -> None:
            if studio is None:
                return
            # Cross-process SSE tail: check events.jsonl mtime.
            try:
                ev_path = studio.events_path
                if ev_path.is_file():
                    ev_mtime = ev_path.stat().st_mtime
                    last_ev_mtime = getattr(server, "_last_events_mtime", 0.0)
                    if ev_mtime != last_ev_mtime:
                        server._last_events_mtime = ev_mtime
                        studio.tail_and_broadcast_cross_process_events()
            except OSError:
                pass
            # Staleness sweep (§13.8 INTENT2-008).
            try:
                studio.sweep_stale_presence_and_claims()
            except Exception:
                pass

        watcher = _BundleWatcher(bundle_root, _reload, interval=1.0, tick_fn=_tick)

    # --tunnel: public sharing without touching the bind or the
    # bundle config. The tunnel hostname joins allowed_hosts at runtime so
    # studio writes (comments, applies) work through the tunnel; the HTTP
    # server itself stays loopback-only.
    # Tunnel state lives on the server object (not run_server locals) so the
    # ``POST /__tunnel`` admin route can attach/detach a tunnel at runtime
    # (``okf tunnel <bundle>``) without restarting serve. base_allowed_hosts
    # is the pre-tunnel allowlist a detach restores.
    server.base_allowed_hosts = tuple(allowed_hosts_final)  # type: ignore[attr-defined]
    server.tunnel_proc = None  # type: ignore[attr-defined]
    server.tunnel_url = None  # type: ignore[attr-defined]
    server._tunnel_lock = threading.Lock()  # type: ignore[attr-defined]
    from datetime import datetime, timezone
    server.started_iso = (  # type: ignore[attr-defined]
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    if tunnel:
        actual_port = server.server_address[1]
        try:
            proc_url = start_quick_tunnel(actual_port)
            server.tunnel_proc, server.tunnel_url = proc_url  # type: ignore[attr-defined]
        except RuntimeError as e:
            print(f"WARNING: --tunnel failed: {e}", file=sys.stderr)
            print("WARNING: continuing local-only.", file=sys.stderr)
        if server.tunnel_url:  # type: ignore[attr-defined]
            tunnel_host = server.tunnel_url.split("://", 1)[1]  # type: ignore[attr-defined]
            server.allowed_hosts = tuple(allowed_hosts_final) + (tunnel_host,)  # type: ignore[attr-defined]
            print(
                f"WARNING: --tunnel makes this bundle READABLE BY ANYONE with the\n"
                f"WARNING: link below (quick tunnels are unauthenticated). Studio\n"
                f"WARNING: writes still require the per-session token.",
                file=sys.stderr,
            )
    tunnel_url = server.tunnel_url  # type: ignore[attr-defined]
    # server.json: lets session-aware CLI verbs (``okf tunnel``) find the
    # running server's port. Written after the tunnel attempt so a
    # --tunnel start records its URL too.
    write_server_state(server, studio)

    server.watcher = watcher
    return server


def run_server(
    bundle_root: str | Path, *, host: str = "127.0.0.1", port: int = 8787,
    watch: bool = True, open_browser: bool = True, name: str | None = None,
    allow_active_code: bool | None = None, studio_edit: bool = True,
    studio_live: bool = True, tunnel: bool = False,
) -> None:
    """Compatibility CLI entrypoint; the embeddable factory owns setup/cleanup."""
    with create_server(
        bundle_root, host=host, port=port, watch=watch, name=name,
        allow_active_code=allow_active_code, studio_edit=studio_edit,
        studio_live=studio_live, tunnel=tunnel,
        # CLI performs its explicit --public-ack gate before calling us.
        allow_network=host not in ("127.0.0.1", "localhost", "::1"),
    ) as server:
        print(f"Serving OKF bundle '{server.name}' at {server.url}/  (Ctrl-C to stop)", flush=True)
        if server.tunnel_url:
            print(f"Public tunnel: {server.tunnel_url}/  (dies with this process)", flush=True)
        if open_browser:
            webbrowser.open(server.url + "/")
        try:
            while server._thread.is_alive():
                server._thread.join(timeout=0.5)
        except KeyboardInterrupt:
            pass
