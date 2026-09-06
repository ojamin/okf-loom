"""Live collaborative studio logic layer (current spec §10-§13).

This module is the **reusable logic layer** the HTTP server (``server.py``),
the headless watcher (``watch.py``), and embedding harnesses all share. It
is deliberately framework-free: no ``http.server`` import, no asyncio. The
HTTP layer in ``server.py`` stays thin and calls into here.

Responsibilities (one implementation each, §17):
  * :class:`EventBus` — thread-safe fan-out for SSE clients (bounded per-client
    queues; a slow client gets deltas dropped + one ``resync`` enqueued so the
    watcher never blocks on a browser, §7.1).
  * :func:`rev_of` — per-doc content hash ``sha1(raw)[:12]`` for live-update
    coherence (§9.3 / D3).
  * :func:`new_id` — sortable ULID-style ids for events / comments (§10.5).
  * :class:`Studio` — session state + the append-only feeds under
    ``<bundle>/.okf-loom/session/`` (``events.jsonl``, ``directives.jsonl``,
    ``presence.json``, ``history/``) and the comment / presence / undo /
    activity API (current spec §11-§13).

Concurrency: append-only JSONL writes are serialized with an advisory file
lock (``fcntl.flock`` POSIX / ``msvcrt.locking`` Windows; O_APPEND fallback)
so multiple writers — the serve process, a separate ``okf watch`` process,
and direct CLI calls — never interleave a record (§10.5 "who writes it").
An in-process :class:`threading.Lock` orders same-process appends on top.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .io_utils import atomic_write_bytes, atomic_write_text
from .paths import validate_segment
from .transactions import file_transaction, serialized

# ---------------------------------------------------------------------------
# Cross-platform advisory file lock for append-only JSONL writes.
# ---------------------------------------------------------------------------

try:  # POSIX
    import fcntl as _fcntl

    def _lock_handle(fh: Any) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)

    def _unlock_handle(fh: Any) -> None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)

    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover - Windows
    try:
        import msvcrt as _msvcrt

        def _lock_handle(fh: Any) -> None:
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_LOCK, 1)

        def _unlock_handle(fh: Any) -> None:
            try:
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass

        _HAVE_FLOCK = True
    except ImportError:  # pragma: no cover
        _HAVE_FLOCK = False

        def _lock_handle(fh: Any) -> None:
            pass

        def _unlock_handle(fh: Any) -> None:
            pass


# ---------------------------------------------------------------------------
# rev + ids
# ---------------------------------------------------------------------------

def rev_of(raw: str | bytes) -> str:
    """Per-doc content hash ``sha1(raw)[:12]`` (§9.3 / D3).

    Used for live-update coherence and to detect the one real collision case
    (a concurrent on-disk edit while the agent writes, §9.4). The agent is the
    sole writer of ``.md`` files through the studio, so this is mainly to keep
    live updates coherent, not to merge concurrent edits.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


# ULID-style sortable id: 10 base32-ish chars of ms timestamp + 16 random.
# Monotonic enough for tail/sort within a session and cheap (no extra dep).
_TS_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32_ms(ms: int) -> str:
    out = []
    for _ in range(10):
        ms, rem = divmod(ms, 32)
        out.append(_TS_ALPHABET[rem])
    return "".join(reversed(out))


def new_id() -> str:
    """A sortable, unique id (ULID-style) for events and comments (§10.5)."""
    ts = _b32_ms(int(time.time() * 1000))
    rand = secrets.token_hex(8).upper()
    return f"{ts}{rand}"


def _normalize_comment_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a comment record for readers (archive-track split).

    Earlier records stored archive as a ``state == "archived"`` value. The
    current model keeps ``archived`` as a separate boolean and preserves the
    underlying lifecycle state (open/resolved/dismissed). On read, we map
    any older ``state == "archived"`` to ``archived=True, state="open"``
    (best-effort: the prior state was most often open) and default
    ``archived`` to ``False`` for new records that lack the field.

    Also ensures ``updated_at`` falls back to ``ts`` for older rows that
    predate the separate updated_at field.

    Also auto-derives ``request_summary`` from ``body`` when not
    set. The studio's collapsed preview shows BOTH a short of what was
    asked (this field) AND a short of what the agent did (the explicit
    ``summary`` field set at resolve time). The ask-short is computed
    once on first read and cached on the record so it is stable across
    reads. Agents can override it explicitly via
    ``okf comment-claim --summary`` (which writes ``request_summary``,
    NOT ``summary`` — the latter is reserved for the resolve-time
    "done" tag).
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    if out.get("state") == "archived":
        out["state"] = "open"
        out["archived"] = True
    out.setdefault("archived", False)
    # updated_at fallback: older rows use ts for both creation and modification.
    if not out.get("updated_at"):
        out["updated_at"] = out.get("ts")
    # request_summary: short version of the comment body (the "ask").
    # Auto-derived once on first read so the collapsed preview can show a
    # useful short even when the agent never set one explicitly. The
    # heuristic is intentionally simple: first sentence boundary or first
    # 100 chars, whichever is shorter. Trailing whitespace + ellipsis.
    if not out.get("request_summary"):
        body = (out.get("body") or "").strip()
        if body:
            # Split on sentence-ending punctuation or newline; take the
            # first non-empty chunk.
            import re as _re
            parts = _re.split(r"[.!?\n]", body, maxsplit=1)
            first = (parts[0] if parts else body).strip()
            if not first:
                first = body
            if len(first) > 100:
                first = first[:100].rstrip() + "\u2026"
            out["request_summary"] = first
        else:
            out["request_summary"] = ""
    return out


def _now_iso() -> str:
    """UTC instant in ISO-8601 with milliseconds + ``Z`` (datetime skill).

    Single clock read (F14): ``time.gmtime`` and the millisecond fraction both
    derive from ONE ``time.time()`` so a ms boundary crossing cannot make the
    ``.mmm`` field inconsistent with the seconds field (e.g. ``...:59.000``
    when the two reads straddled a whole-second rollover).
    """
    now = time.time()
    t = time.gmtime(now)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + f".{int((now * 1000) % 1000):03d}Z"


# ---------------------------------------------------------------------------
# Safe path-component validation (§9.5 path containment invariant)
# ---------------------------------------------------------------------------

# Allow the same characters as a concept-id segment (paths.validate_segment)
# plus colon (group ids may carry ULID-style suffixes) — but reject any path
# separator, leading dot, or percent-encoding that could escape a directory.
_SAFE_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def _assert_safe_path_token(name: str, value: str) -> None:
    """Reject path-traversal-shaped values before they reach filesystem ops.

    The studio writes undo snapshots under ``history/<hex>/<rev>.md`` and
    group manifests under ``history/_groups/<group_id>/manifest.json``. The
    ``rev`` and ``group_id`` values come from HTTP/CLI callers, so they must
    pass the same containment guard as ``concept_id`` (§9.5: "path
    containment on every path-like id"). Hex-encoded ids and content-hash
    revs already pass; this guards against a caller that bypasses those
    encodings (defense-in-depth, §15.5).
    """
    if not isinstance(value, str) or not _SAFE_PATH_TOKEN_RE.match(value):
        raise ValueError(f"unsafe {name} (rejected by §9.5 path containment): {value!r}")


# Activity actions that change the graph structure (§7.2 ``graph`` event).
# After a successful write of one of these, the studio also emits a
# ``graph`` event so the graph view invalidates its cached layout (P1-6).
_GRAPH_ACTIONS: frozenset[str] = frozenset({
    "add_link", "remove_link",
    "add_relation", "remove_relation",
    "add_entity", "remove_entity",
    "write_concept", "remove_concept",
    "repair",
})


# ---------------------------------------------------------------------------
# §12.3 / §15.5: per-kind args schema for the public /__apply HTTP surface.
# ---------------------------------------------------------------------------
# Each entry declares the (required, optional) args for one UpdateOp ``kind``
# the studio's /__apply endpoint accepts. Required entries MUST be present
# and well-typed (a value of ``object`` as the type sentinel means "any
# non-None JSON value"); optional entries are permitted but not required.
# Any key NOT in (required ∪ optional) is rejected with 400 so the public
# HTTP surface is fail-closed — the library handlers (``update._h_*``) stay
# lenient for in-process / CLI callers that may carry extra context (e.g.
# ``allow_forward_reference`` is a CLI-only knob the HTTP surface also
# allows so existing programmatic agents don't break).
#
# The schemas intentionally match the underlying handler signatures in
# ``update._HANDLERS`` (target existence is the handler's call; we only
# validate arg shape here). ``AnyType`` means "required, any JSON type".
class _AnyType:
    """Sentinel type for "required, any JSON type" schema entries."""

    @classmethod
    def _check(cls, value: Any) -> bool:
        return True


_APPLY_KIND_SCHEMAS: dict[str, dict[str, dict[str, type]]] = {
    "add_link": {
        "required": {"target_concept_id": str},
        "optional": {
            "label": str,
            "anchor_text": str,
            "section": (str, type(None)),
            "allow_forward_reference": bool,
        },
    },
    "add_relation": {
        "required": {"target_concept_id": str, "relation_type": str},
        "optional": {"detail": str},
    },
    "add_entity": {
        "required": {"label": str},
        "optional": {"kind": str, "entity_id": str, "aliases": list},
    },
    "set_frontmatter": {
        # ``value`` may be any JSON type (str/int/float/bool/list/dict).
        "required": {"key": str, "value": _AnyType},
        "optional": {},
    },
    "append_body_section": {
        "required": {"heading": str, "body": str},
        "optional": {},
    },
    "update_section": {
        "required": {"heading": str, "body": str},
        "optional": {"mode": str, "create_if_missing": bool},
    },
    "replace_text": {
        "required": {"old": str, "new": str},
        "optional": {"all": bool},
    },
    "set_tag": {
        "required": {"tag": str},
        "optional": {},
    },
    "add_tag": {
        "required": {"tag": str},
        "optional": {},
    },
    "remove_link": {
        "required": {"target_concept_id": str},
        "optional": {},
    },
}


def validate_apply_args(kind: str, args: dict) -> tuple[bool, str]:
    """Fail-closed validation of /__apply args per ``_APPLY_KIND_SCHEMAS``.

    Returns ``(True, "")`` when ``args`` satisfies the schema for ``kind``;
    otherwise ``(False, "<human-readable reason>")``. Reasons use stable
    machine prefixes (``unknown_kind:``, ``missing_arg:``, ``bad_type:``,
    ``unknown_arg:``) so callers can classify responses.

    The library handlers in :mod:`okf_loom.update` ALSO validate per-kind
    args (raising ``KeyError`` → ``bad_args:<key>``); this validator runs
    earlier so the HTTP surface returns 400 (not a 200 with ``applied:
    False``) on bad input, and so unknown args are rejected even when the
    handler would silently ignore them.
    """
    if not isinstance(args, dict):
        return False, "bad_type:args must be a JSON object"
    schema = _APPLY_KIND_SCHEMAS.get(kind)
    if schema is None:
        return False, f"unknown_kind:{kind}"
    required = schema.get("required", {})
    optional = schema.get("optional", {})
    allowed = set(required) | set(optional)
    for key, expected in required.items():
        if key not in args:
            return False, f"missing_arg:{kind}.{key}"
        value = args[key]
        if expected is _AnyType:
            if value is None:
                return False, f"bad_type:{kind}.{key} must not be null"
            continue
        if not isinstance(value, expected):
            return False, (
                f"bad_type:{kind}.{key} must be "
                f"{' or '.join(t.__name__ for t in expected) if isinstance(expected, tuple) else expected.__name__}"
            )
    for key in args:
        if key not in allowed:
            return False, f"unknown_arg:{kind}.{key}"
    return True, ""




# ---------------------------------------------------------------------------
# EventBus — thread-safe fan-out for SSE clients
# ---------------------------------------------------------------------------

from .events import EventBus, _CallbackSub  # compatibility re-exports

# ---------------------------------------------------------------------------
# Session feeds (append-only JSONL)
# ---------------------------------------------------------------------------

def _append_jsonl(path: Path, obj: dict[str, Any], *, proc_lock: threading.Lock,
                  max_bytes: int | None = None, keep: int = 7) -> None:
    """Append one JSON record to ``path`` under a cross-process advisory lock.

    One compact JSON object per line, terminated by ``\\n``. The in-process
    ``proc_lock`` orders same-process writers; the advisory file lock orders
    writers across processes (serve vs. a separate ``okf watch``). The final
    fallback (no fcntl/msvcrt) is a single ``O_APPEND`` write, which is atomic
    for small records on POSIX (§10.5).

    When ``max_bytes`` is set and the active file is at or above the cap, the
    file is rotated to ``<stem>-YYYYMMDD-NN.<ext>`` and a fresh active file
    starts (§10.5). The most recent ``keep`` rotated files are retained; the
    rest are pruned. Rotation runs under the same advisory lock so concurrent
    writers never split a record across files.
    """
    line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with proc_lock:
        # Lock a stable sibling instead of the active JSONL itself. Locking the
        # active file and then renaming it leaves the fd attached to the archive;
        # the first post-rotation record would be appended to the rotated file.
        # The stable lock keeps the cross-process critical section while we
        # rotate first, then open the fresh active path for the actual append.
        lock_path = path.with_name(f".{path.name}.lock")
        lock_fh = open(lock_path, "a+b")
        try:
            if _HAVE_FLOCK:
                _lock_handle(lock_fh)
            try:
                # §10.5 rotation: cap the active file before the append.
                # Runs INSIDE the advisory lock (QUA2-003) so a concurrent
                # writer cannot observe a half-rotated state or race on the
                # rename target.
                if max_bytes is not None and max_bytes > 0:
                    try:
                        if path.is_file() and path.stat().st_size >= max_bytes:
                            _rotate_jsonl(path, keep=keep)
                    except OSError:
                        pass  # best-effort; the append still goes through.
                with open(path, "ab") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            finally:
                if _HAVE_FLOCK:
                    _unlock_handle(lock_fh)
        finally:
            lock_fh.close()


def _rotate_jsonl(active: Path, *, keep: int) -> Path | None:
    """Rename ``active`` to a timestamped sibling and prune old rotations.

    Returns the rotated path (or ``None`` if rotation did not occur — e.g.
    the active file does not exist). Called inside the advisory lock from
    :func:`_append_jsonl`. The naming scheme is ``<stem>-YYYYMMDD-NN.<ext>``
    so lexicographic sort == chronological order within a day (§10.5).
    """
    if not active.is_file():
        return None
    stem = active.stem
    suffix = active.suffix
    parent = active.parent
    day = time.strftime("%Y%m%d", time.gmtime())
    # Find the next NN for today (avoid collisions under bursty writes).
    nn = 1
    while True:
        candidate = parent / f"{stem}-{day}-{nn:02d}{suffix}"
        if not candidate.exists():
            break
        nn += 1
        if nn > 999:
            # Pathological: 999 rotations in one day. Stop renaming rather
            # than spin; the active file keeps growing but the system stays
            # usable.
            return None
    os.rename(active, candidate)
    # Prune: keep only the most recent ``keep`` rotated files.
    pattern = f"{stem}-*-*{suffix}"
    rotated = sorted(parent.glob(pattern))
    if len(rotated) > keep:
        for stale in rotated[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass
    return candidate


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Skip a torn final line (a write in progress); never crash
                # the whole feed reader on one bad record.
                continue


def _read_jsonl_with_rotations(active: Path) -> Iterator[dict[str, Any]]:
    """Yield events across rotated + active JSONL files in oldest-first order.

    Used by :meth:`Studio.read_events` and :meth:`Studio.list_comments` so a
    caller does not need to know about rotation. Rotated files match the
    pattern ``<stem>-YYYYMMDD-NN.<ext>`` and sort lexicographically into
    chronological order within a day; the active file is read last.
    """
    stem = active.stem
    suffix = active.suffix
    parent = active.parent
    rotated = sorted(parent.glob(f"{stem}-*-*{suffix}"))
    for r in rotated:
        yield from _read_jsonl(r)
    yield from _read_jsonl(active)


def _event_dedup_key(event: dict[str, Any]) -> str | None:
    """Unique per-append key for cross-process broadcast dedup.

    ``event_id`` is the right identity: :meth:`Studio.append_event` stamps
    a fresh ULID on EVERY append, unconditionally. Neither of the other
    candidates is unique:

    * ``id`` — comment lifecycle events reuse the COMMENT id as the event
      id (the SSE payload contract; the client's upsertComment keys on
      it), so an id collides across a comment's open→claimed→resolved
      transitions.
    * ``seq`` — a per-instance in-memory counter initialised from the
      feed at construction, so two processes whose Studio instances both
      predate the other's writes stamp colliding seqs.

    The ``id|seq`` composite fallback covers rows written before
    ``event_id`` existed (their combination is unique in all but
    pathological multi-writer interleavings).
    """
    eid = event.get("event_id")
    if eid:
        return f"eid:{eid}"
    rid = event.get("id")
    if rid is None:
        return None
    return f"id:{rid}|seq:{event.get('seq')}"


# ---------------------------------------------------------------------------
# Studio — session state + the comment / presence / undo / activity API
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    """Outcome of :meth:`Studio.save_concept` (the single write funnel).

    A successful write: ``ok=True``, ``rev`` is the new content-hash,
    ``activity_id`` is the appended activity event id (for change-list
    Undo lookup), and ``snap_rev`` is the prior-bytes rev (or ``None``
    when the write was not undoable / created a new file with empty prior).

    A §9.3/§9.4 collision: ``ok=False, conflict=True``, ``current_rev`` is
    the on-disk rev that mismatched ``expected_rev``; the write did NOT
    happen. The caller surfaces this (the studio conflict UX, §9.4) — the
    studio never silent-clobbers.

    Other failures (forbidden_paths, max_edits_per_action, malformed id):
    ``ok=False, conflict=False, error=<msg>``.
    """
    ok: bool
    conflict: bool = False
    rev: str | None = None
    expected_rev: str | None = None
    current_rev: str | None = None
    concept_id: str | None = None
    activity_id: str | None = None
    snap_rev: str | None = None
    error: str | None = None

@dataclass
class Studio:
    """Session state for one running server (§6 ``server.state``).

    Holds the :class:`EventBus`, the bundle + session paths, the in-process
    append lock, and a monotonic ``seq`` counter. The HTTP layer, the watcher,
    and embedding harnesses call the methods here; the actual ``.md`` writes
    happen through :meth:`save_concept` — the **single internal write path**
    (§17) that the CLI mutators, the HTTP `/__apply`/`/__undo` endpoints, and
    embedding harnesses all share. ``save_concept`` validates, atomically
    writes, snapshots for undo, records an attributed activity event, marks
    the rev as logged so the watcher doesn't double-log it, and broadcasts
    `changed` (+ `graph` for graph-affecting ops) over SSE.
    """

    bundle_root: Path
    session_dir: Path
    bus: EventBus = field(default_factory=EventBus)
    log_edits: bool = True
    bundle_name: str = ""
    # §10.5 events.jsonl rotation caps (None = no rotation; spec default 8 MiB
    # / 7 rotated files, set via :meth:`for_bundle` config).
    events_max_bytes: int | None = 8 * 1024 * 1024
    events_keep: int = 7
    # Mutable session counters (guarded by _lock). On startup these are
    # seeded from the existing events.jsonl so a new process joining an
    # existing session continues the monotonic sequence (P1-2
    # cross-process dedup).
    seq: int = 0
    bundle_rev: int = 0
    presence: dict[str, Any] = field(default_factory=lambda: {"actor": "agent", "state": "idle"})
    # INTENT2-008: presence + claim staleness TTLs (§13.8 robust liveness).
    # A dropped agent process degrades gracefully: presence → idle after
    # presence_ttl_s (default 300s); stale claims → open after
    # claim_ttl_s (default 600s). The sweep runs on watcher tick.
    _presence_ts: float = field(default_factory=lambda: time.time(), repr=False)
    presence_ttl_s: float = 300.0
    claim_ttl_s: float = 600.0
    # P1-4 / QUA2-011: RLock (re-entrant) so idempotency's lookup→post_comment→
    # store critical section can hold _lock across post_comment (which itself
    # takes _lock for its append). A plain Lock deadlocks here.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # F3 (§10.5 disk-event dedup): concept_id → most recent content-hash rev a
    # mutator/undo already logged. The watcher's disk-origin ``emit_change``
    # skips an id whose current on-disk rev matches this value, so a write
    # that already logged itself is NOT re-logged when the watcher notices the
    # mtime change (no double-logging). Guarded by ``_lock`` AND persisted to
    # ``.last-logged.json`` so a SEPARATE process (serve + a concurrent
    # ``okf watch``) shares the same dedup state (P1-2 cross-process drift).
    _last_logged_rev: dict[str, str] = field(default_factory=dict, repr=False)
    # Cross-process SSE tail (INTENT2-001 / Bundle H finding): bounded set of
    # event ids THIS process published in-process via ``append_event`` so the
    # watcher's tail-and-rebroadcast path doesn't double-broadcast them.
    # Bounded at 256 entries (ring); entries are evicted oldest-first.
    _recently_published_events: set = field(default_factory=set, repr=False)
    _recently_published_order: list = field(default_factory=list, repr=False)
    # §12.3 opt-in constraints (default empty = fully free agent, §1.1/D8).
    constraints: dict[str, Any] = field(default_factory=dict)

    # --- lifecycle -------------------------------------------------------

    def transaction(self):
        """Coordinate this bundle's cooperating writers across processes."""
        self.ensure_session()
        return file_transaction(self.session_dir / ".transaction.lock")

    @classmethod
    def for_bundle(cls, bundle_root: str | Path, *, session_rel: str = ".okf-loom/session",
                   log_edits: bool = True, bundle_name: str = "",
                   max_queue: int = 64,
                   events_max_bytes: int | None = 8 * 1024 * 1024,
                   events_keep: int = 7,
                   constraints: dict[str, Any] | None = None) -> "Studio":
        root = Path(bundle_root)
        studio = cls(
            bundle_root=root,
            session_dir=root / session_rel,
            bus=EventBus(max_queue=max_queue),
            log_edits=log_edits,
            bundle_name=bundle_name,
            events_max_bytes=events_max_bytes,
            events_keep=events_keep,
            constraints=dict(constraints) if constraints else {},
        )
        # P1-2: resume monotonic seq/bundle_rev + cross-process dedup map
        # from any existing session state so a new process joining an
        # existing session does not reset the counters or re-log writes a
        # sibling process already logged.
        studio._resume_from_existing_session()
        return studio

    @classmethod
    def for_configured_bundle(cls, bundle_root: str | Path, *, bundle_name: str = "",
                              max_queue: int = 64) -> "Studio":
        """Create a Studio using ``studio:`` config for the bundle.

        ``serve`` already honored ``studio.session_dir``; CLI and watch paths
        must use the same resolver so session state has one authoritative
        location instead of splitting token/comment/presence files between the
        configured directory and the default ``.okf-loom/session``.
        """
        from .config import OkfConfig

        root = Path(bundle_root)
        sc = OkfConfig.load(root).studio
        return cls.for_bundle(
            root,
            session_rel=sc.session_path,
            log_edits=sc.log_edits,
            bundle_name=bundle_name,
            max_queue=max_queue,
            events_max_bytes=sc.events_max_bytes,
            events_keep=sc.events_keep,
            constraints=dict(sc.constraints) if sc.constraints else None,
        )

    def _resume_from_existing_session(self) -> None:
        """Adopt the highest seq/rev + the dedup map from an existing session.

        Called once from :meth:`for_bundle`. If no events.jsonl exists, the
        counters stay at 0 and the dedup map stays empty (fresh session).
        """
        try:
            max_seq = 0
            max_rev = 0
            for ev in _read_jsonl(self.events_path):
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
                rev = ev.get("rev")
                if isinstance(rev, int) and rev > max_rev:
                    max_rev = rev
            with self._lock:
                self.seq = max_seq
                self.bundle_rev = max_rev
            # Restore the cross-process dedup map.
            self._load_last_logged()
        except OSError:
            pass

    @property
    def events_path(self) -> Path:
        return self.session_dir / "events.jsonl"

    @property
    def directives_path(self) -> Path:
        return self.session_dir / "directives.jsonl"

    @property
    def presence_path(self) -> Path:
        return self.session_dir / "presence.json"

    @property
    def token_path(self) -> Path:
        """Per-session CSRF token file (current spec §14).

        ``serve`` writes the per-session token here (0o600) so an external
        agent can read it via ``okf token <bundle>`` or the library, then
        drive ``/__apply`` / ``/__presence`` / ``/__comment`` over HTTP.
        """
        return self.session_dir / ".token"

    @property
    def server_state_path(self) -> Path:
        """Live-server state file (``server.json``).

        ``serve`` writes ``{host, port, pid, started, url, tunnel_url}``
        here at startup (and refreshes it when a tunnel attaches/detaches)
        so session-aware CLI verbs — ``okf tunnel``, and anything else that
        must reach the RUNNING server over HTTP rather than the session
        files — can find the port without scanning processes. Removed on
        clean shutdown; a stale file (crash) is detected by the connect
        failing.
        """
        return self.session_dir / "server.json"

    @property
    def last_logged_path(self) -> Path:
        return self.session_dir / ".last-logged.json"

    @property
    def history_dir(self) -> Path:
        return self.session_dir / "history"

    # --- §9 idempotency cache (P1-11 / QUA idempotency-retry) -----------
    #
    # ``POST /__comment`` and ``POST /__undo`` accept an optional
    # ``Idempotency-Key`` header (RFC-style). Within a 10-minute window, a
    # re-POST with the same key + body returns the ORIGINAL response — no
    # duplicate directive, no double-apply. Records live in
    # ``.okf-loom/session/.idempotency/<key>.json`` and are written atomically
    # (tmp + rename) so a crash mid-write never produces a half record that
    # pins the key forever. The 10-minute TTL is intentionally short: it
    # covers the realistic network-retry window without becoming a permanent
    # duplicate-suppression table.
    IDEMPOTENCY_TTL_SECONDS: int = 600  # 10 minutes.

    @property
    def idempotency_dir(self) -> Path:
        return self.session_dir / ".idempotency"

    def _idempotency_path(self, key: str) -> Path:
        """Resolve the on-disk path for one idempotency record.

        The key is validated against the same segment regex as a concept-id
        segment (paths.validate_segment) plus colon + slash so common
        idempotency-key schemes (UUID v4 with dashes, ULIDs, ``<scope>:<id>``)
        are accepted without escaping. Path-traversal-shaped keys are
        rejected before they reach the path join (§9.5 path containment).
        """
        if not isinstance(key, str) or not key:
            raise ValueError("idempotency key must be a non-empty string")
        # Allow common idempotency-key characters: UUID dashes, colons,
        # underscores, dots. Reject path separators + leading dots.
        if not re.match(r"^[A-Za-z0-9._:-]{1,256}$", key) or key.startswith(".."):
            raise ValueError(f"unsafe idempotency key: {key!r}")
        return self.idempotency_dir / f"{key}.json"

    def idempotency_lookup(self, key: str, *, body: dict) -> dict | None:
        """Return the cached response for ``(key, body)`` if a fresh record
        exists, else ``None``.

        ``body`` is included in the cache key (via content-hash) so a key
        reused with DIFFERENT intent does not silently return the prior
        response (RFC 9110 idempotency-key semantics: same key + same
        intent = replay; same key + different intent = conflict, but we
        treat it as a fresh call to stay permissive).
        """
        try:
            path = self._idempotency_path(key)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        # TTL: drop records older than the window so a long-lived session
        # does not pin keys indefinitely.
        ts = record.get("ts")
        if not isinstance(ts, (int, float)) or time.time() - ts > self.IDEMPOTENCY_TTL_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        # Body fingerprint: same key + same body = replay.
        if record.get("body_fingerprint") != self._body_fingerprint(body):
            return None
        return record.get("response")

    def idempotency_store(self, key: str, *, body: dict, response: dict) -> None:
        """Atomically persist ``(key, body) → response`` for replay.

        SEC2-001: also opportunistically sweeps expired (TTL > 10 min) and
        over-cap (default 10000 files) cache entries so a buggy/authorized
        attacker can't exhaust inodes by spamming unique keys.
        """
        try:
            path = self._idempotency_path(key)
        except ValueError:
            return  # unsafe key: skip caching (the request itself still runs)
        self.idempotency_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "ts": time.time(),
            "body_fingerprint": self._body_fingerprint(body),
            "response": response,
        }
        atomic_write_text(
            path,
            json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str),
        )
        # SEC2-001: opportunistic sweep — cheap because it only reads mtime +
        # only fires on store (write-rate, not read-rate). Best-effort; an
        # OSError here just means we tried to sweep during a concurrent
        # cleanup.
        try:
            self._idempotency_sweep()
        except OSError:
            pass

    # SEC2-001 caps. 10-min TTL matches the lookup window; the file-count
    # cap bounds inode usage by a buggy or malicious in-loopback client.
    _IDEMPOTENCY_TTL_S: float = 600.0
    _IDEMPOTENCY_MAX_FILES: int = 10_000

    def _idempotency_sweep(self) -> None:
        """Delete expired + over-cap idempotency cache files (best-effort)."""
        try:
            files = list(self.idempotency_dir.glob("*.json"))
        except OSError:
            return
        if not files:
            return
        now = time.time()
        # Pass 1: drop expired.
        for f in files:
            try:
                if (now - f.stat().st_mtime) > self._IDEMPOTENCY_TTL_S:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
        # Pass 2: if still over cap, drop oldest by mtime.
        try:
            files = list(self.idempotency_dir.glob("*.json"))
        except OSError:
            return
        if len(files) <= self._IDEMPOTENCY_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for stale in files[:len(files) - self._IDEMPOTENCY_MAX_FILES]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _body_fingerprint(body: dict) -> str:
        """Stable content-hash of a request body for idempotency matching.

        Uses sha1 of the canonical JSON encoding so two semantically-
        identical bodies (key-order differences) fingerprint the same."""
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


    def ensure_session(self) -> None:
        """Create the session directory tree (idempotent).

        Also drops a self-contained .gitignore inside the bundle's
        .okf-loom/ workdir so session state (incl. the auth token) can
        never be committed, whatever repo the bundle lives in.
        """
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        from .io_utils import ensure_self_ignored
        ensure_self_ignored(self.session_dir)

    def _next_seq(self) -> int:
        with self._lock:
            self.seq += 1
            return self.seq

    def _next_bundle_rev(self) -> int:
        with self._lock:
            self.bundle_rev += 1
            return self.bundle_rev

    def current_rev(self) -> int:
        with self._lock:
            return self.bundle_rev

    # --- cross-process dedup map (P1-2) ---------------------------------

    def _load_last_logged(self) -> None:
        """Load the cross-process dedup map from disk (best-effort)."""
        try:
            if self.last_logged_path.is_file():
                data = json.loads(self.last_logged_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    with self._lock:
                        # Merge: in-memory wins for entries we have already
                        # mutated this process (we don't lose our own writes).
                        merged = {str(k): str(v) for k, v in data.items()
                                  if isinstance(v, (str, int))}
                        merged.update(self._last_logged_rev)
                        self._last_logged_rev = merged
        except (OSError, json.JSONDecodeError):
            pass

    def _save_last_logged(self) -> None:
        """Persist the dedup map atomically (best-effort, advisory-locked).

        QUA2-006 fix: MERGE with the on-disk map rather than overwrite, so a
        sibling process's entries (e.g. a concurrent ``okf watch``) are not
        lost when this process writes its own. Without the merge, cross-
        process dedup drifts and disk events get double-logged.
        """
        try:
            self.ensure_session()
            # Read the current on-disk map (best-effort), merge our in-memory
            # entries on top, then atomically write the merged result. The
            # advisory flock on events.jsonl is NOT taken here — the file is
            # tiny and the merge window is short; atomic_write_text prevents
            # tearing. A truly atomic cross-process merge would need a dedicated
            # flock on .last-logged.json, but the loss-of-entries failure mode
            # is the one we're fixing, and the merge handles it.
            existing: dict[str, str] = {}
            try:
                if self.last_logged_path.is_file():
                    data = json.loads(self.last_logged_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        existing = {
                            str(k): str(v) for k, v in data.items()
                            if isinstance(v, (str, int))
                        }
            except (OSError, json.JSONDecodeError):
                pass
            with self._lock:
                merged = dict(existing)
                merged.update(self._last_logged_rev)
                snapshot = dict(merged)
            atomic_write_text(
                self.last_logged_path,
                json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False),
            )
        except OSError:
            pass

    # --- events feed (§10.5) --------------------------------------------

    def append_event(self, event: dict[str, Any], *, publish: bool = True) -> dict[str, Any]:
        """Stamp, persist, and (optionally) broadcast a session event.

        Fills ``id``/``ts``/``seq`` if absent, appends to ``events.jsonl``
        under the advisory lock (with §10.5 rotation when configured), then
        publishes to the bus. Returns the fully attributed event (the dict
        callers hand to SSE clients).

        (§17 library API alias: ``events_append``.)
        """
        self.ensure_session()
        ev = dict(event)
        ev.setdefault("id", new_id())
        # Unconditional per-append identity for cross-process dedup. ``id``
        # can NOT serve: comment lifecycle events reuse the comment id (the
        # SSE contract), and ``seq`` is a per-instance counter that collides
        # across processes. See _event_dedup_key.
        ev["event_id"] = new_id()
        ev.setdefault("ts", _now_iso())
        ev.setdefault("seq", self._next_seq())
        ev.setdefault("rev", self._next_bundle_rev())
        _append_jsonl(
            self.events_path, ev, proc_lock=self._lock,
            max_bytes=self.events_max_bytes, keep=self.events_keep,
        )
        if publish:
            self.bus.publish(ev)
            # Track the event so the cross-process tail doesn't re-broadcast it.
            self._remember_published(_event_dedup_key(ev))
        return ev

    def _remember_published(self, key: str | None) -> None:
        """Track an event dedup key as published by THIS process.

        Keys come from :func:`_event_dedup_key` — ``event_id``-based, NOT
        ``id``-based. Comment lifecycle events deliberately reuse the
        COMMENT id as the event ``id`` (the SSE contract: the client's
        upsertComment keys on it), so an id-based set poisoned itself:
        once the creation event was published in-process, every later
        claim/resolve event for that comment carried the same id and was
        skipped by the tail forever (CLI state flips never reached open
        tabs). ``event_id`` is a fresh ULID stamped on every append.
        """
        if not key:
            return
        with self._lock:
            self._recently_published_events.add(key)
            self._recently_published_order.append(key)
            # Bound the ring at 256 entries.
            while len(self._recently_published_order) > 256:
                old = self._recently_published_order.pop(0)
                self._recently_published_events.discard(old)

    def tail_and_broadcast_cross_process_events(self) -> list[dict[str, Any]]:
        """Tail events.jsonl for entries written by ANOTHER process and
        broadcast them to THIS process's SSE subscribers.

        Cross-process SSE gap (INTENT2-001 / Bundle H finding): when a CLI
        mutator in a separate process calls save_concept, the activity /
        changed / graph events land in events.jsonl but the CLI's bus has no
        subscribers and this process's bus never saw the append. The watcher's
        disk-origin emit_change correctly DEDUPES the .md write (via
        .last-logged.json), but that leaves the browser with NO signal at
        all. This method tails events.jsonl past the highest id we've seen,
        skips ids we published ourselves (``_recently_published_events``),
        and broadcasts the rest. Returns the broadcast entries for inspection.

        Idempotent + bounded: reads at most the last 500 events from the
        feed (sufficient for any realistic burst), filters, publishes. Called
        from the watcher's ``_reload`` path when the bundle changed AND from
        its every-tick ``_tick`` path when events.jsonl's mtime moved
        (ARCH4-001 — session-only writes like a CLI claim/resolve/presence
        never touch a ``.md`` file).

        Dedup is by :func:`_event_dedup_key` (``event_id``-based). It was
        id-based, which silently dropped every comment LIFECYCLE event:
        comment events reuse the comment id as the event id (the SSE
        contract), so after the creation event was published in-process,
        the claim/resolve rows written by a CLI process carried an
        already-seen id and were never broadcast — an open tab never saw
        the state flip.
        """
        broadcast: list[dict[str, Any]] = []
        try:
            rows = list(_read_jsonl_with_rotations(self.events_path))
        except OSError:
            return broadcast
        with self._lock:
            seen = set(self._recently_published_events)
        # Walk newest-first. Skip events we've already seen (in-process
        # publishes) but CONTINUE past them — do NOT break — because
        # serve's own events (e.g. a ``graph`` event from the watcher's
        # disk diff) can land BETWEEN the CLI's older activity/changed
        # events and this tail call. Stopping at the first seen key (the
        # old behavior, ARCH3-001) skipped the CLI's events entirely.
        # The 500-row window bounds the work; the ``seen`` set prevents
        # double-broadcast within this call + across calls.
        for row in reversed(rows[-500:]):
            key = _event_dedup_key(row)
            if key is None:
                continue
            if key in seen:
                continue
            broadcast.append(row)
            seen.add(key)  # don't re-broadcast within this call
        # Broadcast oldest-first so the browser sees them in order.
        broadcast.reverse()
        for row in broadcast:
            try:
                self.bus.publish(row)
                self._remember_published(_event_dedup_key(row))
            except Exception:
                pass
        return broadcast

    # §17 library API alias.
    def events_append(self, event: dict[str, Any], *, publish: bool = True
                      ) -> dict[str, Any]:
        return self.append_event(event, publish=publish)

    def record_activity(self, *, actor: str, action: str, ids: list[str],
                        summary: str, origin: str = "mutator",
                        undoable: bool = False, group_id: str | None = None,
                        detail: dict[str, Any] | None = None,
                        emit_graph: bool | None = None) -> dict[str, Any]:
        """Record an agent/CLI activity entry (§7.2 ``activity`` / §12.2).

        When ``action`` is in :data:`_GRAPH_ACTIONS` and ``emit_graph`` is
        ``None`` (default), a follow-on ``graph`` event is emitted so the
        graph view invalidates its cached layout (P1-6). Set ``emit_graph``
        explicitly to override.
        """
        ev: dict[str, Any] = {
            "type": "activity",
            "actor": actor,
            "origin": origin,
            "action": action,
            "ids": list(ids),
            "summary": summary,
            "undoable": bool(undoable),
        }
        if group_id:
            _assert_safe_path_token("group_id", group_id)
            ev["group_id"] = group_id
        if detail:
            ev["detail"] = detail
        out = self.append_event(ev)
        # §7.2 graph event: after a graph-affecting activity, emit ``graph``
        # so the graph view patches (P1-6). One graph event per activity; the
        # graph view coalesces if several arrive close together.
        if emit_graph is True or (emit_graph is None and action in _GRAPH_ACTIONS):
            self.append_event({"type": "graph", "ids": list(ids), "origin": origin})
        return out

    def read_events(self, *, since: str | None = None, limit: int = 200,
                    actor: str | None = None, concept: str | None = None,
                    order: str = "asc") -> list[dict[str, Any]]:
        """Read + filter the events feed (§10.5 / §12.2 change-list source).

        Reads across rotated + active files (§10.5) so rotation does not
        truncate the visible history.

        ``order``:
          * ``"asc"`` (default): oldest-first, after the ``since`` cursor.
            Used by ``okf watch --since`` for replay (oldest unplayed → newest).
          * ``"desc"``: newest-first. Used by the change-list panel which
            must surface RECENT activity; on a busy session an asc limit
            returns the OLDEST N rows, hiding the agent's latest pass at the
            top (Bundle H finding). When ``order="desc"`` the ``since``
            cursor is ignored (it is an oldest-first concept).
        """
        rows = list(_read_jsonl_with_rotations(self.events_path))
        if order == "desc":
            # Newest-first: reverse the chronological order, then filter,
            # then take the leading limit. The since cursor is ignored
            # because it's anchored at "give me everything AFTER this id",
            # which is an oldest-first concept.
            rows.reverse()
            out: list[dict[str, Any]] = []
            for row in rows:
                if actor and row.get("actor") != actor:
                    continue
                if concept and concept not in (row.get("ids") or []):
                    continue
                out.append(row)
                if len(out) >= limit:
                    break
            return out
        # asc (default): oldest-first after the since cursor.
        out = []
        skipping = since is not None
        for row in rows:
            if skipping:
                if row.get("event_id") == since or row.get("id") == since:
                    skipping = False
                continue
            if actor and row.get("actor") != actor:
                continue
            if concept and concept not in (row.get("ids") or []):
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def emit_change(self, *, kind: str, ids: list[str], origin: str,
                    graph: bool = False) -> dict[str, Any]:
        """Emit a ``changed``/``created``/``removed``/``graph`` event (§7.2).

        Used by the watcher change-diff (origin ``disk``) and by mutator /
        undo callers (origin ``mutator`` / ``undo``). Per §10.5, a disk event
        whose concept content rev matches the most recent rev a mutator/undo
        already logged for that concept is de-duped (skipped) — so an agent or
        undo write that already logged itself is NOT re-logged when the
        watcher sees the mtime change.

        Mutator/undo callers populate the dedup map via :meth:`mark_logged`
        (called from :meth:`save_concept` and :meth:`restore_snapshot`);
        this method never dedupes a non-disk origin. If every id of a disk
        event is deduped, nothing is appended or published and a
        ``{"deduped": True}`` marker dict is returned (no caller inspects the
        return value today).

        Dedup is **cross-process** (P1-2): the dedup map is reloaded from
        ``.last-logged.json`` at the top of every disk emit so a separate
        ``okf watch`` process sees writes a sibling ``serve`` process logged.
        """
        # P1-2: refresh from disk so a separate process's writes are honored.
        if origin == "disk":
            self._load_last_logged()
        emit_ids = list(ids)
        if origin == "disk" and kind in ("changed", "created", "removed"):
            kept: list[str] = []
            for cid in emit_ids:
                with self._lock:
                    logged = self._last_logged_rev.get(cid)
                # Skip only when the id was logged by a mutator AND the
                # current on-disk rev still matches it (same content). A
                # missing/unreadable file → unknown rev → keep (log it).
                if logged is not None and self._current_disk_rev(cid) == logged:
                    continue
                kept.append(cid)
            emit_ids = kept
        if not emit_ids and not graph:
            return {"type": kind, "ids": [], "origin": origin, "graph": graph,
                    "deduped": True}
        return self.append_event(
            {"type": kind, "ids": emit_ids, "origin": origin, "graph": graph}
        )

    def mark_logged(self, concept_id: str, raw: str | bytes) -> None:
        """Record that ``concept_id`` was just written with content ``raw``.

        The watcher's disk-origin :meth:`emit_change` de-dupes an id whose
        current on-disk rev equals the value stored here (§10.5), so a write
        that already logged itself (a :meth:`save_concept` write or an
        :meth:`restore_snapshot` undo) is not re-logged when the watcher
        notices the mtime change.

        The map is also persisted atomically to ``.last-logged.json`` so a
        SEPARATE process (``serve`` + a concurrent ``okf watch``) shares the
        same dedup state — closing the P1-2 cross-process double-logging gap.
        """
        with self._lock:
            self._last_logged_rev[concept_id] = rev_of(raw)
        self._save_last_logged()

    def _current_disk_rev(self, concept_id: str) -> str | None:
        """Content-hash rev of ``concept_id``'s CURRENT on-disk bytes, or None.

        Used by :meth:`emit_change` to decide whether a disk event duplicates
        a write a mutator/undo already logged. Reads
        ``<bundle_root>/<concept_id>.md`` (SPEC §2 concept-id → path
        convention: the id is the file path with ``.md`` stripped). Returns
        ``None`` if the file is missing or unreadable so the caller falls
        through to logging — a missing/unreadable file must never suppress a
        real change event.
        """
        try:
            data = (self.bundle_root / f"{concept_id}.md").read_bytes()
        except OSError:
            return None
        return rev_of(data)

    # --- single internal write path (current spec §10/§13 ``save_concept``) -------

    @serialized
    def save_concept(self, *, concept_id: str, raw: str | bytes,
                     actor: str = "agent", action: str = "write_concept",
                     origin: str = "mutator",
                     group_id: str | None = None,
                     expected_rev: str | None = None,
                     summary: str | None = None,
                     detail: dict[str, Any] | None = None,
                     undoable: bool = True,
                     emit_graph: bool | None = None,
                     path: Path | None = None,
                     publish: bool = True,
                     delete: bool = False) -> "WriteResult":
        """The single internal atomic write path for the studio (current spec §10/§13).

        Every studio-aware write — CLI mutators (``okf link-add`` /
        ``entity-add`` / ``update`` / ``repair`` / ``write-concept`` /
        ``set-frontmatter``), the HTTP ``/__apply`` endpoint, and the
        ``/__undo`` restore — routes through this method so that:

        * the write is atomic (tmp + rename, AGENTS.md hard rule #8);
        * the prior bytes are snapshotted for one-click Undo (§12.5);
        * a group of writes (shared ``group_id``) is undoable as one pass;
        * an attributed ``activity`` event is appended to ``events.jsonl``
          with actor/origin/action/ids/summary (so the change list shows
          *who* did *what*, not just *something changed*);
        * the new content-hash rev is marked logged so the watcher's disk
          emit_change de-dupes it (no double-logging);
        * a `changed` event (and `graph` for graph-affecting ops) is
          broadcast over SSE so every open tab patches in place with no
          refresh.

        Args:
            concept_id: canonical concept id (e.g. ``tables/orders``). Used
                for path resolution when ``path`` is None, and for activity
                attribution.
            raw: the new file bytes (str or bytes).
            actor: who is doing the write (default ``agent``). Used in the
                activity event.
            action: short op name for the activity event (e.g.
                ``add_link``, ``add_entity``, ``write_concept``,
                ``undo_restore``). Drives the auto-emit-``graph`` heuristic
                when ``emit_graph`` is None.
            origin: where the write came from (``mutator`` / ``http-apply``
                / ``undo`` / ``auto-repair``). Used in the activity event
                and the change event.
            group_id: optional group manifest id. When set, the snapshot is
                added to ``history/_groups/<group_id>/manifest.json`` so
                the whole pass reverts in one click (§12.5).
            expected_rev: when set, the on-disk content-hash rev is compared
                to this value BEFORE the write. A mismatch means someone
                (usually the user, editing on disk) changed the file
                between read-time and write-time — the §9.3/§9.4 collision
                case. The function returns ``WriteResult(conflict=True)``
                WITHOUT writing, so the caller can surface the conflict
                instead of silent-clobbering (§9 acceptance: "no silent
                clobber").
            summary: human-readable one-liner for the activity event
                (default: ``f"{action} on {concept_id}"``).
            detail: op-specific detail dict for the activity event
                (target concept, link type, args, etc.).
            undoable: when True (default), snapshot the prior bytes for
                Undo. Set False for ops that shouldn't get an Undo button
                (e.g. ``repair`` that touches many files atomically — the
                caller may prefer a single group-undo instead).
            emit_graph: override the auto-emit-``graph`` heuristic. None
                means "auto: emit ``graph`` iff ``action in _GRAPH_ACTIONS``".
            path: optional explicit file path. When None, the path is
                derived from ``bundle_root / f"{concept_id}.md"``.
            publish: when False, skip the SSE broadcast (used by callers
                that batch many writes and broadcast once at the end).

        Returns:
            A :class:`WriteResult`. On conflict, ``conflict=True`` and
            ``current_rev`` is the on-disk rev that mismatched; the write
            did NOT happen. On success, ``ok=True``, ``rev`` is the new
            content-hash, ``activity_id`` is the appended event id, and
            ``snap_rev`` is the prior-bytes rev (for explicit Undo lookup).
        """
        from .paths import concept_id_from_str, concept_id_to_path, ConceptIdError  # late import

        # Validate the concept_id lexically BEFORE any filesystem op so a
        # malformed id (HTTP-supplied) cannot reach the path join. The path is
        # built from the validated tuple, never the raw string: a raw id like
        # "/etc/x" survives validation (empty segments are dropped) but would
        # make a raw-string join absolute and escape the bundle.
        try:
            cid = concept_id_from_str(concept_id)
        except ConceptIdError as e:
            return WriteResult(ok=False, conflict=False, error=f"bad concept id: {e}")
        target_path = path if path is not None else concept_id_to_path(self.bundle_root, cid)
        try:
            target_path.resolve().relative_to(self.bundle_root.resolve())
        except (ValueError, OSError, RuntimeError):
            return WriteResult(ok=False, error="target escapes bundle", concept_id=concept_id)
        if isinstance(raw, str):
            raw_bytes = raw.encode("utf-8")
        else:
            raw_bytes = raw

        # Read prior bytes (None if file doesn't exist yet — created case).
        prior_bytes: bytes | None = None
        if target_path.is_file():
            try:
                prior_bytes = target_path.read_bytes()
            except OSError as exc:
                return WriteResult(ok=False, error=f"cannot read prior content: {exc}",
                                   concept_id=concept_id)
        if delete and prior_bytes is None:
            return WriteResult(ok=False, conflict=True, error="already absent", concept_id=concept_id)

        # §9.3/§9.4 collision guard. expected_rev is the content-hash the
        # caller saw at read-time; if the on-disk rev has moved, someone
        # (the user, in their editor) edited the file concurrently. We do
        # NOT clobber; we return conflict and let the caller surface it.
        if expected_rev is not None:
            current_rev = rev_of(prior_bytes) if prior_bytes is not None else None
            if current_rev != expected_rev:
                return WriteResult(
                    ok=False, conflict=True,
                    expected_rev=expected_rev, current_rev=current_rev,
                    concept_id=concept_id,
                )

        # §12.3 opt-in forbidden_paths constraint. The user opt-in only;
        # default empty (fully free agent, §1.1/D8).
        forbidden = self.constraints.get("forbidden_paths") or []
        if isinstance(forbidden, list) and forbidden:
            import fnmatch
            for pat in forbidden:
                if fnmatch.fnmatch(concept_id, str(pat)):
                    return WriteResult(
                        ok=False, conflict=False,
                        error=f"forbidden_paths constraint blocked {concept_id!r}",
                        concept_id=concept_id,
                    )

        # §12.3 opt-in max_edits_per_action constraint. Counts writes that
        # share a group_id; the (N+1)th is refused. Per-action only; not a
        # global cap. The user opt-in; default absent (no cap).
        # QUA4-007 fix: hold _lock across the cap check AND pre-reserve a
        # slot so concurrent writes can't both pass the cap. The RLock is
        # re-entrant so this doesn't deadlock against _record_group_member
        # (which also takes _lock). We pre-reserve by writing the member
        # entry NOW (inside the lock); if save_concept fails downstream,
        # the extra entry is harmless (group-undo just restores one fewer).
        if group_id is not None:
            cap = self.constraints.get("max_edits_per_action")
            if isinstance(cap, int) and cap > 0:
                with self._lock:
                    members = self._read_group_manifest_locked(group_id) or []
                    if len(members) >= cap:
                        return WriteResult(
                            ok=False, conflict=False,
                            error=f"max_edits_per_action ({cap}) reached for group {group_id!r}",
                            concept_id=concept_id,
                        )

        # Snapshot prior bytes for Undo BEFORE the write (§12.5). Atomic.
        snap_rev: str | None = None
        if undoable and prior_bytes is not None:
            snap_rev = self.snapshot_for_undo(
                concept_id=concept_id, raw=prior_bytes, group_id=group_id,
            )
        elif undoable and prior_bytes is None:
            # Absence is distinct from an existing empty file. A tombstone
            # revision makes creation undoable without corrupting the bundle.
            snap_rev = self.snapshot_for_undo(
                concept_id=concept_id, raw=b"", group_id=group_id, existed=False,
            )

        # Atomic write (AGENTS.md hard rule #8).
        if delete:
            target_path.unlink()
        else:
            atomic_write_bytes(target_path, raw_bytes)

        # Attribute + log + broadcast (the heart of the funnel).
        new_rev = "absent" if delete else rev_of(raw_bytes)
        if not delete:
            self.mark_logged(concept_id, raw_bytes)
        activity_summary = summary or f"{action} on {concept_id}"
        activity_detail = dict(detail or {})
        if snap_rev is not None:
            activity_detail.setdefault("before", snap_rev)
            activity_detail.setdefault("before_concept", concept_id)
        activity = self.record_activity(
            actor=actor, action=action, ids=[concept_id],
            summary=activity_summary, origin=origin,
            undoable=undoable, group_id=group_id, detail=activity_detail,
            emit_graph=emit_graph,
        )
        if publish:
            self.emit_change(kind="removed" if delete else "changed", ids=[concept_id], origin=origin)
            # The watcher's disk emit for THIS rev will dedupe (mark_logged
            # above). For non-disk origins the change event is the canonical
            # broadcast.
        return WriteResult(
            ok=True, conflict=False, rev=new_rev, concept_id=concept_id,
            activity_id=activity.get("id"), snap_rev=snap_rev,
        )

    # --- undo (§12.5) ---------------------------------------------------

    @serialized
    def restore_snapshot(self, *, concept_id: str, rev: str,
                         actor: str = "agent", origin: str = "undo",
                         group_id: str | None = None,
                         publish: bool = True) -> "WriteResult":
        """Restore a prior snapshot through :meth:`save_concept` (§12.5).

        The restore is itself an attributed, undoable write — undoing a
        change is itself a change that appears in the change list (so the
        user can redo by undoing the undo). Reads the prior snapshot bytes
        and routes them through ``save_concept(action="undo_restore")``.
        """
        raw = self.undo_snapshot(concept_id=concept_id, rev=rev)
        if raw is None:
            return WriteResult(
                ok=False, conflict=False,
                error=f"no snapshot for {concept_id} @ {rev}",
                concept_id=concept_id,
            )
        return self.save_concept(
            concept_id=concept_id, raw=raw, actor=actor,
            action="undo_restore", origin=origin,
            group_id=group_id, undoable=True,
            summary=f"undo {concept_id} @ {rev}", publish=publish,
            delete=rev == "absent",
        )

    # --- comments / directives (§9) -------------------------------------

    @serialized
    def post_comment(self, *, concept: str, body: str,
                     anchor: dict[str, Any] | None = None,
                     actor: str = "user", detail: dict[str, Any] | None = None,
                     parent_id: str | None = None,
                     state: str = "open") -> dict[str, Any]:
        """Append a user comment/ask as an ``open`` directive (§9.1).

        ``parent_id`` (optional) creates a threaded reply to a parent
        comment. The comment is anchored to the same concept as the parent.

        When replying to an archived parent, the parent is
        auto-unarchived (archive is a soft hide, not a closed state). The
        new reply itself is never archived.

        ``state`` may be ``"open"`` (default — an ask that is work for the
        agent) or ``"resolved"`` (a statement that needs no follow-up).
        Agent-authored replies posted via ``okf comment-reply`` use
        ``"resolved"`` so ``wait --for comment`` never hands the agent its
        own reply back as new work, and the thread-archive gate (every
        comment resolved) stays satisfiable without the agent resolving
        its own remarks.
        """
        if state not in ("open", "resolved"):
            raise ValueError(f"comment state must be open|resolved, got {state!r}")
        self.ensure_session()
        # Auto-unarchive parent on reply: adding to an archived thread
        # reopens it for business.
        if parent_id:
            parent = self.get_comment(parent_id)
            if parent and parent.get("archived"):
                self.update_comment(parent_id, archived=False)
        cid = new_id()
        now = _now_iso()
        # Auto-derive request_summary from body (short version of
        # the ask) so the collapsed preview has a useful short from the
        # moment the comment is posted, before the agent claims/resolves.
        import re as _re
        _body_trim = body.strip()
        _req_sum = ""
        if _body_trim:
            _parts = _re.split(r"[.!?\n]", _body_trim, maxsplit=1)
            _first = (_parts[0] if _parts else _body_trim).strip() or _body_trim
            if len(_first) > 100:
                _first = _first[:100].rstrip() + "\u2026"
            _req_sum = _first
        directive = {
            "id": cid,
            "ts": now,
            "updated_at": now,
            "concept": concept,
            "anchor": anchor or {},
            "actor": actor,
            "body": body,
            "state": state,
            "claimed_by": actor if state == "resolved" else None,
            "resolved_activity": [],
            "detail": detail or {},
            "archived": False,
            "request_summary": _req_sum,
        }
        if parent_id:
            directive["parent_id"] = parent_id
        # SEC2-002: rotate directives.jsonl too (default 2 MiB / 3 files) so
        # comment spam can't fill the disk. directives are kept longer than
        # events because comment state is the source of truth for the UI's
        # comment panel — but rotation preserves the most recent records.
        _append_jsonl(self.directives_path, directive, proc_lock=self._lock,
                      max_bytes=2 * 1024 * 1024, keep=3)
        # Broadcast a comment event so the UI + agent see it live. Carries
        # ``actor`` + ``parent_id`` so a tab that did NOT post the comment
        # still threads a reply under its parent (before this, only the
        # posting tab knew — the SSE payload lacked parent_id, so other
        # open tabs rendered replies as roots until a reload).
        self.append_event({
            "type": "comment", "id": cid, "concept": concept,
            "anchor": anchor or {}, "state": state,
            "claimed_by": directive["claimed_by"],
            "actor": actor,
            "body": body, "resolved_activity": [],
            "archived": False, "updated_at": now, "ts": now,
            "request_summary": _req_sum,
            **({"parent_id": parent_id} if parent_id else {}),
        })
        return directive

    def list_comments(self, *, state: str | None = None,
                      concept: str | None = None) -> list[dict[str, Any]]:
        """List directives, filtered by state / concept (§9.1).

        ``directives.jsonl`` is append-only: a lifecycle transition appends a
        new full record for the same id. We keep only the **latest** record
        per id (last write wins), so a resolved comment does not also show as
        open.

        Compatibility migration: older records with ``state == "archived"``
        (pre-separate-track archive) are normalized to ``archived=True,
        state="open"`` on read so the UI's archive filter works uniformly.
        """
        latest: dict[str, dict[str, Any]] = {}
        for row in _read_jsonl_with_rotations(self.directives_path):
            latest[row.get("id", "")] = row
        out: list[dict[str, Any]] = []
        for row in latest.values():
            row = _normalize_comment_row(row)
            if state and row.get("state") != state:
                continue
            if concept and row.get("concept") != concept:
                continue
            out.append(row)
        return out

    def get_comment(self, comment_id: str) -> dict[str, Any] | None:
        # Append-only feed: the latest record for the id is the current state.
        latest: dict[str, Any] | None = None
        for row in _read_jsonl_with_rotations(self.directives_path):
            if row.get("id") == comment_id:
                latest = row
        if latest is None:
            return None
        return _normalize_comment_row(latest)

    @serialized
    def update_comment(self, comment_id: str, *, state: str | None = None,
                       claimed_by: str | None = None,
                       resolved_activity: list | None = None,
                       reply: str | None = None,
                       archived: bool | None = None,
                       summary: str | None = None,
                       request_summary: str | None = None,
                       body: str | None = None) -> dict[str, Any] | None:
        """Transition a comment's lifecycle (§9.1): claim / resolve / dismiss.

        ``directives.jsonl`` is append-only; a transition appends a new record
        for the same ``id`` carrying the new state, and :meth:`list_comments`
        / :meth:`get_comment` return the latest record per id.

        ``archived`` is a SEPARATE track from ``state``. Archiving a
        comment does NOT change its open/resolved/dismissed state — it only
        flips a boolean that the UI uses to hide the thread. Unarchiving
        restores the prior visibility without touching state. ``summary`` is
        a short agent-authored blurb (set on claim/resolve) shown in the
        collapsed preview. ``updated_at`` is bumped on every transition;
        ``ts`` is preserved as the creation timestamp.

        TWO summary fields are semantically distinct:
          * ``request_summary`` — short of what was ASKED. Set explicitly
            by ``okf comment-claim --summary`` (claim-time = "what the
            user wants"). Auto-derived from body if never set (see
            ``_normalize_comment_row``).
          * ``summary`` — short of what the agent DID. Set by
            ``okf comment-resolve --summary`` (resolve-time = "what the
            agent did"). Empty until resolve.
        The collapsed preview shows BOTH so the panel reads as a
        ask→done status board.
        """
        current = self.get_comment(comment_id)
        if current is None:
            return None
        if state == "claimed":
            if current.get("state") in ("resolved", "dismissed"):
                raise ValueError("claim_conflict: comment is no longer open")
            if (current.get("state") == "claimed"
                    and current.get("claimed_by") not in (None, claimed_by)):
                raise ValueError("claim_conflict: comment is owned by another agent")
        updated = dict(current)
        if state is not None:
            updated["state"] = state
        if claimed_by is not None:
            updated["claimed_by"] = claimed_by
        if resolved_activity is not None:
            updated["resolved_activity"] = list(resolved_activity)
        if reply is not None:
            updated["reply"] = reply
        if archived is not None:
            updated["archived"] = bool(archived)
        if summary is not None:
            updated["summary"] = summary
        if request_summary is not None:
            updated["request_summary"] = request_summary
        if body is not None and body != updated.get("body"):
            # User-side comment edit (the enter-too-soon fix). The edit
            # bumps updated_at, so an agent blocked in ``wait`` re-receives
            # the still-open comment with the corrected body. Re-derive
            # request_summary from the new body unless this call also sets
            # it explicitly — the stored short must describe the CURRENT
            # ask, not the pre-edit one.
            updated["body"] = body
            if request_summary is None:
                updated["request_summary"] = ""
        # Preserve creation ts; bump updated_at on every transition.
        if "ts" not in updated:
            updated["ts"] = _now_iso()
        updated["updated_at"] = _now_iso()
        # Ensure request_summary is present (auto-derive on write so the
        # stored record is self-contained; the read-side normalize is the
        # backstop for older rows).
        if not updated.get("request_summary"):
            body = (updated.get("body") or "").strip()
            if body:
                import re as _re
                parts = _re.split(r"[.!?\n]", body, maxsplit=1)
                first = (parts[0] if parts else body).strip() or body
                if len(first) > 100:
                    first = first[:100].rstrip() + "\u2026"
                updated["request_summary"] = first
            else:
                updated["request_summary"] = ""
        _append_jsonl(self.directives_path, updated, proc_lock=self._lock)
        self.append_event({
            "type": "comment", "id": comment_id, "concept": updated.get("concept", ""),
            "anchor": updated.get("anchor", {}), "state": updated.get("state", "open"),
            "claimed_by": updated.get("claimed_by"), "body": updated.get("body", ""),
            "resolved_activity": updated.get("resolved_activity", []),
            "reply": updated.get("reply"),
            "archived": updated.get("archived", False),
            "summary": updated.get("summary"),
            "request_summary": updated.get("request_summary"),
            "updated_at": updated.get("updated_at"),
            "ts": updated.get("ts"),
            # actor + parent_id keep non-posting tabs' comment trees correct
            # (same rationale as post_comment's SSE payload).
            "actor": updated.get("actor"),
            **({"parent_id": updated["parent_id"]} if updated.get("parent_id") else {}),
        })
        return updated

    # §17 library API alias — explicit "resolve" verb for the agent loop.
    @serialized
    def resolve_comment(self, comment_id: str, *,
                        reply: str | None = None,
                        activity_ids: list[str] | None = None,
                        actor: str = "agent",
                        summary: str | None = None) -> dict[str, Any] | None:
        """Mark a comment ``resolved`` with optional reply + activity links.

        §17 names this as the programmatic equivalent of the HTTP agent loop:
        ``okf comment-claim <bundle> <id>`` → agent does its scoped work →
        ``okf comment-resolve <bundle> <id> --reply … --activity <id>[,<id>]``.

        INTENT2-007 (iter-3): also back-stamps ``comment_id`` on each named
        activity event in events.jsonl so the change list can render a
        back-link ("resolved comment X"). The back-stamp is a small
        ``comment_link`` event that carries the (activity_id, comment_id)
        pair; the UI reads it to render the back-link.

        ``summary`` is a short agent-authored blurb shown in the
        collapsed thread preview. Separate from ``reply`` (which is the
        long-form resolution message) so the panel can show a quick tag
        without expanding.
        """
        updated = self.update_comment(
            comment_id, state="resolved",
            claimed_by=actor,
            resolved_activity=activity_ids,
            reply=reply,
            summary=summary,
        )
        if updated is not None and activity_ids:
            # INTENT2-007: back-stamp comment_id on each activity event so
            # the change list can link back to the originating comment.
            for aid in activity_ids:
                try:
                    self.append_event({
                        "type": "comment_link",
                        "activity_id": aid,
                        "comment_id": comment_id,
                        "concept": updated.get("concept", ""),
                    })
                except Exception:
                    pass  # best-effort; the resolve itself succeeded
        return updated

    # --- presence (current spec §12) --------------------------------------

    def set_presence(self, *, actor: str = "agent", state: str = "idle",
                     focus: str | None = None,
                     message: str | None = None) -> dict[str, Any]:
        """Set + broadcast agent presence (current spec §12).

        INTENT2-008 (iter-3): records a timestamp so the staleness sweep
        can revert to ``idle`` when the agent process drops (§13.8 robust
        liveness). The TTL is configurable via ``self.presence_ttl_s``
        (default 300s = 5 min).

        ``message`` is an optional short free-text progress line ("linking
        3 of 7 tables…") shown next to the state chip, so a long
        multi-concept pass reads as visible progress instead of a static
        "editing". Capped at 200 chars; re-post to update it mid-pass.
        """
        presence = {"actor": actor, "state": state}
        if focus:
            presence["focus"] = focus
        if message:
            msg = str(message).strip()
            if len(msg) > 200:
                msg = msg[:200].rstrip() + "…"
            if msg:
                presence["message"] = msg
        with self._lock:
            self.presence = presence
            self._presence_ts = time.time()
        # Persist (best-effort; presence is ephemeral). atomic_write_text
        # uses a unique mkstemp tmp so concurrent writers (serve process vs
        # `okf presence` CLI) can't clobber each other's tmp file.
        try:
            self.ensure_session()
            atomic_write_text(self.presence_path, json.dumps(presence))
        except OSError:
            pass
        self.append_event({"type": "presence", "actor": actor, "state": state,
                           **({"focus": focus} if focus else {}),
                           **({"message": presence["message"]}
                              if presence.get("message") else {})})
        return presence

    # §17 library API alias.
    def post_presence(self, *, actor: str = "agent", state: str = "idle",
                      focus: str | None = None,
                      message: str | None = None) -> dict[str, Any]:
        return self.set_presence(actor=actor, state=state, focus=focus,
                                 message=message)

    def get_presence(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.presence)

    def sweep_stale_presence_and_claims(self) -> dict[str, list[str]]:
        """§13.8 robust-liveness staleness sweep (INTENT2-008).

        ARCH5-001 fix (iter-5): also check presence.json's mtime as a
        cross-process freshness signal. The CLI agent's set_presence
        writes presence.json cross-process, but serve's in-process
        _presence_ts was never updated by that write — so serve would
        falsely revert a LIVE CLI agent to idle after the TTL. By also
        reading presence.json's mtime, the sweep correctly tracks the
        CLI agent's last-known-alive time.
        """
        result: dict[str, Any] = {"presence_reverted": False, "claims_reverted": []}
        now = time.time()
        # ARCH5-001: check presence.json mtime for cross-process freshness.
        try:
            if self.presence_path.is_file():
                file_ts = self.presence_path.stat().st_mtime
                with self._lock:
                    if file_ts > self._presence_ts:
                        self._presence_ts = file_ts  # CLI agent is alive
        except OSError:
            pass
        # Presence TTL: revert to idle if stale.
        with self._lock:
            stale = (
                self.presence.get("actor") == "agent"
                and self.presence.get("state") in ("editing", "thinking", "watching")
                and (now - self._presence_ts) > self.presence_ttl_s
            )
        if stale:
            self.set_presence(actor="agent", state="idle")
            result["presence_reverted"] = True
        # Claim TTL: revert stale claims to open.
        try:
            for c in self.list_comments(state="claimed"):
                ts_str = c.get("ts", "")
                # ts is ISO-8601; parse it to a epoch float.
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    claimed_ts = dt.timestamp()
                except (ValueError, OSError):
                    continue  # unparseable ts; skip
                if (now - claimed_ts) > self.claim_ttl_s:
                    self.update_comment(c["id"], state="open", claimed_by=None)
                    result["claims_reverted"].append(c["id"])
        except Exception:
            pass  # best-effort; the sweep must never crash the watcher
        return result

    # --- §12.3 opt-in constraints (post_suggestion) ---------------------

    def post_suggestion(self, *, concept: str, action: str, args: dict[str, Any],
                        summary: str, group_id: str | None = None,
                        actor: str = "agent") -> dict[str, Any]:
        """Append a proposal to ``proposals.jsonl`` (§12.3 opt-in propose_only).

        When the user has opted into ``studio.constraints.propose_only: true``,
        the agent writes PlannedAction envelopes here instead of writing
        files. A ``suggestions`` SSE event fires; a review panel (built via
        the client extension API) accepts/dismisses. Accept → ``POST /__apply``
        runs the whitelisted UpdateOp via :meth:`save_concept` (§12.3).

        No-op (returns ``{}``) when ``propose_only`` is not enabled, so the
        agent's normal ``save_concept`` writes apply directly (§1.1 / D8).
        """
        if not (self.constraints.get("propose_only") is True):
            return {}
        self.ensure_session()
        proposal = {
            "id": new_id(),
            "ts": _now_iso(),
            "concept": concept,
            "action": action,
            "args": dict(args),
            "summary": summary,
            "actor": actor,
            "state": "open",
            "group_id": group_id,
        }
        path = self.session_dir / "proposals.jsonl"
        # SEC2-008: rotate proposals.jsonl (default 1 MiB / 2 files). Smaller
        # than directives because proposals are an opt-in (propose_only) stream.
        _append_jsonl(path, proposal, proc_lock=self._lock,
                      max_bytes=1 * 1024 * 1024, keep=2)
        self.append_event({"type": "suggestions", "proposal": proposal["id"],
                           "concept": concept})
        return proposal

    # --- undo (§12.5) ---------------------------------------------------

    def snapshot_for_undo(self, *, concept_id: str, raw: str | bytes,
                          group_id: str | None = None, existed: bool = True) -> str:
        """Snapshot prior concept bytes for undo; returns the ``rev`` (§12.5).

        Stored at ``history/<safe_cid>/<rev>.md`` (capped ring per concept).
        When ``group_id`` is set, the snapshot is also listed in the group
        manifest so a whole agent pass reverts in one click.

        The snapshot write is ATOMIC (P1-4, AGENTS.md hard rule #8): a crash
        mid-write leaves no torn snapshot that could silently corrupt a
        future undo. The group manifest write is likewise atomic.
        """
        rev = rev_of(raw) if existed else "absent"
        # F8: reversible, collision-free encoding. ``concept_id.replace("/",
        # "__")`` was non-injective — ``a__b`` and ``a/b`` mapped to the same
        # history dir. Hex of the UTF-8 bytes is injective (and mirrors
        # :meth:`undo_snapshot`), so every concept id gets a unique dir. The
        # group manifest stores the PLAIN concept id, so group undo does not
        # need the encoding.
        _assert_safe_path_token("rev", rev)
        safe = concept_id.encode("utf-8").hex() or "_root"
        d = self.history_dir / safe
        d.mkdir(parents=True, exist_ok=True)
        # P1-4: atomic write for the snapshot itself.
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        atomic_write_bytes(d / f"{rev}.md", raw_bytes)
        # Cap the ring: keep the most recent N snapshots per concept.
        _cap_history_ring(d, limit=50)
        if group_id:
            self._record_group_member(group_id, concept_id, rev)
        return rev

    def _record_group_member(self, group_id: str, concept_id: str, rev: str) -> None:
        # QUA2-001 fix: hold self._lock for the read-modify-write of the group
        # manifest. Without this, two concurrent save_concept(group_id="G1")
        # calls both read the current manifest, both append, and the second
        # atomic_write_text overwrites the first → lost group member → broken
        # group-undo. The lock serializes same-process writers; cross-process
        # safety would need a dedicated flock on the manifest file.
        _assert_safe_path_token("group_id", group_id)
        _assert_safe_path_token("rev", rev)
        with self._lock:
            gdir = self.history_dir / "_groups" / group_id
            gdir.mkdir(parents=True, exist_ok=True)
            manifest = gdir / "manifest.json"
            members: list[dict[str, str]] = []
            if manifest.is_file():
                try:
                    members = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    members = []
            entry = {"concept": concept_id, "rev": rev}
            if entry not in members:
                members.append(entry)
            # P1-4: atomic manifest write (tmp + rename) — INSIDE the lock so
            # the read-modify-write is serialized.
            atomic_write_text(
                manifest,
                json.dumps(members, separators=(",", ":"), ensure_ascii=False),
            )

    def undo_snapshot(self, *, concept_id: str, rev: str) -> str | None:
        """Read a prior snapshot's raw bytes for restore (§12.5)."""
        # P2-1: validate rev before it reaches the path join.
        _assert_safe_path_token("rev", rev)
        # F8: must mirror snapshot_for_undo's reversible hex encoding so the
        # dir written there is found here.
        safe = concept_id.encode("utf-8").hex() or "_root"
        f = self.history_dir / safe / f"{rev}.md"
        if not f.is_file():
            return None
        return f.read_text(encoding="utf-8")

    def undo_group(self, group_id: str) -> list[dict[str, str]] | None:
        """Return the members of a group for whole-pass undo (§12.5)."""
        _assert_safe_path_token("group_id", group_id)
        with self._lock:
            return self._read_group_manifest_locked(group_id)

    def _read_group_manifest_locked(self, group_id: str) -> list[dict[str, str]] | None:
        """Read a group's manifest WITHOUT taking ``self._lock``.

        Caller MUST already hold ``self._lock``. Used internally by
        :meth:`save_concept`'s ``max_edits_per_action`` cap check (QUA2-004)
        so it doesn't self-deadlock against :meth:`_record_group_member`
        (which also takes the lock).
        """
        manifest = self.history_dir / "_groups" / group_id / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            return json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


def _cap_history_ring(d: Path, *, limit: int) -> None:
    """Keep only the most recent ``limit`` snapshots in a concept history dir."""
    try:
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[limit:]:
        try:
            stale.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Agent foreground wait primitive (current spec §12 — the agent loop)
# ---------------------------------------------------------------------------

def wait_for_work(
    bundle_root: str | Path,
    *,
    kinds: tuple[str, ...] = ("comment",),
    since: str | None = None,
    timeout: float | None = None,
    interval: float = 0.5,
) -> dict[str, Any] | None:
    """Block until there is new work for the agent, then return it (current spec §12).

    This is the **agent's foreground wait primitive**: the agent (running in
    an opencode / Claude Code / Codex session) calls this, it BLOCKS until a
    new open comment and/or a new change event appears, returns that one item,
    and exits — so the agent wakes, acts on it (edits via mutators, resolves
    the comment), then calls ``wait_for_work`` again. It MUST run in the
    agent's foreground (the agent is the processor); never background it — a
    backgrounded wait has no consumer for its output.

    **Pre-existing open comments** (current spec §12): on startup, if there are
    already open comments the agent hasn't seen, the function returns the
    FIRST one immediately (it doesn't skip them by establishing a baseline
    at the latest id). Subsequent calls process the rest, then wait for
    truly new work. This means the agent loop naturally drains any backlog
    on first run, then settles into wait-for-new mode.

    Args:
        bundle_root: path to the OKF bundle.
        kinds: what to wait for — ``"comment"`` (a new OPEN user comment) and/or
            ``"change"`` (a new changed/created/removed event). Default waits
            for a new open comment (the user's ask).
        since: only items whose feed id sorts after this are considered. If
            ``None`` (default), pre-existing open comments are returned first
            (backlog drain); once the backlog is empty, only truly new work
            is returned.
        timeout: max seconds to wait (``None`` = block indefinitely until
            work arrives). On timeout, returns ``None``.
        interval: seconds between feed polls (default 0.5).

    Returns:
        The first matching item (a comment dict for ``"comment"``, an event
        dict for ``"change"``), or ``None`` on timeout.
    """
    import time as _time

    root = Path(bundle_root)
    studio = Studio.for_configured_bundle(root)
    want_comment = "comment" in kinds
    want_change = "change" in kinds

    # --- Pre-existing open comments (backlog drain) ---
    # On startup, if since is not pinned, return the FIRST open comment
    # immediately. This lets the agent pick up work that was posted before
    # it started, rather than skipping it. Subsequent calls drain the rest,
    # then the baseline logic below kicks in for truly new work.
    # The one-work-item contract stands (block once, print one item, exit),
    # but the returned comment carries a ``queue`` field — the OTHER open
    # comments still pending — so the agent can see backlog depth without
    # diffing ``comment-list`` between loop turns.
    def _with_queue(comment: dict[str, Any]) -> dict[str, Any]:
        pending = sorted(
            (c for c in studio.list_comments(state="open")
             if c.get("id") != comment.get("id")),
            key=lambda c: c.get("ts", ""),
        )
        return {
            "kind": "comment", **comment,
            "queue": {"pending": len(pending),
                      "ids": [c.get("id") for c in pending]},
        }

    if want_comment and since is None:
        open_comments = studio.list_comments(state="open")
        if open_comments:
            # Return the oldest open comment first (FIFO backlog drain).
            open_comments.sort(key=lambda c: c.get("ts", ""))
            return _with_queue(open_comments[0])

    # Establish the baseline: the latest ids currently in each feed, so we only
    # return work that is NEW since the call began (unless `since` pins it).
    def _latest_id(path: Path) -> str | None:
        last = None
        for row in _read_jsonl_with_rotations(path):
            if row.get("id"):
                last = row["id"]
        return last

    # P2-18: for comments, also track the latest DIRECTIVE RECORD ts. A
    # comment that is open → resolved → re-opened keeps the SAME id but
    # gains a new ts (the re-open transition appends a new directives.jsonl
    # record). Filtering by id alone would miss the re-open. We track the
    # latest record ts and treat any open comment whose latest ts is newer
    # as fresh work — so a re-opened comment wakes the agent again.
    #
    # ``ts`` is the IMMUTABLE creation timestamp; ``updated_at`` is the field
    # bumped on every transition. We check both for back-compat with records
    # written before the separate updated_at field existed.
    def _latest_directive_ts(path: Path) -> str | None:
        last = None
        for row in _read_jsonl_with_rotations(path):
            ts = row.get("updated_at") or row.get("ts")
            if isinstance(ts, str):
                last = ts
        return last

    base_comment = since if (since and want_comment) else _latest_id(studio.directives_path)
    base_comment_ts = _latest_directive_ts(studio.directives_path)
    base_event = since if (since and want_change) else _latest_id(studio.events_path)

    deadline = None if timeout is None else (_time.monotonic() + timeout)
    while True:
        if want_comment:
            for c in studio.list_comments(state="open"):
                # A comment is "new" if its id is newer than the baseline
                # (a fresh ask) OR its latest updated_at is newer than the
                # baseline (a re-open of a previously-resolved comment, P2-18).
                c_ts = c.get("updated_at") or c.get("ts")
                is_new_id = base_comment is None or c.get("id", "") > base_comment
                is_reopened = (
                    base_comment_ts is not None
                    and isinstance(c_ts, str)
                    and c_ts > base_comment_ts
                )
                if is_new_id or is_reopened:
                    return _with_queue(c)
        if want_change:
            for e in studio.read_events(limit=500):
                eid = e.get("id", "")
                if e.get("type") in ("changed", "created", "removed") and (
                    base_event is None or eid > base_event
                ):
                    return {"kind": "change", **e}
        if deadline is not None and _time.monotonic() >= deadline:
            return None
        _time.sleep(interval)
