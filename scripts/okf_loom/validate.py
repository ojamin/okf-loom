"""Validator implementing SPEC §9 conformance plus soft-issue warnings.

Per SPEC §9 a bundle is conformant iff:
    1. Every non-reserved .md file has a parseable YAML frontmatter block.
    2. Every frontmatter block has a non-empty `type` field.
    3. Reserved filenames (`index.md`, `log.md`) follow §6/§7 structure.

All other constraints are SOFT guidance; consumers MUST NOT reject a bundle
for them (SPEC §9 explicit list). This validator therefore splits findings
into `errors` (conformance failures) and `warnings` (soft issues), and the
CLI exposes a `--strict` flag that promotes warnings to errors for CI use.

Validation profiles (current spec §5)
-----------------------------
Named profiles remap finding *codes* to different severities. The canonical
profile-aware entry point is :func:`validate_bundle_with_profile`; the bare
:func:`validate_bundle` always uses the ``spec`` baseline. The selected
profile is recorded on the :class:`ValidationReport` and surfaced in
``as_dict()`` under the ``"profile"`` key (so library callers see it without
the CLI post-hoc injecting it).

Code → resulting severity by profile (only the remapped codes are shown;
every other code keeps its ``spec`` severity under all profiles):

    ===========================  =========  =========  ========
    Finding code                 spec       producer   loose
    ===========================  =========  =========  ========
    concept.missing_type         ERROR      ERROR      WARNING
    concept.missing_recommended_keys  WARNING  ERROR   WARNING
    ===========================  =========  =========  ========

Orthogonal modifiers (compose with ANY profile):

    --fail-on-broken-links   promotes ``link.broken`` → ERROR.
    --strict                 promotes every remaining WARNING → ERROR
                             (applied AFTER the profile remap, so e.g.
                             ``loose`` + ``--strict`` re-promotes the
                             demoted ``concept.missing_type`` back to ERROR).

Note on ``--checks``: when an explicit ``--checks`` subset is given on the
CLI, :func:`validate_bundle` is called directly and profile remapping is
NOT applied (profiles compose over the full check set). In that path the
JSON ``profile`` reflects the requested name for traceability but no
severity remap runs; see ``cli.cmd_validate`` for the reconciliation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import (
    RECOMMENDED_FRONTMATTER_KEYS,
    SPEC_VERSION,
)
from .model import Bundle, Concept, IndexFile
from .paths import ConceptId, ConceptIdError, concept_id_from_str, concept_id_to_str


class Severity(str, Enum):
    ERROR = "error"      # SPEC §9 conformance failure
    WARNING = "warning"  # soft issue (SPEC-mandated tolerance)
    INFO = "info"        # advisory (e.g. unused capability declarations)


@dataclass(frozen=True)
class Finding:
    """One validator finding."""

    code: str
    severity: Severity
    message: str
    path: Path | None = None
    line: int | None = None
    concept_id: ConceptId | None = None
    # Optional structured detail (for machine consumers).
    detail: dict | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "line": self.line,
            "concept_id": (
                concept_id_to_str(self.concept_id) if self.concept_id else None
            ),
            "detail": self.detail,
        }


@dataclass
class ValidationReport:
    bundle_root: Path
    findings: list[Finding] = field(default_factory=list)
    spec_version: str = SPEC_VERSION
    bundle_okf_version: str | None = None
    # Current spec §5: the profile that produced this report (spec/producer/loose).
    # ``validate_bundle`` leaves it at the default "spec"; ``apply_profile``
    # / ``validate_bundle_with_profile`` stamp the effective profile. Emitted
    # in ``as_dict()`` so library callers see it without CLI injection.
    profile: str = "spec"

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.INFO]

    @property
    def ok(self) -> bool:
        """True iff there are no ERROR findings (warnings are tolerated)."""
        return not self.errors

    @property
    def ok_strict(self) -> bool:
        """True iff there are no ERROR or WARNING findings."""
        return not self.errors and not self.warnings

    def as_dict(self) -> dict:
        return {
            "bundle_root": str(self.bundle_root),
            "spec_version": self.spec_version,
            "bundle_okf_version": self.bundle_okf_version,
            "profile": self.profile,
            "ok": self.ok,
            "ok_strict": self.ok_strict,
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "info": len(self.infos),
            },
            # iter3 P2-3: sort findings deterministically (§3.2) so
            # ``--format json`` output is byte-stable regardless of
            # Bundle dict-insertion order. Sort by (path, line, code,
            # concept_id) — the same logical order a human scans.
            "findings": [
                f.as_dict()
                for f in sorted(
                    self.findings,
                    key=lambda f: (
                        str(f.path) if f.path else "",
                        f.line or 0,
                        f.code,
                        str(f.concept_id) if f.concept_id else "",
                    ),
                )
            ],
        }


# ---------------------------------------------------------------------------
# Validation profiles (current spec §5)
# ---------------------------------------------------------------------------

# Profile code → severity remap tables.
#
# `spec` (default): SPEC §9 conformance codes are errors; soft issues are
#   warnings/info.  (This is the baseline behavior of ``validate_bundle``.)
# `producer`: ``spec`` PLUS ``concept.missing_recommended_keys`` → ERROR
#   (agent-produced bundles must be complete).
# `loose`: ``spec`` EXCEPT ``concept.missing_type`` → WARNING
#   (migration aid; syntax errors stay errors).

_PROFILE_REMAPS: dict[str, dict[str, Severity]] = {
    "spec": {},
    "producer": {
        "concept.missing_recommended_keys": Severity.ERROR,
    },
    "loose": {
        "concept.missing_type": Severity.WARNING,
    },
}

# Codes promoted to ERROR by ``--fail-on-broken-links``. asset.missing is a
# broken reference to a file just like link.broken, so the flag covers both.
_BROKEN_LINK_CODES: frozenset[str] = frozenset({"link.broken", "asset.missing"})


def apply_profile(
    report: ValidationReport,
    *,
    profile: str = "spec",
    fail_on_broken_links: bool = False,
) -> ValidationReport:
    """Remap finding severities by validation profile (current spec §5).

    Returns a **new** :class:`ValidationReport` with remapped severities.
    The original report is not mutated. ``Finding`` is frozen, so new
    instances are constructed with the updated severity.

    Args:
        report: the base validation report from :func:`validate_bundle`.
        profile: one of ``"spec"``, ``"producer"``, ``"loose"``.
        fail_on_broken_links: if True, promote ``link.broken`` to ERROR.

    The remap table per profile:

    ============ =================================================
    Profile      Behavior
    ============ =================================================
    ``spec``     Current behavior (no remap).
    ``producer`` ``spec`` + ``concept.missing_recommended_keys`` → ERROR
    ``loose``    ``spec`` except ``concept.missing_type`` → WARNING
    ============ =================================================

    ``--fail-on-broken-links`` is orthogonal and composes with any profile.

    Raises:
        ValueError: if ``profile`` is not one of the known profile names
            (``"spec"`` / ``"producer"`` / ``"loose"``). This is fail-closed:
            an unknown profile name must not silently behave as ``spec``
            while the JSON report claims the typo name.
    """
    if profile not in _PROFILE_REMAPS:
        raise ValueError(
            f"Unknown validation profile {profile!r}; "
            f"expected one of {sorted(_PROFILE_REMAPS)}."
        )
    remap = _PROFILE_REMAPS[profile]
    new_findings: list[Finding] = []
    for f in report.findings:
        new_sev = f.severity
        if f.code in remap:
            new_sev = remap[f.code]
        if fail_on_broken_links and f.code in _BROKEN_LINK_CODES:
            new_sev = Severity.ERROR
        if new_sev is not f.severity:
            new_findings.append(
                Finding(
                    code=f.code,
                    severity=new_sev,
                    message=f.message,
                    path=f.path,
                    line=f.line,
                    concept_id=f.concept_id,
                    detail=f.detail,
                )
            )
        else:
            new_findings.append(f)
    return ValidationReport(
        bundle_root=report.bundle_root,
        findings=new_findings,
        spec_version=report.spec_version,
        bundle_okf_version=report.bundle_okf_version,
        profile=profile,
    )


def validate_bundle_with_profile(
    bundle: Bundle,
    *,
    profile: str = "spec",
    fail_on_broken_links: bool = False,
    strict: bool = False,
) -> ValidationReport:
    """Validate ``bundle`` and apply a profile + optional strict + broken-link fail.

    Convenience wrapper: ``validate_bundle`` → ``apply_profile`` → strict promotion.

    Args:
        bundle: loaded OKF bundle.
        profile: validation profile name (``"spec"`` / ``"producer"`` / ``"loose"``).
        fail_on_broken_links: promote ``link.broken`` to ERROR.
        strict: promote remaining WARNING to ERROR (applied after profile remap).

    Returns:
        Profile-adjusted :class:`ValidationReport`.
    """
    base = validate_bundle(bundle)
    report = apply_profile(
        base, profile=profile, fail_on_broken_links=fail_on_broken_links
    )
    if strict:
        new_findings: list[Finding] = []
        for f in report.findings:
            if f.severity is Severity.WARNING:
                new_findings.append(
                    Finding(
                        code=f.code,
                        severity=Severity.ERROR,
                        message=f.message,
                        path=f.path,
                        line=f.line,
                        concept_id=f.concept_id,
                        detail=f.detail,
                    )
                )
            else:
                new_findings.append(f)
        report = ValidationReport(
            bundle_root=report.bundle_root,
            findings=new_findings,
            spec_version=report.spec_version,
            bundle_okf_version=report.bundle_okf_version,
            profile=report.profile,
        )
    return report


# ---------------------------------------------------------------------------
# Load-warning codes that are SPEC §9 conformance failures (→ ERROR).
# ---------------------------------------------------------------------------

# §9 #1: every non-reserved .md file MUST have parseable YAML frontmatter.
_CONFORMANCE_PARSE_CODES: frozenset[str] = frozenset({
    "concept.parse_failed",
})

# §9 #2: every frontmatter block MUST contain a non-empty `type` field.
_CONFORMANCE_TYPE_CODES: frozenset[str] = frozenset({
    "concept.missing_type",
})

# §9 #3: reserved filenames MUST follow §6 / §7 structure.
#   - index.md: no frontmatter except at root (§6, §11).
#   - log.md: ISO 8601 YYYY-MM-DD date headings (§7 "MUST use").
_CONFORMANCE_STRUCTURE_CODES: frozenset[str] = frozenset({
    "index.unexpected_frontmatter",
    "log.bad_date_heading",
})

# Note: `log.not_newest_first` is left as WARNING — SPEC §7 describes
# "newest first" as the conventional ordering, not as a MUST. A
# not-newest-first log is still parseable and useful; treating it as a
# hard conformance failure would be too aggressive.

_CONFORMANCE_ERROR_CODES: frozenset[str] = (
    _CONFORMANCE_PARSE_CODES
    | _CONFORMANCE_TYPE_CODES
    | _CONFORMANCE_STRUCTURE_CODES
)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def validate_bundle(
    bundle: Bundle,
    *,
    checks: "CheckSpec | None" = None,
) -> ValidationReport:
    """Run all validators against a loaded bundle.

    Args:
        bundle: a loaded `Bundle`.
        checks: optional CheckSpec to enable/disable individual checks
            (default: all enabled).

    Returns:
        A ValidationReport with errors (SPEC §9 conformance) and warnings
        (soft issues). The report's `ok` flag is the SPEC-mandated pass/fail.
    """
    spec = checks or CheckSpec()
    report = ValidationReport(
        bundle_root=bundle.root,
        bundle_okf_version=bundle.okf_version,
    )

    # Promote load warnings to findings.
    # SPEC §9 conformance failures (violations of the three hard rules:
    # parseable frontmatter, non-empty `type`, reserved-filename structure)
    # become ERROR. Everything else stays WARNING.
    # The codes promoted here are the ones the loader emits when it detects
    # a §9 #1 / #2 / #3 violation; see model.py `_load_concept`/`_load_index`/
    # `_load_log` for the producers.
    for w in bundle.warnings:
        sev = (
            Severity.ERROR
            if w.code in _CONFORMANCE_ERROR_CODES
            else Severity.WARNING
        )
        report.findings.append(
            Finding(
                code=w.code,
                severity=sev,
                message=w.message,
                path=w.path,
                line=w.line,
            )
        )

    if spec.spec_version_check:
        _check_spec_version(bundle, report)
    if spec.concept_required_keys:
        _check_concept_required_keys(bundle, report)
    if spec.concept_recommended_keys:
        _check_concept_recommended_keys(bundle, report)
    if spec.governed_metadata:
        _check_governed_metadata(bundle, report)
    if spec.reserved_filenames:
        _check_reserved_filenames(bundle, report)
    if spec.link_integrity:
        _check_link_integrity(bundle, report)
        _check_asset_integrity(bundle, report)
        _check_citations_collision(bundle, report)
    if spec.index_structure:
        _check_index_structure(bundle, report)
    if spec.log_structure:
        _check_log_structure(bundle, report)
    if spec.tag_normalization:
        _check_tag_normalization(bundle, report)
    if spec.capability_declarations:
        _check_capability_declarations(bundle, report)
    if spec.timestamp_validity:
        _check_timestamp_validity(bundle, report)

    return report


# ---------------------------------------------------------------------------
# CheckSpec
# ---------------------------------------------------------------------------

# P2-7 (iter-3): map user-facing dotted finding codes (the codes users see in
# ``okf validate`` output and in JSON ``findings[].code``) to the CheckSpec
# category check that produces them. ``CheckSpec.only`` accepts BOTH the
# internal category names (``link_integrity``, ``concept_recommended_keys``,
# …) AND these dotted codes (``link.broken``, ``concept.missing_type``, …) so
# users can filter by the codes they actually see without memorising the
# undocumented internal names. A dotted code enables the responsible category
# check. Codes produced at load time (concept.missing_type, concept.parse_failed,
# …) are promoted into the report unconditionally by validate_bundle's
# load-warning loop (they are §9 conformance failures), so they appear
# regardless of which category is enabled; the mapping here only governs
# ACCEPTANCE in --checks (so the code is not rejected as "unknown").
_FINDING_CODE_TO_CATEGORY: dict[str, str] = {
    # spec_version_check
    "spec.version_undeclared": "spec_version_check",
    "spec.version_major_mismatch": "spec_version_check",
    "spec.version_newer_minor": "spec_version_check",
    "spec.version_older_minor": "spec_version_check",
    # concept_required_keys (concept.missing_type + parse-failure codes are
    # §9 #1/#2 conformance failures surfaced from load warnings)
    "concept.missing_type": "concept_required_keys",
    "concept.parse_failed": "concept_required_keys",
    "concept.unreadable": "concept_required_keys",
    "concept.body_too_large": "concept_required_keys",
    "concept.bad_id": "concept_required_keys",
    "concept.path_escapes_bundle": "concept_required_keys",
    "concept.duplicate_id": "concept_required_keys",
    # concept_recommended_keys
    "concept.missing_recommended_keys": "concept_recommended_keys",
    # governed_metadata
    "frontmatter.aliases_malformed": "governed_metadata",
    "frontmatter.entities_malformed": "governed_metadata",
    "frontmatter.provenance_malformed": "governed_metadata",
    "frontmatter.citations_malformed": "governed_metadata",
    "frontmatter.relations_malformed": "governed_metadata",
    "relation.duplicate": "governed_metadata",
    # reserved_filenames (also surfaces non-root index frontmatter)
    "index.non_root_frontmatter": "reserved_filenames",
    # link_integrity (link checks + asset/image checks + citations-collision
    # run under the same gate)
    "link.broken": "link_integrity",
    "link.form_inconsistency": "link_integrity",
    "link.wikilink_present": "link_integrity",
    "asset.missing": "link_integrity",
    "asset.out_of_bundle": "link_integrity",
    "concept.citations_both_forms": "link_integrity",
    # index_structure (index.missing_for_directory is the check-produced code;
    # index.parse_failed / index.unexpected_frontmatter are load warnings)
    "index.missing_for_directory": "index_structure",
    "index.parse_failed": "index_structure",
    "index.unexpected_frontmatter": "index_structure",
    # log_structure
    "log.bad_date_heading": "log_structure",
    "log.not_newest_first": "log_structure",
    # tag_normalization
    "tag.case_inconsistency": "tag_normalization",
    # capability_declarations
    "capability.declared": "capability_declarations",
    # timestamp_validity
    "concept.bad_timestamp": "timestamp_validity",
}


@dataclass(frozen=True)
class CheckSpec:
    """Toggle individual validator checks. All default to True."""

    spec_version_check: bool = True
    concept_required_keys: bool = True
    concept_recommended_keys: bool = True
    governed_metadata: bool = True
    reserved_filenames: bool = True
    link_integrity: bool = True
    index_structure: bool = True
    log_structure: bool = True
    tag_normalization: bool = True
    capability_declarations: bool = True
    timestamp_validity: bool = True

    @classmethod
    def only(cls, *names: str) -> "CheckSpec":
        """Build a CheckSpec with ONLY the named checks enabled.

        Accepts BOTH internal category names (``link_integrity``,
        ``concept_recommended_keys``, …) AND user-facing dotted finding
        codes (``link.broken``, ``concept.missing_type``, …) as they appear
        in ``okf validate`` output. A dotted code enables the category check
        responsible for producing it (P2-7 / iter-3). Mixed input is allowed.
        """
        defaults = {f.name for f in cls.__dataclass_fields__.values()}
        # Resolve each requested name to a category. Dotted codes map via
        # _FINDING_CODE_TO_CATEGORY; category names pass through unchanged.
        resolved: set[str] = set()
        unknown: list[str] = []
        for n in names:
            if n in defaults:
                resolved.add(n)
            elif n in _FINDING_CODE_TO_CATEGORY:
                resolved.add(_FINDING_CODE_TO_CATEGORY[n])
            else:
                unknown.append(n)
        if unknown:
            raise ValueError(
                f"Unknown check names: {sorted(unknown)}. "
                f"Valid: category names ({sorted(defaults)}) or dotted "
                f"finding codes (e.g. link.broken, concept.missing_type)."
            )
        return cls(**{n: (n in resolved) for n in defaults})


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _parse_version(v: str) -> tuple[int, int]:
    """Parse a ``"major.minor"`` version string into a (major, minor) tuple.

    Returns ``(0, 0)`` for unparseable input so callers can treat malformed
    versions conservatively (older than anything).
    """
    import re

    m = re.match(r"^\s*(\d+)(?:\.(\d+))?", v.strip())
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


def _check_spec_version(bundle: Bundle, report: ValidationReport) -> None:
    if bundle.okf_version is None:
        # SPEC §11: bundles MAY declare a version; absence is legal.
        report.findings.append(
            Finding(
                code="spec.version_undeclared",
                severity=Severity.INFO,
                message=(
                    "Bundle does not declare an okf_version in its root index.md. "
                    f"This toolkit assumes OKF v{SPEC_VERSION}."
                ),
            )
        )
        return
    # SPEC §11 mandates best-effort consumption on version mismatch. We:
    #   - same major + same minor → fully compatible, no finding.
    #   - same major + different minor → WARNING (compatible additions per
    #     SPEC §11; toolkit does best-effort).
    #   - different major → WARNING (may have breaking changes; still
    #     best-effort per SPEC §11 mandate, but the consumer should know).
    #   - malformed version → WARNING (cannot reason about compatibility).
    bundle_v = _parse_version(bundle.okf_version)
    toolkit_v = _parse_version(SPEC_VERSION)
    if bundle_v == toolkit_v:
        return  # exact match, no finding
    if bundle_v[0] != toolkit_v[0]:
        report.findings.append(
            Finding(
                code="spec.version_major_mismatch",
                severity=Severity.WARNING,
                message=(
                    f"Bundle declares okf_version={bundle.okf_version!r}; this "
                    f"toolkit implements v{SPEC_VERSION}. Major-version "
                    "differences MAY include breaking changes (SPEC §11). "
                    "Continuing with best-effort consumption."
                ),
            )
        )
    elif bundle_v > toolkit_v:
        report.findings.append(
            Finding(
                code="spec.version_newer_minor",
                severity=Severity.WARNING,
                message=(
                    f"Bundle declares okf_version={bundle.okf_version!r}, newer "
                    f"than okf-loom's v{SPEC_VERSION}. Some features may be "
                    "unrecognised; doing best-effort consumption per SPEC §11."
                ),
            )
        )
    else:
        # Older minor — backward-compatible per SPEC §11 (minor bumps add
        # optional features). No warning needed; this is the normal case
        # for an older bundle read by a newer toolkit.
        report.findings.append(
            Finding(
                code="spec.version_older_minor",
                severity=Severity.INFO,
                message=(
                    f"Bundle declares okf_version={bundle.okf_version!r}; this "
                    f"toolkit implements v{SPEC_VERSION}. Backward-compatible "
                    "(SPEC §11 minor bumps add optional features only)."
                ),
            )
        )


def _check_concept_required_keys(bundle: Bundle, report: ValidationReport) -> None:
    # `type` is the only SPEC-required key. Missing type was already promoted
    # from load warnings; nothing else to do here.
    pass


def _check_concept_recommended_keys(
    bundle: Bundle, report: ValidationReport
) -> None:
    for c in bundle.concepts.values():
        missing = [
            k for k in RECOMMENDED_FRONTMATTER_KEYS if not c.frontmatter.get(k)
        ]
        if missing:
            report.findings.append(
                Finding(
                    code="concept.missing_recommended_keys",
                    severity=Severity.WARNING,
                    message=(
                        f"Concept is missing recommended frontmatter keys: "
                        f"{', '.join(missing)}"
                    ),
                    path=c.path,
                    concept_id=c.id,
                    detail={"missing": missing},
                )
            )


def _governed_finding(
    report: ValidationReport,
    concept: Concept,
    *,
    code: str,
    message: str,
    detail: dict[str, Any],
) -> None:
    """Append one stable, warning-level governed-metadata finding."""
    report.findings.append(
        Finding(
            code=code,
            severity=Severity.WARNING,
            message=message,
            path=concept.path,
            concept_id=concept.id,
            detail=detail,
        )
    )


def _bad_string_field(entry: dict[str, Any], field_name: str) -> bool:
    """True when a present governed object field is not a non-empty string."""
    return field_name in entry and (
        not isinstance(entry[field_name], str) or not entry[field_name].strip()
    )


def _check_governed_metadata(bundle: Bundle, report: ValidationReport) -> None:
    """Validate shapes of the five governed extension keys.

    These are soft producer-quality checks, not additions to OKF v0.1's hard
    conformance surface.  Unknown top-level keys and unknown fields inside a
    valid governed object remain legal and untouched.
    """
    for concept in bundle.concepts.values():
        fm = concept.frontmatter

        if "aliases" in fm:
            aliases = fm["aliases"]
            code = "frontmatter.aliases_malformed"
            if not isinstance(aliases, list):
                _governed_finding(
                    report, concept, code=code,
                    message="`aliases` must be a list of strings or alias objects.",
                    detail={"key": "aliases", "reason": "expected_list", "actual": type(aliases).__name__},
                )
            else:
                for i, alias in enumerate(aliases):
                    reason = None
                    if isinstance(alias, str):
                        if not alias.strip():
                            reason = "empty_string"
                    elif isinstance(alias, dict):
                        if _bad_string_field(alias, "label") or "label" not in alias:
                            reason = "missing_or_invalid_label"
                        elif "discoverable" in alias and not isinstance(alias["discoverable"], bool):
                            reason = "discoverable_must_be_boolean"
                    else:
                        reason = "expected_string_or_object"
                    if reason:
                        _governed_finding(
                            report, concept, code=code,
                            message=f"Malformed `aliases` entry at index {i}: {reason}.",
                            detail={"key": "aliases", "index": i, "reason": reason},
                        )

        if "entities" in fm:
            entities = fm["entities"]
            code = "frontmatter.entities_malformed"
            if not isinstance(entities, list):
                _governed_finding(
                    report, concept, code=code,
                    message="`entities` must be a list of strings or entity objects.",
                    detail={"key": "entities", "reason": "expected_list", "actual": type(entities).__name__},
                )
            else:
                for i, entity in enumerate(entities):
                    reason = None
                    if isinstance(entity, str):
                        if not entity.strip():
                            reason = "empty_string"
                    elif isinstance(entity, dict):
                        if _bad_string_field(entity, "label") or "label" not in entity:
                            reason = "missing_or_invalid_label"
                        elif any(_bad_string_field(entity, k) for k in ("id", "kind")):
                            reason = "id_and_kind_must_be_non_empty_strings"
                        elif "aliases" in entity and (
                            not isinstance(entity["aliases"], list)
                            or any(not isinstance(a, str) or not a.strip() for a in entity["aliases"])
                        ):
                            reason = "aliases_must_be_non_empty_string_list"
                    else:
                        reason = "expected_string_or_object"
                    if reason:
                        _governed_finding(
                            report, concept, code=code,
                            message=f"Malformed `entities` entry at index {i}: {reason}.",
                            detail={"key": "entities", "index": i, "reason": reason},
                        )

        for key, fields in (
            ("provenance", ("source", "note", "timestamp")),
            ("citations", ("id", "text", "url")),
        ):
            if key not in fm:
                continue
            value = fm[key]
            code = f"frontmatter.{key}_malformed"
            if not isinstance(value, list):
                _governed_finding(
                    report, concept, code=code,
                    message=f"`{key}` must be a list of objects.",
                    detail={"key": key, "reason": "expected_list", "actual": type(value).__name__},
                )
                continue
            for i, entry in enumerate(value):
                reason = None
                if not isinstance(entry, dict):
                    reason = "expected_object"
                elif not any(
                    isinstance(entry.get(k), str) and bool(entry.get(k, "").strip())
                    for k in fields
                ):
                    reason = "no_renderable_fields"
                elif any(_bad_string_field(entry, k) for k in fields):
                    reason = "fields_must_be_non_empty_strings"
                if reason:
                    _governed_finding(
                        report, concept, code=code,
                        message=f"Malformed `{key}` entry at index {i}: {reason}.",
                        detail={"key": key, "index": i, "reason": reason},
                    )

        if "relations" in fm:
            relations = fm["relations"]
            code = "frontmatter.relations_malformed"
            if not isinstance(relations, list):
                _governed_finding(
                    report, concept, code=code,
                    message="`relations` must be a list of typed relation objects.",
                    detail={"key": "relations", "reason": "expected_list", "actual": type(relations).__name__},
                )
                continue
            seen_relations: dict[tuple[str, str], int] = {}
            for i, relation in enumerate(relations):
                reason = None
                normalized_target = None
                if not isinstance(relation, dict):
                    reason = "expected_object"
                elif _bad_string_field(relation, "target") or "target" not in relation:
                    reason = "missing_or_invalid_target"
                elif _bad_string_field(relation, "type") or "type" not in relation:
                    reason = "missing_or_invalid_type"
                elif "detail" in relation and not isinstance(relation["detail"], str):
                    reason = "detail_must_be_string"
                else:
                    try:
                        normalized_target = concept_id_to_str(
                            concept_id_from_str(relation["target"])
                        )
                    except (ConceptIdError, ValueError):
                        reason = "invalid_target_concept_id"
                if reason:
                    _governed_finding(
                        report, concept, code=code,
                        message=f"Malformed `relations` entry at index {i}: {reason}.",
                        detail={"key": "relations", "index": i, "reason": reason},
                    )
                    continue
                logical_key = (relation["type"].strip(), normalized_target or "")
                first = seen_relations.get(logical_key)
                if first is not None:
                    _governed_finding(
                        report, concept, code="relation.duplicate",
                        message=(
                            f"Duplicate typed relation at index {i}; it repeats "
                            f"index {first} for ({logical_key[0]}, {logical_key[1]})."
                        ),
                        detail={
                            "key": "relations",
                            "index": i,
                            "first_index": first,
                            "type": logical_key[0],
                            "target": logical_key[1],
                        },
                    )
                else:
                    seen_relations[logical_key] = i


def _check_reserved_filenames(bundle: Bundle, report: ValidationReport) -> None:
    # Reserved filenames used for non-reserved purposes already caught at
    # load. Here we check the inverse: a concept path that collides with a
    # reserved name at any level.
    for rel, idx in bundle.indexes.items():
        # Non-root index files with frontmatter is already a load warning;
        # surface it again under the dedicated code for clarity.
        if not idx.is_root and idx.frontmatter:
            report.findings.append(
                Finding(
                    code="index.non_root_frontmatter",
                    severity=Severity.WARNING,
                    message=(
                        "Non-root index.md carries frontmatter; SPEC §6 reserves "
                        "frontmatter for the bundle-root index.md only."
                    ),
                    path=idx.path,
                )
            )


def _is_valid_directory_index_link(link: "Link", bundle: Bundle) -> bool:
    """Return True when ``link`` points at a directory ``index.md``
    that exists on disk under the bundle root.

    The parser produces a ConceptId for ``/foo/index.md`` (the path
    parses cleanly to ``("foo", "index")``), but the bundle model does
    not register reserved filenames as concepts — so the link lands in
    ``graph.unresolved`` and is flagged ``link.broken`` even though the
    viewer serves it correctly as a directory index page. This helper
    recognises the pattern and checks disk existence so the validator
    can skip the false positive.

    Handles:
      * absolute: ``/foo/index.md``, ``/index.md``
      * relative: ``./index.md``, ``index.md``, ``../sibling/index.md``
        (resolved against the source concept's directory)

    Anchor fragments (``index.md#section``) are stripped before the
    disk check.
    """
    raw = link.target_raw or ""
    # Strip a trailing anchor fragment.
    path_part = raw.split("#", 1)[0]
    # Fast path: only inspect paths that look like index references.
    if not path_part.endswith("index.md"):
        return False
    # Resolve against the appropriate base.
    if path_part.startswith("/"):
        # Absolute bundle-relative.
        candidate = bundle.root / path_part.lstrip("/")
    else:
        # Relative: resolve against the source concept's directory.
        source_concept = bundle.concepts.get(link.source)
        if source_concept is None or source_concept.path is None:
            return False
        try:
            candidate = (source_concept.path.parent / path_part).resolve()
        except (ValueError, OSError):
            return False
        # Must stay inside the bundle root (out-of-bundle index.md is
        # handled by the parser's external_out_of_bundle path, not here).
        try:
            candidate.relative_to(bundle.root.resolve())
        except ValueError:
            return False
    return candidate.is_file()


def _check_link_integrity(bundle: Bundle, report: ValidationReport) -> None:
    """SPEC §5.3: broken links are tolerated; flag as warnings.

    Two categories of "broken" links are silently accepted
    rather than flagged, because they are valid references that the
    bundle-scoped resolver cannot classify:

    1. **Directory-index references** (``/foo/index.md`` or
       ``./index.md``): ``index.md`` is a SPEC §3.1 reserved filename
       that exists on disk as a directory listing but is not registered
       as a concept (only non-reserved ``.md`` files are). Links to a
       directory's ``index.md`` are valid in the viewer; the validator
       now skips the broken-link warning when the file exists on disk.

    2. **Out-of-bundle references** (``../../docs/foo.md``): paths that
       escape the bundle root are now classified as
       ``external_out_of_bundle`` by the parser when the target file
       exists in the workspace, so they never enter ``unresolved``. The
       remaining broken-link findings here are genuinely missing files.
    """
    graph = bundle.graph()
    seen: set[tuple[ConceptId, str, int]] = set()
    for link in graph.unresolved:
        key = (link.source, link.target_raw, link.line)
        if key in seen:
            continue
        seen.add(key)
        # Directory index links that exist on disk
        # are valid (the viewer serves them as directory indexes). The
        # bundle-scoped concept resolver skipped them because index.md
        # is reserved; resolve them against the filesystem here.
        if _is_valid_directory_index_link(link, bundle):
            continue
        # Build a clearer message for out-of-bundle broken links.
        raw = link.target_raw
        is_out_of_bundle = raw.startswith("../") or "/.." in raw or raw == ".."
        if is_out_of_bundle:
            message = (
                f"Out-of-bundle link target does not exist in workspace: "
                f"{raw!r}"
            )
        else:
            message = (
                f"Internal link target does not exist in bundle: {raw!r}"
            )
        report.findings.append(
            Finding(
                code="link.broken",
                severity=Severity.WARNING,
                message=message,
                path=bundle.concepts[link.source].path if link.source in bundle.concepts else None,
                line=link.line,
                concept_id=link.source,
                detail={
                    "target_raw": raw,
                    "form": link.form,
                    "label": link.label,
                    "out_of_bundle": is_out_of_bundle,
                },
            )
        )

    # Detect absolute-vs-relative inconsistency: a concept referenced by both
    # forms is legal but worth flagging for curation consistency.
    by_target: dict[ConceptId, set[str]] = {}
    for link in graph.edges:
        if link.target and link.target in graph.concept_ids:
            by_target.setdefault(link.target, set()).add(link.form)
    for target, forms in by_target.items():
        if len(forms) > 1:
            report.findings.append(
                Finding(
                    code="link.form_inconsistency",
                    severity=Severity.INFO,
                    message=(
                        f"Concept {concept_id_to_str(target)!r} is referenced via "
                        f"multiple link forms: {sorted(forms)}. SPEC §5.1 "
                        "recommends the absolute form for stability."
                    ),
                    concept_id=target,
                    detail={"forms": sorted(forms)},
                )
            )

    # SPEC §10: wikilinks are a supported migration convenience, not a
    # problem -- so this is an INFO nudge to convert them to standard
    # markdown links for plain-OKF-consumer compatibility, never a warning.
    # One finding per wikilink occurrence (deduped by source/target/line).
    wl_seen: set[tuple[ConceptId, str, int]] = set()
    for link in graph.edges + graph.unresolved:
        if link.form != "wikilink":
            continue
        key = (link.source, link.target_raw, link.line)
        if key in wl_seen:
            continue
        wl_seen.add(key)
        report.findings.append(
            Finding(
                code="link.wikilink_present",
                severity=Severity.INFO,
                message=(
                    f"Wikilink to {link.target_raw!r}; consider converting to a "
                    "standard markdown link for plain-OKF-consumer compatibility."
                ),
                path=(
                    bundle.concepts[link.source].path
                    if link.source in bundle.concepts
                    else None
                ),
                line=link.line,
                concept_id=link.source,
                detail={
                    "target_raw": link.target_raw,
                    "label": link.label,
                },
            )
        )


def _check_asset_integrity(bundle: Bundle, report: ValidationReport) -> None:
    """Image targets must exist for the viewer to display them.

    Images are deliberately not graph edges (spec: only ``[label](x.md)``
    links are), so a typo'd screenshot path was previously invisible to
    every check while rendering as a broken image in the studio and in
    static builds. Two findings:

    * ``asset.missing`` (WARNING, promoted by ``--fail-on-broken-links``):
      the target file does not exist — neither inside the bundle nor, for
      relative paths that escape it, anywhere in the workspace.
    * ``asset.out_of_bundle`` (INFO): the target file exists but lives
      outside the bundle root. Editors and GitHub render it, but the live
      studio and static builds only serve files under the bundle root, so
      it shows as broken there.

    External URLs (any scheme, including the CSP-blocked remote images and
    the sanitizer-blocked ``data:`` URIs) are skipped: they are not file
    references. Targets are percent-decoded before the existence check
    (``foo%20bar.png`` names ``foo bar.png``).
    """
    from urllib.parse import unquote
    from .parse import extract_image_targets

    for cid, concept in bundle.concepts.items():
        source_dir = concept.path.parent if concept.path else bundle.root
        seen: set[tuple[str, int]] = set()
        for target_raw, line in extract_image_targets(concept.body):
            key = (target_raw, line)
            if key in seen:
                continue
            seen.add(key)
            target = unquote(target_raw.split("#", 1)[0])
            if not target or target.startswith("//"):
                continue
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target):
                continue  # external URL / data: — not a file reference
            if target.startswith("/"):
                candidate = bundle.root / target.lstrip("/")
                out_of_bundle = False
            else:
                candidate = source_dir / target
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(bundle.root.resolve())
                    out_of_bundle = False
                except (ValueError, OSError, RuntimeError):
                    out_of_bundle = True
            try:
                exists = candidate.is_file()
            except OSError:
                exists = False
            if exists and not out_of_bundle:
                continue
            if exists and out_of_bundle:
                report.findings.append(
                    Finding(
                        code="asset.out_of_bundle",
                        severity=Severity.INFO,
                        message=(
                            f"Image target {target_raw!r} exists but lives outside "
                            "the bundle root; the live studio and static builds "
                            "only serve bundle-local files, so it will render "
                            "broken there."
                        ),
                        path=concept.path,
                        line=line,
                        concept_id=cid,
                        detail={"target_raw": target_raw},
                    )
                )
                continue
            report.findings.append(
                Finding(
                    code="asset.missing",
                    severity=Severity.WARNING,
                    message=f"Image target does not exist: {target_raw!r}",
                    path=concept.path,
                    line=line,
                    concept_id=cid,
                    detail={
                        "target_raw": target_raw,
                        "out_of_bundle": out_of_bundle,
                    },
                )
            )


def _check_citations_collision(bundle: Bundle, report: ValidationReport) -> None:
    """SPEC §7.4: ``citations:`` is a governed frontmatter key; a body
    ``# Citations`` heading is the compatibility OKF convention. When a concept
    carries BOTH, the viewer would render duplicate lists (the renderer
    suppresses the body heading when frontmatter is present). Surface an
    INFO so the author can pick one representation."""
    for cid, concept in bundle.concepts.items():
        fm_citations = concept.frontmatter.get("citations")
        if not (isinstance(fm_citations, list) and fm_citations):
            continue
        has_body_section = any(
            h.text.strip().lower() == "citations" for h in concept.headings
        )
        if has_body_section:
            report.findings.append(
                Finding(
                    code="concept.citations_both_forms",
                    severity=Severity.INFO,
                    message=(
                        "Concept has both a frontmatter `citations:` key and a "
                        "body `# Citations` heading. The viewer renders the "
                        "frontmatter form and suppresses the body heading; "
                        "consider removing the body section for clarity."
                    ),
                    path=concept.path,
                    concept_id=cid,
                )
            )


def _check_index_structure(bundle: Bundle, report: ValidationReport) -> None:
    # SPEC §6: index.md lists directory contents for progressive disclosure.
    # We surface two soft issues:
    #   (a) directories with concepts but no index.md at all (loss of PD)
    #   (b) stale generated index.md that's missing entries
    dir_has_index: set[Path] = {idx.rel_path.parent for idx in bundle.indexes.values()}
    concept_dirs: set[Path] = set()
    for c in bundle.concepts.values():
        concept_dirs.add(c.rel_path.parent)
    for d in sorted(concept_dirs):
        if d not in dir_has_index:
            report.findings.append(
                Finding(
                    code="index.missing_for_directory",
                    severity=Severity.WARNING,
                    message=(
                        f"Directory {d}/ contains concepts but has no index.md "
                        "for progressive disclosure (SPEC §6)."
                    ),
                    path=bundle.root / d,
                )
            )


def _check_log_structure(bundle: Bundle, report: ValidationReport) -> None:
    # SPEC §7: log.md uses ISO 8601 YYYY-MM-DD headings, newest first.
    for rel, log in bundle.logs.items():
        prev: str | None = None
        prev_was_iso = False
        for entry in log.entries:
            is_iso = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.date))
            if not is_iso:
                report.findings.append(
                    Finding(
                        code="log.bad_date_heading",
                        severity=Severity.WARNING,
                        message=(
                            f"Log heading is not ISO 8601 YYYY-MM-DD: "
                            f"{entry.raw_heading!r}"
                        ),
                        path=log.path,
                    )
                )
            # P1-6 (iter-4): only check newest-first ordering between ISO
            # dates. Non-ISO heading text vs ISO date is a meaningless string
            # compare that fires spuriously (e.g. "Notes" > "2024-01-01").
            if is_iso and prev_was_iso and entry.date > prev:
                report.findings.append(
                    Finding(
                        code="log.not_newest_first",
                        severity=Severity.WARNING,
                        message=(
                            f"Log entries are not newest-first: {entry.date} "
                            f"appears after {prev}."
                        ),
                        path=log.path,
                    )
                )
            if is_iso:
                prev = entry.date
                prev_was_iso = True
            # non-ISO entries don't update prev (meaningless for ordering)


def _check_tag_normalization(bundle: Bundle, report: ValidationReport) -> None:
    # Surface tags that differ only by case/whitespace (curator hint).
    norm: dict[str, str] = {}
    for c in bundle.concepts.values():
        for t in c.tags:
            key = t.strip().lower()
            if not key:
                continue
            if key in norm and norm[key] != t:
                report.findings.append(
                    Finding(
                        code="tag.case_inconsistency",
                        severity=Severity.INFO,
                        message=(
                            f"Tags differ only by case/spacing: {norm[key]!r} "
                            f"vs {t!r}"
                        ),
                        path=c.path,
                        concept_id=c.id,
                    )
                )
            else:
                norm.setdefault(key, t)


def _check_capability_declarations(
    bundle: Bundle, report: ValidationReport
) -> None:
    # We just record what the bundle declares; resolution happens in extensions.
    for ext in bundle.okf_extensions:
        report.findings.append(
            Finding(
                code="capability.declared",
                severity=Severity.INFO,
                message=f"Bundle declares capability: {ext}",
            )
        )


_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def _check_timestamp_validity(bundle: Bundle, report: ValidationReport) -> None:
    for c in bundle.concepts.values():
        ts = c.timestamp
        if ts is None:
            continue
        if not _ISO8601_RE.match(str(ts)):
            report.findings.append(
                Finding(
                    code="concept.bad_timestamp",
                    severity=Severity.WARNING,
                    message=(
                        f"timestamp is not ISO 8601: {ts!r}"
                    ),
                    path=c.path,
                    concept_id=c.id,
                )
            )
