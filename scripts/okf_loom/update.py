"""Apply a reviewed update plan to a bundle.

Plans are JSON documents produced by an agent (e.g. from ``discover`` output)
and reviewed by a human. This module applies them idempotently and with
round-trip safety: every mutation goes through ``parse_document`` -> modify ->
``serialize_document``, preserving unknown frontmatter keys and key order.

All file writes are atomic via ``okf_loom.io_utils.atomic_write_text``
(tmp via ``mkstemp`` + ``os.replace``); tmp files are cleaned up on any
exception.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Bundle, Concept, Heading
from .parse import (
    _LINK_RE,
    extract_headings,
    parse_document,
)
from .paths import (
    ConceptId,
    ConceptIdError,
    concept_id_from_str,
    concept_id_to_path,
    concept_id_to_str,
)
from .io_utils import atomic_write_text
# Round-trip-preserving YAML serializer lives in its own leaf module
# (``roundtrip.py``). Re-export the public surface here so existing
# ``from okf_loom.update import serialize_document_round_trip`` callers
# keep working; ``_values_equal`` is also re-imported because this module's
# apply handlers use it directly.
from .roundtrip import (
    _values_equal,
    patch_frontmatter_block,
    serialize_document_round_trip,
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class UpdateOp:
    """A single update operation.

    Attributes:
        kind: one of ``add_link``, ``set_tag``, ``add_tag``, ``set_frontmatter``,
            ``add_relation``, ``append_body_section``, ``remove_link``.
        target: concept id the op applies to (a ConceptId tuple; serialized as
            a slash-separated string in the JSON plan).
        args: kind-specific arguments (see module docstring of apply handlers).
    """

    kind: str
    target: ConceptId
    args: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        from .paths import concept_id_to_str

        return {
            "kind": self.kind,
            "target": concept_id_to_str(self.target),
            "args": dict(self.args),
        }


@dataclass
class UpdatePlan:
    """A reviewed collection of update operations."""

    bundle_root: str | None
    description: str
    ops: list[UpdateOp]
    plan_kind: str
    skipped_actions: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "bundle_root": self.bundle_root,
            "description": self.description,
            "plan_kind": self.plan_kind,
            "ops": [op.as_dict() for op in self.ops],
            "skipped_actions": list(self.skipped_actions),
        }


def load_plan(path: str | Path) -> UpdatePlan:
    """Load a JSON update plan from disk.

    Accepts two formats:
        1. **UpdatePlan** (preferred): ``{"ops": [...], "plan_kind": "..."}``
        2. **DiscoveryReport** (auto-converted): ``{"suggestions": [...]}`` —
           each actionable suggestion is converted to an UpdateOp so the
           documented workflow ``okf discover --out plan.json && okf update
           --plan plan.json`` works end-to-end without manual JSON editing.

    The auto-conversion maps:
        - ``unlinked_mentions`` → ``add_link`` (label = suggested title,
          target = target_concept_id)
        - ``missing_descriptions`` → deferred as ``needs_agent_input``
          (skipped_actions entry; never auto-writes a ``TODO:`` placeholder
          into a hand-authored concept file — AGENTS.md hard rule #7).
        - Other rules (missing_indexes, broken_links, orphan_concepts,
          missing_relations_hint) → ``unhandled_discovery_rule``
          (skipped_actions entry; require manual decisions).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    # Auto-convert action-envelope plan (current spec §7) → update plan.
    if "actions" in data and "ops" not in data:
        ops, skipped = _actions_to_ops(data["actions"])
        return UpdatePlan(
            bundle_root=data.get("bundle_root"),
            description=f"Action-envelope plan ({len(ops)} ops from {len(data['actions'])} actions, {len(skipped)} skipped)",
            ops=ops,
            plan_kind="action-envelope",
            skipped_actions=skipped,
        )

    # Auto-convert discovery report → update plan.
    if "suggestions" in data and "ops" not in data:
        ops, skipped = _suggestions_to_ops(data["suggestions"])
        return UpdatePlan(
            bundle_root=data.get("bundle_root"),
            description=(
                f"Auto-converted from discovery report ({len(ops)} ops "
                f"from {len(data['suggestions'])} suggestions, "
                f"{len(skipped)} skipped)"
            ),
            ops=ops,
            plan_kind="discovery-autoconvert",
            skipped_actions=skipped,
        )

    ops: list[UpdateOp] = []
    for op_data in data.get("ops", []):
        target_str = op_data.get("target")
        if not target_str:
            raise ValueError(f"Update op missing 'target': {op_data!r}")
        try:
            target = concept_id_from_str(str(target_str))
        except ConceptIdError as e:
            raise ValueError(
                f"Update op has invalid target {target_str!r}: {e}"
            ) from e
        ops.append(
            UpdateOp(
                kind=str(op_data.get("kind", "")),
                target=target,
                args=dict(op_data.get("args", {})),
            )
        )
    return UpdatePlan(
        bundle_root=data.get("bundle_root"),
        description=str(data.get("description", "")),
        ops=ops,
        plan_kind=str(data.get("plan_kind", "update")),
    )


def _actions_to_ops(
    actions: list[dict],
) -> tuple[list[UpdateOp], list[dict]]:
    """Convert action-envelope PlannedActions to UpdateOps (current spec §7).

    Returns ``(ops, skipped)`` where ``skipped`` contains actions that
    require agent input (template actions with unfilled ``<...>`` slots).

    Two consumption paths, in priority order:

      1. **Structured (preferred):** when an action carries ``op_payload``
         (populated by ``plan.build_plan`` for mechanical actions since
         P2-10/iter-1), build the UpdateOp(s) DIRECTLY from the dict — no
         argv re-parse. This eliminates the emitter/parser drift class of
         bugs (the P1-5 iter-3 convergence bug was of this class).
      2. **argv fallback:** when ``op_payload`` is absent (older plan
         files written before op_payload existed, or hand-authored plans),
         re-parse ``argv`` via :func:`_parse_argv`. Behavior-identical to
         path 1 for every verb the planner emits.

    Maps:
        - ``add_link`` (with argv/op_payload) → ``add_link``
        - ``add_description`` → ``set_frontmatter`` (if value slot is filled)
        - ``mirror_relation`` → ``add_link`` + ``add_relation``
        - ``refresh_index`` / ``create_index`` → skipped (run ``okf index``)
        - Template actions → skipped with reason ``needs_agent_input``
    """
    ops: list[UpdateOp] = []
    skipped: list[dict] = []

    for action in actions:
        verb = action.get("action", "")
        argv = action.get("argv")
        argv_template = action.get("argv_template")
        op_payload = action.get("op_payload")

        # If only a template (no concrete argv), skip.
        # Template actions never carry op_payload (the planner sets op_payload
        # only on mechanical actions), so this check fires before the
        # op_payload branch for every template action.
        if not argv and argv_template:
            skipped.append({
                "action": verb,
                "concept_id": action.get("concept_id"),
                "reason": "needs_agent_input",
            })
            continue

        # Path 1 (preferred): structured payload — no argv re-parse.
        if op_payload is not None:
            _ops_from_payload(verb, op_payload, action, ops, skipped)
            continue

        # Path 2 (argv fallback): parse concrete argv to extract args.
        if argv:
            parsed = _parse_argv(argv)
            if parsed is None:
                skipped.append({
                    "action": verb,
                    "reason": "unparseable_argv",
                })
                continue

            source = parsed.get("--source")
            target = parsed.get("--target")
            label = parsed.get("--label")
            relation_type = parsed.get("--relation")

            if verb in ("add_link", "mirror_relation") and source and target:
                try:
                    source_cid = concept_id_from_str(source)
                except ConceptIdError:
                    skipped.append({"action": verb, "reason": "invalid_source"})
                    continue
                # Add the link
                ops.append(UpdateOp(
                    kind="add_link",
                    target=source_cid,
                    args={
                        "label": label or target.split("/")[-1],
                        "target_concept_id": target,
                        "section": None,
                    },
                ))
                # If mirror_relation, also add the typed relation
                if verb == "mirror_relation" and relation_type:
                    ops.append(UpdateOp(
                        kind="add_relation",
                        target=source_cid,
                        args={
                            "target_concept_id": target,
                            "relation_type": relation_type,
                            "detail": "",
                        },
                    ))

            elif verb == "add_description" and source:
                # Check if the value slot was filled (not a template)
                value = parsed.get("--value")
                if value and "<" not in value:
                    try:
                        source_cid = concept_id_from_str(source)
                    except ConceptIdError:
                        skipped.append({
                            "action": verb,
                            "concept_id": action.get("concept_id"),
                            "reason": "invalid_source",
                        })
                        continue
                    ops.append(UpdateOp(
                        kind="set_frontmatter",
                        target=source_cid,
                        args={"key": "description", "value": value},
                    ))
                else:
                    skipped.append({
                        "action": verb,
                        "concept_id": action.get("concept_id"),
                        "reason": "needs_agent_input",
                    })

            elif verb in ("create_index", "refresh_index"):
                # Index regeneration is handled by `okf index`, not update ops.
                skipped.append({
                    "action": verb,
                    "reason": "run_okf_index_directly",
                })
            else:
                # Unhandled verb with concrete argv — don't silently drop
                skipped.append({
                    "action": verb,
                    "concept_id": action.get("concept_id"),
                    "reason": "unhandled_verb",
                })
        else:
            # Neither argv nor argv_template — action is malformed
            if not argv_template:
                skipped.append({
                    "action": verb,
                    "concept_id": action.get("concept_id"),
                    "reason": "no_argv_or_template",
                })

    return ops, skipped


def _ops_from_payload(
    verb: str,
    op_payload: dict,
    action: dict,
    ops: list[UpdateOp],
    skipped: list[dict],
) -> None:
    """Build UpdateOp(s) directly from a structured ``op_payload`` dict.

    P2-10 (iter-1 architecture Candidate 2): the preferred application path.
    ``op_payload`` carries the SAME information as ``argv`` but as a dict
    keyed by the consuming handler's expected args, so this function is a
    flat lookup rather than a parser. It MUST produce UpdateOps identical to
    the argv-fallback :func:`_parse_argv` path for every verb the planner emits
    (see ``test_op_payload_path_matches_argv_path`` for the parity contract).

    The ``handler`` key names the primary UpdateOp kind. ``relation`` (when
    present) drives a secondary ``add_relation`` op for ``mirror_relation``.

    Fail-closed: any missing required key, unknown handler, or invalid
    concept id is appended to ``skipped`` with a machine-readable reason —
    nothing is silently dropped (mirrors the argv-path discipline).
    """
    handler = op_payload.get("handler")
    concept_id_str = action.get("concept_id")

    if handler == "add_link":
        source = op_payload.get("source")
        target = op_payload.get("target")
        if not source or not target:
            skipped.append({
                "action": verb,
                "concept_id": concept_id_str,
                "reason": "op_payload_missing_source_or_target",
            })
            return
        try:
            source_cid = concept_id_from_str(str(source))
        except ConceptIdError:
            skipped.append({
                "action": verb,
                "concept_id": concept_id_str,
                "reason": "invalid_source",
            })
            return
        # Label: precomputed by the planner; fall back to the same default
        # the argv path uses only if a hand-written payload omits it.
        label = op_payload.get("label") or str(target).split("/")[-1]
        section = op_payload.get("section")
        ops.append(UpdateOp(
            kind="add_link",
            target=source_cid,
            args={
                "label": label,
                "target_concept_id": str(target),
                "section": section,
            },
        ))
        # mirror_relation: also add the typed relation.
        relation = op_payload.get("relation")
        if relation:
            ops.append(UpdateOp(
                kind="add_relation",
                target=source_cid,
                args={
                    "target_concept_id": str(target),
                    "relation_type": str(relation),
                    "detail": "",
                },
            ))
        return

    if handler == "regenerate_indexes":
        # Index regeneration is applied via ``okf index`` directly, not an
        # UpdateOp — same skip reason as the argv path so the two paths are
        # observably identical to consumers of the skipped list.
        skipped.append({
            "action": verb,
            "reason": "run_okf_index_directly",
        })
        return

    if handler == "set_frontmatter":
        # iter2 P2-7: parity with the argv path's ``add_description →
        # set_frontmatter`` branch. The planner currently makes
        # add_description a template-only action (no op_payload), so this
        # handler is only reachable via hand-authored plans or a future
        # emitter. Without it, a plan carrying op_payload for
        # add_description would silently drop as
        # ``unhandled_op_payload_handler:set_frontmatter`` while the argv
        # path would apply it — a latent divergence.
        source = op_payload.get("source")
        value = op_payload.get("value")
        key = op_payload.get("key", "description")
        # Mirror the argv path's template-slot guard: if the value still
        # contains a ``<`` placeholder, it needs agent input.
        if not source or not value or "<" in str(value):
            skipped.append({
                "action": verb,
                "concept_id": concept_id_str,
                "reason": "needs_agent_input",
            })
            return
        try:
            source_cid = concept_id_from_str(str(source))
        except ConceptIdError:
            skipped.append({
                "action": verb,
                "concept_id": concept_id_str,
                "reason": "invalid_source",
            })
            return
        ops.append(UpdateOp(
            kind="set_frontmatter",
            target=source_cid,
            args={"key": key, "value": value},
        ))
        return

    # Unknown handler — fail closed (never silently drop).
    skipped.append({
        "action": verb,
        "concept_id": concept_id_str,
        "reason": f"unhandled_op_payload_handler:{handler}",
    })


def _parse_argv(argv: list[str]) -> dict[str, str] | None:
    """Parse an argv list into ``{flag: value}`` pairs.

    Handles ``--flag value`` and ``--flag=value`` forms.
    Positional args (like ``okf``, ``link-add``, bundle path) are ignored.
    """
    if not argv:
        return None
    result: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            if "=" in arg:
                key, _, val = arg.partition("=")
                result[key] = val
                i += 1
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                result[arg] = argv[i + 1]
                i += 2
            else:
                i += 1
        else:
            i += 1
    return result


def _suggestions_to_ops(
    suggestions: list[dict],
) -> tuple[list[UpdateOp], list[dict]]:
    """Convert discovery suggestions to UpdateOps (best-effort).

    Returns ``(ops, skipped)`` mirroring :func:`_actions_to_ops` so the
    caller can populate ``UpdatePlan.skipped_actions``.

    Only ``unlinked_mentions`` has a clear mechanical single-concept fix
    (one ``add_link`` op per suggestion). All other discovery rules are
    recorded in ``skipped`` so nothing is silently dropped:

      * ``missing_descriptions`` -> ``needs_agent_input``. Deferred rather
        than auto-writing a ``TODO:`` placeholder, because mutating a
        hand-authored concept file with a stub value violates AGENTS.md
        hard rule #7 ("Auto-update must not destroy hand-curated
        content") and is inconsistent with the action-envelope path,
        which defers template ``add_description`` actions the same way.

      * ``missing_indexes``, ``broken_links``, ``orphan_concepts``,
        ``missing_relations_hint`` -> ``unhandled_discovery_rule``
        (require manual decisions / cross-concept coordination).

      * Any unknown / future discovery rule -> ``unhandled_discovery_rule``
        (forward-compatible; never silently dropped).
    """
    _UNHANDLED_RULES = frozenset({
        "missing_indexes", "broken_links",
        "orphan_concepts", "missing_relations_hint",
    })
    ops: list[UpdateOp] = []
    skipped: list[dict] = []
    for s in suggestions:
        rule = s.get("rule", "")
        detail = s.get("detail")
        if not isinstance(detail, dict):
            detail = {}

        # Defer unhandled discovery rules FIRST so they get the right
        # reason even when concept_id is missing (``missing_indexes`` is
        # directory-scoped and routinely emits concept_id=None).
        if rule in _UNHANDLED_RULES:
            skipped.append({
                "rule": rule,
                "concept_id": s.get("concept_id"),
                "reason": "unhandled_discovery_rule",
            })
            continue

        source_cid_str = s.get("concept_id")
        if not source_cid_str:
            skipped.append({
                "rule": rule,
                "reason": "missing_concept_id",
            })
            continue
        try:
            source_cid = concept_id_from_str(str(source_cid_str))
        except ConceptIdError:
            skipped.append({
                "rule": rule,
                "concept_id": source_cid_str,
                "reason": "invalid_concept_id",
            })
            continue

        if rule == "unlinked_mentions":
            target_cid_str = (
                detail.get("suggested_target_concept_id")
                or s.get("target_concept_id")
            )
            if not target_cid_str:
                skipped.append({
                    "rule": rule,
                    "concept_id": source_cid_str,
                    "reason": "missing_target_concept_id",
                })
                continue
            try:
                concept_id_from_str(str(target_cid_str))
            except ConceptIdError:
                skipped.append({
                    "rule": rule,
                    "concept_id": source_cid_str,
                    "reason": "invalid_target_concept_id",
                })
                continue
            label = detail.get("label") or str(target_cid_str).split("/")[-1]
            ops.append(UpdateOp(
                kind="add_link",
                target=source_cid,
                args={
                    "label": label,
                    "target_concept_id": str(target_cid_str),
                    "section": None,
                },
            ))
        elif rule == "missing_descriptions":
            # Never auto-write a ``TODO:`` placeholder into a hand-authored
            # concept file (AGENTS.md hard rule #7). Defer to the agent
            # exactly like the action-envelope path defers template
            # ``add_description`` actions.
            skipped.append({
                "rule": rule,
                "concept_id": source_cid_str,
                "reason": "needs_agent_input",
            })
        else:
            # Unknown / future discovery rule — record so it isn't
            # silently dropped.
            skipped.append({
                "rule": rule,
                "concept_id": source_cid_str,
                "reason": "unhandled_discovery_rule",
            })
    return ops, skipped


# ---------------------------------------------------------------------------
# apply_plan
# ---------------------------------------------------------------------------

# Reuse the link regex from parse so removal/inspection stays consistent with
# how links are extracted elsewhere in okf-loom (imported at top).


def apply_plan(
    bundle: Bundle,
    plan: UpdatePlan,
    *,
    apply: bool = True,
    dry_run: bool = False,
    studio: Any = None,
    actor: str = "agent",
    origin: str = "mutator",
    group_id: str | None = None,
) -> dict:
    """Apply (or dry-run) an update plan against a bundle.

    Args:
        bundle: a loaded ``Bundle``. Concept objects are mutated in place so
            that subsequent ops in the same plan observe earlier changes.
        plan: the ``UpdatePlan`` to apply.
        apply: when True (default), write modified files atomically.
        dry_run: when True, do NOT write; results describe what would happen.
            In-memory concepts are still mutated so a multi-op plan reports
            accurate idempotency (e.g. a duplicate add_link reports
            ``already_linked``).
        studio: optional :class:`okf_loom.studio.Studio`. When provided
            (current spec §10 / §13), every successful concept write is routed
            through ``studio.save_concept(...)`` — the single internal
            atomic-write funnel that snapshots for undo, emits an
            attributed activity event, marks the rev as logged so the
            watcher doesn't double-log it, and broadcasts `changed`
            (+ `graph` for graph-affecting ops) over SSE. Without ``studio``
            (the no-session compatibility path), writes go directly through
            :func:`_write_concept` (atomic_write_text only, no attribution,
            no undo snapshot). CLI mutators detect an active
            ``<bundle>/.okf-loom/session/`` and pass the studio in; HTTP
            ``/__apply`` and ``/__undo`` always pass it in.
        actor: when ``studio`` is set, the activity actor (default ``agent``).
        origin: when ``studio`` is set, the activity origin (default
            ``mutator`` — set to ``http-apply`` for the HTTP endpoint).
        group_id: optional group manifest id (§12.5); shares undo across
            all writes in this plan as one group.

    Returns:
        A summary dict::

            {
                "plan_kind": str,
                "total_ops": int,
                "applied": int,
                "skipped": int,
                "results": [[op_dict, result_dict], ...],
                "skipped_actions": list[dict],
            }

        P2-24: each op in ``results`` is rendered via :meth:`UpdateOp.as_dict`
        so the structure is JSON-serialisable without leaking the
        ``UpdateOp`` Python ``repr`` into ``--format json`` output.
    """
    results: list[tuple[UpdateOp, dict]] = []
    mutated_any = False
    try:
        for op in plan.ops:
            result = _apply_one(
                bundle, op, apply=apply, dry_run=dry_run,
                studio=studio, actor=actor, origin=origin, group_id=group_id,
            )
            results.append((op, result))
            if result.get("applied"):
                mutated_any = True
    finally:
        # ALWAYS invalidate after any mutation — even on dry_run.
        # dry_run mutates in-memory concepts for idempotency checking, so
        # derived caches (graph, content_index, search backends) must be
        # invalidated to stay consistent with the mutated concept state.
        if mutated_any:
            bundle.invalidate()

    applied = sum(1 for _, r in results if r["applied"])
    # P2-24: render each op via as_dict() so consumers (incl. --format json)
    # see ``[[op_dict, result_dict], ...]`` instead of an UpdateOp repr.
    json_results = [[op.as_dict(), result] for op, result in results]
    return {
        "plan_kind": plan.plan_kind,
        "total_ops": len(plan.ops),
        "applied": applied,
        "skipped": len(results) - applied,
        "results": json_results,
        "skipped_actions": list(plan.skipped_actions),
    }


# ---------------------------------------------------------------------------
# Per-op handlers
# ---------------------------------------------------------------------------


def _apply_one(bundle: Bundle, op: UpdateOp, *, apply: bool, dry_run: bool,
               studio: Any = None, actor: str = "agent",
               origin: str = "mutator",
               group_id: str | None = None) -> dict:
    handler = _HANDLERS.get(op.kind)
    concept = bundle.concepts.get(op.target)
    if concept is None:
        return {
            "applied": False,
            "reason": "concept_not_found",
            "path": None,
        }
    if handler is None:
        return {
            "applied": False,
            "reason": f"unknown_op_kind:{op.kind}",
            "path": str(concept.rel_path),
        }
    try:
        applied, reason, mutated = handler(bundle, concept, op.args)
    except (ConceptIdError, KeyError, TypeError, ValueError) as e:
        return {
            "applied": False,
            "reason": f"bad_args:{e}",
            "path": str(concept.rel_path),
        }
    if mutated and apply and not dry_run:
        # Current spec §10/§13: when a Studio is attached, route the write through
        # ``studio.save_concept(...)`` so it is attributed, undoable, and
        # broadcasts a proper change/graph event. Without a studio (no-session
        # CLI / library callers), write atomically through the existing
        # _write_concept path (no attribution, no undo snapshot).
        if studio is not None:
            from .paths import concept_id_to_str
            cid_str = concept_id_to_str(concept.id)
            raw = serialize_document_round_trip(
                concept.raw_text, concept.frontmatter, concept.body
            )
            # §9.3 collision guard: compare the on-disk rev to the rev we
            # observed at the last sync point. The "sync point" is
            # ``concept.raw_text`` — set by Bundle.load on first read, and
            # updated by THIS block after every successful save_concept so a
            # multi-op plan targeting the SAME concept (e.g. ``link-add
            # --relation`` runs add_link then add_relation on the same source)
            # does NOT spuriously conflict with its own earlier op (ARCH2-001).
            # If the user (their editor) edited the file in the window between
            # the last sync and now, the on-disk bytes will differ from
            # ``concept.raw_text`` and the conflict surfaces.
            expected_rev = None
            try:
                prior = concept.path.read_bytes() if concept.path.is_file() else None
                if prior is not None and prior != concept.raw_text.encode("utf-8"):
                    from .studio import rev_of
                    # The on-disk bytes differ from what we last saw — either a
                    # concurrent user edit OR our own earlier op in this plan.
                    # expected_rev pins what we expected to overwrite; if the
                    # current on-disk rev mismatches, save_concept returns
                    # conflict instead of clobbering.
                    expected_rev = rev_of(concept.raw_text)
            except OSError:
                pass
            write_result = studio.save_concept(
                concept_id=cid_str, raw=raw, actor=actor,
                action=op.kind, origin=origin, group_id=group_id,
                expected_rev=expected_rev,
                detail={"args": dict(op.args)},
                summary=f"{op.kind} on {cid_str}",
            )
            if write_result.conflict:
                # INTENT2-001: emit an ``agent_conflict`` activity event so
                # the SSE stream carries the conflict to the browser even
                # when the agent is running via CLI mutators (not HTTP
                # /__apply). The browser's §9.4 conflict modal listens for
                # these events too, not just HTTP 409 responses.
                try:
                    studio.append_event({
                        "type": "agent_conflict",
                        "concept": cid_str,
                        "expected_rev": write_result.expected_rev,
                        "current_rev": write_result.current_rev,
                        "origin": origin,
                        "action": op.kind,
                    })
                except Exception:
                    pass
                return {
                    "applied": False,
                    "reason": "rev_conflict",
                    "path": str(concept.rel_path),
                    "conflict": {
                        "expected_rev": write_result.expected_rev,
                        "current_rev": write_result.current_rev,
                    },
                }
            if not write_result.ok:
                return {
                    "applied": False,
                    "reason": f"studio_blocked:{write_result.error}",
                    "path": str(concept.rel_path),
                }
            # ARCH2-001 fix: advance the in-memory sync point so the next op
            # in this plan (often targeting the SAME concept — e.g.
            # ``link-add --relation``) computes its expected_rev against the
            # post-write state, not the stale load-time bytes. Without this,
            # the second op sees the new on-disk bytes from op-1, mismatches
            # the stale ``concept.raw_text``, and spuriously returns
            # ``rev_conflict``.
            concept.raw_text = raw
        else:
            _write_concept(concept)
    return {
        "applied": applied,
        "reason": reason,
        "path": str(concept.rel_path) if mutated or applied else None,
    }


def _write_concept(concept: Concept) -> None:
    """Atomically serialize and write a concept back to its file.

    P1-24: routes through :func:`serialize_document_round_trip` so the
    original frontmatter formatting (flow/block style, quote style, key
    order, comments) is preserved wherever the diff allows text-surgery.
    """
    content = serialize_document_round_trip(
        concept.raw_text, concept.frontmatter, concept.body
    )
    atomic_write_text(concept.path, content)


# Atomic writes are provided by ``okf_loom.io_utils.atomic_write_text``.
# The old module-private ``_atomic_write`` helper has been removed in
# favour of the shared, mkstemp-based implementation that is safe under
# concurrent writers and cleans up tmp files on exception.


def _refresh_headings(concept: Concept) -> None:
    concept.headings = [
        Heading(level, slug, text)
        for (level, slug, text) in extract_headings(concept.body)
    ]


# Handler return shape: (applied, reason, mutated)
_HandlerResult = tuple[bool, "str | None", bool]


# ---- add_link --------------------------------------------------------------


def _absolute_link(target: Concept) -> str:
    """Absolute bundle-relative markdown link (SPEC §5.1 preferred form).

    Returns ``/path/to/concept.md`` — stable when files move within a
    subdirectory, and the form AGENTS.md Hard Rule #4 recommends.
    """
    rel = str(target.rel_path).replace(os.sep, "/")
    return f"/{rel}"


def _relative_link(source: Concept, target: Concept) -> str:
    """Relative markdown path from the source concept's dir to the target."""
    rel = os.path.relpath(target.path, start=source.path.parent)
    # Normalize to forward slashes for markdown/URL safety.
    return rel.replace(os.sep, "/")


def _append_to_section(body: str, section: str | None, line: str) -> str:
    """Append ``line`` into the named ``# section`` (or end of body)."""
    if not section:
        return body.rstrip("\n") + "\n" + line + "\n"
    pat = re.compile(
        rf"^(#*)\s*{re.escape(section)}\s*#*\s*$", re.MULTILINE
    )
    m = pat.search(body)
    if not m:
        # Section heading absent -> fall back to end of body.
        return body.rstrip("\n") + "\n" + line + "\n"
    rest_start = m.end()
    nxt = re.search(r"^#{1,6}\s+\S", body[rest_start:], re.MULTILINE)
    insert_at = rest_start + nxt.start() if nxt else len(body)
    prefix = body[:insert_at]
    suffix = body[insert_at:]
    if not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + line + "\n" + suffix


def _forward_reference_link_target(bundle: Bundle, target_cid: ConceptId) -> str:
    """Absolute bundle-relative markdown link path (SPEC §5.1 preferred form)
    for a not-yet-written target concept.

    Mirrors :func:`_absolute_link` but derives the path from the concept id
    alone (there is no ``Concept`` object to read ``rel_path`` from). Used by
    :func:`_h_add_link` when ``allow_forward_reference`` is set so a link can
    point at not-yet-written knowledge (AGENTS.md hard rule #3).
    """
    target_path = concept_id_to_path(bundle.root, target_cid)
    rel = str(target_path.relative_to(bundle.root)).replace(os.sep, "/")
    return f"/{rel}"


def _h_add_link(bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    label = str(args.get("label") or "").strip()
    target_str = args.get("target_concept_id")
    if not target_str:
        raise KeyError("target_concept_id")
    target_cid = concept_id_from_str(str(target_str))
    allow_forward_reference = bool(args.get("allow_forward_reference"))
    target = bundle.concepts.get(target_cid)
    if target is None:
        if not allow_forward_reference:
            # Default fail-closed (P2-6 / iter-3): a markdown link points at a
            # concrete file. Reject so the user knows the target is missing;
            # they can opt in to a forward reference with
            # --allow-forward-reference. SPEC §3 / AGENTS.md hard rule #3
            # permits forward references but okf-loom defaults to safe.
            return False, "target_concept_not_found", False
        # Forward reference (SPEC §3 / AGENTS.md hard rule #3): the target
        # concept does not exist yet — "not-yet-written knowledge", not
        # malformed. Build the link against the concept-id-derived absolute
        # path (SPEC §5.1 form). Consumers MUST tolerate the broken link
        # until the target is written.
        link_target = _forward_reference_link_target(bundle, target_cid)
        # Idempotent: skip if a link to that raw target path already exists.
        # (Resolved-target matching does not work for forward references
        # because the target cannot resolve, so match on target_raw instead.)
        existing_raws = {
            link.target_raw for link in c.links(bundle_root=bundle.root)
        }
        if link_target in existing_raws:
            return False, "already_linked", False
        text = str(args.get("anchor_text") or label or target_str).strip()
        link_md = f"* [{text}]({link_target})"
        section = args.get("section")
        c.body = _append_to_section(c.body, str(section) if section else None, link_md)
        return True, None, True
    # Idempotent: if a link to that target already exists, no-op.
    existing_targets = {
        link.target for link in c.links(bundle_root=bundle.root) if link.target
    }
    if target_cid in existing_targets:
        return False, "already_linked", False
    text = str(args.get("anchor_text") or label or target.title).strip()
    link_md = f"* [{text}]({_absolute_link(target)})"
    section = args.get("section")
    c.body = _append_to_section(c.body, str(section) if section else None, link_md)
    return True, None, True


# ---- set_tag ---------------------------------------------------------------


def _h_set_tag(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    tag = str(args.get("tag", ""))
    cur = c.frontmatter.get("tags")
    if cur == [tag]:
        return False, "same_value", False
    c.frontmatter["tags"] = [tag]
    return True, None, True


# ---- add_tag ---------------------------------------------------------------


def _h_add_tag(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    tag = str(args.get("tag", ""))
    cur = c.frontmatter.get("tags")
    if cur is None:
        c.frontmatter["tags"] = [tag]
        return True, None, True
    if isinstance(cur, str):
        if cur == tag:
            return False, "already_tagged", False
        c.frontmatter["tags"] = [cur, tag]
        return True, None, True
    if isinstance(cur, list):
        if tag in cur:
            return False, "already_tagged", False
        cur.append(tag)
        return True, None, True
    # Unexpected type: coerce to a clean list.
    c.frontmatter["tags"] = [tag]
    return True, None, True


# ---- set_frontmatter -------------------------------------------------------


def _h_set_frontmatter(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    key = str(args.get("key", ""))
    if not key:
        raise KeyError("key")
    new_value = args.get("value")
    current = c.frontmatter.get(key)
    # Idempotency: skip if the value is already set to the same value.
    # P1-6(a): use strict type-aware equality so bool/int/float changes
    # are NOT silently dropped as ``same_value`` (Python ``True == 1``
    # would otherwise mask the mutation).
    if _values_equal(current, new_value):
        return False, "same_value", False
    # AGENTS.md hard rule #7: "Auto-update must not destroy hand-curated
    # content." If the key already has a non-empty, non-placeholder value
    # and the new value is a placeholder (starts with "TODO:"), skip to
    # avoid clobbering the user's hand-written content.
    if (
        current
        and isinstance(current, str)
        and isinstance(new_value, str)
        and new_value.startswith("TODO:")
        and not current.startswith("TODO:")
    ):
        return False, "would_overwrite_hand_written", False
    c.frontmatter[key] = new_value
    return True, None, True


# ---- add_relation ----------------------------------------------------------


def _h_add_relation(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    """Append a typed relation entry to the ``relations:`` frontmatter key.

    P2-29 DECISION: this handler does NOT validate that ``target_concept_id``
    resolves to an existing concept in the bundle. OKF permissiveness
    (SPEC §9 / AGENTS.md hard rule #3) explicitly allows forward references:
    a relation may point at not-yet-written knowledge. This differs
    deliberately from :func:`_h_add_link`, which DOES validate the target
    exists — because a markdown link points at a concrete file (a broken
    file link is a different failure mode than a forward-reference relation).
    See ``test_add_relation_accepts_forward_reference`` for the regression.
    """
    target_str = args.get("target_concept_id")
    if not target_str:
        raise KeyError("target_concept_id")
    # Validate the shape (raises ConceptIdError -> caught upstream).
    target_text = str(target_str).strip()
    target_cid = concept_id_from_str(target_text)
    relation_type = str(args.get("relation_type", "related_to")).strip()
    if not relation_type:
        raise ValueError("relation_type must be a non-empty string")
    entry = {
        "target": target_text,
        "type": relation_type,
        "detail": str(args.get("detail", "")),
    }
    cur = c.frontmatter.get("relations")
    if cur is None:
        c.frontmatter["relations"] = [entry]
        return True, None, True
    if isinstance(cur, list):
        exists = False
        for relation in cur:
            if not isinstance(relation, dict):
                continue
            if str(relation.get("type", "")).strip() != relation_type:
                continue
            try:
                existing_target = concept_id_from_str(
                    str(relation.get("target", "")).strip()
                )
            except (ConceptIdError, ValueError):
                continue
            if existing_target == target_cid:
                exists = True
                break
        if exists:
            return False, "already_related", False
        cur.append(entry)
        return True, None, True
    # Fail closed: a governed value with the wrong shape is hand-authored
    # content too. Replacing it would silently destroy the evidence that
    # validation surfaces; require the author to repair the shape first.
    return False, "malformed_relations", False


# ---- append_body_section ---------------------------------------------------


def _h_append_body_section(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    heading = str(args.get("heading", "")).strip()
    if not heading:
        raise KeyError("heading")
    existing = any(
        h.text.strip().lower() == heading.lower() for h in c.headings
    )
    if existing:
        return False, "section_exists", False
    section_body = str(args.get("body", ""))
    block = f"\n# {heading}\n\n{section_body}\n"
    c.body = c.body.rstrip("\n") + block
    _refresh_headings(c)
    return True, None, True


# ---- update_section / replace_text (block-level partial updates) -----------
#
# The partial-update mutators exist so an agent can say "swap this block,
# keep the rest" instead of reconstructing a whole document through
# ``write-concept --force`` (which is where staging copies — and their
# clobber-drift risk — come from). Both mutate ONLY the body; frontmatter
# stays untouched, so the round-trip serializer preserves it byte-for-byte.


def _parse_heading_spec(spec: str) -> tuple[int | None, str]:
    """Parse a heading spec into ``(level, text)``.

    ``"## Banking"`` → ``(2, "Banking")`` (level-pinned match);
    ``"Banking"`` → ``(None, "Banking")`` (matches any level).
    """
    s = spec.strip()
    m = re.match(r"^(#{1,6})\s+(.+?)\s*$", s)
    if m:
        return len(m.group(1)), m.group(2).strip()
    return None, s


def _section_spans(body: str) -> list[dict]:
    """Locate every ATX heading outside fenced code blocks.

    Returns dicts of ``{level, text, start, content_start, end}`` where
    ``start`` is the offset of the heading line, ``content_start`` is the
    offset just past the heading line's newline, and ``end`` is the offset
    of the next heading with level <= this level (subsections belong to
    their parent section — same outline semantics as the viewer's section
    rail), or ``len(body)``.
    """
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
    fence_re = re.compile(r"^(```|~~~)")
    spans: list[dict] = []
    offset = 0
    in_fence = False
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if fence_re.match(stripped):
            in_fence = not in_fence
        elif not in_fence:
            m = heading_re.match(stripped)
            if m:
                spans.append({
                    "level": len(m.group(1)),
                    "text": m.group(2).strip(),
                    "start": offset,
                    "content_start": offset + len(line),
                })
        offset += len(line)
    for i, span in enumerate(spans):
        end = len(body)
        for nxt in spans[i + 1:]:
            if nxt["level"] <= span["level"]:
                end = nxt["start"]
                break
        span["end"] = end
    return spans


def _strip_leading_matching_heading(fragment: str, section_text: str) -> str:
    """Drop the fragment's first line when it repeats the target heading.

    Agents naturally include the heading in a ``frag.md`` they hand to
    ``update-section``; without this, the section would render its own
    heading twice.
    """
    lead = fragment.lstrip("\n")
    first, sep, rest = lead.partition("\n")
    lvl, text = _parse_heading_spec(first)
    if lvl is not None and text.casefold() == section_text.casefold():
        return rest if sep else ""
    return fragment


def _h_update_section(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    """Replace (or append to) ONE section's content, keeping the rest.

    ``heading`` may be level-pinned (``"## Banking"``) or bare
    (``"Banking"``, any level); matching is case-insensitive on the heading
    text. Fail-closed: a missing section is ``section_not_found`` (unless
    ``create_if_missing``), a duplicate heading is ``section_ambiguous:N`` —
    never a silent guess. The section span includes its subsections (up to
    the next heading of the same or higher level).
    """
    heading_spec = str(args.get("heading", "")).strip()
    if not heading_spec:
        raise KeyError("heading")
    if args.get("body") is None:
        raise KeyError("body")
    fragment = str(args.get("body"))
    mode = str(args.get("mode", "replace"))
    if mode not in ("replace", "append"):
        raise ValueError(f"mode must be replace|append, got {mode!r}")
    level_req, text_req = _parse_heading_spec(heading_spec)
    matches = [
        s for s in _section_spans(c.body)
        if s["text"].casefold() == text_req.casefold()
        and (level_req is None or s["level"] == level_req)
    ]
    if not matches:
        if bool(args.get("create_if_missing")):
            level = level_req or 2
            block = f"\n{'#' * level} {text_req}\n\n{fragment.strip()}\n"
            c.body = c.body.rstrip("\n") + block
            _refresh_headings(c)
            return True, "section_created", True
        return False, "section_not_found", False
    if len(matches) > 1:
        return False, f"section_ambiguous:{len(matches)}", False
    sec = matches[0]
    fragment = _strip_leading_matching_heading(fragment, sec["text"])
    content = fragment.strip("\n")
    tail = c.body[sec["end"]:]
    if mode == "append":
        prefix = c.body[:sec["end"]]
        if not prefix.endswith("\n"):
            prefix += "\n"
        candidate = prefix.rstrip("\n") + "\n\n" + content + "\n" + ("\n" if tail else "") + tail
    else:
        mid = ("\n" + content + "\n") if content else "\n"
        if tail:
            mid += "\n"
        candidate = c.body[:sec["content_start"]] + mid + tail
    if candidate == c.body:
        return False, "section_unchanged", False
    c.body = candidate
    _refresh_headings(c)
    return True, None, True


def _h_replace_text(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    """Exact textual patch on the body: swap ``old`` for ``new``.

    Fail-closed: zero matches is ``text_not_found``; multiple matches is
    ``text_ambiguous:N`` unless ``all`` is set (include more surrounding
    context in ``old`` to disambiguate — same contract as an editor's
    replace-exact).
    """
    old = args.get("old")
    if not old:
        raise KeyError("old")
    old = str(old)
    new = str(args.get("new", ""))
    if old == new:
        return False, "same_value", False
    count = c.body.count(old)
    if count == 0:
        return False, "text_not_found", False
    if count > 1 and not bool(args.get("all")):
        return False, f"text_ambiguous:{count}", False
    c.body = c.body.replace(old, new)
    _refresh_headings(c)
    return True, f"replaced {count} occurrence(s)", True


# ---- remove_link -----------------------------------------------------------


def _h_remove_link(bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    target_str = args.get("target_concept_id")
    if not target_str:
        raise KeyError("target_concept_id")
    target_cid = concept_id_from_str(str(target_str))
    links = c.links(bundle_root=bundle.root)
    raws_to_remove = {
        link.target_raw for link in links if link.target == target_cid
    }
    if not raws_to_remove:
        return False, "not_linked", False
    count = 0

    def _blank(m: re.Match) -> str:
        nonlocal count
        if m.group("target") in raws_to_remove:
            count += 1
            # Replace the whole link with its label text (keeps prose readable).
            return m.group("label")
        return m.group(0)

    c.body = _LINK_RE.sub(_blank, c.body)
    _refresh_headings(c)
    return True, f"removed {count} link(s)", True


# ---- add_entity (current spec §7) -------------------------------------------


def _h_add_entity(_bundle: Bundle, c: Concept, args: dict) -> _HandlerResult:
    """Append an entity to ``entities:`` frontmatter (idempotent on label+kind)."""
    label = str(args.get("label", "")).strip()
    if not label:
        raise KeyError("label")
    kind = str(args.get("kind", "")).strip()
    entity_id = args.get("entity_id")
    entity_aliases = args.get("aliases", [])

    entry: dict = {"label": label}
    if kind:
        entry["kind"] = kind
    if entity_id:
        entry["id"] = str(entity_id)
    if entity_aliases:
        entry["aliases"] = list(entity_aliases)

    cur = c.frontmatter.get("entities")
    if cur is None:
        c.frontmatter["entities"] = [entry]
        return True, None, True
    if isinstance(cur, list):
        # Idempotent: check if entity with same label+kind already exists
        for existing in cur:
            if isinstance(existing, dict):
                if (
                    existing.get("label", "").lower() == label.lower()
                    and str(existing.get("kind", "")).lower() == kind.lower()
                ):
                    return False, "entity_exists", False
            elif isinstance(existing, str) and existing.lower() == label.lower():
                return False, "entity_exists", False
        cur.append(entry)
        return True, None, True
    # Unexpected shape: overwrite
    c.frontmatter["entities"] = [entry]
    return True, None, True


_HANDLERS: dict[str, Any] = {
    "add_link": _h_add_link,
    "set_tag": _h_set_tag,
    "add_tag": _h_add_tag,
    "set_frontmatter": _h_set_frontmatter,
    "add_relation": _h_add_relation,
    "append_body_section": _h_append_body_section,
    "update_section": _h_update_section,
    "replace_text": _h_replace_text,
    "remove_link": _h_remove_link,
    "add_entity": _h_add_entity,
}


# ---------------------------------------------------------------------------
# write_concept — library-level create/update for a single concept (P1-25)
# ---------------------------------------------------------------------------
#
# Extracted from cli.cmd_write_concept so the round-trip-safe mutation
# pipeline has a single, testable library seam. The CLI verb becomes a thin
# argparse→kwargs adapter (cli.cmd_write_concept). Reuses the same
# discipline as _h_set_frontmatter (preserve unknown keys + order; honor
# would_overwrite_hand_written), the round-trip-preserving serializer
# (P1-24), and atomic_write_text (SPEC §3.4 / AGENTS.md hard rule #8).
#
# P2-26: tags dedupe on the CREATE path (UPDATE already dedupes via
# _h_add_tag's order-preserving membership check).
#
# P2-27: returns the canonical 3-key shape ``{status, path, id}``. For
# backward compat with code that reads the compatibility ``created`` / ``updated``
# keys (e.g. tests/test_authoring.py written by Bundle A), we ALSO emit the
# alias key as a path-string value. New consumers
# should read ``status`` + ``path``; the aliases are deprecated.
#
# P0-1 (already fixed in cli.py): concept_id_to_path joins bundle_root
# internally, so we use it DIRECTLY — never ``bundle_root / path(...)``.

# Canonical frontmatter key order for newly-created concepts (SPEC §8.1).
_CREATE_KEY_ORDER: tuple[str, ...] = (
    "type", "title", "description", "resource", "tags", "timestamp",
)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Dedupe a list preserving first-occurrence order (P2-26)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


class WriteConceptError(Exception):
    """Raised by :func:`write_concept` for caller-visible failure modes.

    The CLI maps each variant to a specific exit code (see
    ``cmd_write_concept``):
      * ``body_refused`` → exit 1 (caller must pass --force).
      * ``reserved_filename`` → exit 1.
      * ``bundle_not_found`` → exit 2.
      * ``rev_conflict`` → exit 1 (concurrent edit; see §9.3/§9.4).
      * ``studio_blocked`` → exit 1 (studio constraint refused the write).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def write_concept(
    bundle_or_root: "Bundle | str | Path",
    cid: ConceptId | str,
    *,
    type: str,
    title: str | None = None,
    description: str | None = None,
    resource: str | None = None,
    tags: list[str] | None = None,
    timestamp: str | None = None,
    body: str | None = None,
    body_file: str | Path | None = None,
    force: bool = False,
    defaults: bool = True,
    studio: Any = None,
    actor: str = "agent",
    origin: str = "mutator",
    group_id: str | None = None,
) -> dict:
    """Create or update a single concept file via the round-trip-safe pipeline.

    Args:
        bundle_or_root: a ``Bundle``, a bundle-root ``Path``, or a path
            string. When a ``Bundle`` is passed, its ``.root`` is used.
        cid: concept id (tuple or slash-separated string, e.g.
            ``("tables", "orders")`` or ``"tables/orders"``).
        type: concept type (SPEC-required). On UPDATE, overrides the
            existing type when non-None.
        title, description, resource, timestamp: optional recommended
            frontmatter keys. On UPDATE, override the existing value when
            non-None; ``None`` leaves the existing value untouched.
        tags: optional list of tags. On CREATE: deduped preserving order
            (P2-26). On UPDATE: appended to the existing list (deduped,
            order-preserving) — same discipline as ``_h_add_tag``.
        body: optional body text. Mutually exclusive with ``body_file``.
        body_file: optional path to read the body from.
        force: when True, allows overwriting a non-empty existing body.
            Without ``--force``, supplying ``body``/``body_file`` against a
            concept whose existing body is non-empty raises
            ``WriteConceptError(code='body_refused')``.
        defaults: when True (the default), CREATE fills the mechanically
            derivable recommended keys that ``validate --strict`` would
            otherwise flag: ``resource`` defaults to the bundle-absolute
            concept path (the ``/demo/showcase.md`` convention) and
            ``timestamp`` to the current UTC instant. Explicit values always
            win; UPDATE never injects defaults (an existing concept may
            deliberately omit them). Pass ``defaults=False`` (CLI:
            ``--no-defaults``) for the bare pre-existing behaviour.
        studio: optional :class:`okf_loom.studio.Studio`. When provided
            (current spec §10/§13), the write is routed through
            ``studio.save_concept(...)`` — the single internal write funnel
            that attributes the write to the agent, snapshots prior bytes
            for one-click Undo, marks the rev as logged for watcher dedup,
            and broadcasts `changed` (+ `graph` for graph-affecting ops)
            over SSE. Without ``studio`` (no-session compatibility path), writes go
            directly through ``atomic_write_text`` (no attribution, no
            undo snapshot).
        actor: when ``studio`` is set, the activity actor (default ``agent``).
        origin: when ``studio`` is set, the activity origin (default
            ``mutator``).
        group_id: optional group manifest id (§12.5) so this write is
            undoable as part of a larger agent pass.

    Returns:
        ``{"status": "created" | "updated", "path": str, "id": str,
        "reason": str | None}``. For backward compat, the matching compatibility
        key (``created`` or ``updated``) is also present, holding the path
        string — new consumers should prefer ``status`` + ``path``.

    Raises:
        WriteConceptError: ``bundle_not_found``, ``reserved_filename``,
            or ``body_refused``.
        ConceptIdError: invalid concept id.
    """
    # Resolve bundle_root + concept_path. concept_id_to_path already joins
    # bundle_root, so we use it directly (P0-1 regression guard).
    if isinstance(bundle_or_root, Bundle):
        bundle_root = Path(bundle_or_root.root)
    else:
        bundle_root = Path(bundle_or_root)
    if not bundle_root.exists() or not bundle_root.is_dir():
        raise WriteConceptError(
            "bundle_not_found",
            f"Bundle directory not found: {bundle_root}",
        )

    if isinstance(cid, str):
        cid_tuple = concept_id_from_str(cid)
    else:
        cid_tuple = cid
        # Validate segments.
        concept_id_from_str("/".join(cid_tuple))
    concept_path = concept_id_to_path(bundle_root, cid_tuple)
    cid_str = concept_id_to_str(cid_tuple)

    # Guard: refuse to write to reserved filenames (index.md, log.md).
    from . import RESERVED_FILENAMES
    reserved_lower = {r.lower() for r in RESERVED_FILENAMES}
    if concept_path.name.lower() in reserved_lower:
        raise WriteConceptError(
            "reserved_filename",
            f"refusing to overwrite reserved filename '{concept_path.name}' "
            f"(SPEC §6/§7 — use a different concept id)",
        )

    # Resolve body text.
    body_text = ""
    if body is not None and body_file is not None:
        raise WriteConceptError(
            "body_refused",
            "--body and --body-file are mutually exclusive",
        )
    if body is not None:
        body_text = body
    elif body_file is not None:
        body_text = Path(body_file).read_text(encoding="utf-8")

    if concept_path.exists():
        # ----- UPDATE -----
        original_raw = concept_path.read_text(encoding="utf-8")
        try:
            existing_fm, existing_body = parse_document(original_raw)
        except Exception:
            # If the existing file is unparseable, fall through to safe_dump
            # by treating it as no-original (the round-trip serializer will
            # then use safe_dump).
            existing_fm, existing_body = {}, original_raw
            original_raw = ""

        fm = dict(existing_fm)  # preserve key order + unknown keys

        # Apply provided frontmatter keys in canonical order so updates
        # don't reorder existing entries (the round-trip serializer relies
        # on the dict-iteration order matching the original file's order
        # for unchanged keys).
        for k, v in (
            ("type", type),
            ("title", title),
            ("description", description),
            ("resource", resource),
            ("timestamp", timestamp),
        ):
            if v is not None:
                fm[k] = v

        # Tags: append (dedupe, preserve order) — same discipline as
        # _h_add_tag's UPDATE path.
        if tags:
            cur_tags = fm.get("tags", [])
            if not isinstance(cur_tags, list):
                cur_tags = [cur_tags] if cur_tags else []
            # Preserve order, dedupe against existing + within new tags.
            merged = list(cur_tags)
            seen: set[str] = set(str(t) for t in merged)
            for t in tags:
                ts = str(t)
                if ts not in seen:
                    merged.append(ts)
                    seen.add(ts)
            fm["tags"] = merged

        # Body replace semantics (SPEC §8.1 cross-cutting #5).
        new_body = existing_body
        if body_text:
            if force or not existing_body.strip():
                new_body = body_text
            else:
                raise WriteConceptError(
                    "body_refused",
                    f"refusing to overwrite non-empty body in {concept_path}; "
                    f"use --force",
                )

        # Round-trip-preserving serialize (P1-24).
        content = serialize_document_round_trip(original_raw, fm, new_body)
        # P1-2 (ARCH2-002): route through save_concept when a studio is set
        # so the write is attributed + undoable + broadcast. Without a
        # studio, fall back to atomic_write_text (no-session compatibility path).
        if studio is not None:
            expected_rev = None
            try:
                prior_bytes = concept_path.read_bytes() if concept_path.is_file() else None
                if prior_bytes is not None and prior_bytes != original_raw.encode("utf-8"):
                    from .studio import rev_of
                    expected_rev = rev_of(original_raw)
            except OSError:
                pass
            res = studio.save_concept(
                concept_id=cid_str, raw=content, actor=actor,
                action="write_concept", origin=origin, group_id=group_id,
                expected_rev=expected_rev,
                summary=f"wrote concept {cid_str}",
            )
            if not res.ok:
                if res.conflict:
                    raise WriteConceptError(
                        "rev_conflict",
                        f"concurrent edit on {cid_str}: expected {res.expected_rev}, "
                        f"current {res.current_rev}",
                    )
                raise WriteConceptError("studio_blocked", res.error or "studio write refused")
        else:
            atomic_write_text(concept_path, content)

        return {
            "status": "updated",
            "path": str(concept_path),
            "id": cid_str,
            "reason": None,
            # Compatibility alias (deprecated; P2-27 spec wording is status+path).
            "updated": str(concept_path),
        }

    # ----- CREATE -----
    concept_path.parent.mkdir(parents=True, exist_ok=True)

    # Strict-friendly defaults: fill the mechanically derivable recommended
    # keys so a freshly created concept doesn't immediately fail
    # ``validate --strict`` on resource/timestamp (feedback: creating then
    # needing two set-frontmatter calls to satisfy strict). Only the keys a
    # machine can derive without inventing content — title/description/tags
    # stay the author's job (AGENTS.md hard rule #7: no placeholder prose).
    defaults_applied: list[str] = []
    if defaults:
        if resource is None:
            rel = str(concept_path.relative_to(bundle_root)).replace(os.sep, "/")
            resource = f"/{rel}"
            defaults_applied.append("resource")
        if timestamp is None:
            from datetime import datetime, timezone
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            defaults_applied.append("timestamp")

    # Build frontmatter in canonical order (SPEC §8.1).
    fm: dict[str, Any] = {"type": type}
    if title is not None:
        fm["title"] = title
    if description is not None:
        fm["description"] = description
    if resource is not None:
        fm["resource"] = resource
    if tags:
        # P2-26: dedupe on CREATE, preserving first-occurrence order.
        fm["tags"] = _dedupe_preserve_order([str(t) for t in tags])
    if timestamp is not None:
        fm["timestamp"] = timestamp

    body_out = body_text or ""
    # New concept → no original raw text → safe_dump canonical form.
    content = serialize_document_round_trip(None, fm, body_out)
    # P1-2: route through save_concept on CREATE too — the prior_bytes is
    # None (new file) so save_concept records an undoable create.
    if studio is not None:
        res = studio.save_concept(
            concept_id=cid_str, raw=content, actor=actor,
            action="write_concept", origin=origin, group_id=group_id,
            summary=f"created concept {cid_str}",
        )
        if not res.ok:
            raise WriteConceptError("studio_blocked", res.error or "studio write refused")
    else:
        atomic_write_text(concept_path, content)

    out: dict[str, Any] = {
        "status": "created",
        "path": str(concept_path),
        "id": cid_str,
        "reason": None,
        # Compatibility alias (deprecated; P2-27 spec wording is status+path).
        "created": str(concept_path),
    }
    if defaults_applied:
        out["defaults_applied"] = defaults_applied
    return out
