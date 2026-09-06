# Runtime architecture map

The runtime is not distributed as an installed package. It is source code under
`scripts/okf_loom/` and is invoked through the checked-in `scripts/okf-loom` helper.

| Area | Files |
|---|---|
| CLI dispatcher | `scripts/okf_loom/cli.py`, `__main__.py` |
| Data model | `model.py`, `parse.py`, `paths.py`, `roundtrip.py`, `ignore.py` |
| Validation | `validate.py` |
| Search/index/discovery | `search.py`, `index.py`, `discover.py`, `plan.py`, `update.py` |
| Bundle config/capabilities | `config.py`, `extensions.py` |
| Rendering/server/studio | `render.py`, `server.py`, `studio.py`, `watch.py` |
| Viewer assets | `viewer/templates/`, `viewer/static/`, `viewer/OVERRIDES.md` |
| Archive/proof helpers | `scripts/build_skill_archive.py`, `scripts/capture_viewer_proof.py`, `scripts/capture_signal_controls.py`, `scripts/lint-js.sh` |

Authoritative documentation:

- [`docs-bundle/reference/architecture.md`](../docs-bundle/reference/architecture.md)
- [`docs-bundle/reference/cli.md`](../docs-bundle/reference/cli.md)
- [`docs-bundle/reference/http_routes.md`](../docs-bundle/reference/http_routes.md)
- [`docs-bundle/reference/spec.md`](../docs-bundle/reference/spec.md)

Tests in `tests/` are the executable examples for checkout-local behavior.

Studio platform boundaries:

- `transactions.py`: cooperating writer serialization across threads/processes.
- `events.py`, `watcher.py`, `lifecycle.py`: event delivery, filesystem refresh,
  and explicit host-controlled server lifetime.
- `contracts.py`, `client.py`, `viewer/static/client.js`: API discovery and
  dependency-free integration clients.
- `viewer/workspace.py`, `viewer/static/workspace.*`: shared reading navigation
  and progressive catalog behavior.
- `viewer/static/document-patch.js`: bounded incremental document reconciliation.

See [embedding and migration](../docs/overhaul/integration.md),
[intent inventory](../docs/overhaul/intent.md), and
[overhaul plan](../docs/overhaul/plan.md).
