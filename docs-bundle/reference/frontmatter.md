---
type: Reference
title: Frontmatter contract
description: The OKF frontmatter contract (SPEC §4.1) — required and recommended keys, round-trip preservation, and YAML gotchas.
resource: /reference/frontmatter.md
tags: [frontmatter, yaml, reference]
timestamp: "2026-06-29T00:00:00Z"
---

# Frontmatter contract

Every OKF concept is a markdown file whose body is preceded by a YAML
frontmatter block delimited by `---` lines. The contract is defined by
SPEC §4.1 and enforced by [`scripts/okf-loom validate`](cli.md#validate).
See [index.md](/reference/index.md) for the rest of the reference quadrant.

A minimal conformant concept:

```markdown
---
type: Reference
---

# Body

Free markdown.
```

That is the entire hard requirement: parseable YAML and a non-empty
`type`. Everything else in this document is recommendation or convention.

# Keys

## Required

| Key | Type | Notes |
|---|---|---|
| `type` | string | **REQUIRED by SPEC §9.** Short string naming the kind of thing (e.g. `Table`, `API Endpoint`, `Tutorial`, `Reference`). Not centrally registered; consumers tolerate unknown types. The only hard-required key. |

## Recommended

| Key | Type | Default | Notes |
|---|---|---|---|
| `title` | string | filename stem (underscores → spaces) | Human display name. Index generators and the viewer use it. |
| `description` | string | empty | One-line summary. Used by index generators and search snippets. |
| `resource` | string (URI) | absent | Canonical URI of the underlying asset. Absent for abstract concepts. |
| `tags` | list of strings | empty | Cross-cutting themes. Must be a YAML list. |
| `timestamp` | string (ISO 8601) | absent | Last-modified datetime. Quote it (see YAML gotchas). |

Missing recommended keys surface as `WARNING` findings under the
default `spec` profile; they become `ERROR` under the `producer`
profile (see [cli.md § validate](cli.md#validate)).

## Custom keys (SPEC §2 round-trip)

Producers MAY add arbitrary keys. Consumers — including okf-loom —
MUST preserve them verbatim on round-trip. okf-loom preserves
frontmatter key order and unknown keys through every
parse → modify → serialize cycle.

Several custom keys are recognised by the
[capability registry](capabilities.md) and activate capabilities by
their presence (auto-activation):

| Custom key | Activates |
|---|---|
| `relations` | `okf.cap.typed_relations` |
| `entities` | `okf.cap.entities` |
| `aliases` | `okf.cap.aliases` |
| `provenance` | `okf.cap.provenance` |
| `citations` | `okf.cap.citations` |

## `aliases`

Use `aliases:` for alternate names that should be searchable and visible in
the viewer. The legacy shorthand is a list of strings:

```yaml
aliases:
  - Purchase Orders
  - Sales Orders
```

When an alias is useful for search but too broad for automatic unlinked-mention
discovery, use object form and set `discoverable: false`:

```yaml
aliases:
  - label: Architecture
    discoverable: false
```

The alias remains indexed by search and shown by the viewer, but
`scripts/okf-loom discover` will not suggest links from incidental mentions of
that alias. String aliases are discoverable by default.

All five governed values use list outer shapes. `aliases` and `entities` may
contain non-empty strings or their documented object forms; `provenance`,
`citations`, and `relations` contain objects. Relation objects require non-empty
string `target` and `type` fields. Provenance/citation objects require at least
one renderable documented field. Validation reports malformed outer/entry
shapes as warnings without dropping unknown keys or unknown object fields.
Exact typed relation duplicates are reported by `(source, type, target)`.

Example combining recommended + custom keys:

```yaml
---
type: API Endpoint
title: Create Order
description: POST a new order to the checkout service.
resource: https://api.example.com/v2/orders
tags: [checkout, write-api, v2]
timestamp: "2026-06-15T10:00:00Z"
relations:
  - target: services/checkout
    type: implemented_by
    detail: POST handler in checkout service
entities:
  - id: entity/order
    label: Order
    kind: business_entity
    aliases: [purchase, transaction]
citations:
  - id: "1"
    text: "Internal data dictionary"
    url: https://wiki.example.com/data/orders
owner: team-checkout
sla_tier: "1"
---
```

# Reserved filename frontmatter

| File | Frontmatter allowed? |
|---|---|
| Bundle-root `index.md` | Yes — but only `okf_version`, `okf_extensions`, `generated`. SPEC §6 / §11. |
| Non-root `index.md` | **No.** Pure directory listing. |
| `log.md` | **No.** SPEC §7 structure only. |
| Any other `.md` | Yes — the full frontmatter contract applies. |

A bundle-root `index.md` declaring capabilities:

```yaml
---
okf_version: "0.1"
okf_extensions:
  - okf.cap.typed_relations
  - okf.cap.entities
generated: true
---
```

See [links.md](links.md) for the link contract and
[capabilities.md](capabilities.md) for the opt-in mechanisms.

# YAML gotchas

YAML 1.1 (the dialect PyYAML implements by default) silently coerces
unquoted scalars. okf-loom's validator downgrades
`timestamp_validity` findings to WARNING, but the underlying parse is
still lossy. Quote the following:

## Timestamps

YAML parses unquoted ISO dates as `datetime.datetime`, not as a string.

```yaml
# BAD — parses as datetime, not a string; round-trip may reformat it.
timestamp: 2026-06-29T00:00:00Z

# GOOD — preserved as a literal string.
timestamp: "2026-06-29T00:00:00Z"
```

This applies to any key carrying a datetime: `timestamp`,
`updated_at`, `created_at`, `provenance.collected_at`, etc.

## Version-like strings

YAML parses `1.10` as float `1.1` and `3.0` as float `3.0`. Quote
anything that must round-trip verbatim.

```yaml
# BAD — `1.10` becomes `1.1`; `3.0` becomes `3.0` (lossy on round-trip).
version: 1.10
bigquery_dataset_version: 3.0

# GOOD
version: "1.10"
bigquery_dataset_version: "3.0"
```

The same applies to numeric ids (`id: "0007"`) and SLA tiers
(`sla_tier: "1"`).

## Booleans

YAML 1.1 treats `yes`, `no`, `on`, `off`, `true`, `false` (case-insensitive)
as booleans. A tag literally named `no` becomes `False`. Quote any
string that coincides with a boolean token if you mean it as text.

```yaml
# BAD — `no` becomes boolean False.
tags: [yes, no, maybe]

# GOOD
tags: ["yes", "no", "maybe"]
```

Note: `okf-loom.config.yaml` accepts the boolean tokens for genuine boolean
fields (see [config_yaml.md](config_yaml.md) § Boolean coercion), but
the same rule does not apply to free-form frontmatter strings.

## `tags` must be a list

SPEC §4.1 implies a list. okf-loom tolerates a scalar tag, but the
canonical form is a YAML list.

```yaml
# BAD — a comma-separated string. Parses as one tag named "a, b, c".
tags: a, b, c

# GOOD — flow form.
tags: [a, b, c]

# GOOD — block form.
tags:
  - a
  - b
  - c
```

## Trailing whitespace and newlines

okf-loom normalises trailing whitespace to a single trailing
newline on write. Do not expect byte-identical round-trip if you
hand-format YAML; semantic identity is preserved.

## Aliases and merge keys

okf-loom's YAML loader (`_NodeLimitedSafeLoader`) disables
alias/anchor construction and merge keys as a DoS guard. Frontmatter
must be fully materialised; you cannot `<<: *base` to share blocks.

# Round-trip guarantees

okf-loom guarantees the following through every
parse → modify → serialize cycle (the `roundtrip.py` module):

| Property | Guarantee |
|---|---|
| Unknown keys | Preserved verbatim. |
| Key order | Preserved (the `_FmEntry` ordered representation). |
| `type` value | Preserved exactly. |
| Recommended keys when set | Preserved exactly. |
| List values | Order preserved. |
| Inline vs block style | The dumper emits block style for nested structures; simple scalars stay inline. |
| Trailing whitespace | Normalised to a single trailing newline. |

# Validation

Run [`scripts/okf-loom validate`](cli.md#validate) to surface frontmatter findings.
Useful check subsets:

```bash
scripts/okf-loom validate path/to/bundle --checks concept_required_keys,concept_recommended_keys
scripts/okf-loom validate path/to/bundle --checks governed_metadata
scripts/okf-loom validate path/to/bundle --checks timestamp_validity
scripts/okf-loom validate path/to/bundle --profile producer   # recommended keys → ERROR
```

The check categories that touch frontmatter:

| Check | Category |
|---|---|
| `concept.missing_type` | `concept_required_keys` |
| `concept.missing_recommended_keys` | `concept_recommended_keys` |
| `frontmatter.*_malformed` | `governed_metadata` |
| `relation.duplicate` | `governed_metadata` |
| `concept.unknown_type` | `concept_recommended_keys` (WARNING) |
| `concept.bad_timestamp` | `timestamp_validity` |
| `concept.tag_normalization` | `tag_normalization` |

# Authoring tools

The CLI mutator verbs go through the same parse → modify → serialize
pipeline, so behaviour is identical whether you hand-edit or use a verb:

- [`scripts/okf-loom write-concept`](cli.md#write-concept) — create or update a concept.
- [`scripts/okf-loom set-frontmatter`](cli.md#set-frontmatter) — set one frontmatter key.
- [`scripts/okf-loom link-add`](cli.md#link-add) — append a markdown link (and optional typed relation).
- [`scripts/okf-loom entity-add`](cli.md#entity-add) — append an entity.

See [/how-to/author_with_verbs.md](/how-to/author_with_verbs.md) for
the workflow, and [/tutorials/first_bundle.md](/tutorials/first_bundle.md)
for the first-bundle walk-through.

# See also

- [index.md](/reference/index.md) — reference quadrant index.
- [cli.md](cli.md) — CLI command reference.
- [links.md](links.md) — link forms and graph edges.
- [capabilities.md](capabilities.md) — capability registry and auto-activation.
- [config_yaml.md](config_yaml.md) — bundle-root config (separate file, separate contract).
- [/explanation/architecture.md](/explanation/architecture.md) — module-by-module design.
- [/reference/spec.md](/reference/spec.md) — the current okf-loom contract.
- Upstream OKF SPEC §4.1 — the base wire-format frontmatter contract.
