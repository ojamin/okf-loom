"""Host-controlled HTTP server lifecycle, independent of CLI process ownership."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import subprocess
import threading


class StudioServer(ThreadingHTTPServer):
    """A bound server that starts explicitly and closes deterministically.

    Use ``with create_server(bundle, port=0) as server`` in an embedding host.
    ``server.url`` contains the actual selected port. No browser is opened.
    The host owns its agent, event subscribers and any additional policies.
    """
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.watcher = None
        self._thread = None
        self._closed = False
        self._lifecycle_lock = threading.RLock()
        self.stop_event = threading.Event()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def start(self):
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("server is closed")
            if self._thread is not None:
                return self
            if self.watcher is not None:
                self.watcher.start()
            self._thread = threading.Thread(target=self.serve_forever,
                                            name="okf-http", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self.stop_event.set()
            if self.watcher is not None:
                self.watcher.stop()
                if self.watcher.is_alive():
                    self.watcher.join(timeout=3)
            if self._thread is not None:
                self.shutdown()
                self._thread.join(timeout=3)
            proc = getattr(self, "tunnel_proc", None)
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            studio = getattr(self, "studio", None)
            if studio is not None:
                try:
                    state = json.loads(studio.server_state_path.read_text())
                    if state.get("port") == self.server_address[1]:
                        studio.server_state_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
            self.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
