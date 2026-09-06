"""Shared, progressively enhanced navigation for every reading surface."""
from __future__ import annotations

from html import escape
from ..model import Bundle


def navigation(bundle: Bundle, *, mode: str, root_prefix: str) -> str:
    static = mode == "static"
    root = root_prefix or ("./" if static else "/")
    index = root + ("index.html" if static else "")
    graph = root + ("__graph.html" if static else "__graph")
    search = root + ("__search.html" if static else "__search")
    directories = sorted({cid[0] for cid in bundle.concepts if len(cid) > 1})
    links = "".join(
        f'<a class="okf-workspace-nav__folder" href="{escape(root + directory + ("/index.html" if static else "/"), quote=True)}">'
        f'{escape(directory)}<span>{sum(1 for cid in bundle.concepts if cid[0] == directory)}</span></a>'
        for directory in directories
    )
    return (
        '<nav class="okf-workspace-nav" aria-label="Workspace">'
        '<span class="okf-workspace-nav__label">WORKSPACE</span>'
        f'<a href="{escape(index, quote=True)}">Library</a>'
        f'<a href="{escape(graph, quote=True)}">Knowledge graph</a>'
        f'<a href="{escape(search, quote=True)}">Search knowledge</a>'
        '<span class="okf-workspace-nav__label">COLLECTIONS</span>' + links +
        '<div class="okf-workspace-nav__foot">Plain Markdown.<br>Your knowledge, connected.</div>'
        '</nav>'
    )


def workspace_assets(prefix: str) -> str:
    return (f'<link rel="stylesheet" href="{prefix}/workspace.css">'
            f'<script src="{prefix}/workspace.js" defer></script>')
