"""okf-loom CLI dispatcher.

Entry: `scripts/okf-loom <command> [opts]` from this repo checkout.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import (
    SPEC_VERSION,
    LOOM_VERSION,
    REQUIRED_FRONTMATTER_KEYS,
    RECOMMENDED_FRONTMATTER_KEYS,
)
from .model import Bundle
from .paths import concept_id_to_str


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _load_bundle(path: str | Path) -> Bundle:
    return Bundle.load(path)


# ---------------------------------------------------------------------------
# Live studio integration: detect an active session and route mutator writes
# through Studio.save_concept (current spec §10/§13)
# ---------------------------------------------------------------------------


def _maybe_studio_for_bundle(bundle_path: str | Path,
                             group_id: str | None = None,
                             actor: str = "agent",
                             origin: str = "mutator") -> "Any | None":
    """Return a Studio for the bundle if an active session exists, else None.

    The CLI mutators (``link-add``, ``entity-add``, ``update``, ``repair``,
    ``write-concept``, ``set-frontmatter``) call this to detect whether a
    studio session is live for the bundle — i.e. ``scripts/okf-loom serve`` (or a prior
    ``scripts/okf-loom watch``) has created the configured session directory. When it has, the
    mutator routes its writes through ``apply_plan(studio=...)`` so every
    write:

    * is attributed (actor/origin/action) in events.jsonl + the change list;
    * is undoable (single + group) via the history ring;
    * emits a `changed` (+ `graph` for graph-affecting ops) event over SSE;
    * is de-duplicated by the watcher's disk emit_change.

    When no session is active (the no-session / non-studio case), returns None
    and the mutator writes directly through the compatibility atomic path (no
    attribution, no undo snapshot) — preserving behavior for non-studio callers.

    ``group_id`` is threaded through so a multi-step agent pass (e.g.
    ``okf link-add --group-id G1 ...`` then ``okf entity-add --group-id G1
    ...``) is undoable as one group via ``POST /__undo {group_id: "G1"}``.
    """
    from .studio import Studio
    bundle_root = Path(bundle_path)
    studio = Studio.for_configured_bundle(bundle_root)
    if not studio.session_dir.is_dir():
        return None
    # Stash the call-scoped group_id + actor on the studio so apply_plan
    # callers can pull them from a single arg. (apply_plan takes them as
    # explicit kwargs; this attribute is for forward-compat / inspection.)
    studio.constraints = studio.constraints or {}  # ensure dict
    return studio


def _studio_aware_apply(bundle: Bundle, plan: "Any",
                        *, dry_run: bool, args: "Any",
                        action_origin: str = "mutator") -> dict:
    """Run apply_plan with studio attribution if a session is active.

    A thin wrapper that detects the studio for the bundle's root, threads it
    (plus actor/origin/group_id) into ``update.apply_plan``, and returns the
    result. When no session is active, behaves exactly like the compatibility
    ``apply_plan(bundle, plan, apply=True, dry_run=...)`` call.
    """
    from .update import apply_plan
    group_id = getattr(args, "group_id", None)
    actor = getattr(args, "actor", "agent") or "agent"
    studio = _maybe_studio_for_bundle(bundle.root, group_id=group_id, actor=actor)
    return apply_plan(
        bundle, plan, apply=True, dry_run=dry_run,
        studio=studio, actor=actor, origin=action_origin,
        group_id=group_id,
    )


# P1-7 (SPEC §3.7 exit-code contract). Reasons emitted by
# ``update._apply_one`` and the per-kind handlers in ``update.py`` split
# into two well-defined classes:
#
#   * HARD FAILURE — operation-level failure → exit 1.
#   * IDEMPOTENT NO-OP — protective / already-satisfied → exit 0.
#
# The previous implementation only treated ``concept_not_found`` as a
# failure, which produced an indefensible asymmetry: ``link-add --source
# NO_SUCH`` exited 1 (concept_not_found on the source) but
# ``link-add --source REAL --target NO_SUCH`` exited 0
# (target_concept_not_found on the link target). Spec §3.7 fixes both as
# exit 1.
_HARD_FAILURE_REASONS_EXACT: frozenset[str] = frozenset({
    "concept_not_found",         # update._apply_one: target concept missing
    "target_concept_not_found",  # update._h_add_link: link target missing
    "rev_conflict",              # §9.3 collision: concurrent on-disk edit (INTENT2-001)
    "section_not_found",         # update._h_update_section: heading missing
    "text_not_found",            # update._h_replace_text: old text missing
    "malformed_relations",       # update._h_add_relation: key is not a list
})
# These carry a free-form suffix after ":" (e.g. ``bad_args:'key'``,
# ``unknown_op_kind:foo``); match by prefix.
_HARD_FAILURE_REASON_PREFIXES: tuple[str, ...] = (
    "bad_args:",
    "unknown_op_kind:",
    "section_ambiguous:",   # update-section: duplicate headings, refuse to guess
    "text_ambiguous:",      # replace-text: old matches >1 place without --all
)
# Protective no-ops. ``would_overwrite_hand_written`` is intentionally
# here: it is a guard that REFUSED to clobber hand-curated content, not
# an operation-level failure. Surfacing it as exit 1 would conflate "I
# refused to destroy your work" with "I failed to find the concept you
# asked for" — the spec classifies it as a permissive (SPEC §9) no-op.
_IDEMPOTENT_NOOP_REASONS: frozenset[str] = frozenset({
    "same_value",
    "already_linked",
    "already_related",
    "already_tagged",
    "entity_exists",
    "section_exists",
    "section_unchanged",
    "not_linked",
    "would_overwrite_hand_written",
})


def _is_hard_failure_reason(reason: str | None) -> bool:
    """Return True iff ``reason`` indicates an operation-level failure
    (SPEC §3.7 exit 1). Used by both single-op verbs and ``cmd_update``.
    """
    if not reason:
        return False
    if reason in _HARD_FAILURE_REASONS_EXACT:
        return True
    return any(reason.startswith(p) for p in _HARD_FAILURE_REASON_PREFIXES)


def _single_op_exit_code(result: dict) -> int:
    """Derive the exit code for a single-op authoring verb (SPEC §3.7).

    Maps the per-op ``reason`` field emitted by ``apply_plan`` to:

    * ``0`` — success: at least one op applied, OR every skip is an
      idempotent protective no-op (``same_value``, ``already_*``,
      ``would_overwrite_hand_written``, …).
    * ``1`` — operation-level failure: no op applied AND at least one op
      hit a hard-failure reason (``concept_not_found``,
      ``target_concept_not_found``, ``bad_args:*``, ``unknown_op_kind:*``).

    "Applied wins": a verb that fires multiple ops (e.g. ``link-add
    --relation`` emits ``add_link`` + ``add_relation``) returns 0 if ANY
    op applied, even when a sibling op hard-failed. This mirrors
    ``apply_plan``'s per-op independence and keeps the verb's exit code
    meaningful for the common "one link added, one relation was a
    duplicate" case.

    Multi-op ``cmd_update`` uses the same reason classification but a
    different aggregation (see :func:`cmd_update`).
    """
    if result.get("applied", 0) > 0:
        return 0
    for _op_dict, op_result in result.get("results", []):
        if _is_hard_failure_reason(op_result.get("reason")):
            return 1
    return 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args.bundle)
    graph = bundle.graph()
    # P1-20 (SPEC §7 L490): count concepts carrying each governed optional key.
    _GOVERNED_KEYS = ("aliases", "entities", "provenance", "citations", "relations")
    governed_key_counts: dict[str, int] = {k: 0 for k in _GOVERNED_KEYS}
    for c in bundle.concepts.values():
        for k in _GOVERNED_KEYS:
            if isinstance(c.frontmatter.get(k), list) and c.frontmatter.get(k):
                governed_key_counts[k] += 1
    info = {
        "name": bundle.name,
        "root": str(bundle.root),
        "spec_version_implemented": SPEC_VERSION,
        "toolkit_version": LOOM_VERSION,
        "bundle_okf_version": bundle.okf_version,
        "okf_extensions": bundle.okf_extensions,
        "concept_count": len(bundle.concepts),
        "type_count": len(bundle.types()),
        "tag_count": len(bundle.tags()),
        "types": sorted(bundle.types()),
        "index_count": len(bundle.indexes),
        "log_count": len(bundle.logs),
        "internal_links": len(graph.edges),
        "unresolved_links": len(graph.unresolved),
        "external_links": len(graph.external),
        "load_warnings": len(bundle.warnings),
        "governed_key_counts": governed_key_counts,
        "active_capabilities": sorted(bundle.capabilities().active_ids),
    }
    if args.format == "json":
        _print_json(info)
    else:
        for k, v in info.items():
            if isinstance(v, list):
                v = ", ".join(v) if v else "(none)"
            print(f"  {k:>28}: {v}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .validate import (
        validate_bundle_with_profile,
        CheckSpec,
        Severity,
    )
    from .config import OkfConfig

    bundle = _load_bundle(args.bundle)
    cfg = OkfConfig.load(bundle.root)
    # Resolve defaults: CLI flag wins, then config, then built-in default
    profile = args.profile or cfg.validate.default_profile or "spec"
    fail_on_broken = getattr(args, "fail_on_broken_links", False)
    if not fail_on_broken and cfg.validate.fail_on_broken_links:
        fail_on_broken = cfg.validate.fail_on_broken_links

    if args.checks:
        # When explicit --checks is given, use the raw validate_bundle path
        # (profiles don't compose with check subsets).
        from .validate import validate_bundle
        spec = CheckSpec.only(*args.checks.split(","))
        report = validate_bundle(bundle, checks=spec)
    else:
        report = validate_bundle_with_profile(
            bundle,
            profile=profile,
            fail_on_broken_links=fail_on_broken,
            strict=False,  # strict is checked by the CLI exit-code logic below
        )

    if args.format == "json":
        # P1-12: ValidationReport.as_dict() emits "profile" authoritatively.
        # Under --checks the profile remap does not run (§5: profiles do not
        # compose with check subsets), so report.profile truthfully stays
        # "spec" rather than claiming the requested profile (P2-6).
        _print_json(report.as_dict())
    else:
        print(f"OKF Bundle: {bundle.root}")
        print(
            f"Conformance: {'PASS' if report.ok else 'FAIL'}  "
            f"Strict: {'PASS' if report.ok_strict else 'FAIL'}  "
            f"(errors={len(report.errors)} warnings={len(report.warnings)} "
            f"info={len(report.infos)})"
        )
        if profile != "spec":
            print(f"Profile: {profile}")
        for f in report.findings:
            loc = str(f.path) if f.path else ""
            if f.line:
                loc += f":{f.line}"
            cid = (
                " " + concept_id_to_str(f.concept_id)
                if f.concept_id
                else ""
            )
            print(f"  [{f.severity.value.upper():7}] {f.code:32} {loc}{cid}")
            print(f"            {f.message}")
    # Exit code: 0 ok, 1 conformance fail, 2 strict fail
    if not report.ok:
        return 1
    if args.strict and not report.ok_strict:
        return 2
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    from .render import _graph_grouping_metadata

    bundle = _load_bundle(args.bundle)
    graph = bundle.graph()

    def _node_dict(c: Any) -> dict[str, Any]:
        metadata = _graph_grouping_metadata(c)
        return {
            "id": concept_id_to_str(c.id),
            "type": c.type,
            "title": c.title,
            "tags": c.tags,
            "path": str(c.rel_path),
            "metadata": metadata,
            "graph_cluster": metadata.get("graph_cluster", ""),
            "source_system": metadata.get("source_system", ""),
        }

    out: dict[str, Any] = {
        "nodes": [
            _node_dict(c)
            # §3.2: sort explicitly by concept_id; never rely on dict
            # insertion order (Bundle.load happens to sort via rglob today,
            # but in-memory Bundle construction and embedders must not
            # silently break determinism).
            for c in sorted(bundle.concepts.values(), key=lambda c: c.id)
        ],
        "edges": [
            {
                "source": concept_id_to_str(e.source),
                "target": (
                    concept_id_to_str(e.target) if e.target else None
                ),
                "target_raw": e.target_raw,
                "form": e.form,
                "label": e.label,
            }
            # §3.2: deterministic edge ordering by (source, target, target_raw).
            for e in sorted(
                graph.edges,
                key=lambda e: (
                    e.source,
                    e.target if e.target is not None else (),
                    e.target_raw,
                ),
            )
        ],
        "external": [
            {
                "source": concept_id_to_str(e.source),
                "target_raw": e.target_raw,
                "label": e.label,
            }
            # §3.2: deterministic external-edge ordering.
            for e in sorted(
                graph.external,
                key=lambda e: (e.source, e.target_raw),
            )
        ],
    }
    if args.format == "dot":
        _emit_dot(out)
    elif args.format == "json":
        _print_json(out)
    else:
        print(f"# {len(out['nodes'])} concepts, {len(out['edges'])} internal edges, "
              f"{len(out['external'])} external links")
        for n in out["nodes"][:50]:
            print(f"  {n['id']:50} [{n['type']}]")
        if len(out["nodes"]) > 50:
            print(f"  ... ({len(out['nodes']) - 50} more)")
    return 0


def cmd_graph_quality(args: argparse.Namespace) -> int:
    from .graph_quality import analyze_graph_quality, format_text

    bundle = _load_bundle(args.bundle)
    report = analyze_graph_quality(bundle)
    if args.format == "json":
        _print_json(report.as_dict())
    else:
        print(format_text(report), end="")
    return 0


def _emit_dot(graph_data: dict) -> None:
    print("digraph okf {")
    print('  rankdir=LR;')
    for n in graph_data["nodes"]:
        nid = n["id"].replace('"', "'")
        label = (n["title"] or n["id"]).replace('"', "'")
        print(f'  "{nid}" [label="{label}"];')
    for e in graph_data["edges"]:
        if e["target"] is None:
            continue
        s = e["source"].replace('"', "'")
        t = e["target"].replace('"', "'")
        print(f'  "{s}" -> "{t}";')
    print("}")


def cmd_search(args: argparse.Namespace) -> int:
    from .search import search_bundle, SearchMode
    from .config import OkfConfig

    bundle = _load_bundle(args.bundle)
    cfg = OkfConfig.load(bundle.root)
    # Resolve default: CLI flag wins, then config, then built-in default
    mode_str = args.mode or cfg.search.default_mode or "lexical"
    mode = SearchMode(mode_str)

    # Validate mode-specific flag combinations
    has_edge_filter = any(getattr(args, k, None) for k in ("relation", "source", "target"))
    if has_edge_filter and mode != SearchMode.RELATION:
        print(
            "error: --relation/--source/--target are only valid with --mode relation",
            file=sys.stderr,
        )
        return 1

    try:
        kwargs: dict[str, Any] = dict(
            mode=mode,
            type_filter=args.type,
            tag=args.tag,
            limit=args.limit,
            semantic_min_score=getattr(args, "min_semantic_score", None),
            hybrid_require=getattr(args, "hybrid_require", "any"),
        )
        if mode == SearchMode.RELATION:
            kwargs["relation"] = getattr(args, "relation", None)
            kwargs["source"] = getattr(args, "source", None)
            kwargs["target"] = getattr(args, "target", None)
        results = search_bundle(bundle, args.query or "", **kwargs)
    except NotImplementedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        # P1-38: keep ValueError (SearchMode parse + bad concept-id filters)
        # as exit 1. Let FileNotFoundError propagate to main() -> exit 2
        # (§3.7) so missing bundles surface as CI-style failures, not
        # operation-level ones.
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.format == "json":
        _print_json([r.as_dict() for r in results])
    else:
        # P2-5 (iter-3): mode-aware snippet cap. For relation/entity modes
        # the snippets ARE the result data (matched edges / matched fields),
        # so capping at 2 silently hid most of the data a user asked for.
        # Show all there. For the text-body modes (lexical/semantic/hybrid/
        # tag) keep the 2-snippet cap (long body excerpts) and print a
        # ``+N more`` footer so truncation is visible instead of silent.
        if mode in (SearchMode.RELATION, SearchMode.ENTITY):
            snippet_cap: int | None = None
        else:
            snippet_cap = 2
        if not results:
            print("(no matches)")
        for r in results:
            # P2-3: :.4f so tiny sqlite-fts bm25 floats (e.g. 0.0023) don't
            # render as 0.000 in text mode. JSON output is exact.
            print(f"  {r.score:7.4f}  {concept_id_to_str(r.concept_id):50}  {r.title}")
            if snippet_cap is None or len(r.snippets) <= snippet_cap:
                shown = r.snippets
                more = 0
            else:
                shown = r.snippets[:snippet_cap]
                more = len(r.snippets) - snippet_cap
            for snip in shown:
                print(f"           … {snip}")
            if more:
                print(f"           … (+{more} more)")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from .plan import build_plan

    bundle = _load_bundle(args.bundle)
    rules = args.rules.split(",") if args.rules else None
    # Current spec §7: --scope restricts the plan to a concept set; --neighbors
    # expands it one graph hop. Both default to None/False (whole bundle).
    scope = args.scope.split(",") if getattr(args, "scope", None) else None
    neighbors = getattr(args, "neighbors", False)
    plan = build_plan(
        bundle,
        rules=rules,
        portable=getattr(args, "portable", False),
        scope=scope,
        neighbors=neighbors,
    )

    if args.out:
        # §3.4 / AGENTS hard rule #8: all file mutations go through
        # atomic_write_text (tmp + os.replace). An interrupted `okf plan --out`
        # must not leave a truncated plan that a downstream `okf update --plan`
        # would then read.
        from .io_utils import atomic_write_text

        atomic_write_text(
            Path(args.out),
            __import__("json").dumps(plan.as_dict(), indent=2, ensure_ascii=False),
        )

    if args.format == "json":
        _print_json(plan.as_dict())
    else:
        by_action: dict[str, int] = {}
        for a in plan.actions:
            by_action[a.action] = by_action.get(a.action, 0) + 1
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_action.items()))
        print(f"# {len(plan.actions)} actions ({summary})")
        # P2-3: when --portable is set, every action's argv uses "." as the
        # bundle argument. Surface the chdir precondition up front so an
        # operator copy-pasting the emitted commands knows where to run
        # them (current spec §7: argv must be "runnable as-is").
        if getattr(args, "portable", False):
            print("# (portable: argv uses '.' — run from the bundle directory)")
        for a in plan.actions:
            cid = f" [{a.concept_id}]" if a.concept_id else ""
            print(f"  [{a.action:20}] {cid}")
            print(f"            {a.why}")
            if a.argv:
                print(f"            -> {' '.join(a.argv)}")
            elif a.argv_template:
                print(f"            -> {' '.join(a.argv_template)}")
            if a.agent_instruction:
                print(f"            NOTE: {a.agent_instruction}")
        # P2-17: surface Plan.skipped in text mode. P1-19 added it to
        # as_dict (JSON consumers could see it) but text consumers had no
        # visibility — suggestions that did not become actions were
        # silently invisible. Mirror the JSON shape so a text user can see
        # what discovery dropped and why.
        if plan.skipped:
            print(f"# {len(plan.skipped)} suggestion(s) skipped:")
            for rule, reason in plan.skipped:
                print(f"#   {rule}: {reason}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from .discover import discover_suggestions

    bundle = _load_bundle(args.bundle)
    rules = args.rules.split(",") if args.rules else None
    # Current spec §7: --scope restricts discovery to a concept set. When
    # --neighbors is also set, the CLI expands the scope by one graph hop
    # (discover itself only filters to the resolved set; the neighbour
    # expansion lives here in the CLI / plan layer).
    scope = args.scope.split(",") if getattr(args, "scope", None) else None
    neighbors = getattr(args, "neighbors", False)
    if scope is not None and neighbors:
        # concept_id_to_str is already imported at module level; only fetch
        # the parser + error here (do NOT re-import concept_id_to_str — that
        # would make it a function-local name and shadow the module global).
        from .paths import ConceptIdError, concept_id_from_str

        graph = bundle.graph()
        expanded: set[str] = set()
        for s in scope:
            s = s.strip()
            if not s:
                continue
            try:
                cid = concept_id_from_str(s)
            except (ConceptIdError, ValueError):
                continue
            expanded.add(concept_id_to_str(cid))
            expanded |= {concept_id_to_str(n) for n in graph.neighbours(cid, max_depth=1)}
        scope = sorted(expanded) if expanded else scope
    report = discover_suggestions(
        bundle,
        rules=rules,
        scope=scope,
        min_confidence=getattr(args, "min_confidence", 0.5),
        include_low_confidence=getattr(args, "include_low_confidence", False),
    )
    if args.format == "json":
        _print_json(report.as_dict())
    else:
        suppressed = getattr(report, "suppressed", [])
        extra = f" ({len(suppressed)} suppressed)" if suppressed else ""
        print(f"# {len(report.suggestions)} suggestions across {len(report.by_rule())} rules{extra}")
        bucket_counts = report.as_dict().get("actionability_counts", {})
        if bucket_counts:
            bucket_text = ", ".join(
                f"{name}={count}" for name, count in bucket_counts.items()
            )
            print(f"  actionability: {bucket_text}")
        for s in report.suggestions:
            print(
                f"  [{s.severity:8}] {s.rule:30} {concept_id_to_str(s.concept_id) if s.concept_id else '-'}"
            )
            print(f"            {s.message}")
            if s.rule == "unlinked_mentions" and "confidence" in s.detail:
                print(f"            confidence: {s.detail['confidence']}")
            if s.action:
                print(f"            -> {s.action}")
    if args.out:
        # §3.4 / AGENTS hard rule #8: atomic write so an interrupted
        # `okf discover --out` cannot leave a truncated suggestions file.
        from .io_utils import atomic_write_text

        atomic_write_text(
            Path(args.out),
            json.dumps(report.as_dict(), indent=2, default=str),
        )
        # P1-4 (iter-3): don't corrupt stdout JSON with a trailing line.
        if args.format != "json":
            print(f"\nWritten: {args.out}")
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    from .extensions import default_registry

    registry = default_registry()
    if args.bundle:
        bundle = _load_bundle(args.bundle)
        # P0-2c: use Bundle.capabilities() (not a re-resolve) so body-syntax
        # auto-activation like okf.cap.wikilinks (SPEC §10) is reflected.
        resolved = bundle.capabilities()
        out = {
            "declared": list(bundle.okf_extensions),
            "active": sorted(resolved.active_ids),
            "undeclared": resolved.undeclared,
            "available": [c.id for c in registry.all()],
        }
    else:
        out = {
            "available": [
                {
                    "id": c.id,
                    "tier": c.tier,
                    "min_spec_version": c.min_spec_version,
                    "description": c.description,
                    "frontmatter_keys": list(c.frontmatter_keys),
                }
                for c in sorted(registry.all(), key=lambda c: (c.tier, c.id))
            ]
        }
    if args.format == "json":
        _print_json(out)
    else:
        print(f"# Capabilities (SPEC v{SPEC_VERSION})")
        if "active" in out:
            print(f"\nBundle-declared: {out['declared']}")
            print(f"Active (after auto-activation): {len(out['active'])}")
            for a in out["active"]:
                print(f"  - {a}")
            if out["undeclared"]:
                print(f"\nUndeclared (not recognised by okf-loom): {out['undeclared']}")
            print(f"\nAll known capabilities ({len(out['available'])}):")
        for c in out["available"]:
            if isinstance(c, str):
                print(f"  - {c}")
            else:
                print(f"  - [{c['tier']}] {c['id']}")
                print(f"      {c['description']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server
    from .config import OkfConfig
    from .viewer.assets import set_operator_consent

    bundle = _load_bundle(args.bundle)
    cfg = OkfConfig.load(bundle.root)
    # Pass viewer config title to run_server if set
    viewer_title = cfg.viewer.title if cfg.viewer.title else None
    # P1-40: an explicit --allow-active-code flag wins over the env var.
    if args.allow_active_code is not None:
        set_operator_consent(bool(args.allow_active_code))

    # Current spec §14: --public / non-loopback bind requires an
    # EXPLICIT acknowledgement. The product default is loopback (single-user
    # admin trust boundary, §15.2). Exposing to a network is the user
    # choosing to change the trust boundary, so it MUST be acknowledged —
    # either via the second-level ``--public-ack`` flag (CI-friendly) or via
    # an interactive ``y/N`` prompt on a TTY. Without either, refuse to
    # start. Closes the historical "warn-and-continue" finding the spec calls out.
    host = "0.0.0.0" if getattr(args, "public", False) else args.host
    non_loopback = host not in ("127.0.0.1", "localhost", "::1")
    if non_loopback:
        ack_flag = getattr(args, "public_ack", False)
        if ack_flag:
            pass  # explicit ack via flag (CI / scripts)
        elif sys.stdin.isatty():
            print(
                f"WARNING: binding to {host} exposes the studio (with NO auth, no\n"
                f"WARNING: rate-limiting, and raw bundle bodies) to every host that\n"
                f"WARNING: can reach this machine. The studio's CSRF + Origin guards\n"
                f"WARNING: only protect against OTHER ORIGINS driving your localhost\n"
                f"WARNING: server — they do NOT protect against users on the network.\n"
                f"WARNING: Only do this on a trusted network.\n",
                file=sys.stderr,
            )
            try:
                answer = input(f"Bind {host}? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                print("Refusing to bind non-loopback without acknowledgement.",
                      file=sys.stderr)
                return 2
        else:
            print(
                f"error: non-loopback bind ({host}) requires --public-ack or an\n"
                f"error: interactive TTY acknowledgement. Re-run with --public-ack\n"
                f"error: (scripts/CI) or run without --public for local-only access.",
                file=sys.stderr,
            )
            return 2

    run_server(
        args.bundle,
        host=host,
        port=args.port,
        watch=not args.no_watch,
        open_browser=not args.no_open,
        name=viewer_title,
        allow_active_code=args.allow_active_code,
        # Current spec §9-§14: --no-edit makes a read-only kiosk (no comments);
        # --no-watch-ui disables SSE live push. Both default to the full
        # studio (D4 — everything ON by default).
        studio_edit=not getattr(args, "no_edit", False),
        studio_live=not getattr(args, "no_watch_ui", False),
        tunnel=getattr(args, "tunnel", False),
    )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Headless change feed for the agent (current spec §12)."""
    from .watch import run_watch

    return run_watch(
        args.bundle,
        emit=args.emit,
        since=args.since,
        auto_repair=args.auto_repair,
        debounce_ms=args.debounce_ms,
    )


def cmd_wait(args: argparse.Namespace) -> int:
    """Agent foreground wait primitive (current spec §12).

    BLOCKS until there is new work for the agent (a new open comment and/or a
    new change event), prints it as JSON, and exits 0 — so the agent wakes,
    acts on it, then calls ``scripts/okf-loom wait`` again. MUST run in the agent's
    foreground (the agent is the processor); never background it.
    """
    from .studio import wait_for_work

    kinds = tuple(k for k in (args.for_ or ("comment",)))
    item = wait_for_work(
        args.bundle,
        kinds=kinds,
        since=args.since,
        timeout=args.timeout,
        interval=args.interval,
    )
    if item is None:
        return 1  # timed out with no work
    _print_json(item)
    return 0


# ---------------------------------------------------------------------------
# agent loop: token + comment claim/resolve (current spec §12/§14)
# ---------------------------------------------------------------------------


def cmd_token(args: argparse.Namespace) -> int:
    """Print the current studio CSRF token (current spec §14).

    The studio server writes a per-session token to the configured session
    directory (``<bundle>/.okf-loom/session/.token`` by default, mode 0600)
    when ``scripts/okf-loom serve`` starts.
    An external agent reads it via this command (or the library) and then
    drives ``/__apply`` / ``/__presence`` / ``/__comment`` / ``/__undo`` over
    HTTP by attaching it as the ``X-OKF-Token`` header.

    Exits 0 with the token on stdout (no trailing newline) when a session is
    live; exits 1 with a diagnostic on stderr when no session exists.
    """
    from .studio import Studio

    token_path = Studio.for_configured_bundle(args.bundle).token_path
    if not token_path.is_file():
        print(
            f"error: no studio session found at {token_path}\n"
            f"error: start one with `scripts/okf-loom serve {args.bundle}`.",
            file=sys.stderr,
        )
        return 1
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        print(f"error: could not read token file: {e}", file=sys.stderr)
        return 1
    if not token:
        print("error: token file is empty.", file=sys.stderr)
        return 1
    sys.stdout.write(token)
    sys.stdout.flush()
    return 0


def cmd_tunnel(args: argparse.Namespace) -> int:
    """Attach/detach a public tunnel on a RUNNING studio session (§9/§14).

    ``serve --tunnel`` decides at startup; this verb decides at runtime —
    no restart, no lost session token, no dropped SSE clients. It reads
    the live server's port from ``<session>/server.json`` and drives the
    token-guarded ``POST /__tunnel`` admin route.
    """
    from .studio import Studio
    import json as _json
    import urllib.request
    import urllib.error

    studio = Studio.for_configured_bundle(args.bundle)
    state_path = studio.server_state_path
    if not state_path.is_file():
        print(
            f"error: no live studio server found for this bundle\n"
            f"error: (missing {state_path}).\n"
            f"error: start one with `scripts/okf-loom serve {args.bundle} --no-open`.",
            file=sys.stderr,
        )
        return 2
    try:
        state = _json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as e:
        print(f"error: could not read server state: {e}", file=sys.stderr)
        return 1
    token_path = studio.token_path
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        print(f"error: could not read session token: {e}", file=sys.stderr)
        return 1

    action = "start"
    if getattr(args, "stop", False):
        action = "stop"
    elif getattr(args, "status", False):
        action = "status"
    host = state.get("host") or "127.0.0.1"
    port = state.get("port")
    req = urllib.request.Request(
        f"http://{host}:{port}/__tunnel",
        data=_json.dumps({"action": action}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-OKF-Token": token},
        method="POST",
    )
    try:
        # cloudflared start can take a while to print its URL; the server
        # waits up to 45s for it, so give the HTTP call more than that.
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = _json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        print(f"error: tunnel {action} failed: "
              f"{payload.get('error') or e}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(
            f"error: could not reach the studio server on port {port}: {e}\n"
            f"error: the session state may be stale (server crashed?). "
            f"Restart with `scripts/okf-loom serve {args.bundle} --no-open`.",
            file=sys.stderr,
        )
        return 1

    if args.format == "json":
        _print_json(payload)
        return 0
    url = payload.get("url")
    if action == "status":
        print(f"tunnel: {url or '(none)'}")
    elif action == "stop":
        print("tunnel stopped" if payload.get("stopped") else "no tunnel was running")
    else:
        already = " (already running)" if payload.get("already_running") else ""
        print(f"Public tunnel: {url}/{already}  (dies with the serve process)")
        print("Anyone with this link can READ the bundle; studio writes still "
              "require the per-session token.")
    return 0


def cmd_comment_claim(args: argparse.Namespace) -> int:
    """Mark a comment ``claimed`` by the agent (current spec §12).

    Part of the documented agent loop:
    ``scripts/okf-loom wait`` (returns an open comment) →
    ``scripts/okf-loom comment-claim <id>`` → do the scoped work via mutators →
    ``scripts/okf-loom comment-resolve <id>``.

    INTENT2-005: claiming a comment also auto-flips presence to
    ``editing <concept>`` so the user sees the agent act on their ask
    immediately (current spec §12). Pass ``--no-presence`` to skip the flip.
    """
    from .studio import Studio

    studio = Studio.for_configured_bundle(args.bundle)
    studio.ensure_session()
    # Claim-time --summary writes request_summary (the "what was
    # asked" short), NOT summary (which is reserved for resolve's "what
    # the agent did" tag). This gives the collapsed preview TWO fields:
    # request_summary = the ask, summary = the done.
    summary = getattr(args, "summary", None)
    updated = studio.update_comment(
        args.comment_id, state="claimed", claimed_by=args.actor,
        request_summary=summary,
    )
    if updated is None:
        print(f"error: no such comment: {args.comment_id}", file=sys.stderr)
        return 1
    # INTENT2-005: auto-flip presence to ``editing <concept>`` so the user
    # sees the agent pick up their ask. The concept comes from the comment.
    if not getattr(args, "no_presence", False):
        concept = updated.get("concept")
        if concept:
            try:
                # Carry the ask into the presence line so the user sees WHAT
                # the agent is editing, not just that it is editing.
                studio.set_presence(
                    actor=args.actor, state="editing", focus=concept,
                    message=summary or updated.get("request_summary") or None,
                )
            except Exception:
                pass  # presence is best-effort; the claim itself succeeded
    if args.format == "json":
        _print_json(updated)
    else:
        print(f"  claimed {args.comment_id} (by {args.actor})")
    return 0


def cmd_comment_resolve(args: argparse.Namespace) -> int:
    """Mark a comment ``resolved`` with optional reply + activity links (§9).

    Part of the documented agent loop. ``--activity id1,id2`` links the
    activity entries the agent just created so the change-list "Undo" on
    this comment reverts the whole pass (§12.5 group-undo).
    """
    from .studio import Studio

    studio = Studio.for_configured_bundle(args.bundle)
    studio.ensure_session()
    activity_ids: list[str] | None = None
    if args.activity:
        activity_ids = [a.strip() for a in args.activity.split(",") if a.strip()]
    summary = getattr(args, "summary", None)
    updated = studio.resolve_comment(
        args.comment_id, reply=args.reply, activity_ids=activity_ids,
        actor=args.actor, summary=summary,
    )
    if updated is None:
        print(f"error: no such comment: {args.comment_id}", file=sys.stderr)
        return 1
    if args.format == "json":
        _print_json(updated)
    else:
        print(f"  resolved {args.comment_id} (by {args.actor})")
    return 0


def cmd_comment_reply(args: argparse.Namespace) -> int:
    """Post a threaded agent reply WITHOUT resolving the comment (§12).

    The mid-thread channel: when a comment is ambiguous or arrives cut
    off, reply in the thread ("did you mean X or Y?") instead of asking
    out-of-band — the parent stays open/claimed, the reply shows live in
    the studio thread, and the user's answer (their reply) wakes ``wait``
    as new work.

    The reply directive is posted with ``state=resolved`` (it is a
    statement, not an ask) so ``wait`` never returns the agent's own reply
    and thread archiving stays one-click.
    """
    from .studio import Studio

    studio = Studio.for_configured_bundle(args.bundle)
    studio.ensure_session()
    parent = studio.get_comment(args.comment_id)
    if parent is None:
        print(f"error: no such comment: {args.comment_id}", file=sys.stderr)
        return 1
    body = args.body
    if body is None and args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    if not (body or "").strip():
        print("error: --body (or --body-file) is required and must be "
              "non-empty", file=sys.stderr)
        return 2
    # Two-level threading cap (§12): replying to a reply hoists to the root
    # so the thread never nests deeper than the UI displays.
    root_id = parent.get("parent_id") or args.comment_id
    reply = studio.post_comment(
        concept=parent.get("concept", ""),
        body=body,
        actor=args.actor,
        parent_id=root_id,
        state="resolved",
    )
    if args.format == "json":
        _print_json(reply)
    else:
        print(f"  replied to {root_id} (by {args.actor}): {reply['id']}")
    return 0


def cmd_comment_list(args: argparse.Namespace) -> int:
    """List comments / directives (current spec §12)."""
    from .studio import Studio

    studio = Studio.for_configured_bundle(args.bundle)
    comments = studio.list_comments(state=args.state, concept=args.concept)
    if args.format == "json":
        _print_json(comments)
    else:
        if not comments:
            print("(no comments)")
            return 0
        for c in comments:
            cid = c.get("id", "?")[:10]
            state = c.get("state", "?")
            concept = c.get("concept", "?")
            body = (c.get("body", "") or "")[:60]
            print(f"  [{state:9}] {cid}  {concept:30}  {body}")
    return 0


def cmd_presence(args: argparse.Namespace) -> int:
    """Set agent presence (current spec §12).

    Mirrors ``POST /__presence`` from the CLI: ``okf presence <bundle>
    --state editing --focus tables/orders``.
    """
    from .studio import Studio

    studio = Studio.for_configured_bundle(args.bundle)
    studio.ensure_session()
    presence = studio.set_presence(
        actor=args.actor, state=args.state, focus=args.focus,
        message=getattr(args, "message", None),
    )
    if args.format == "json":
        _print_json(presence)
    else:
        focus = f" focus={presence['focus']}" if presence.get("focus") else ""
        msg = f" — {presence['message']}" if presence.get("message") else ""
        print(f"  presence: {presence['actor']} {presence['state']}{focus}{msg}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from .render import render_single_file
    from .viewer.assets import set_operator_consent, effective_allow_active_code

    # P2-8 (parity with serve/build): an explicit --allow-active-code flag wins
    # over the env var. Without this, `okf render` had no CLI consent surface.
    if args.allow_active_code is not None:
        set_operator_consent(bool(args.allow_active_code))

    bundle = _load_bundle(args.bundle)
    # P2-15 (iter-3): parity with cmd_serve/build_site — warn the operator when
    # the effective active-code gate is open (overrides + plugins run). Without
    # this, `okf render --allow-active-code` silently applied overrides with no
    # trust-implication notice.
    if effective_allow_active_code(bundle.root):
        import sys as _sys
        print(
            f"WARNING: active code (viewer overrides + plugins) is ENABLED for "
            f"bundle {bundle.root}. Only run this for bundles whose "
            f"override/plugin sources you trust.",
            file=_sys.stderr,
        )
    # Default output is in CWD, NOT inside the bundle (avoids polluting the
    # user's curated bundle tree with rendered artifacts).
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path.cwd() / f"{bundle.name}.html"
    stats = render_single_file(bundle, out_path, name=args.name)
    print(
        f"Rendered {stats['concepts']} concepts / {stats['edges']} edges "
        f"-> {out_path} ({stats['bytes']} bytes)"
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from .render import build_site
    from .viewer.assets import set_operator_consent

    # P1-40: an explicit --allow-active-code flag wins over the env var.
    if args.allow_active_code is not None:
        set_operator_consent(bool(args.allow_active_code))

    bundle = _load_bundle(args.bundle)
    out_dir = Path(args.out)
    stats = build_site(
        bundle, out_dir, target=args.target, name=args.name,
        allow_active_code=args.allow_active_code,
    )
    print(f"Built {args.target} site -> {out_dir}")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from .index import regenerate_indexes

    bundle = _load_bundle(args.bundle)
    if args.dry_run:
        from .index import plan_index_regeneration

        plan = plan_index_regeneration(bundle)
        _print_json(plan)
        return 0
    written = regenerate_indexes(bundle, frozen=args.frozen)
    print(f"Regenerated {len(written)} index.md file(s):")
    for p in written:
        print(f"  {p}")
    if getattr(args, "emit_json", False):
        # Current spec §8: emit portable derived JSON artifacts under
        # <bundle>/.okf-loom/index/. Byte-stable, no absolute paths, atomic writes.
        from .index import emit_derived_json

        derived = emit_derived_json(bundle)
        print(f"Emitted {len(derived)} derived JSON artifact(s):")
        for p in derived:
            try:
                rel = Path(p).relative_to(bundle.root)
                print(f"  {rel}")
            except ValueError:
                print(f"  {p}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    from .log import append_log_entry

    bundle = _load_bundle(args.bundle)
    if args.append:
        out = append_log_entry(
            bundle,
            kind=args.kind or "Update",
            entry=args.append,
            date_str=args.date,
            log_rel=args.log_path,
            dry_run=args.dry_run,
        )
        print(out)
    else:
        # §3.2: sort logs by relative path so listing order is deterministic
        # regardless of Bundle insertion order.
        for rel, log in sorted(bundle.logs.items()):
            print(f"# {rel}")
            for e in log.entries:
                print(f"  {e.date}  ({len(e.lines)} entries)")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from .update import load_plan, apply_plan

    bundle = _load_bundle(args.bundle)
    plan = load_plan(args.plan)
        # Current spec §10/§13: route through the studio funnel when a session is
    # active (P0-1). Falls back to the compatibility atomic path otherwise.
    summary = _studio_aware_apply(
        bundle, plan, dry_run=args.dry_run, args=args,
    )
    if args.format == "json":
        _print_json(summary)
    else:
        print(f"Plan: {summary['plan_kind']}  ops: {summary['total_ops']}")
        for op, result in summary["results"]:
            mark = "✓" if result.get("applied") else "·"
            # P0-3 (iter-2): P2-24 made `op` a JSON-safe dict (as_dict), not an
            # UpdateOp instance. Read target/kind via dict keys; fall back to
            # attribute access for any caller still passing UpdateOp objects.
            target = op.get("target") if isinstance(op, dict) else getattr(op, "target", None)
            kind = op.get("kind") if isinstance(op, dict) else getattr(op, "kind", "?")
            # `target` from as_dict() is ALREADY a slash-string (concept_id_to_str
            # applied during serialization); only re-render when it's still a
            # ConceptId tuple (UpdateOp instance path). Rendering a string via
            # concept_id_to_str raises ConceptIdError on multi-segment ids.
            if isinstance(target, tuple):
                cid_str = concept_id_to_str(target) if target else "-"
            else:
                cid_str = target if target else "-"
            reason = result.get("reason")
            suffix = f" ({reason})" if reason else ""
            print(f"  {mark} [{kind}] {cid_str}{suffix}")
        # Surface skipped actions with actionable hints
        skipped_actions = summary.get("skipped_actions", [])
        if skipped_actions:
            index_skips = [s for s in skipped_actions if s.get("reason") == "run_okf_index_directly"]
            agent_skips = [s for s in skipped_actions if s.get("reason") == "needs_agent_input"]
            other_skips = [s for s in skipped_actions if s.get("reason") not in ("run_okf_index_directly", "needs_agent_input")]
            total_deferred = len(index_skips) + len(agent_skips) + len(other_skips)
            if total_deferred:
                print(f"\n  {total_deferred} action(s) deferred:")
                if index_skips:
                    print(f"    {len(index_skips)} index action(s) → Run: okf index {args.bundle}")
                if agent_skips:
                    print(f"    {len(agent_skips)} action(s) need agent input (see plan for details)")
                if other_skips:
                    print(f"    {len(other_skips)} action(s) skipped ({', '.join(set(s.get('reason','?') for s in other_skips))})")
    # P2-4: a fully-stale plan (every target renamed / deleted) applies
    # nothing and would otherwise exit 0 — indistinguishable from a clean
    # idempotent re-run. SPEC §3.7 classifies hard-failure reasons
    # (concept_not_found / target_concept_not_found / bad_args:* /
    # unknown_op_kind:*) as operation-level failures → exit 1. Pure
    # idempotent no-ops (all already_*/same_value) still exit 0. This
    # mirrors :func:`_single_op_exit_code`'s reason classification but
    # applies the multi-op aggregation (any hard-failure op fails the
    # run when nothing applied).
    if summary.get("applied", 0) == 0:
        for _op_dict, op_result in summary.get("results", []):
            if _is_hard_failure_reason(op_result.get("reason")):
                return 1
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    print(f"# okf-loom {LOOM_VERSION} implements SPEC v{SPEC_VERSION}")
    print("# No automated migrations are defined for this version yet.")
    # Accept both positional `bundle` and `--bundle <path>` for ergonomics.
    bundle_path = args.bundle or getattr(args, "bundle_flag", None)
    if bundle_path:
        bundle = _load_bundle(bundle_path)
        print(f"# Bundle declares okf_version={bundle.okf_version!r}")
        if bundle.okf_extensions:
            print(f"# Bundle declares okf_extensions={bundle.okf_extensions}")
        if bundle.okf_version and bundle.okf_version != SPEC_VERSION:
            print(f"# NOTE: bundle version differs from toolkit version; "
                  f"best-effort consumption per SPEC §11.")
    # P2-48: --check/--apply/--to-spec are accepted for forward-compat but
    # have no effect yet (no migrations defined). Surface this clearly so a
    # user scripting `okf upgrade --apply` doesn't silently no-op.
    if getattr(args, "apply", False) or getattr(args, "check", False):
        print("# NOTE: --check/--apply have no effect yet; no migrations are "
              "defined for SPEC v0.1.")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from .bootstrap import bootstrap_bundle

    result = bootstrap_bundle(args.dest, name=args.name)
    print(f"Bootstrapped OKF bundle '{result['name']}' at {result['dest']}")
    for f in result["created"]:
        print(f"  created: {f}")
    print(
        f"\nNext steps:\n"
        f"  1. Add concept .md files (each needs at least `type:` frontmatter).\n"
        f"  2. Run `scripts/okf-loom validate {result['dest']}` to check conformance.\n"
        f"  3. Run `scripts/okf-loom discover {result['dest']}` to find gaps.\n"
        f"  4. Run `scripts/okf-loom serve {result['dest']}` to view the wiki."
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .bootstrap import import_directory

    result = import_directory(
        args.src, args.dest,
        default_type=args.default_type,
        overwrite=args.overwrite,
    )
    prefix = "Bootstrapped + imported" if result["bootstrapped"] else "Imported"
    print(
        f"{prefix} {result['imported']} file(s) into {result['dest']} "
        f"({result['skipped']} skipped)"
    )
    if result["guessed_types"]:
        print("\nGuessed types (review and adjust):")
        for filename, type_name in sorted(result["guessed_types"].items()):
            print(f"  {filename:40} → {type_name}")
    print(
        f"\nNext steps:\n"
        f"  1. Review the guessed types above; adjust frontmatter as needed.\n"
        f"  2. Run `scripts/okf-loom validate {result['dest']}`.\n"
        f"  3. Run `scripts/okf-loom discover {result['dest']}` to find missing links/descriptions.\n"
        f"  4. Run `scripts/okf-loom serve {result['dest']}` to view the wiki."
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """``okf init --bundle DIR [--name NAME]`` (current spec §8).

    Extends ``bootstrap`` with an ``okf-loom.config.yaml`` scaffolding file.
    Reuses ``bootstrap_bundle`` for index.md + log.md and its
    non-empty-destination guard (FileExistsError). Never clobbers existing
    files: if the destination is non-empty, bootstrap raises before this
    command writes anything.
    """
    from .bootstrap import bootstrap_bundle
    from .config import CONFIG_FILENAME, DEFAULT_CONFIG_YAML
    from .io_utils import atomic_write_text

    bundle_dir = args.bundle
    result = bootstrap_bundle(bundle_dir, name=args.name)

    # Write the commented-defaults config (current spec §8). bootstrap_bundle
    # has already guaranteed the destination is empty, so there is no
    # existing okf-loom.config.yaml to clobber. Atomic write protects concurrent
    # readers from seeing a half-written file.
    cfg_path = Path(bundle_dir) / CONFIG_FILENAME
    atomic_write_text(cfg_path, DEFAULT_CONFIG_YAML)
    result["created"].append(CONFIG_FILENAME)

    if args.format == "json":
        _print_json(result)
    else:
        print(f"Initialised OKF bundle '{result['name']}' at {result['dest']}")
        for f in result["created"]:
            print(f"  created: {f}")
        print(
            f"\nNext steps:\n"
            f"  1. Add concept .md files (each needs at least `type:` frontmatter).\n"
            f"  2. Edit `{CONFIG_FILENAME}` to customise viewer/search/validate defaults.\n"
            f"  3. Run `scripts/okf-loom validate {result['dest']}` to check conformance.\n"
            f"  4. Run `scripts/okf-loom discover {result['dest']}` to find gaps.\n"
            f"  5. Run `scripts/okf-loom serve {result['dest']}` to view the wiki."
        )
    return 0


# ---------------------------------------------------------------------------
# Authoring mutator verbs (current spec §7)
# ---------------------------------------------------------------------------


def cmd_write_concept(args: argparse.Namespace) -> int:
    """Create or update a single concept file (current spec §7).

    P1-25: this is now a thin argparse→kwargs adapter over the
    round-trip-safe library seam :func:`okf_loom.update.write_concept`.
    All mutation logic (parse → mutate → serialize → atomic_write, with
    P1-24 round-trip preservation, P2-26 CREATE-path tag dedupe, and the
    P0-1 concept_id_to_path-no-double-join guard) lives in the library.
    """
    from .update import write_concept, WriteConceptError

    # P1-2 (ARCH2-002): route through the studio funnel when an active
    # session exists, so write_concept is attributed + undoable + broadcast.
    # Falls back to the compatibility atomic path when no session is live.
    studio = _maybe_studio_for_bundle(args.bundle)
    try:
        result = write_concept(
            args.bundle,
            args.id,
            type=args.type,
            title=args.title,
            description=args.description,
            resource=args.resource,
            timestamp=args.timestamp,
            tags=list(args.tag) if args.tag else None,
            body=args.body,
            body_file=args.body_file,
            force=args.force,
            defaults=not getattr(args, "no_defaults", False),
            studio=studio,
            actor=getattr(args, "actor", "agent") or "agent",
            origin="mutator",
            group_id=getattr(args, "group_id", None),
        )
    except WriteConceptError as e:
        # Map library failure codes to the documented exit codes.
        print(f"error: {e.message}", file=sys.stderr)
        if e.code == "bundle_not_found":
            return 2
        # body_refused, reserved_filename, rev_conflict, studio_blocked →
        # operation-level failure (exit 1).
        return 1

    if args.format == "json":
        # P2-27: emit the canonical 3-key shape ``{status, path, id}``.
        # The compatibility ``created`` / ``updated`` alias keys are also present
        # (deprecated) so consumers that still read them keep working.
        _print_json(result)
    else:
        action = "Created" if result["status"] == "created" else "Updated"
        print(f"{action} concept '{result['id']}' at {result['path']}")
        if result.get("defaults_applied"):
            print(f"  (defaulted: {', '.join(result['defaults_applied'])} — "
                  f"pass --no-defaults to skip)")
    return 0


def cmd_set_frontmatter(args: argparse.Namespace) -> int:
    """Set a frontmatter key on a concept (current spec §7)."""
    from .update import load_plan, apply_plan
    import json as _json

    value: Any
    if args.json_value:
        value = _json.loads(args.value)
    else:
        value = args.value

    plan_data = {
        "bundle_root": args.bundle,
        "description": f"Set {args.key} on {args.id}",
        "plan_kind": "update",
        "ops": [{
            "kind": "set_frontmatter",
            "target": args.id,
            "args": {"key": args.key, "value": value},
        }],
    }
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        _json.dump(plan_data, f)
        plan_path = f.name
    try:
        bundle = _load_bundle(args.bundle)
        plan = load_plan(plan_path)
        result = _studio_aware_apply(bundle, plan, dry_run=args.dry_run, args=args)
        if args.format == "json":
            _print_json(result)
        else:
            for op_str, op_result in result.get("results", []):
                status = "✓" if op_result.get("applied") else "·"
                reason = op_result.get("reason", "")
                detail = f" ({reason})" if reason else ""
                print(f"  {status} set {args.key}={value!r} on {args.id}{detail}")
        # P2-28: single-op verbs exit 1 when the target concept doesn't exist.
        return _single_op_exit_code(result)
    finally:
        Path(plan_path).unlink(missing_ok=True)


def cmd_update_section(args: argparse.Namespace) -> int:
    """Replace or append to ONE section of a concept body (current spec §7).

    The block-level partial-update mutator: swap a section, keep the rest
    of the document — no whole-body reconstruction, no staging copies.
    Fail-closed on a missing heading (unless ``--create-if-missing``) and
    on ambiguous duplicate headings.
    """
    from .update import load_plan
    import json as _json

    if (args.body is None) == (args.body_file is None):
        print("error: exactly one of --body / --body-file is required",
              file=sys.stderr)
        return 2
    body = args.body
    if args.body_file is not None:
        body = Path(args.body_file).read_text(encoding="utf-8")

    plan_data = {
        "bundle_root": args.bundle,
        "description": f"Update section {args.heading!r} on {args.id}",
        "plan_kind": "update",
        "ops": [{
            "kind": "update_section",
            "target": args.id,
            "args": {
                "heading": args.heading,
                "body": body,
                "mode": "append" if args.append else "replace",
                "create_if_missing": args.create_if_missing,
            },
        }],
    }
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        _json.dump(plan_data, f)
        plan_path = f.name
    try:
        bundle = _load_bundle(args.bundle)
        plan = load_plan(plan_path)
        result = _studio_aware_apply(bundle, plan, dry_run=args.dry_run, args=args)
        if args.format == "json":
            _print_json(result)
        else:
            for _op, op_result in result.get("results", []):
                status = "✓" if op_result.get("applied") else "·"
                reason = op_result.get("reason", "")
                detail = f" ({reason})" if reason else ""
                verb = "append to" if args.append else "update"
                print(f"  {status} {verb} section {args.heading!r} on {args.id}{detail}")
        return _single_op_exit_code(result)
    finally:
        Path(plan_path).unlink(missing_ok=True)


def cmd_replace_text(args: argparse.Namespace) -> int:
    """Exact textual patch on a concept body (current spec §7).

    ``--old`` must match exactly once (or pass ``--all``); zero matches or
    an ambiguous match fails closed with exit 1 — never a silent guess.
    """
    from .update import load_plan
    import json as _json

    if (args.old is None) == (args.old_file is None):
        print("error: exactly one of --old / --old-file is required",
              file=sys.stderr)
        return 2
    if (args.new is None) == (args.new_file is None):
        print("error: exactly one of --new / --new-file is required",
              file=sys.stderr)
        return 2
    old = args.old if args.old is not None else Path(args.old_file).read_text(encoding="utf-8")
    new = args.new if args.new is not None else Path(args.new_file).read_text(encoding="utf-8")

    plan_data = {
        "bundle_root": args.bundle,
        "description": f"Replace text on {args.id}",
        "plan_kind": "update",
        "ops": [{
            "kind": "replace_text",
            "target": args.id,
            "args": {"old": old, "new": new, "all": args.all},
        }],
    }
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        _json.dump(plan_data, f)
        plan_path = f.name
    try:
        bundle = _load_bundle(args.bundle)
        plan = load_plan(plan_path)
        result = _studio_aware_apply(bundle, plan, dry_run=args.dry_run, args=args)
        if args.format == "json":
            _print_json(result)
        else:
            for _op, op_result in result.get("results", []):
                status = "✓" if op_result.get("applied") else "·"
                reason = op_result.get("reason", "")
                detail = f" ({reason})" if reason else ""
                print(f"  {status} replace text on {args.id}{detail}")
        return _single_op_exit_code(result)
    finally:
        Path(plan_path).unlink(missing_ok=True)


def cmd_link_add(args: argparse.Namespace) -> int:
    """Add a markdown link (+ optional typed relation) between concepts (current spec §7)."""
    from .update import load_plan, apply_plan
    import json as _json

    ops: list[dict] = [{
        "kind": "add_link",
        "target": args.source,
        "args": {
            "label": args.label or args.target.split("/")[-1],
            "target_concept_id": args.target,
            "section": args.section,
            # P2-6 (iter-3): opt-in forward reference. Default False keeps the
            # fail-closed target-existence check; the flag allows a link to
            # not-yet-written knowledge (SPEC §3 / AGENTS.md hard rule #3).
            "allow_forward_reference": getattr(
                args, "allow_forward_reference", False
            ),
        },
    }]
    if args.relation:
        ops.append({
            "kind": "add_relation",
            "target": args.source,
            "args": {
                "target_concept_id": args.target,
                "relation_type": args.relation,
                "detail": args.evidence or "",
            },
        })

    plan_data = {
        "bundle_root": args.bundle,
        "description": f"Link {args.source} → {args.target}",
        "plan_kind": "update",
        "ops": ops,
    }
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        _json.dump(plan_data, f)
        plan_path = f.name
    try:
        bundle = _load_bundle(args.bundle)
        plan = load_plan(plan_path)
        result = _studio_aware_apply(bundle, plan, dry_run=args.dry_run, args=args)
        if args.format == "json":
            _print_json(result)
        else:
            for op_str, op_result in result.get("results", []):
                status = "✓" if op_result.get("applied") else "·"
                reason = op_result.get("reason", "")
                detail = f" ({reason})" if reason else ""
                print(f"  {status} link {args.source} → {args.target}{detail}")
        # P2-28: single-op verbs exit 1 when the target concept doesn't exist.
        # (cmd_link_add can fire TWO ops when --relation is given; the
        # concept_not_found skip applies to the SOURCE concept id, which is
        # the same for both ops, so a single _single_op_exit_code call
        # covers the whole verb.)
        return _single_op_exit_code(result)
    finally:
        Path(plan_path).unlink(missing_ok=True)


def cmd_entity_add(args: argparse.Namespace) -> int:
    """Add an entity to a concept (current spec §7)."""
    from .update import load_plan, apply_plan
    import json as _json

    entity_args: dict[str, Any] = {"label": args.label}
    if args.kind:
        entity_args["kind"] = args.kind
    if args.entity_id:
        entity_args["entity_id"] = args.entity_id
    if args.alias:
        entity_args["aliases"] = list(args.alias)

    plan_data = {
        "bundle_root": args.bundle,
        "description": f"Add entity '{args.label}' to {args.id}",
        "plan_kind": "update",
        "ops": [{
            "kind": "add_entity",
            "target": args.id,
            "args": entity_args,
        }],
    }
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        _json.dump(plan_data, f)
        plan_path = f.name
    try:
        bundle = _load_bundle(args.bundle)
        plan = load_plan(plan_path)
        result = _studio_aware_apply(bundle, plan, dry_run=args.dry_run, args=args)
        if args.format == "json":
            _print_json(result)
        else:
            for op_str, op_result in result.get("results", []):
                status = "✓" if op_result.get("applied") else "·"
                reason = op_result.get("reason", "")
                detail = f" ({reason})" if reason else ""
                print(f"  {status} entity '{args.label}' → {args.id}{detail}")
        # P2-28: single-op verbs exit 1 when the target concept doesn't exist.
        return _single_op_exit_code(result)
    finally:
        Path(plan_path).unlink(missing_ok=True)


def cmd_repair(args: argparse.Namespace) -> int:
    """Mechanical-fix umbrella command (current spec §7)."""
    from .plan import build_plan, Plan as PlanObj
    from .update import load_plan, apply_plan
    from .index import regenerate_indexes
    import json as _json

    bundle = _load_bundle(args.bundle)
    plan = build_plan(bundle)

    # P1-17: filter plan.actions to MECHANICAL-only in BOTH dry-run and apply.
    # Mechanical = runnable argv + no template/instruction/confidence. Only
    # add_link / create_index / refresh_index / mirror_relation qualify; this
    # keeps the dry-run and apply action sets identical (both mechanical-only)
    # and prevents leaking agent-instruction templates (add_description,
    # connect, fix_broken_link, add_relation) into the repair preview.
    def _is_mechanical(a) -> bool:
        return (
            a.argv is not None
            and a.confidence is None
            and a.agent_instruction is None
            and a.argv_template is None
        )

    show_all = args.all or (not args.indexes and not args.mirror_relations)
    mechanical: list = []
    for action in plan.actions:
        if not _is_mechanical(action):
            continue
        if show_all or (
            (args.indexes and action.action in ("create_index", "refresh_index"))
            or (args.mirror_relations and action.action == "mirror_relation")
        ):
            mechanical.append(action)

    if not args.apply:
        # Dry-run: print plan
        if args.format == "json":
            _print_json({
                "bundle_root": plan.bundle_root,
                "mechanical_actions": len(mechanical),
                "actions": [a.as_dict() for a in mechanical],
            })
        else:
            print(f"# {len(mechanical)} mechanical fix(es)")
            for a in mechanical:
                cid = f" [{a.concept_id}]" if a.concept_id else ""
                print(f"  [{a.action:20}] {cid}")
                print(f"            {a.why}")
                if a.argv:
                    print(f"            -> {' '.join(a.argv)}")
        return 0

    # Apply path.
    applied_count = 0
    mutation_applied = 0
    apply_result: dict | None = None
    index_regen = False

    # P1-16: include mechanical `add_link` actions (from unlinked_mentions,
    # which §6.2 L406 classifies as mechanical) in the scoped-Plan→apply_plan,
    # using the same mechanism as mirror_relation. Both are argv-bearing
    # mutations that update.load_plan can convert to add_link ops.
    apply_actions = [
        a for a in mechanical if a.action in ("mirror_relation", "add_link")
    ]
    if apply_actions:
        scoped_plan = PlanObj(bundle_root=plan.bundle_root, actions=apply_actions)
        plan_data = scoped_plan.as_dict()
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            _json.dump(plan_data, f)
            plan_path = f.name
        try:
            loaded = load_plan(plan_path)
            # Current spec §10/§13: route through the studio funnel so mechanical
            # repairs are attributed, undoable, and broadcast a change/graph
            # event when a session is active (P0-1).
            apply_result = _studio_aware_apply(
                bundle, loaded, dry_run=False, args=args,
                action_origin="auto-repair",
            )
            for _op_str, op_result in apply_result.get("results", []):
                if op_result.get("applied"):
                    mutation_applied += 1
                    applied_count += 1
        finally:
            Path(plan_path).unlink(missing_ok=True)

    # Run index regen AFTER mutations (so indexes reflect new links)
    has_index_work = any(
        a.action in ("create_index", "refresh_index") for a in mechanical
    )
    if has_index_work:
        bundle.invalidate()  # reload graph after mutations
        bundle = _load_bundle(args.bundle)  # fresh load
        # §11 / AGENTS.md hard rule #7 / P2-2 / ARCH2-003: when a session is
        # active, route the index regen through the studio funnel so each
        # refresh is attributed (actor=agent, origin=auto-repair), appears
        # on the change list, and emits a `changed` event for live patching.
        # undoable=False + group_id lets one Undo revert the whole repair
        # pass instead of stamping N undo buttons for N indexes.
        group_id = getattr(args, "group_id", None)
        actor = getattr(args, "actor", "agent") or "agent"
        studio = _maybe_studio_for_bundle(
            args.bundle, group_id=group_id, actor=actor,
        )
        regenerate_indexes(
            bundle,
            studio=studio,
            group_id=group_id,
            actor=actor,
            origin="auto-repair",
        )
        applied_count += 1
        index_regen = True

    # P1-18: guard ALL trailing print() calls so --format json emits a single
    # parseable JSON document on stdout (no interleaved text corruption).
    if args.format == "json":
        _print_json({
            "bundle_root": plan.bundle_root,
            "applied": applied_count,
            "mutations_applied": mutation_applied,
            "index_regen": index_regen,
            "apply_result": apply_result,
        })
    else:
        if mutation_applied:
            print(f"  Applied {mutation_applied} mutation fix(es)")
        if index_regen:
            print("  ✓ Regenerated indexes")
        if applied_count == 0:
            print("  (nothing to apply)")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts/okf-loom",
        description=(
            f"okf-loom v{LOOM_VERSION} (implements OKF SPEC v{SPEC_VERSION}). "
            "Validate, search, discover, view, and update Open Knowledge Format bundles."
        ),
    )
    p.add_argument("--version", action="version", version=f"okf-loom {LOOM_VERSION} (SPEC v{SPEC_VERSION})")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_bundle(sp, dest="bundle"):
        sp.add_argument(dest, help="Path to an OKF bundle directory")
        sp.add_argument("--format", choices=("text", "json", "md", "dot"), default="text")

    def add_studio_flags(sp) -> None:
        """Add the live-studio attribution flags to a mutator subparser.

        ``--group-id`` lets a multi-step agent pass share an undo group
        (§12.5) so the whole pass reverts with one ``POST /__undo {group_id}``.
        ``--actor`` overrides the activity actor (default ``agent``) for
        attribution in events.jsonl + the change list.
        """
        sp.add_argument(
            "--group-id", dest="group_id", default=None,
            help="Group this write with others sharing the same group id for "
                  "one-click group Undo (current spec §13).",
        )
        sp.add_argument(
            "--actor", dest="actor", default="agent",
            help="Activity actor for the change-list entry (default: agent).",
        )

    sp = sub.add_parser("info", help="Show bundle summary")
    add_bundle(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("validate", help="Validate bundle against SPEC §9 (+ soft warnings)")
    add_bundle(sp)
    sp.add_argument("--strict", action="store_true", help="Treat warnings as errors")
    sp.add_argument(
        "--profile",
        choices=("spec", "producer", "loose"),
        default=None,
        help="Validation profile (spec=default, producer=strict recommended keys, loose=tolerate missing type)",
    )
    sp.add_argument(
        "--fail-on-broken-links",
        action="store_true",
        help="Promote broken-link warnings to errors",
    )
    sp.add_argument(
        "--checks",
        help=(
            "Comma-separated subset of checks to run (default: all). "
            "Accepts category names (spec_version_check, "
            "concept_required_keys, concept_recommended_keys, "
            "reserved_filenames, link_integrity, index_structure, "
            "log_structure, tag_normalization, capability_declarations, "
            "timestamp_validity) OR dotted finding codes from validate "
            "output (e.g. link.broken, concept.missing_recommended_keys, "
            "concept.missing_type). A dotted code enables the category "
            "check that produces it."
        ),
    )
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("graph", help="Print the bundle link graph")
    add_bundle(sp)
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser("graph-quality", help="Report advisory graph usefulness signals")
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_graph_quality)

    sp = sub.add_parser("search", help="Search concepts (lexical/semantic/hybrid/tag/entity/relation)")
    add_bundle(sp, dest="bundle")
    sp.add_argument("query", nargs="?", default="", help="Search query (optional in relation mode)")
    sp.add_argument("--mode", default=None,
                    choices=("lexical", "semantic", "hybrid", "tag", "entity", "relation"))
    sp.add_argument("--type", dest="type", help="Filter by concept type")
    sp.add_argument("--tag", help="Filter by tag")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument(
        "--min-semantic-score", type=float,
        help="Opt-in SemanticLite relevance threshold in [0,1] (semantic/hybrid)",
    )
    sp.add_argument(
        "--hybrid-require", choices=("any", "lexical", "semantic", "both"),
        default="any",
        help="Require evidence from selected backend(s) before Hybrid RRF",
    )
    sp.add_argument("--relation", help="Relation type filter (relation mode)")
    sp.add_argument("--source", help="Source concept id filter (relation mode)")
    sp.add_argument("--target", help="Target concept id filter (relation mode)")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("discover", help="Discover missing links/indexes/relations; emit agent-readable plan")
    add_bundle(sp)
    sp.add_argument("--rules", help="Comma-separated subset of discovery rules")
    sp.add_argument("--out", help="Write JSON plan to this path")
    # Current spec §7: scoped enrichment.
    sp.add_argument(
        "--scope",
        help="Comma-separated concept ids to restrict discovery to (current spec §7)",
    )
    sp.add_argument(
        "--neighbors",
        action="store_true",
        help="Expand --scope to direct graph neighbors (1 hop, in+out edges). "
             "The expansion happens here in the CLI; discover itself only "
             "filters to the resolved scope set.",
    )
    sp.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="Minimum confidence for noisy heuristic suggestions such as unlinked_mentions (default 0.5)",
    )
    sp.add_argument(
        "--include-low-confidence",
        action="store_true",
        help="Include low-confidence heuristic suggestions instead of listing them under suppressed",
    )
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("plan", help="Build an executable action plan (discovery + indexes + relation mirroring)")
    # P3-2: plan emits text or JSON only; do NOT use add_bundle (which would
    # silently accept md/dot). Inline the positional bundle + restricted format.
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.add_argument("--rules", help="Comma-separated subset of discovery rules")
    sp.add_argument("--out", help="Write JSON plan to this path")
    # Current spec §7: scoped enrichment.
    sp.add_argument(
        "--scope",
        help="Comma-separated concept ids to restrict the plan to (current spec §7)",
    )
    sp.add_argument(
        "--neighbors",
        action="store_true",
        help="Expand --scope to direct graph neighbors (1 hop, in+out edges)",
    )
    # P2-3: --portable emits bundle-relative argv ("." as the bundle arg) so a
    # committed plan is portable across machines. Default (off) emits the
    # resolved absolute bundle path so argv is runnable as-is from any CWD
    # (current spec §7). When --portable is set, operators MUST run the emitted
    # commands from the bundle directory (the chdir precondition is surfaced
    # in the plan text output and the Plan.as_dict docstring).
    sp.add_argument(
        "--portable",
        action="store_true",
        help="Emit bundle-relative argv ('.' as the bundle arg) so the plan "
             "is portable across machines. Run the emitted commands from the "
             "bundle directory. Default (off) emits absolute paths.",
    )
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("capabilities", help="List/resolve OKF capabilities")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.add_argument("--bundle", help="Resolve capabilities for this bundle")
    sp.set_defaults(func=cmd_capabilities)

    sp = sub.add_parser("serve", help="Run the live HTTP wiki server")
    sp.add_argument("bundle")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-watch", action="store_true")
    sp.add_argument("--no-open", action="store_true")
    sp.add_argument(
        "--allow-active-code",
        dest="allow_active_code",
        action="store_true",
        default=None,
        help="Operator consent: enable viewer overrides + plugins for bundles "
             "that declare viewer.allow_active_code: true (current spec §14). "
             "Default defers to the OKF_LOOM_ALLOW_ACTIVE_CODE env var.",
    )
    # Current spec §9-§14: studio flags. Defaults make the full studio ON (D4).
    sp.add_argument(
        "--no-edit",
        dest="no_edit",
        action="store_true",
        help="Read-only kiosk: live reads stay on, but commenting/directing "
             "(POST endpoints) are disabled (current spec §9/§14).",
    )
    sp.add_argument(
        "--tunnel",
        dest="tunnel",
        action="store_true",
        help="Also start a Cloudflare quick tunnel (needs cloudflared on "
             "PATH) and print a public https URL. The tunnel hostname is "
             "added to allowed_hosts at runtime so studio comments/edits "
             "work through it; the server itself stays on loopback. Anyone "
             "with the link can READ the bundle.",
    )
    sp.add_argument(
        "--public",
        dest="public",
        action="store_true",
        help="Opt into a network bind (host 0.0.0.0). Requires --public-ack "
             "or an interactive y/N ack (§15.2). Default binds 127.0.0.1 "
             "(loopback-only trust boundary).",
    )
    sp.add_argument(
        "--public-ack",
        dest="public_ack",
        action="store_true",
        help="Acknowledge the trust-boundary change implied by --public / a "
             "non-loopback --host non-interactively (CI / scripts). Without "
             "this, --public on a non-TTY refuses to start (exit 2).",
    )
    sp.add_argument(
        "--no-watch-ui",
        dest="no_watch_ui",
        action="store_true",
        help="Disable SSE live push (studio reads stay available).",
    )
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser(
        "watch",
        help="Headless change feed for the agent (current spec §12); no HTTP server",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument(
        "--emit",
        choices=("jsonl", "text"),
        default="jsonl",
        help="Output format per line (default jsonl, the §7.2 event schema)",
    )
    sp.add_argument(
        "--since",
        default=None,
        help="Replay events from events.jsonl after this event id, then tail live",
    )
    sp.add_argument(
        "--auto-repair",
        dest="auto_repair",
        action="store_true",
        help="Run the mechanical okf repair (index regen + relation mirroring) "
             "on change, debounced by --debounce-ms",
    )
    sp.add_argument(
        "--debounce-ms",
        dest="debounce_ms",
        type=int,
        default=600,
        help="Coalesce auto-repair runs by this many milliseconds (default 600)",
    )
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser(
        "wait",
        help="Agent foreground wait: block until there is work (a new open "
             "comment and/or change), print it, exit (current spec §12). Run this in "
             "your foreground — you are the processor; do NOT background it.",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument(
        "--for",
        dest="for_",
        nargs="+",
        choices=("comment", "change"),
        default=None,
        help="What to wait for: comment (a new OPEN user comment) and/or "
             "change (a new changed/created/removed event). Default: comment.",
    )
    sp.add_argument(
        "--since",
        default=None,
        help="Only consider feed items whose id sorts after this",
    )
    sp.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Max seconds to wait (default: block until work arrives). "
             "Exits 1 on timeout with no work.",
    )
    sp.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Seconds between feed polls (default 0.5; lower for tighter "
             "agent loop latency, higher for fewer polls on idle bundles)",
    )
    sp.set_defaults(func=cmd_wait)

    # --- agent loop: token + comment claim/resolve + presence (current spec §12/§14) ---
    sp = sub.add_parser(
        "token",
        help="Print the current studio CSRF token (current spec §14). Run "
             "`scripts/okf-loom serve` first; this reads the configured session .token.",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.set_defaults(func=cmd_token)

    sp = sub.add_parser(
        "tunnel",
        help="Attach a public cloudflared quick tunnel to an ALREADY RUNNING "
             "studio session (no restart). --stop detaches it; --status "
             "reports the current URL. Startup-time alternative: "
             "`serve --tunnel`.",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--stop", action="store_true",
                     help="Detach the running tunnel instead of starting one")
    grp.add_argument("--status", action="store_true",
                     help="Print the current tunnel URL (or none) and exit")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_tunnel)

    sp = sub.add_parser(
        "comment-claim",
        help="Mark a comment `claimed` by the agent (current spec §12). The agent "
             "loop is: `scripts/okf-loom wait` → claim → do the work via mutators → "
             "`scripts/okf-loom comment-resolve`.",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("comment_id", help="The comment id from `scripts/okf-loom wait`")
    sp.add_argument("--actor", default="agent", help="Who is claiming (default: agent)")
    sp.add_argument(
        "--summary",
        help="Short agent-authored blurb summarising what the "
             "user ASKED for. Written to the request_summary field so the "
             "collapsed thread preview shows it as the 'ask' half of the "
             "ask/done pair. Auto-derived from the comment body if not "
             "set, so this flag is optional — use it only when the body "
             "is long and you want a sharper one-line tag.",
    )
    sp.add_argument(
        "--no-presence", dest="no_presence", action="store_true",
        help="Don't auto-flip presence to 'editing <concept>' on claim "
             "(INTENT2-005). Default flips so the user sees the agent act.",
    )
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_comment_claim)

    sp = sub.add_parser(
        "comment-resolve",
        help="Mark a comment `resolved` with optional reply + activity links "
             "(current spec §12 / §13 group-undo).",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("comment_id", help="The comment id")
    sp.add_argument("--reply", help="Optional short reply shown in the change list")
    sp.add_argument(
        "--summary",
        help="Short agent-authored blurb summarising what the "
             "agent DID (the 'done' half of the ask/done pair). Written "
             "to the summary field; shown in the collapsed preview "
             "alongside request_summary (the ask).",
    )
    sp.add_argument(
        "--activity",
        help="Comma-separated activity ids the resolution links to (group-undo)",
    )
    sp.add_argument("--actor", default="agent", help="Who is resolving (default: agent)")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_comment_resolve)

    sp = sub.add_parser(
        "comment-reply",
        help="Post a threaded reply to a comment WITHOUT resolving it "
             "(current spec §12). Use it to ask a clarifying question in "
             "the thread when the ask is ambiguous — the comment stays "
             "open/claimed and the user's answer wakes `wait` again.",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("comment_id", help="The comment id to reply to (replies "
                                       "to a reply hoist to the thread root)")
    sp.add_argument("--body", help="Reply text (shown in the thread)")
    sp.add_argument("--body-file", help="Read the reply text from this file")
    sp.add_argument("--actor", default="agent", help="Who is replying (default: agent)")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_comment_reply)

    sp = sub.add_parser(
        "comment-list",
        help="List comments / directives, optionally filtered (current spec §12).",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("--state", choices=("open", "claimed", "resolved", "dismissed", "archived"))
    sp.add_argument("--concept", help="Filter to a specific concept id")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_comment_list)

    sp = sub.add_parser(
        "presence",
        help="Set agent presence from the CLI (current spec §12). Mirrors POST /__presence.",
    )
    sp.add_argument("bundle", help="Path to an OKF bundle directory")
    sp.add_argument("--state", default="idle",
                    help="idle|watching|thinking|editing (default: idle)")
    sp.add_argument("--focus", help="The concept id the agent is focused on")
    sp.add_argument(
        "--message",
        help="Short free-text progress line shown next to the state chip "
             "(max 200 chars). Re-post mid-pass to update it — e.g. "
             "'linking 3 of 7 tables…' — so a long pass reads as progress, "
             "not a static 'editing'.",
    )
    sp.add_argument("--actor", default="agent")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_presence)

    sp = sub.add_parser("render", help="Render a self-contained viz.html (single-file viewer)")
    sp.add_argument("bundle")
    sp.add_argument("--out")
    sp.add_argument("--name")
    sp.add_argument(
        "--allow-active-code",
        dest="allow_active_code",
        action="store_true",
        default=None,
        help="Operator consent for active code (viewer overrides/plugins) in the single-file render.",
    )
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("build", help="Build a multi-file static site")
    sp.add_argument("bundle")
    sp.add_argument("--target", choices=("spa", "static", "single-file"), default="spa")
    sp.add_argument("--out", default="_site")
    sp.add_argument("--name")
    sp.add_argument(
        "--allow-active-code",
        dest="allow_active_code",
        action="store_true",
        default=None,
        help="Operator consent for build-time active code (overrides + plugins).",
    )
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("index", help="Regenerate SPEC §6 index.md files (safe; respects markers)")
    sp.add_argument("bundle")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--frozen", action="store_true",
                    help="CI mode: fail rather than write to hand-authored files")
    sp.add_argument(
        "--emit-json",
        action="store_true",
        help="Emit portable content.json + graph.json under <bundle>/.okf-loom/index/ (current spec §8)",
    )
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("log", help="Append to SPEC §7 log.md (safe; append-only)")
    sp.add_argument("bundle")
    sp.add_argument("--append", help="Entry text to append under today's date")
    sp.add_argument("--kind", default="Update",
                    help="Bold-tag kind (Update/Creation/Deprecation/Initialization)")
    sp.add_argument("--date", help="Override date (YYYY-MM-DD; default today)")
    sp.add_argument("--log-path", default="log.md", help="Path to log.md (relative to bundle)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("update", help="Apply an update plan (add link/tag/relation, etc.)")
    sp.add_argument("bundle")
    sp.add_argument("--plan", required=True, help="Path to a JSON update plan")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("upgrade", help="Check or apply spec-version migrations")
    sp.add_argument("bundle", nargs="?", help="Path to an OKF bundle directory")
    sp.add_argument("--bundle", dest="bundle_flag", help="Alternative: --bundle <path>")
    sp.add_argument("--check", action="store_true")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--to-spec", default=SPEC_VERSION)
    sp.set_defaults(func=cmd_upgrade)

    sp = sub.add_parser("bootstrap", help="Scaffold a new empty OKF bundle")
    sp.add_argument("dest", help="Destination directory for the new bundle")
    sp.add_argument("--name", help="Bundle display name (default: directory name)")
    sp.set_defaults(func=cmd_bootstrap)

    sp = sub.add_parser(
        "import",
        help="Import existing markdown files into an OKF bundle (injects type frontmatter)",
    )
    sp.add_argument("src", help="Source directory containing .md files")
    sp.add_argument("dest", help="Destination OKF bundle directory")
    sp.add_argument(
        "--default-type", default="Reference",
        help="Default concept type for files without a type guess (default: Reference)",
    )
    sp.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite existing files in dest (default: skip)",
    )
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser(
        "init",
        help="Scaffold a new OKF bundle with index.md, log.md, and okf-loom.config.yaml (current spec §8)",
    )
    sp.add_argument(
        "--bundle", required=True,
        help="Destination directory for the new bundle (must not exist or be empty)",
    )
    sp.add_argument("--name", help="Bundle display name (default: directory name)")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    sp.set_defaults(func=cmd_init)

    # --- Authoring verbs (current spec §7) ---
    sp = sub.add_parser("write-concept", help="Create or update a single concept file")
    sp.add_argument("--bundle", required=True, help="Bundle directory")
    sp.add_argument("--id", required=True, help="Concept id (e.g. tables/orders)")
    sp.add_argument("--type", required=True, help="Concept type (required)")
    sp.add_argument("--title", help="Concept title")
    sp.add_argument("--description", help="Concept description")
    sp.add_argument("--resource", help="Resource URL")
    sp.add_argument("--timestamp", help="ISO timestamp")
    sp.add_argument("--tag", action="append", help="Tag (repeatable)")
    sp.add_argument("--body", help="Body text")
    sp.add_argument("--body-file", help="Read body from this file")
    sp.add_argument("--force", action="store_true", help="Allow overwriting existing body")
    sp.add_argument(
        "--no-defaults", dest="no_defaults", action="store_true",
        help="On create, do NOT auto-fill the mechanically derivable "
             "recommended keys (resource = the bundle-absolute concept "
             "path, timestamp = now UTC). Default: fill them so the new "
             "concept passes `validate --strict` without follow-up "
             "set-frontmatter calls. Explicit --resource/--timestamp "
             "always win.",
    )
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_write_concept)

    sp = sub.add_parser("set-frontmatter", help="Set a frontmatter key on a concept")
    sp.add_argument("--bundle", required=True, help="Bundle directory")
    sp.add_argument("--id", required=True, help="Concept id")
    sp.add_argument("--key", required=True, help="Frontmatter key")
    sp.add_argument("--value", required=True, help="Value (or JSON if --json-value)")
    sp.add_argument("--json-value", action="store_true", help="Parse value as JSON")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_set_frontmatter)

    sp = sub.add_parser(
        "update-section",
        help="Replace (or append to) ONE section of a concept body — the "
             "block-level partial update. Keeps the rest of the document "
             "untouched; no whole-body --force rewrite needed.",
    )
    sp.add_argument("--bundle", required=True, help="Bundle directory")
    sp.add_argument("--id", required=True, help="Concept id")
    sp.add_argument(
        "--heading", required=True,
        help="Section heading to target. Level-pinned ('## Banking') or "
             "bare ('Banking', matches any level); case-insensitive. "
             "Duplicate matches fail closed (section_ambiguous) — pin the "
             "level to disambiguate.",
    )
    sp.add_argument("--body", help="New section content (replaces the whole "
                                   "section INCLUDING its subsections)")
    sp.add_argument("--body-file", help="Read the new section content from this file "
                                        "(a leading copy of the target heading is stripped)")
    sp.add_argument(
        "--append", action="store_true",
        help="Append to the end of the section instead of replacing it",
    )
    sp.add_argument(
        "--create-if-missing", dest="create_if_missing", action="store_true",
        help="Create the section at the end of the body when the heading "
             "is absent (default: fail with section_not_found)",
    )
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_update_section)

    sp = sub.add_parser(
        "replace-text",
        help="Exact textual patch on a concept body: swap --old for --new. "
             "Fail-closed on zero or ambiguous matches.",
    )
    sp.add_argument("--bundle", required=True, help="Bundle directory")
    sp.add_argument("--id", required=True, help="Concept id")
    sp.add_argument("--old", help="Exact text to replace (must match exactly "
                                  "once unless --all)")
    sp.add_argument("--old-file", help="Read the exact old text from this file")
    sp.add_argument("--new", help="Replacement text")
    sp.add_argument("--new-file", help="Read the replacement text from this file")
    sp.add_argument(
        "--all", action="store_true",
        help="Replace every occurrence (default: >1 match fails closed "
             "with text_ambiguous — add surrounding context to --old to "
             "pin one occurrence)",
    )
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_replace_text)

    sp = sub.add_parser("link-add", help="Add a markdown link (+ optional typed relation)")
    sp.add_argument("--bundle", required=True, help="Bundle directory")
    sp.add_argument("--source", required=True, help="Source concept id")
    sp.add_argument("--target", required=True, help="Target concept id")
    sp.add_argument("--label", help="Link label")
    sp.add_argument("--section", help="Section to add link in")
    sp.add_argument("--relation", help="Also add a typed relation of this type")
    sp.add_argument("--evidence", help="Evidence/detail for the relation")
    sp.add_argument(
        "--allow-forward-reference",
        dest="allow_forward_reference",
        action="store_true",
        help="Allow the link target to not yet exist in the bundle (a forward "
             "reference / not-yet-written knowledge; SPEC §3, AGENTS.md hard "
             "rule #3). The link is written in SPEC §5.1 absolute form and "
             "will be 'broken' until the target concept is created; consumers "
             "MUST tolerate that. Default: fail (exit 1) if the target is "
             "missing, so you know the link would dangle.",
    )
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_link_add)

    sp = sub.add_parser("entity-add", help="Add an entity to a concept")
    sp.add_argument("--bundle", required=True, help="Bundle directory")
    sp.add_argument("--id", required=True, help="Concept id")
    sp.add_argument("--label", required=True, help="Entity label")
    sp.add_argument("--kind", help="Entity kind (e.g. business_entity)")
    sp.add_argument("--entity-id", help="Entity id (e.g. entity/customer)")
    sp.add_argument("--alias", action="append", help="Entity alias (repeatable)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--format", choices=("text", "json"), default="text")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_entity_add)

    sp = sub.add_parser("repair", help="Run mechanical fixes (indexes, relation mirroring)")
    add_bundle(sp)
    sp.add_argument("--apply", action="store_true", help="Apply fixes (default: dry-run)")
    sp.add_argument("--indexes", action="store_true", help="Regenerate stale indexes")
    sp.add_argument("--mirror-relations", action="store_true", help="Mirror typed relations to body links")
    sp.add_argument("--all", action="store_true", help="All mechanical fixes")
    add_studio_flags(sp)
    sp.set_defaults(func=cmd_repair)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileExistsError as e:
        # Current spec §8 / task exit-code contract: a non-empty destination
        # for `okf init` (and any future FileExistsError guard) exits 2.
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # Catch-all: any unhandled exception (ConceptIdError, etc.) → exit 1
        # with a clean message instead of a traceback.
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
