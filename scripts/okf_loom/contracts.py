"""Versioned, transport-neutral discovery and mutation schemas.

The API version is independent of the OKF document format and runtime version.
This manifest describes executable routes; it never advertises reserved hooks
as implemented. It intentionally contains no token, host path or agent secrets.
"""
from __future__ import annotations

from . import LOOM_VERSION, SPEC_VERSION
from .studio import _APPLY_KIND_SCHEMAS, _AnyType

API_VERSION = "1"
API_PREFIX = "/__api/v1"

# Stable names resolve to the original routes, which remain supported.
ROUTES = {
    "discovery": ("GET", "/__api/v1"),
    "health": ("GET", "/__health"),
    "document": ("GET", "/__data/doc"),
    "content": ("GET", "/__data/content.json"),
    "graph": ("GET", "/__data/graph.json"),
    "search": ("GET", "/__search"),
    "events": ("GET", "/__data/events"),
    "stream": ("GET", "/__events"),
    "comments": ("GET", "/__comments"),
    "comment": ("POST", "/__comment"),
    "comment_update": ("POST", "/__comment-update"),
    "claim": ("POST", "/__claim"),
    "resolve": ("POST", "/__resolve"),
    "presence": ("POST", "/__presence"),
    "apply": ("POST", "/__apply"),
    "save": ("POST", "/__save"),
    "undo": ("POST", "/__undo"),
    "diff": ("GET", "/__diff"),
    "preview": ("POST", "/__preview"),
}


def _json_type(t):
    if t is _AnyType:
        return {}
    if isinstance(t, tuple):
        return {"anyOf": [_json_type(v) for v in t]}
    return {"type": {str: "string", bool: "boolean", int: "integer",
                     float: "number", list: "array", dict: "object",
                     type(None): "null"}[t]}


def operation_schemas():
    return {
        kind: {
            "type": "object", "additionalProperties": False,
            "required": list(spec["required"]),
            "properties": {k: _json_type(v) for k, v in
                           (spec["required"] | spec["optional"]).items()},
        }
        for kind, spec in _APPLY_KIND_SCHEMAS.items()
    }


def discovery(*, editing: bool = True, live: bool = True):
    return {
        "api_version": API_VERSION, "runtime_version": LOOM_VERSION,
        "format_version": SPEC_VERSION,
        "capabilities": {
            "editing": editing, "live": live,
            "search_modes": ["lexical", "semantic", "hybrid", "tag", "entity", "relation"],
            "viewer_extensions": {"panel": "supported", "viewMode": "supported",
                                  "toolbar": "reserved", "graphDecorator": "reserved",
                                  "suggestionRenderer": "reserved"},
        },
        "routes": {name: {"method": method, "path": path}
                   for name, (method, path) in ROUTES.items()},
        "apply_args": operation_schemas(),
        "auth": {"header": "X-OKF-Token", "mutations_require_token": True,
                 "diff_requires_token": True, "cross_origin": "same-origin or explicit host allowlist"},
        "events": {"transport": "sse", "fallback": "cursor-polling",
                   "identity": "event_id", "replay_order": "asc"},
    }
