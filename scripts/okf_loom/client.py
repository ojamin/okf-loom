"""Small stdlib client for embedding in an external agent or application.

No process spawning, token discovery, hidden retries or model calls. The caller
supplies the server address and token and controls retries/lifetime explicitly.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class StudioError(RuntimeError):
    def __init__(self, status: int, payload):
        self.status, self.payload = status, payload
        super().__init__(f"Studio HTTP {status}: {payload}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a session token to a redirect destination.
        return None


class StudioClient:
    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 15):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an http(s) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query or fragment")
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self._opener = build_opener(_NoRedirect())

    def request(self, path: str, *, data=None, query=None, idempotency_key=None):
        if not path.startswith("/__") or "://" in path:
            raise ValueError("only studio routes are supported")
        url = self.base_url + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Accept": "application/json"}
        if self.token and (data is not None or path == "/__diff"):
            headers["X-OKF-Token"] = self.token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        req = Request(url, data=body, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"error": raw}
            raise StudioError(exc.code, payload) from exc

    def discover(self):
        return self.request("/__api/v1")

    def health(self):
        return self.request("/__health")

    def document(self, concept: str):
        return self.request("/__data/doc", query={"id": concept})

    def search(self, query: str, *, mode: str = "lexical"):
        return self.request("/__search", query={"q": query, "mode": mode, "format": "json"})

    def comments(self, *, state=None):
        return self.request("/__comments", query={"state": state})

    def events(self, *, since=None, limit=200):
        return self.request("/__data/events", query={"since": since, "limit": limit, "order": "asc"})

    def comment(self, concept: str, body: str, *, anchor=None, idempotency_key=None):
        return self.request("/__comment", data={"concept": concept, "body": body, "anchor": anchor or {}},
                            idempotency_key=idempotency_key)

    def claim(self, comment_id: str, *, actor: str, summary=None):
        return self.request("/__claim", data={"id": comment_id, "actor": actor, "summary": summary})

    def resolve(self, comment_id: str, *, summary=None, activity=None, reply=None):
        return self.request("/__resolve", data={"id": comment_id, "summary": summary,
                                                "activity": activity or [], "reply": reply})

    def apply(self, kind: str, target: str, args: dict, *, expected_rev=None, group_id=None):
        data = {"kind": kind, "target": target, "args": args}
        if expected_rev is not None:
            data["expected_rev"] = expected_rev
        if group_id is not None:
            data["group_id"] = group_id
        return self.request("/__apply", data=data)

    def undo(self, *, concept=None, rev=None, group_id=None, idempotency_key=None):
        data = {k: v for k, v in {"concept": concept, "rev": rev, "group_id": group_id}.items() if v is not None}
        return self.request("/__undo", data=data, idempotency_key=idempotency_key)
