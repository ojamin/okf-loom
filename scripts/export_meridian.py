#!/usr/bin/env python3
"""Export an installable Meridian bundle; no node build or private SDK required."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent


def public_origin(value: str) -> str:
    url = urlsplit(value)
    host = url.hostname or ""
    if (url.scheme != "https" or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/") or url.port not in (None, 443)
            or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+", host)
            or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))):
        raise ValueError("Use a public HTTPS origin without credentials, path or nonstandard port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "https://" + host
    raise ValueError("Meridian does not permit plugin connections to IP literals")


def export_plugin(output: Path, *, origin: str, key: str, version: str,
                  privacy_url: str) -> dict:
    origin = public_origin(origin)
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*)+", key):
        raise ValueError("Use a reverse-DNS plugin key")
    if key.startswith(("com.meridian.", "meridian.", "app.meridian.")):
        raise ValueError("Meridian reserves this plugin namespace")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a release version such as 1.0.0")
    if not privacy_url.startswith("https://") or not urlsplit(privacy_url).hostname:
        raise ValueError("A public HTTPS privacy-policy URL is required")
    # A generated export must never erase a checkout or an existing installation.
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new version directory")
    permission = "network:" + urlsplit(origin).hostname
    permissions = ["boards:read", "items:read", "storage", permission]
    definitions = [
        ("workspace", "board_view", "Loom", {"defaultTitle": "Loom", "itemAccess": "none",
          "supportsFilters": False, "supportsGroupBy": False, "supportsSearch": False, "minHeightPx": 600, "mobile": True}),
        ("concept", "item_view", "Knowledge", {"tabLabel": "Knowledge", "order": 40, "minHeightPx": 500}),
        ("knowledge", "dashboard_widget", "Loom knowledge", {"defaultTitle": "Knowledge", "defaultWidth": 8,
          "defaultHeight": 6, "minWidth": 4, "minHeight": 4, "scope": "boards", "maxBoards": 1}),
    ]
    manifest = {
        "manifestVersion": 1, "key": key, "name": "OKF Loom", "version": version,
        "description": "A live Markdown knowledge workspace: concepts, relationships, search, source editing, comments, agent activity and undo.",
        "author": "OKF Loom contributors", "license": "Apache-2.0", "minHostVersion": "1.3.74",
        "privacyPolicyUrl": privacy_url, "permissions": permissions,
        "settingsSchema": {"type": "object", "properties": {
            "startConcept": {"type": "string", "title": "Starting concept", "default": "",
                             "description": "Optional bundle-relative concept ID. Each placement can pin its own concept."}},
            "additionalProperties": False},
        "extensions": [{"key": k, "type": t, "name": n, "permissions": permissions,
                        "client": {"module": "./workspace.js", "export": "LoomWorkspace"}, "config": c}
                       for k, t, n, c in definitions],
    }
    dist = output / "dist"
    dist.mkdir(parents=True)
    for name in ("workspace.js", "bridge.js", "styles.js"):
        shutil.copyfile(ROOT / "plugins/meridian" / name, dist / name)
    shutil.copyfile(ROOT / "scripts/okf_loom/viewer/static/client.js", dist / "client.js")
    (dist / "configuration.js").write_text("export const configuration = " + json.dumps({"origin": origin}) + ";\n")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "package.json").write_text(json.dumps({"name": key, "version": version, "type": "module", "private": True}, indent=2) + "\n")
    shutil.copyfile(ROOT / "plugins/meridian/README.md", output / "README.md")
    shutil.copyfile(ROOT / "LICENSE", output / "LICENSE")
    hashes = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "SHA256SUMS.json").write_text(json.dumps(hashes, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="Public HTTPS origin of the Loom server")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--key", default="io.github.ojamin.okf-loom")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--privacy-url", required=True, help="Published operator privacy notice")
    args = parser.parse_args()
    try:
        export_plugin(args.out, origin=args.origin, key=args.key, version=args.version, privacy_url=args.privacy_url)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(f"Meridian plugin exported to {args.out.resolve()}")
