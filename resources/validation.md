# Validation and fail-closed behavior

Default OKF v0.1 conformance is intentionally small:

1. every concept file has parseable YAML frontmatter;
2. every concept frontmatter has non-empty `type`;
3. reserved `index.md` and `log.md` files follow their structural rules.

Everything else is warning/info by default: broken links, missing image
targets (`asset.missing` — also promoted by `--fail-on-broken-links`;
images that exist but live outside the bundle root get an
`asset.out_of_bundle` info, since the studio and static builds cannot
display them), missing recommended keys, stale indexes, non-normalized
tags, unknown capabilities, and wikilink conversion suggestions.
Governed metadata shape problems (`aliases`, `entities`, `provenance`,
`citations`, `relations`) and duplicate typed relations are warnings too, so
they preserve base conformance but fail `--strict`.

## Commands

```bash
scripts/okf-loom validate path/to/bundle
scripts/okf-loom validate path/to/bundle --strict
scripts/okf-loom validate path/to/bundle --profile producer
scripts/okf-loom validate path/to/bundle --fail-on-broken-links
scripts/okf-loom validate path/to/bundle --format json
```

Exit behavior:

- `0`: selected profile passes;
- `1`: hard conformance errors;
- `2`: strict/CI warning promotion or some usage/bundle/I/O errors. Check stderr
  or JSON output before treating every exit 2 as the same failure class.

## Authoring fail-closed rule

Consumers tolerate not-yet-written links. Authoring commands are stricter:
`link-add` rejects a missing target unless `--allow-forward-reference` is
present. Use the flag only when the missing target is intentional.
