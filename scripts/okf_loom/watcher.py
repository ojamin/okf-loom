"""Deterministic filesystem watching, reusable without an HTTP server."""
from __future__ import annotations
from contextvars import copy_context
from pathlib import Path
import sys
import threading

class BundleWatcher(threading.Thread):
    """Polls the bundle dir for ``.md`` mtime changes and reloads the bundle.

    Uses ``os.stat`` rather than watchdog, per the hard-constraint list.

    Reload safety (P1-37): the snapshot is advanced ONLY after a successful
    reload. On failure the exception is logged to stderr (with the failing
    path) and the snapshot is left untouched so the next tick retries — a
    transient malformed save (e.g. a half-written file) is not silently
    consumed and the browser never shows stale content with no signal.
    """

    def __init__(self, bundle_root: Path, reload_fn, *,
                 interval: float = 1.0, tick_fn=None) -> None:
        super().__init__(daemon=True, name="okf-watcher")
        self._context = copy_context()
        self._root = Path(bundle_root)
        self._reload_fn = reload_fn
        self._tick_fn = tick_fn  # fires EVERY tick (tail + sweep), even when .md unchanged
        self._interval = interval
        # NOTE: named ``_stop_event`` (not ``_stop``) to avoid shadowing
        # ``threading.Thread._stop()``, which the base class invokes during
        # ``join()``. The previous ``self._stop = Event()`` shadow broke
        # ``join()`` once the thread had terminated (TypeError: 'Event'
        # object is not callable) — hidden in production because the watcher
        # is a daemon whose join() is never called, but exposed by tests
        # and by any embedding harness that shuts the watcher down cleanly.
        self._stop_event = threading.Event()
        self._snapshot = self._scan()

    def _scan(self) -> dict[Path, float]:
        snap: dict[Path, float] = {}
        if not self._root.is_dir():
            return snap
        # Same exclusion semantics as Bundle.load (spec §5): default
        # excludes + .gitignore + bundle.exclude. Config and .gitignore are
        # re-read every tick (cheap small files; the dir pruning makes the
        # scan far cheaper than the old rglob), so serving a workspace root
        # live-updates when excludes — or new OKF files — appear.
        from .config import OkfConfig, OkfConfigError
        from .ignore import iter_markdown_files
        try:
            bundle_cfg = OkfConfig.load(self._root).bundle
        except OkfConfigError:
            from .config import BundleConfig
            bundle_cfg = BundleConfig()  # Bundle.load warns; keep scanning
        for p in iter_markdown_files(
            self._root,
            exclude=bundle_cfg.exclude,
            include=bundle_cfg.include,
            respect_gitignore=bundle_cfg.respect_gitignore,
        ):
            try:
                snap[p] = p.stat().st_mtime
            except OSError:
                continue
        # P2-6: also track the bundle config + viewer override config/palette,
        # so an operator editing okf-loom.config.yaml (e.g. flipping
        # allow_active_code false->true for lock-down) triggers a reload +
        # cache invalidation without needing to also touch an .md file.
        from .config import CONFIG_FILENAME
        for extra in (
            self._root / CONFIG_FILENAME,
            self._root / ".okf-loom" / "viewer" / "config.json",
            self._root / ".okf-loom" / "viewer" / "palette.json",
        ):
            try:
                if extra.is_file():
                    snap[extra] = extra.stat().st_mtime
            except OSError:
                continue
        return snap

    def run(self) -> None:
        self._context.run(self._run)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            snap = self._scan()
            if snap == self._snapshot:
                self._tick()
                continue
            # Snapshot changed → attempt reload. Only advance self._snapshot
            # AFTER reload succeeds so a transient failure (malformed save,
            # disk hiccup, permission blip) is retried on the next tick
            # instead of being silently swallowed.
            try:
                self._reload_fn()
            except Exception as e:  # noqa: BLE001 — reload errors must not kill the thread
                # Log every swallowed reload exception with the failing path
                # so an operator running `okf serve` sees WHY the browser is
                # showing the previous bundle (P1-37 double-silent fix).
                print(
                    f"okf: bundle reload failed for {self._root} "
                    f"(keeping previous bundle; will retry on next tick): {e}",
                    file=sys.stderr,
                )
                continue  # do NOT advance snapshot → next tick retries.
            self._snapshot = snap
            self._tick()

    def _tick(self) -> None:
        # Refresh content before delivering cross-process edit events: clients
        # fetch the new document as soon as they receive an event. Session-only
        # changes still get a tick when the content snapshot is unchanged.
        if self._tick_fn is not None:
            try:
                self._tick_fn()
            except Exception as exc:
                print(f"okf: watcher tick failed: {exc}", file=sys.stderr)

    def stop(self) -> None:
        self._stop_event.set()


