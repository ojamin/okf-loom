"""Bounded event delivery, independent of document storage and HTTP."""
from __future__ import annotations
from contextlib import contextmanager
import queue
import sys
import threading
from typing import Any, Callable

class EventBus:
    """Thread-safe pub/sub for live push (§7.1).

    Each subscriber owns a bounded :class:`queue.Queue`. :meth:`publish`
    fans an event out to every queue with ``put_nowait``; a full queue (slow
    client) gets the event dropped and a single ``resync`` sentinel enqueued
    instead, so a stalled browser never blocks the watcher or other clients.
    """

    def __init__(self, *, max_queue: int = 64) -> None:
        self._max_queue = max_queue
        self._subs: list[queue.Queue] = []
        # Callback subscribers (§17 in-process embedding API). Each entry is
        # (callback, stop_event). A daemon thread per callback drains its own
        # Queue and invokes the callback per event.
        self._cb_subs: list[_CallbackSub] = []
        self._lock = threading.Lock()
        self._pending = threading.local()

    def subscribe(self, callback: Callable[[dict[str, Any]], None] | None = None
                  ) -> "queue.Queue | _CallbackSub":
        """Register a subscriber. Returns the subscriber handle.

        - Without a callback (the SSE-client default): returns a bounded
          :class:`queue.Queue` the caller drains itself. ``publish`` pushes
          events into it (drop+resync on full).
        - With a ``callback`` (§17 in-process embedding API for a harness
          running the server in a daemon thread): returns a
          :class:`_CallbackSub` whose own daemon thread invokes ``callback``
          per event. :meth:`unsubscribe` stops the thread.

        A slow callback is treated like a slow SSE client: pending events
        are drained and replaced with one ``resync`` sentinel, so the
        callback is never blocked indefinitely and the publish path never
        stalls on a stuck subscriber (§7.1 / §13.8).
        """
        if callback is None:
            q: queue.Queue = queue.Queue(maxsize=self._max_queue)
            with self._lock:
                self._subs.append(q)
            return q
        sub = _CallbackSub(callback=callback, max_queue=self._max_queue)
        with self._lock:
            self._cb_subs.append(sub)
        sub.start()
        return sub

    def unsubscribe(self, handle: "queue.Queue | _CallbackSub") -> None:
        # ARCH2-004 fix: snapshot under the lock, then release BEFORE calling
        # handle.stop() (which joins the worker thread). Calling stop() inside
        # self._lock deadlocks for ~2s if the callback re-enters the bus
        # (e.g. via publish()) — the worker thread blocks waiting for the
        # lock we're holding.
        with self._lock:
            if isinstance(handle, _CallbackSub):
                if handle in self._cb_subs:
                    self._cb_subs.remove(handle)
                needs_stop = True
            else:
                needs_stop = False
                if handle in self._subs:
                    self._subs.remove(handle)
        if needs_stop:
            handle.stop()

    def client_count(self) -> int:
        with self._lock:
            return len(self._subs) + len(self._cb_subs)

    @contextmanager
    def batch(self):
        """Deliver a mutation's events only after its read models are current.

        Nestable and local to the calling thread. Bound accumulated work by
        the subscriber queue cap, falling back to a resync for large batches.
        Durable append remains immediate; only in-process delivery is deferred.
        """
        if getattr(self._pending, "events", None) is not None:
            yield
            return
        self._pending.events = []
        try:
            yield
        finally:
            events = self._pending.events
            self._pending.events = None
            for event in events:
                self.publish(event)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every subscriber; drop+resync slow clients."""
        pending = getattr(self._pending, "events", None)
        if pending is not None:
            if pending and pending[0].get("type") == "resync":
                return
            if len(pending) >= self._max_queue:
                pending[:] = [{"type": "resync"}]
            else:
                pending.append(event)
            return
        with self._lock:
            subs = list(self._subs)
            cb_subs = list(self._cb_subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow client: drain stale deltas and enqueue ONE resync so
                # the client knows it fell behind and re-fetches current state.
                self._replace_with_resync(q)
        for cb_sub in cb_subs:
            cb_sub.deliver(event)

    @staticmethod
    def _replace_with_resync(q: queue.Queue) -> None:
        dropped = 0
        while True:
            try:
                q.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        try:
            q.put_nowait({"type": "resync"})
        except queue.Full:
            pass


class _CallbackSub:
    """In-process callback subscriber (§17 ``EventBus.subscribe(callback)``).

    A daemon worker thread drains a bounded :class:`queue.Queue` and invokes
    the callback once per event. A full queue is treated like a slow SSE
    client: pending events are dropped and replaced with one ``resync``
    sentinel (§7.1), so a stuck callback never blocks ``publish`` and never
    accumulates unbounded memory. ``stop()`` signals shutdown and joins.
    """

    def __init__(self, *, callback: Callable[[dict[str, Any]], None],
                 max_queue: int = 64) -> None:
        self._callback = callback
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="okf-eventbus-cb", daemon=True
        )
        self._thread.start()

    def deliver(self, event: dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            # Mirror EventBus slow-client policy: drop deltas + one resync.
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            try:
                self._q.put_nowait({"type": "resync"})
            except queue.Full:
                pass

    def stop(self) -> None:
        self._stop.set()
        # Push a sentinel so a blocking get returns immediately.
        try:
            self._q.put_nowait({"__stop__": True})
        except queue.Full:
            pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if event.get("__stop__"):
                break
            try:
                self._callback(event)
            except Exception as e:
                # A buggy / malicious callback must never kill the worker
                # thread (§13.8 robustness — a dropped agent process degrades
                # gracefully; the studio stays usable). The exception is
                # swallowed by design so one bad subscriber cannot take the
                # event bus down. Surface it on stderr with a unique prefix
                # so it isn't silent — see docs/embedding-guide.md
                # §"EventBus.subscribe(callback) trust boundary" (P3-12 /
                # SEC2-007) for why this is a trust boundary and how to
                # escalate (e.g. redirect stderr to your log collector).
                try:
                    import traceback
                    print(
                        f"[okf-callback] subscriber raised "
                        f"{type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    traceback.print_exc(file=sys.stderr)
                except Exception:
                    # Last-resort: never let the logger itself kill the worker.
                    pass


