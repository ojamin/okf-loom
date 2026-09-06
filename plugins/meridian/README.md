# OKF Loom for Meridian

Loom remains a Markdown bundle with one authoritative editing engine. This
plugin connects Meridian's board view, item tab and dashboard widget to a
running Loom server through Meridian's approved `net.fetch` bridge. It does
not copy your bundle into board cells or bypass the plugin sandbox.

## Export and install

From the Loom checkout:

```sh
python scripts/export_meridian.py \
  --origin https://loom.your-domain.example \
  --privacy-url https://your-domain.example/privacy/loom \
  --version 1.0.0 --out /path/to/okf-loom-1.0.0
```

Use an operator-managed, public HTTPS Loom origin reachable from Meridian's
API service. Configure that hostname in Loom's allowed hosts. A private IP,
localhost, arbitrary tunnel, path prefix or nonstandard port is not a supported
deployment. Meridian independently checks the network grant, public DNS,
HTTPS and response limits. The exporter never starts a server or publishes data.

Copy the exported directory into the configured `PLUGIN_BUNDLED_DIR` shared
by Meridian's API and plugin-sandbox services, then restart those services
through the deployment's normal release process. The directory contains
`manifest.json` beside `dist/`, matching both loaders. Registering a manifest
through the publish API alone does not install these local bundle files.

An account administrator installs the new catalogue version and reviews its
permissions. Add **Loom** as a board view, **Knowledge** as an item tab, or
**Loom knowledge** as a dashboard widget. The host must configure its separate
plugin sandbox origin. This export targets the manifest-v1 runtime inspected
at Meridian `pre-dev` commit `6697c1610245133af28e163f0490b70716bc3371`;
its plugin guide predates the working network bridge and is not a reliable
feature matrix. No Meridian source is included in this export.

## Connection and ownership

The export fixes one destination hostname into both the manifest permission
and the client configuration. There is no wildcard network access. Changing
the destination requires a new exported version and administrator approval.
Only concept IDs pinned to placements are stored in Meridian plugin storage.
Those settings use board/item scopes and an expected storage version on update.
No Meridian board contents are sent to Loom. The board/item read permissions
are implied by Meridian's surface contracts, even though the plugin does not
query board items.

Reading uses Loom's existing public read API. Editing requires the current
Loom session token entered in the frame's **Connect editing** form. The token
stays in memory; it is not a setting, URL parameter, exported file, or saved
storage value. Closing the frame clears it. Its authority is the Loom session's
authority; Meridian board permissions do not create per-concept Loom ACLs.

## Working in the plugin

- Browse concepts and search with all six Loom search methods.
- Read rendered Markdown, inspect exact source and every frontmatter field.
- Follow outgoing links, backlinks, typed relations, entities and evidence.
- Edit or create full source. Saves preserve exact bytes and check the revision
  read at edit time; conflicts retain the draft for deliberate comparison.
- Post quoted or document comments, reply, claim, resolve, reopen and archive.
  Comment retries retain their idempotency key. An existing Loom agent can
  process these same directives with the normal wait/claim/edit/resolve loop.
- Review attributed writes and undo a write or a whole group. Updates refresh
  every five seconds while the frame is visible; an active draft is preserved.
- Pin a starting concept independently in each Meridian placement.

Native rendering admits document markup only; active bundle plugins, custom
scripts, Mermaid/KaTeX execution, and arbitrary media loading do not run inside
Meridian. **Open full studio** retains Loom's specialized renderers, complete
interactive graph, command palette, and viewer extensions. Source is always
available. This boundary is deliberate and must not be represented as complete
visual parity with the standalone studio.

The plugin does not launch an agent, invent a retired `agent_tool` extension,
or give a server handler ambient network access. Autonomous agents continue to
use Loom's CLI or transport clients; Meridian's server-handler context has no
outbound fetch capability. The watching switch signals intent, not proof that
an agent process is running.

## Updates, removal and verification

Versions in Meridian's catalogue are immutable. Export to a new directory with
a new version for every release; the exporter refuses to overwrite an existing
directory. `SHA256SUMS.json` fingerprints the distributable files. Disable or
uninstall through Meridian: polling and the message port stop when the frame
unmounts, and Markdown/history remain on the Loom server.

Run `npm test`, `scripts/lint-js.sh`, and the platform/export Python tests in
Loom. In a Meridian checkout, validate the exported manifest with its actual
`parsePluginManifest` and run the supported plugin installation/surface E2E
lane before claiming deployment acceptance. Unit tests with a host double do
not prove an installed Meridian session.
