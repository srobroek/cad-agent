#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Evidence of a record version 2 design: what each expected check, requirement and part has shown,
at which evidence level, against which configuration; the stage gates and the release service policy
built on it; waivers; and the acceptance digest a freeze and a release anchor.

    uv run --script evidence.py check  --hw HW --version vN [--stage STAGE] [--out .cad-work/evidence.json]
    uv run --script evidence.py digest --hw HW --version vN

Evidence levels are independent facets, never an ordinal badge: CAD-valid, geometrically
manufacturable, process planned, simulation-supported and physically validated can each differ per
requirement and per part. Every obligation `manufacturing_intent.required_checks` enumerates gets one
status (RESULTS) from three sources only: the record itself (intent.* obligations of unknown inputs, and
assembly.joint, which assembly_operations.check_joints judges from the declared joint facts on every
read), the required-check packets producers sealed for the version (hw/checks/<V>/<S>/result.json,
schemas/manufacturing-check-result.schema.json; the one place every producer publishes them, an FEA case
and a stack-up beside their analysis bundle) and the accepted ledger hw/manufacturing/evidence.yaml. An
obligation no producer checks (physical tests, interface fit and acceptance, contact, function, the
creep, fatigue and impact checks) stays unknown until accepted ledger evidence shows it; it is never
passed by default. Unknown, planned and untested never become pass; stale evidence (another version
without an approving impact decision, or a changed dependency fingerprint) counts as nothing; a waiver
stays waived, needs an approved decision, its exact subject and an unexpired scope, and never overrides a
governing safety or regulatory requirement or a geometry-integrity failure. A software producer never
shows physical validation, and physical evidence only covers its own subject (a coupon covers the coupon).

From INTENT on, every stage gate also reads selection.py's report of the same record (the one
definition of the selection assessment and its input digest): a recorded final choice the assessment
does not support, a recorded recommendation or part route a mandatory constraint eliminates, a stale
or unapproved confirmed selection block at every stage; an open (conditional) selection, or none
recorded, is exploration until the design freeze, where the confirmed final choice is required: a
freeze or release with no material and process selection recorded blocks like any other unknown.

`check` prints the aggregate evidence packet cad-recheck stamps: exit 0 when the stage gate passes,
1 when it blocks, 2 on a usage or infrastructure error. `digest` prints the acceptance inventory and
its evidence_sha256, which a record version 2 freeze records in product.yaml flow.frozen and a
release seals: exit 0, or 2 when the inventory cannot be formed.

The module itself is standard library only; load it by path as `cad_record_evidence` (dataclasses
look it up in sys.modules). The CLI reads the record with PyYAML.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if __name__ == "__main__":  # siblings that load this module by name get this instance, not a second copy
    sys.modules.setdefault("cad_record_evidence", sys.modules[__name__])


def _load_module(name: str, path: Path) -> Any:
    """A sibling module loaded once by path and registered in sys.modules, where dataclasses look it up."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path.name} not found at {path}")
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


mi = _load_module("cad_record_manufacturing_intent", SCRIPTS / "manufacturing_intent.py")
impact = _load_module("cad_record_impact", SCRIPTS / "impact.py")
layout = _load_module("cad_record_layout", SCRIPTS / "layout.py")
selection = _load_module("cad_record_selection", SCRIPTS / "selection.py")
operations = _load_module("cad_record_assembly_operations", SCRIPTS / "assembly_operations.py")

STAGES = mi.STAGES
LEVELS = mi.EVIDENCE_LEVELS
PHYSICAL = "physically_validated"
# An obligation's status. Resolved: pass, waived (an approved, scoped waiver; never a pass) and not_applicable.
RESULTS = ("pass", "fail", "waived", "not_applicable", "stale", "planned", "unknown")
RESOLVED = frozenset({"pass", "waived", "not_applicable"})
_WORST_FIRST = ("fail", "stale", "unknown", "planned", "waived", "pass", "not_applicable")
PACKET_KIND = "cad-evidence-packet"
PACKET_SCHEMA_VERSION = 1
INVENTORY_SCHEMA = "cad-acceptance-inventory/1"
LEDGER_FILES = ("verification.csv", "manufacturing/evidence.yaml", "manufacturing/findings.yaml")
SOURCES_DIR = "manufacturing/sources"

CODES: Mapping[str, str] = {
    "evidence.unqualified": "pass or fail evidence that records no fingerprint of a dependency its level relies on",
    "evidence.stale": "evidence of another configuration, part revision or dependency fingerprint",
    "evidence.subject": "evidence about another subject than the obligation it names",
    "evidence.carry_forward": "evidence sealed for another version counted without an approving impact decision",
    "evidence.altered": "evidence whose cited artifact or bundle manifest no longer holds the bytes it names",
    "evidence.csv_unsupported": "verification.csv states pass or waived without current accepted evidence",
    "evidence.physical_claim": "a producer packet claims a physically validated pass",
    "packet.invalid": "a producer check packet that fails its seal or contradicts itself or its version",
    "packet.unexpected": "a producer outcome for an obligation the record does not expect, or at another level",
    "waiver.unapproved": "a waiver whose decision is missing or not the user's approval",
    "waiver.expired": "a waiver past its expiry date",
    "waiver.scope": "a waiver of another subject or obligation than the evidence it is attached to",
    "waiver.forbidden": "a waiver of a governing safety or regulatory requirement or of a geometry-integrity check",
    "finding.critical": "a critical producer finding that no resolution or valid waiver disposes of",
    "gate.record": "the record must be migrated before the stage advances",
    "gate.fail": "a due obligation failed",
    "gate.stale": "a due obligation rests on stale evidence",
    "gate.unresolved": "a due obligation is unknown, untested or only planned",
    "gate.use_case": "the use case does not support the stage: unconfirmed, unknown service class or open safety questions",
    "gate.selection": "the manufacturing selection does not support the stage (selection.py): a recorded final choice "
                      "the assessment does not support, an eliminated recorded route or recommendation, a stale "
                      "confirmed assessment, or an open or unrecorded choice at a freeze",
    "gate.selection_approval": "a confirmed selection whose assessment decision is not the user's approval",
    "release.service_class": "the release scope or service class is unknown",
    "release.scope": "the release purpose, operating limits, exclusions or evidence policy is not recorded",
    "release.trial_not_permitted": "a waived physical obligation (an unvalidated trial) in a service class that forbids trials",
    "release.waiver_forbidden": "a waiver in a safety-critical release",
}

# Fingerprint domains pass or fail evidence must record, by subject kind and level: evidence is tied to
# the configuration (configuration.version, manifest), and through these to material, process and criteria.
_REQUIRED_DOMAINS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "made": {"cad_valid": frozenset({"geometry"}),
             "geometrically_manufacturable": frozenset({"geometry", "route"}),
             "process_planned": frozenset({"route", "capability", "material"}),
             "simulation_supported": frozenset({"geometry", "material", "load", "criterion"}),
             PHYSICAL: frozenset({"geometry", "material", "route", "criterion"})},
    "bought": {"cad_valid": frozenset({"geometry"}),
               "geometrically_manufacturable": frozenset({"geometry", "route"}),
               "process_planned": frozenset({"route"}),
               "simulation_supported": frozenset({"route", "load", "criterion"}),
               PHYSICAL: frozenset({"route", "criterion"})},
    "interface": {"cad_valid": frozenset({"geometry", "interface"}),
                  "geometrically_manufacturable": frozenset({"geometry", "interface"}),
                  "process_planned": frozenset({"geometry", "interface", "criterion"}),
                  "simulation_supported": frozenset({"geometry", "interface", "material", "criterion"}),
                  PHYSICAL: frozenset({"geometry", "interface", "material", "criterion"})},
    "operation": {level: frozenset({"geometry", "interface"}) for level in mi.EVIDENCE_LEVELS},
    "product": {"cad_valid": frozenset({"geometry", "criterion"}),
                "geometrically_manufacturable": frozenset({"geometry", "criterion"}),
                "process_planned": frozenset({"criterion", "use_case"}),
                "simulation_supported": frozenset({"geometry", "criterion", "use_case"}),
                PHYSICAL: frozenset({"geometry", "criterion", "use_case"})},
}

# The minimum release policy per service class: which obligations a release must resolve (families by
# prefix, with the levels they count at), which evidence levels it may claim, and whether an
# unvalidated trial (a waived physical obligation) is permitted. Approved requirements and the
# evidence policy (requirement.*, policy.*) are always in scope: a project may demand more.
_LOWER = frozenset({"cad_valid", "geometrically_manufacturable", "process_planned"})
_MAKE = ("intent.", "cad.", "route.", "process.", "purchase.", "optical.route")
_FIT = _MAKE + ("interface.", "feature.", "assembly.", "motion.play")
_FUNCTION = _FIT + ("engineering.", "function.", "contact.", "motion.", "optical.")


@dataclass(frozen=True)
class ServicePolicy:
    families: tuple[str, ...] | None  # None: every family
    levels: frozenset[str]  # the levels at which `families` are in scope; the lower ones only for appearance
    claims: frozenset[str]
    trial: bool


SERVICE_POLICY: Mapping[str, ServicePolicy] = {
    "appearance_concept": ServicePolicy(_MAKE, _LOWER, _LOWER, False),
    "fit_assembly": ServicePolicy(_FIT, frozenset(LEVELS), _LOWER | {PHYSICAL}, True),
    "functional_demo": ServicePolicy(_FUNCTION, frozenset(LEVELS), frozenset(LEVELS), True),
    "working_prototype": ServicePolicy(None, frozenset(LEVELS), frozenset(LEVELS), True),
    "production": ServicePolicy(None, frozenset(LEVELS), frozenset(LEVELS), False),
    "safety_critical": ServicePolicy(None, frozenset(LEVELS), frozenset(LEVELS), False),
}


class EvidenceError(ValueError):
    """An acceptance inventory that cannot be formed: a missing, linked, escaping or unnormalized path."""


@dataclass(frozen=True)
class Finding:
    """One evidence or gate finding; `path` is project-relative (hw/...), `obligation` the check it is about."""

    code: str
    path: str
    message: str
    severity: str = "error"  # error, warning or info
    obligation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        item = {"path": self.path, "message": self.message, "severity": self.severity, "code": self.code,
                "canonical_location": None, "action": None}
        if self.obligation:
            item["obligation"] = self.obligation
        return item

    def graded(self, severity: str) -> Finding:
        return Finding(self.code, self.path, self.message, severity, self.obligation)


@dataclass(frozen=True)
class ProducerPacket:
    """A producer's required-check packet: its bundle (hw/checks/<V>/<S>), that bundle's manifest sha256
    (what a finding disposition cites) and the parsed result.json."""

    bundle: str
    manifest_sha256: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class Status:
    """What one expected obligation has shown."""

    obligation: Any  # manufacturing_intent.Obligation
    result: str  # one of RESULTS
    sources: tuple[str, ...] = ()  # the packet bundles and evidence ids that decided it
    reasons: tuple[str, ...] = ()
    waiver: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {"result": self.result, "level": self.obligation.level, "stage": self.obligation.stage,
                                "sources": list(self.sources), "reasons": list(self.reasons)}
        if self.waiver is not None:
            item["waiver"] = impact.plain(self.waiver)
        return item


@dataclass(frozen=True)
class ReleaseAssessment:
    """The service-class release policy applied to an assessment: the acceptance block of a release
    manifest version 3 (scope, claims, waivers, open obligations) and every reason that blocks it."""

    service_class: str | None
    scope: Mapping[str, Any] | None
    claims: Mapping[str, tuple[str, ...]]
    waivers: tuple[Mapping[str, Any], ...]
    open_obligations: tuple[str, ...]
    trial: bool
    findings: tuple[Finding, ...]

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")


# --- small helpers -----------------------------------------------------------------------------------------------

def _never(_decision: str | None) -> bool:
    return False


def _worst(results: Iterable[str]) -> str | None:
    found = set(results)
    return next((r for r in _WORST_FIRST if r in found), None)


def _best(results: Sequence[str], waived: bool) -> str:
    """The status of one obligation from all its evidence: a current fail stands unless a valid waiver
    covers it (waived, never pass); a current pass outranks waivers, stale, planned and unknown evidence."""
    found = set(results)
    if "fail" in found:
        return "waived" if waived else "fail"
    for result in ("pass", "waived", "not_applicable", "stale", "planned"):
        if result in found or (result == "waived" and waived):
            return result
    return "unknown"


def subject_path(subject: Mapping[str, Any]) -> str:
    """The record document a subject lives in."""
    if subject.get("PN"):
        return f"hw/parts/{subject['PN']}/part.yaml"
    if subject.get("interface"):
        return "hw/interfaces.yaml"
    if subject.get("assembly_operation"):
        return "hw/assembly.yaml"
    if subject.get("requirements"):
        return "hw/requirements.yaml"
    return "hw/product.yaml"


def _obligation_path(obligation: Mapping[str, Any]) -> str:
    return subject_path(obligation.get("subject") or {})


def _anchor(obligation_id: str) -> str:
    return obligation_id.split("@", 1)[1] if "@" in obligation_id else ""


def _canonical_sha(value: Any) -> str:
    return hashlib.sha256(layout.canonical_json(impact.plain(value))).hexdigest()


def expected_digest(expected: Iterable[Any]) -> str:
    """sha256 of the expected obligations in their schema form, as canonical JSON."""
    return _canonical_sha([o.as_dict() if hasattr(o, "as_dict") else o for o in expected])


def _service_class(record: Any) -> str | None:
    service = record.use_case.service_class
    return service.value if service.known and service.value != "unknown" else None


def _subject_kind(record: Any, subject: Mapping[str, Any]) -> str:
    if subject.get("PN"):
        part = record.parts.get(subject["PN"])
        return "bought" if part is not None and part.sourcing == "catalogue" else "made"
    if subject.get("interface"):
        return "interface"
    if subject.get("assembly_operation"):
        return "operation"
    return "product"


_OWNER = None


def _owning_version(rel: str) -> str | None:
    """The version a project-relative path is sealed under (hw/versions/<V>/..., hw/<bundle kind>/<V>/...)."""
    global _OWNER
    if _OWNER is None:
        kinds = "|".join(re.escape(kind) for kind in ("versions", *layout.bundle_kinds()))
        _OWNER = re.compile(rf"^hw/(?:{kinds})/(v[1-9][0-9]*)(?:/|$)")
    match = _OWNER.match(rel)
    return match.group(1) if match else None


def _requirements_of(record: Any, obligation: Any | None, extra: Iterable[str] = ()) -> list[Any]:
    ids = set(extra)
    if obligation is not None:
        if obligation.requirement:
            ids.add(obligation.requirement)
        ids.update((obligation.subject or {}).get("requirements") or ())
    return [record.requirements[i] for i in sorted(ids) if i in record.requirements]


# --- waivers -----------------------------------------------------------------------------------------------------

def waiver_problem(waiver: Mapping[str, Any], *, record: Any, subject: Mapping[str, Any] | None,
                   obligation: Any | None = None, requirements: Iterable[str] = (),
                   approved: Callable[[str | None], bool] = _never, today: date | None = None,
                   critical: bool = False) -> tuple[str, str] | None:
    """Why `waiver` cannot stand, as (code, message), or None. A waiver needs an approved decision, the
    exact subject of the evidence (and obligation) it waives and an unexpired scope. It is forbidden for
    a requirement whose waiver_policy is forbidden, a requirement whose risk is critical, a mandatory
    requirement of a safety-critical use case, a geometry-integrity check (cad.*) and, in a
    safety-critical use case, a critical finding."""
    today = today or date.today()
    decision = waiver.get("decision")
    if not approved(decision):
        return "waiver.unapproved", f"waiver decision {decision} is missing or is not the user's approval"
    expires = waiver.get("expires")
    if isinstance(expires, str):
        try:
            if date.fromisoformat(expires) < today:
                return "waiver.expired", f"the waiver approved by {decision} expired on {expires}"
        except ValueError:
            return "waiver.expired", f"the waiver approved by {decision} has an unreadable expiry {expires!r}"
    if subject is not None and impact.plain(waiver.get("subject")) != impact.plain(subject):
        return "waiver.scope", (f"the waiver approved by {decision} names {impact.plain(waiver.get('subject'))}, "
                                f"not {impact.plain(subject)}")
    if obligation is not None and waiver.get("obligation") not in (None, obligation.id):
        return "waiver.scope", f"the waiver approved by {decision} waives {waiver['obligation']}, not {obligation.id}"
    service = _service_class(record)
    if obligation is not None and obligation.family.startswith("cad."):
        return "waiver.forbidden", f"{obligation.id} is a geometry-integrity check; no waiver overrides it"
    if critical and service == "safety_critical":
        return "waiver.forbidden", "a critical finding of a safety-critical use case cannot be waived"
    for requirement in _requirements_of(record, obligation, requirements):
        if requirement.waiver_policy == "forbidden":
            return "waiver.forbidden", f"{requirement.id} forbids waivers (waiver_policy: forbidden)"
        if (requirement.risk or {}).get("severity") == "critical":
            return "waiver.forbidden", f"{requirement.id} carries critical risk; no waiver overrides it"
        if service == "safety_critical" and requirement.mandatory:
            return "waiver.forbidden", f"{requirement.id} is a governing requirement of a safety-critical use case"
    return None


# --- the assessment ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Assessment:
    """Every expected obligation's status for one version, the facets per requirement, part, interface
    and operation, the typed verification rows, the findings about the evidence itself, and the
    selection report (selection.assess) of the same record with whether its confirming decision is the
    user's approval (None when the selection is not confirmed)."""

    record: Any
    version: str | None
    manifest_sha256: str | None
    expected: tuple[Any, ...]
    statuses: Mapping[str, Status]
    facets: Mapping[str, Mapping[str, str]]
    verification: tuple[Mapping[str, Any], ...]
    findings: tuple[Finding, ...]
    critical: tuple[Mapping[str, Any], ...]
    complete: bool
    selection: Mapping[str, Any]
    selection_approved: bool | None

    def results(self) -> dict[str, str]:
        return {ident: status.result for ident, status in self.statuses.items()}

    def gate(self, stage: str) -> tuple[Finding, ...]:
        """The findings of the stage gate at `stage`, graded error, warning or info (progressive rigor):
        before layout explicit unknowns and assumptions stay warnings and only failures block; from
        layout every obligation due by the stage must be resolved, a physical one at least planned; a
        design freeze needs every obligation but physical tests resolved and a use case fit for the
        choice; a release applies the service-class policy. Unresolved critical findings block a freeze
        and a release. From INTENT on, `selection_gate` applies the selection report."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        if self.record.version != mi.RECORD_VERSION:
            problem = mi.migration_problem(self.record.product)
            message = problem.message if problem is not None else "the record is not record version 2"
            action = f"; {problem.action}" if problem is not None and problem.action else ""
            return (Finding("gate.record", "hw/product.yaml", message + action),)
        explore = STAGES.index(stage) < STAGES.index("layout")
        if stage == "release":
            findings = list(self.release().findings)
        else:
            findings = obligation_gate([o.as_dict() for o in self.expected], self.results(), stage,
                                       _service_class(self.record))
        for item in self.findings:
            if item.code in ("waiver.forbidden", "evidence.physical_claim"):
                findings.append(item)
            elif item.code == "evidence.csv_unsupported":
                findings.append(item.graded("error" if stage in ("design_freeze", "release") else "warning"))
            elif item.severity == "error":
                findings.append(item.graded("warning" if explore else "error"))
            else:
                findings.append(item)
        if stage in ("design_freeze", "release"):
            use_case = mi.assess_use_case(self.record)
            if not use_case.final_ready:
                findings.append(Finding("gate.use_case", "hw/product.yaml",
                                        "the use case does not support a final choice: " + "; ".join(use_case.blocking)))
            for critical in self.critical:
                findings.append(Finding("finding.critical", critical["bundle"],
                                        f"critical finding {critical['finding']} ({critical.get('code')}) is unresolved: "
                                        f"{critical.get('consequence') or 'no consequence recorded'}",
                                        obligation=critical.get("obligation")))
        findings.extend(selection_gate(self.selection, stage, approved=self.selection_approved))
        return tuple(findings)

    def release(self) -> ReleaseAssessment:
        """The service-class release policy (SERVICE_POLICY): obligations in scope must be resolved, a
        waived physical one makes the release an unvalidated trial (refused for production and safety-
        critical service), safety-critical releases take no waiver at all; out-of-scope obligations stay
        open and unclaimed. Claims list per requirement the levels whose evidence passes and the class
        may claim."""
        record, findings = self.record, []
        service = _service_class(record)
        scope = None
        if service is None:
            findings.append(Finding("release.service_class", "hw/product.yaml",
                                    "the use case's service class is unknown: the release is blocked until the scope "
                                    "and evidence policy are confirmed"))
        else:
            scope, problems = _release_scope(record, service)
            findings.extend(Finding("release.scope", "hw/product.yaml", problem) for problem in problems)
        policy = SERVICE_POLICY.get(service or "", None)
        results = self.results()
        trial, waivers, still_open = False, [], []
        for obligation in self.expected:
            status = self.statuses[obligation.id]
            in_scope = policy is not None and in_release_scope(obligation.as_dict(), service)
            if status.result != "pass" and status.result != "not_applicable":
                still_open.append(obligation.id)
            if status.result == "waived" and in_scope:
                waivers.append(impact.plain(status.waiver) if status.waiver is not None else {})
                if obligation.level == PHYSICAL:
                    trial = True
        findings.extend(obligation_gate([o.as_dict() for o in self.expected], results, "release", service))
        claims = {}
        allowed = policy.claims if policy is not None else frozenset()
        for req_id in sorted(record.requirements):
            levels = tuple(level for level in LEVELS if self.facets.get(req_id, {}).get(level) == "pass" and level in allowed)
            claims[req_id] = levels
        if scope is not None:
            scope = {**scope, "trial": trial}
        return ReleaseAssessment(service, scope, claims, tuple(waivers), tuple(still_open), trial, tuple(findings))

    def packet(self, stage: str, record_sha256: str) -> dict[str, Any]:
        """The aggregate evidence packet (`evidence.py check`, stamped by cad-recheck)."""
        gate = self.gate(stage)
        return {
            "kind": PACKET_KIND, "schema_version": PACKET_SCHEMA_VERSION, "record_version": self.record.version,
            "version": self.version, "version_manifest_sha256": self.manifest_sha256, "record_sha256": record_sha256,
            "expected_sha256": expected_digest(self.expected), "stage": stage,
            "expected": [o.as_dict() for o in self.expected],
            "statuses": {ident: status.as_dict() for ident, status in self.statuses.items()},
            "facets": {key: dict(levels) for key, levels in self.facets.items()},
            "verification": [dict(row) for row in self.verification],
            "findings": [f.as_dict() for f in self.findings], "critical": [dict(item) for item in self.critical],
            "complete": self.complete,
            "selection": selection_summary(self.selection, self.selection_approved),
            "gate": {"stage": stage, "passed": not any(f.severity == "error" for f in gate),
                     "findings": [f.as_dict() for f in gate]},
        }


def in_release_scope(obligation: Mapping[str, Any], service_class: str | None) -> bool:
    """Whether a release of `service_class` must resolve `obligation` (schema form)."""
    family, level = obligation["family"], obligation["level"]
    if family.startswith(("requirement.", "policy.")):
        return True
    policy = SERVICE_POLICY.get(service_class or "")
    if policy is None:
        return True
    if policy.families is None:
        return True
    return family.startswith(policy.families) and level in policy.levels


def obligation_gate(expected: Sequence[Mapping[str, Any]], results: Mapping[str, str], stage: str,
                    service_class: str | None = None) -> list[Finding]:
    """The per-obligation part of the stage gate at `stage`, from obligations in schema form and their
    results: what cad-recheck recomputes from a stamped packet. Due means due by the stage; from a design
    freeze everything is due, a physical test planned at least; a known failure blocks whenever it is
    known, due or not; a release applies `in_release_scope`."""
    index = STAGES.index(stage)
    explore = index < STAGES.index("layout")
    findings = []
    for obligation in expected:
        ident = obligation["id"]
        result = results.get(ident, "unknown")
        path = _obligation_path(obligation)
        if stage == "release":
            if not in_release_scope(obligation, service_class):
                continue
            if result == "waived":
                if service_class == "safety_critical":
                    findings.append(Finding("release.waiver_forbidden", path,
                                            f"{ident} is waived; a safety-critical release takes no waiver", obligation=ident))
                elif obligation["level"] == PHYSICAL and not SERVICE_POLICY.get(service_class or "", SERVICE_POLICY["production"]).trial:
                    findings.append(Finding("release.trial_not_permitted", path,
                                            f"{ident} is waived untested: a {service_class} release is never an "
                                            "unvalidated trial", obligation=ident))
                continue
            if result in RESOLVED:
                continue
        else:
            due = stage == "design_freeze" or STAGES.index(obligation["stage"]) <= index or result == "fail"
            if not due or result in RESOLVED:
                continue
            if result == "planned" and obligation["level"] == PHYSICAL:
                continue  # a physical test may stay planned until the release
        code = {"fail": "gate.fail", "stale": "gate.stale"}.get(result, "gate.unresolved")
        severity = "error" if (not explore or result == "fail") else "warning"
        findings.append(Finding(code, path, f"{ident} is {result} at stage {stage} ({obligation.get('basis', '')})",
                                severity, ident))
    return findings


SELECTION_PATH = "hw/manufacturing/selection.yaml"
# selection.py findings that block at every stage from INTENT: the recorded selection contradicts the
# assessment, or a candidate holding the part route the record states fails a mandatory constraint.
_SELECTION_BLOCKING = frozenset({"selection.final_unsupported", "selection.recorded_recommendation_eliminated",
                                 "selection.recorded_route_eliminated"})


def _selection_message(item: Mapping[str, Any]) -> str:
    subject = impact.subject_key(item.get("subject") or {})
    return f"{item.get('code')} ({subject}): {item.get('consequence')}; {item.get('remedy')}"


def selection_gate(report: Mapping[str, Any], stage: str, *, approved: bool | None = None) -> list[Finding]:
    """The selection part of the stage gate at `stage`, from selection.py's report (selection.assess) of
    the same record; nothing before INTENT. From INTENT on these block at every stage: a recorded final
    choice the assessment does not support, a recorded recommendation or record route a mandatory
    constraint eliminates, a confirmed selection whose input digest is stale or was not checked, and
    (`approved` False; None: not judged here) a confirming decision that is not the user's approval. An
    open choice, or none recorded, is exploration, a warning, until the design freeze; from it the
    confirmed final choice is required (every decision final_allowed, the assessment current) for every
    made part: a freeze or release with no candidates recorded, or with a made part no candidate covers
    (the report's coverage.unselected), blocks like any other unknown."""
    if STAGES.index(stage) < STAGES.index("intent"):
        return []
    final = stage in ("design_freeze", "release")
    graded = "error" if final else "warning"
    status = report.get("status")
    if status == "invalid":
        return [Finding("gate.selection", SELECTION_PATH,
                        "the selection cannot be assessed: the record has errors (validate.py reports them)", graded)]
    findings = [Finding("gate.selection", SELECTION_PATH, _selection_message(item))
                for item in report.get("findings") or () if item.get("code") in _SELECTION_BLOCKING]
    recorded = report.get("recorded") or {}
    assessment = recorded.get("assessment") or {}
    confirmed = recorded.get("status") == "confirmed"
    current = assessment.get("current")
    if current is not True and assessment:
        why = ("its input_sha256 is not the digest of the current selection inputs (selection.input_digest)"
               if current is False else "its input_sha256 was not checked against the record inputs")
        findings.append(Finding("gate.selection", SELECTION_PATH,
                                f"the recorded {recorded.get('status')} assessment is stale: {why}; re-run selection.py "
                                "and record the new assessment through the approval gate",
                                "error" if confirmed else graded))
    if confirmed and approved is False:
        findings.append(Finding("gate.selection_approval", SELECTION_PATH,
                                f"the selection is confirmed by {assessment.get('decision')}, which is missing or is not "
                                "the user's approval"))
    if status != "not_assessed":  # with no candidates at all, the not_assessed finding below says it once
        for pn in (report.get("coverage") or {}).get("unselected") or ():
            findings.append(Finding("gate.selection", SELECTION_PATH,
                                    f"{pn} is a made part no selection candidate covers, so its material and process choice "
                                    "is unknown" + (": a design freeze needs a confirmed selection for every made part" if final
                                                    else "; exploration may continue"), graded))
    if status == "final_ready" and confirmed:
        return findings
    refused = sorted({reason for decision in report.get("decisions") or () for reason in decision.get("refused_because") or ()})
    if status == "not_assessed":
        reason = "no material, process and architecture selection is recorded (selection.yaml holds no candidates)"
    elif status == "final_ready":
        reason = "every decision may be final, but the selection is not confirmed (status confirmed, recommendation final)"
    else:
        reason = f"the selection is {status}" + (f": {'; '.join(refused)}" if refused else "")
    findings.append(Finding("gate.selection", SELECTION_PATH,
                            reason + (": a design freeze needs the confirmed final choice" if final
                                      else "; exploration may continue, but no final choice is made"), graded))
    return findings


def selection_summary(report: Mapping[str, Any], approved: bool | None) -> dict[str, Any]:
    """The selection report as the evidence packet records it: status, digest, what selection.yaml
    records, whether its confirming decision approves, and per decision whether it may be final."""
    recorded = report.get("recorded") or {}
    return {"status": report.get("status"), "input_sha256": report.get("input_sha256"),
            "recorded": {"status": recorded.get("status"),
                         "current": (recorded.get("assessment") or {}).get("current"), "approved": approved},
            "decisions": [{"parts": list(d.get("parts") or ()), "final_allowed": d.get("final_allowed"),
                           "refused_because": list(d.get("refused_because") or ())} for d in report.get("decisions") or ()],
            "findings": sorted({item.get("code") for item in report.get("findings") or ()})}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _mapping(value: object) -> Mapping[str, Any]:
    """`value` when it is a mapping, else an empty one (an absent or malformed block reads as empty)."""
    return value if isinstance(value, Mapping) else {}


def _texts(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _release_scope(record: Any, service: str) -> tuple[dict[str, Any] | None, list[str]]:
    """The acceptance scope (without `trial`) from use_case.release_scope, or why it is not recorded."""
    facts, problems = record.use_case.facts, []
    purpose = facts.get("release_scope.purpose")
    purpose_text = _text(purpose.value) if purpose is not None and purpose.known else None
    if purpose_text is None:
        problems.append("use_case.release_scope.purpose is not a known text")
    lists: dict[str, list[str]] = {}
    for name in ("operating_limits", "exclusions"):
        fact = facts.get(f"release_scope.{name}")
        if fact is not None and fact.not_applicable:
            lists[name] = []
        elif fact is not None and fact.known and (texts := _texts(fact.value)) is not None:
            lists[name] = texts
        else:
            problems.append(f"use_case.release_scope.{name} is unknown; record it (or not applicable)")
    if service in ("functional_demo", "working_prototype", "production", "safety_critical") and not lists.get("operating_limits"):
        problems.append(f"a {service} release needs use_case.release_scope.operating_limits: the loads, duty and "
                        "supervision limits it is accepted within")
    if mi.is_fact(record.use_case.evidence_policy):
        problems.append("use_case.evidence_policy is unknown; confirm the evidence each requirement needs")
    if record.use_case.status != "confirmed" or not record.use_case.decision:
        problems.append("the use case is not confirmed by a decision")
    if problems:
        return None, problems
    return {"service_class": service, "purpose": purpose_text, "operating_limits": lists["operating_limits"],
            "exclusions": lists["exclusions"]}, []


def assess(record: Any, *, version: str | None = None, manifest_sha256: str | None = None,
           geometry: Mapping[str, str | None] | None = None, packets: Sequence[ProducerPacket] = (),
           approved: Callable[[str | None], bool] = _never, today: date | None = None,
           verification: Sequence[Mapping[str, str]] = (),
           stale_since_build: Mapping[str, Sequence[str]] | None = None,
           artifact_sha: Callable[[str], str | None] | None = None,
           selection_sha256: str | None = None,
           part_of: Callable[[str], str | None] | None = None) -> Assessment:
    """The evidence assessment of `record` (manufacturing_intent.Record, the current hw/ record) for the
    built `version` whose manifest has sha256 `manifest_sha256`. `geometry` maps PN to its current model
    source sha256 (impact.current_geometry); `packets` are the version's producer packets; `approved`
    tells whether a decision id is the user's approval; `verification` are the verification.csv rows;
    `stale_since_build` the obligations whose inputs changed since the version was built (impact);
    `selection_sha256` the selection input digest of the record's documents (selection.input_digest),
    without which a recorded selection assessment is never current; `part_of` maps an assembly part
    reference (a PN or @occurrence) to its PN for the joint check (default: a bare PN is itself)."""
    today = today or date.today()
    stale_since_build = stale_since_build or {}
    expected = mi.required_checks(record)
    by_id = {o.id: o for o in expected}
    findings: list[Finding] = []
    candidates: dict[str, list[tuple[str, str, str]]] = {o.id: [] for o in expected}  # (result, source, reason)
    waivers: dict[str, Mapping[str, Any]] = {}
    complete = True

    joints = _joint_results(record, part_of)
    for obligation in expected:
        if obligation.family == "intent.use_case":
            use_case = mi.assess_use_case(record)
            candidates[obligation.id].append(("pass", "record", "the use case is confirmed and supports a final choice")
                                             if use_case.final_ready else ("unknown", "record", "; ".join(use_case.blocking)))
        elif obligation.family == "assembly.joint":
            result, reason = joints.get(obligation.id, ("unknown", obligation.basis))
            candidates[obligation.id].append((result, "record", reason))
        elif obligation.family.startswith(impact.RECORD_FAMILIES):
            candidates[obligation.id].append(("unknown", "record", obligation.basis))

    # producer packets
    accepted_packets = []
    for packet in packets:
        problems = _packet_problems(packet, version, by_id)
        if problems:
            findings.append(Finding("packet.invalid", packet.bundle, f"{packet.bundle}/result.json: " + "; ".join(problems)))
            complete = False
            continue
        accepted_packets.append(packet)
        if packet.data.get("complete") is not True:
            complete = False
        for outcome in packet.data.get("outcomes") or ():
            ident = outcome.get("obligation")
            obligation = by_id.get(ident)
            if obligation is not None and obligation.family.startswith(impact.RECORD_FAMILIES) \
                    and outcome.get("outcome") == "unknown":
                continue  # a producer that names a record-derived unknown (access.py: unknown operations) claims nothing
            if obligation is None or obligation.family.startswith(impact.RECORD_FAMILIES):
                findings.append(Finding("packet.unexpected", packet.bundle,
                                        f"{packet.bundle}: outcome for {ident}, which this record does not expect from a "
                                        "producer", "warning"))
                continue
            if outcome.get("level") not in (None, obligation.level):
                findings.append(Finding("packet.unexpected", packet.bundle,
                                        f"{packet.bundle}: {ident} is shown at {outcome.get('level')}, not its level "
                                        f"{obligation.level}", "warning", ident))
                continue
            result = outcome.get("outcome")
            if obligation.level == PHYSICAL and result == "pass":
                findings.append(Finding("evidence.physical_claim", packet.bundle,
                                        f"{packet.bundle}: a producer cannot physically validate {ident}; record the test "
                                        "in hw/manufacturing/evidence.yaml", obligation=ident))
                continue
            if ident in stale_since_build and result in ("pass", "fail", "not_applicable"):
                candidates[ident].append(("stale", packet.bundle, "its inputs changed since the version was built: "
                                          + ", ".join(stale_since_build[ident])))
                continue
            if result in ("pass", "fail", "not_applicable", "unknown"):
                candidates[ident].append((result, packet.bundle, outcome.get("method") or ""))

    # ledger entries
    entry_results: dict[str, str] = {}
    untargeted: dict[tuple[str, str], list[str]] = {}
    for entry in record.evidence:
        ident = str(entry.get("id"))
        result, reasons, entry_findings = _entry(entry, record, by_id, version, manifest_sha256, geometry or {},
                                                 approved, today, artifact_sha)
        findings.extend(entry_findings)
        entry_results[ident] = result
        targets = _entry_targets(entry, record, expected)
        for target in targets:
            if result == "waived":
                waivers.setdefault(target, entry.get("waiver") or {})
            candidates[target].append((result, ident, "; ".join(reasons)))
        if not targets and entry.get("requirement"):
            untargeted.setdefault((entry["requirement"], entry.get("level")), []).append(result)

    # dispositions of producer findings
    critical = []
    for packet in accepted_packets:
        for item in packet.data.get("findings") or ():
            disposition = _disposition(record, item, packet)
            obligation = by_id.get(item.get("obligation"))
            settled, waiver = _dispose(item, disposition, record, obligation, approved, today, entry_results, findings, packet)
            if waiver is not None and obligation is not None:
                waivers.setdefault(obligation.id, waiver)
            if item.get("severity") == "critical" and item.get("outcome") in ("fail", "unknown") and not settled:
                critical.append({"finding": item.get("id"), "code": item.get("code"), "bundle": packet.bundle,
                                 "obligation": item.get("obligation"), "consequence": item.get("consequence")})

    statuses = {}
    for obligation in expected:
        found = candidates[obligation.id]
        result = _best([r for r, _, _ in found], obligation.id in waivers)
        statuses[obligation.id] = Status(obligation, result, tuple(dict.fromkeys(s for _, s, _ in found)),
                                         tuple(dict.fromkeys(reason for _, _, reason in found if reason)),
                                         waivers.get(obligation.id) if result == "waived" else None)

    facets = _facets(record, expected, statuses, untargeted)
    rows = _verification(record, verification, facets, entry_results, findings)
    report = selection.assess(record, input_sha256=selection_sha256)
    return Assessment(record, version, manifest_sha256, expected, statuses, facets, tuple(rows), tuple(findings),
                      tuple(critical), complete, report, selection_approval(record, approved))


def selection_approval(record: Any, approved: Callable[[str | None], bool]) -> bool | None:
    """Whether a confirmed selection's assessment decision is the user's approval; None when not confirmed."""
    if record.selection.status != "confirmed":
        return None
    assessment = record.selection.raw.get("assessment")
    return approved(assessment.get("decision") if isinstance(assessment, Mapping) else None)


def _joint_results(record: Any, part_of: Callable[[str], str | None] | None) -> dict[str, tuple[str, str]]:
    """assembly.joint obligation id -> (result, reason) from assembly_operations.check_joints over the
    record's declared joint facts: pass only when every joint fact is sourced and suitable, unknown while
    one is not, fail on a violation. Operations it cannot normalize leave every joint unknown."""
    try:
        report = operations.check_joints(operations.from_record(record), record, mi.resolve_property, part_of=part_of)
    except operations.OperationsError as error:
        return {o.id: ("unknown", f"assembly operations cannot be normalized: {error}")
                for o in mi.required_checks(record) if o.family == "assembly.joint"}
    found = {}
    for row in report.outcomes:
        mine = [f for f in report.findings if f.get("obligation") == row["obligation"] and f.get("outcome") == row["outcome"]]
        texts = [(f.get("measured") or {}).get("text") or (f.get("measured") or {}).get("reason") for f in mine]
        found[row["obligation"]] = (row["outcome"], "; ".join(t for t in texts if t) or row["method"])
    return found


def _packet_problems(packet: ProducerPacket, version: str | None, expected: Mapping[str, Any]) -> list[str]:
    """Why a producer packet cannot be counted: not a check-result packet, another version, or a packet
    that contradicts itself (a claimed completeness or result its outcomes do not show)."""
    data = packet.data
    if not isinstance(data, Mapping) or data.get("schema_version") != 1 or data.get("record_version") != mi.RECORD_VERSION:
        return ["not a record version 2 manufacturing check result (schema_version 1)"]
    if version is None or data.get("version") != version:
        return [f"it checks {data.get('version')}, not {version}"]
    problems = []
    outcomes = [o for o in data.get("outcomes") or () if isinstance(o, Mapping)]
    ids = [o.get("obligation") for o in outcomes]
    if len(ids) != len(set(ids)):
        problems.append("it gives an obligation two outcomes")
    packet_expected = {o.get("id"): o for o in data.get("expected") or () if isinstance(o, Mapping)}
    if set(ids) - set(packet_expected):
        problems.append(f"outcomes for obligations it did not expect: {', '.join(sorted(map(str, set(ids) - set(packet_expected))))}")
    for ident, item in packet_expected.items():
        mine = expected.get(ident) if isinstance(ident, str) else None
        if mine is not None and (item.get("level"), item.get("stage"), item.get("family")) != (mine.level, mine.stage, mine.family):
            problems.append(f"it expects {ident} at another level, stage or family than the record")
    covered = set(packet_expected) <= set(ids)
    if data.get("complete") is True and not covered:
        problems.append("it claims complete but lacks outcomes for " + ", ".join(sorted(map(str, set(packet_expected) - set(ids)))))
    results = {o.get("outcome") for o in outcomes}
    shown = "fail" if "fail" in results else ("unknown" if "unknown" in results or not covered else "pass")
    if data.get("result") != shown:
        problems.append(f"its result {data.get('result')!r} is not what its outcomes show ({shown})")
    given = {o.get("obligation"): o.get("outcome") for o in outcomes}
    for item in data.get("findings") or ():
        if isinstance(item, Mapping) and item.get("outcome") == "fail" and given.get(item.get("obligation")) == "pass":
            problems.append(f"its finding {item.get('id')} fails {item.get('obligation')}, whose outcome it gives as pass")
    return problems


def _entry_targets(entry: Mapping[str, Any], record: Any, expected: Sequence[Any]) -> list[str]:
    """The expected obligations a ledger entry speaks to: the one it names, else the requirement and
    evidence-policy obligations of its requirement at its level."""
    if entry.get("obligation"):
        return [entry["obligation"]] if any(o.id == entry["obligation"] for o in expected) else []
    requirement, level = entry.get("requirement"), entry.get("level")
    return [o.id for o in expected if o.requirement == requirement and o.family in (f"requirement.{level}", f"policy.{level}")]


def _entry(entry: Mapping[str, Any], record: Any, expected: Mapping[str, Any], version: str | None,
           manifest_sha256: str | None, geometry: Mapping[str, str | None], approved: Callable[[str | None], bool],
           today: date, artifact_sha: Callable[[str], str | None] | None = None) -> tuple[str, tuple[str, ...], list[Finding]]:
    """(status, reasons, findings) of one accepted ledger entry for the assessed version."""
    ident, level, result = entry.get("id"), entry.get("level"), entry.get("result")
    subject = entry.get("subject") or {}
    path = f"hw/manufacturing/evidence.yaml#/entries/{ident}"
    findings: list[Finding] = []
    named = entry.get("obligation")
    obligation = expected.get(named) if isinstance(named, str) and named else None
    if entry.get("obligation") and obligation is not None and mi.obligation_anchor(subject) != _anchor(obligation.id):
        findings.append(Finding("evidence.subject", path, f"{ident} is about {mi.obligation_anchor(subject)}, but "
                                f"{obligation.id} is about {_anchor(obligation.id)}: evidence covers its own subject only",
                                obligation=obligation.id))
        return "unknown", (f"{ident} is about another subject",), findings
    if result in ("planned", "unknown"):
        return result, (f"{ident} is {result}",), findings
    if result == "waived":
        problem = waiver_problem(entry.get("waiver") or {}, record=record, subject=subject, obligation=obligation,
                                 requirements=[entry["requirement"]] if entry.get("requirement") else (),
                                 approved=approved, today=today)
        if problem is not None:
            findings.append(Finding(problem[0], path, f"{ident}: {problem[1]}", obligation=getattr(obligation, "id", None)))
            return "unknown", (problem[1],), findings
        return "waived", (f"{ident} waived by {entry['waiver'].get('decision')}",), findings
    configuration = entry.get("configuration") or {}
    if version is None:
        return "stale", (f"{ident}: no built version to tie the evidence to",), findings
    if configuration.get("version") != version or configuration.get("manifest_sha256") != manifest_sha256:
        return "stale", (f"{ident} is evidence of {configuration.get('version')}, not the built {version}",), findings
    part = record.parts.get(subject.get("PN")) if subject.get("PN") else None
    if part is not None and configuration.get("part_rev") not in (None, part.raw.get("rev")):
        return "stale", (f"{ident} is evidence of revision {configuration.get('part_rev')}, not {part.raw.get('rev')}",), findings
    recorded = _mapping(entry.get("fingerprints"))
    domains = _REQUIRED_DOMAINS[_subject_kind(record, subject)]
    missing = sorted((domains.get(level, frozenset()) if isinstance(level, str) else frozenset()) - set(recorded))
    if missing:
        findings.append(Finding("evidence.unqualified", path, f"{ident} ({level} {result}) records no fingerprint of "
                                f"{', '.join(missing)}: the evidence is not tied to what it relied on"))
        return "unknown", (f"{ident} is unqualified",), findings
    changed = impact.evidence_staleness(entry, impact.fingerprints(record, subject, geometry))
    if changed:
        return "stale", (f"{ident} relied on {', '.join(changed)} that changed",), findings
    claimed = {a.get("path"): a.get("sha256") for a in entry.get("artifacts") or () if isinstance(a, Mapping)}
    if isinstance(entry.get("bundle"), Mapping):
        claimed[f"{entry['bundle'].get('path')}/{layout.MANIFEST}"] = entry["bundle"].get("manifest_sha256")
    altered = sorted(str(rel) for rel, sha in claimed.items() if artifact_sha is not None and artifact_sha(str(rel)) != sha)
    if altered:
        findings.append(Finding("evidence.altered", path, f"{ident} cites {', '.join(altered)}, which no longer hold the "
                                "bytes it names: the evidence is void until it is accepted again"))
        return "unknown", (f"{ident} cites altered evidence",), findings
    cited = [a.get("path") for a in entry.get("artifacts") or () if isinstance(a, Mapping)]
    if isinstance(entry.get("bundle"), Mapping):
        cited.append(entry["bundle"].get("path"))
    foreign = sorted({owner for rel in cited if isinstance(rel, str) and (owner := _owning_version(rel)) not in (None, version)})
    if foreign and not (entry.get("decisions") and all(approved(d) for d in entry["decisions"])):
        findings.append(Finding("evidence.carry_forward", path,
                                f"{ident} cites evidence sealed for {', '.join(foreign)} as evidence of {version} without an "
                                "approving impact decision; run impact.py and record the decision"))
        return "stale", (f"{ident} is carried from {', '.join(foreign)} without an approved impact assessment",), findings
    return (result if isinstance(result, str) else "unknown"), (f"{ident} {level} {result}",), findings


def _disposition(record: Any, item: Mapping[str, Any], packet: ProducerPacket) -> Mapping[str, Any] | None:
    """The latest disposition of producer finding `item` of `packet` (findings.yaml is append-only)."""
    found = None
    for disposition in record.dispositions:
        producer = disposition.get("producer") or {}
        if (disposition.get("finding") == item.get("id") and producer.get("bundle") == packet.bundle
                and producer.get("manifest_sha256") == packet.manifest_sha256):
            found = disposition
    return found


def _dispose(item: Mapping[str, Any], disposition: Mapping[str, Any] | None, record: Any, obligation: Any | None,
             approved: Callable[[str | None], bool], today: date, entry_results: Mapping[str, str],
             findings: list[Finding], packet: ProducerPacket) -> tuple[bool, Mapping[str, Any] | None]:
    """(whether the finding is settled, the valid waiver it carries). Resolved needs current passing
    replacement evidence or an approved decision; an accepted waiver must stand; declining never
    settles a fail or unknown outcome."""
    if disposition is None:
        return False, None
    status = disposition.get("status")
    path = f"hw/manufacturing/findings.yaml#/dispositions/{item.get('id')}"
    if status == "resolved":
        replacements = disposition.get("replacement_evidence") or ()
        if replacements:
            return all(entry_results.get(e) == "pass" for e in replacements), None
        return approved(disposition.get("decision")), None
    if status == "accepted_waiver":
        problem = waiver_problem(disposition.get("waiver") or {}, record=record, subject=item.get("subject"),
                                 obligation=obligation, requirements=item.get("requirements") or (), approved=approved,
                                 today=today, critical=item.get("severity") == "critical")
        if problem is not None:
            findings.append(Finding(problem[0], path, f"finding {item.get('id')} of {packet.bundle}: {problem[1]}",
                                    obligation=getattr(obligation, "id", None)))
            return False, None
        return True, disposition.get("waiver")
    return False, None


def _facets(record: Any, expected: Sequence[Any], statuses: Mapping[str, Status],
            untargeted: Mapping[tuple[str, str], list[str]]) -> dict[str, dict[str, str]]:
    """Per requirement, part, interface, assembly operation and the product: each evidence level's
    result, the worst over its obligations (not_required where none applies)."""
    collected: dict[str, dict[str, list[str]]] = {}

    def add(key: str, level: str, result: str) -> None:
        collected.setdefault(key, {}).setdefault(level, []).append(result)

    for obligation in expected:
        result = statuses[obligation.id].result
        add(impact.subject_key(obligation.subject), obligation.level, result)
        for req in {obligation.requirement, *((obligation.subject or {}).get("requirements") or ())} - {None}:
            add(req, obligation.level, result)
    for (req, level), results in untargeted.items():
        if level in LEVELS:
            add(req, level, _best(results, False))
    keys = {"product", *record.parts, *record.interfaces, *record.requirements, *collected}
    return {key: {level: (_worst(collected.get(key, {}).get(level, ())) or "not_required") for level in LEVELS}
            for key in sorted(keys)}


def _verification(record: Any, rows: Sequence[Mapping[str, str]], facets: Mapping[str, Mapping[str, str]],
                  entry_results: Mapping[str, str], findings: list[Finding]) -> list[dict[str, Any]]:
    """The typed verification rows: each verification.csv row with its method, criterion and stated
    result, the result its evidence shows (the worst required facet; unknown without evidence) and the
    ledger entries it rests on, with what each is tied to. A stated pass or waiver without that
    evidence is an evidence.csv_unsupported finding."""
    typed = []
    for index, row in enumerate(rows, start=2):
        req = row.get("requirement_id") or ""
        levels = {level: result for level, result in (facets.get(req) or {}).items() if result != "not_required"}
        shown = _worst(levels.values()) or "unknown"
        requirement = record.requirements.get(req)
        evidence = []
        for entry in record.evidence:
            if entry.get("requirement") == req:
                configuration = entry.get("configuration") or {}
                evidence.append({"id": entry.get("id"), "level": entry.get("level"), "stated": entry.get("result"),
                                 "result": entry_results.get(str(entry.get("id"))), "method": entry.get("method"),
                                 "version": configuration.get("version"), "part_rev": configuration.get("part_rev"),
                                 "tied_to": sorted(entry.get("fingerprints") or {})})
        stated = (row.get("result") or "").strip()
        if stated == "pass" and shown != "pass" or stated == "waived" and shown not in ("pass", "waived"):
            findings.append(Finding("evidence.csv_unsupported", f"hw/verification.csv:{index}",
                                    f"{req} states {stated} but its accepted evidence shows {shown}: record current "
                                    "evidence in hw/manufacturing/evidence.yaml (a CSV result alone is not evidence)"))
        typed.append({"requirement": req, "method": row.get("method"),
                      "criterion": requirement.criteria_text if requirement is not None else row.get("criteria"),
                      "acceptance": [impact.plain(c) for c in requirement.acceptance] if requirement is not None else [],
                      "stated": stated, "result": shown, "levels": levels, "evidence": evidence})
    return typed


# --- ledger findings validate.py reports -------------------------------------------------------------------------

def ledger_findings(record: Any, *, approved: Callable[[str | None], bool], today: date | None = None) -> list[Finding]:
    """The waivers of the evidence ledger and of finding dispositions that cannot stand, independent of
    any version: what validate.py reports about the record."""
    findings, today = [], today or date.today()
    expected = {o.id: o for o in mi.required_checks(record)} if record.version == mi.RECORD_VERSION else {}
    for index, entry in enumerate(record.evidence):
        if entry.get("result") != "waived":
            continue
        obligation = expected.get(entry.get("obligation"))
        problem = waiver_problem(entry.get("waiver") or {}, record=record, subject=entry.get("subject"),
                                 obligation=obligation, requirements=[entry["requirement"]] if entry.get("requirement") else (),
                                 approved=approved, today=today)
        if problem is not None:
            findings.append(Finding(problem[0], f"hw/manufacturing/evidence.yaml#/entries/{index}/waiver",
                                    f"{entry.get('id')}: {problem[1]}"))
    for index, disposition in enumerate(record.dispositions):
        if disposition.get("status") != "accepted_waiver":
            continue
        waiver = disposition.get("waiver") or {}
        problem = waiver_problem(waiver, record=record, subject=None, obligation=expected.get(waiver.get("obligation")),
                                 approved=approved, today=today)
        if problem is not None:
            findings.append(Finding(problem[0], f"hw/manufacturing/findings.yaml#/dispositions/{index}/waiver",
                                    f"finding {disposition.get('finding')}: {problem[1]}"))
    return findings


def csv_findings(record: Any, rows: Sequence[Mapping[str, str]], stage: str | None) -> list[Finding]:
    """verification.csv rows that state pass or waived with no accepted ledger entry of that result for
    their requirement, version aside: an error from the design freeze on, a warning before it. A CSV
    result alone is a summary, never evidence; `assess` judges whether the entry is current."""
    if record.version != mi.RECORD_VERSION:
        return []
    accepted: dict[str, set[str]] = {}
    for entry in record.evidence:
        if entry.get("requirement"):
            accepted.setdefault(entry["requirement"], set()).add(str(entry.get("result")))
    strict = stage in ("design_freeze", "release") or stage not in STAGES
    findings = []
    for index, row in enumerate(rows, start=2):
        req, stated = row.get("requirement_id") or "", (row.get("result") or "").strip()
        wanted = {"pass": {"pass"}, "waived": {"pass", "waived"}}.get(stated)
        if wanted and not (accepted.get(req, set()) & wanted):
            findings.append(Finding("evidence.csv_unsupported", f"hw/verification.csv:{index}",
                                    f"{req} states {stated} but hw/manufacturing/evidence.yaml holds no {stated} entry "
                                    "for it: a CSV result is a summary of accepted evidence, not evidence",
                                    "error" if strict else "warning"))
    return findings


_GENERATED_ROOTS = frozenset({"versions", "releases", "renders", "captures", "checks", "drawings", "motion"})


# --- inputs: packets, record digest, acceptance inventory ---------------------------------------------------------

def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def collect_packets(hw: Path, version: str, version_manifest_sha256: str) -> tuple[list[ProducerPacket], list[Finding]]:
    """The producer check packets sealed for `version` (hw/checks/<V>/<S>/result.json, each a
    manufacturing check result), and a packet.invalid finding for each bundle whose result.json cannot be
    read or is no required-check packet, that has no manifest, fails its seal or belongs to another build
    of the version."""
    hw = Path(hw)
    root = layout.locate(hw, f"hw/checks/{version}")
    packets, findings = [], []
    for directory in sorted(root.iterdir()) if root.is_dir() else []:
        result = directory / "result.json"
        if not result.is_file():
            continue  # validate.py reports a bundle folder without its result
        rel = layout.relative(hw, directory)
        problems: list[str] = []
        data: Any = None
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.append(f"cannot read result.json: {error}")
        else:
            if not (isinstance(data, dict) and "expected" in data and "outcomes" in data):
                problems.append("result.json is not a required-check packet: it has no expected and outcomes")
        if not (directory / layout.MANIFEST).is_file():
            problems.append(f"it has no {layout.MANIFEST}")
        elif not problems:
            problems = layout.verify(directory)  # a sealed bundle's manifest parses
            manifest = json.loads((directory / layout.MANIFEST).read_text(encoding="utf-8")) if not problems else {}
            if not problems and manifest.get("version_manifest_sha256") != version_manifest_sha256:
                problems = [f"it is bound to another build of {version}"]
        if problems:
            findings.append(Finding("packet.invalid", rel, f"{rel}: " + "; ".join(problems)))
            continue
        packets.append(ProducerPacket(rel, _sha256(directory / layout.MANIFEST), data))
    return packets, findings


_INPUT_PATTERNS = None


def is_record_input(rel: str) -> bool:
    """Whether `rel` (relative to hw/) is a record input an assessment reads: a record document, the
    verification table or a frozen source."""
    global _INPUT_PATTERNS
    if _INPUT_PATTERNS is None:
        _INPUT_PATTERNS = [re.compile(pattern.replace(".", r"\.").replace("*", "[^/]+")) for pattern, _ in mi.DOCUMENT_SCHEMAS]
    return (any(p.fullmatch(rel) for p in _INPUT_PATTERNS) or rel == "verification.csv"
            or rel.startswith(f"{SOURCES_DIR}/") or re.fullmatch(r"parts/[^/]+/sources/.+", rel) is not None)


def record_inputs(hw: Path) -> dict[str, bytes]:
    """The bytes of every record input below `hw`, by path relative to it (links are never followed)."""
    hw, found = Path(hw), {}
    for directory, folders, files in os.walk(hw, followlinks=False):
        folders[:] = sorted(f for f in folders if not (Path(directory) / f).is_symlink()
                            and not (directory == str(hw) and f in _GENERATED_ROOTS) and f not in (".cad", ".git"))
        for name in files:
            path = Path(directory) / name
            rel = path.relative_to(hw).as_posix()
            if not path.is_symlink() and path.is_file() and is_record_input(rel):
                found[rel] = path.read_bytes()
    return found


def record_digest(files: Mapping[str, bytes]) -> str:
    """sha256 of record inputs (relative path -> bytes) as path-framed canonical JSON."""
    return _canonical_sha({"record": sorted([rel, hashlib.sha256(data).hexdigest()] for rel, data in files.items()
                                            if is_record_input(rel))})


def _safe_file(hw: Path, rel: str) -> Path:
    """The regular file `rel` (relative to hw/) names; EvidenceError for an unnormalized or escaping
    path, a link anywhere on the way, or a missing file."""
    if not isinstance(rel, str) or not rel or "\\" in rel or rel.startswith("/") or posixpath.normpath(rel) != rel \
            or rel.split("/")[0] == "..":
        raise EvidenceError(f"hw/{rel} is not a normalized path inside hw/")
    path = Path(hw)
    for part in rel.split("/"):
        path = path / part
        if path.is_symlink():
            raise EvidenceError(f"hw/{rel} lies behind or is a symbolic link")
    if not path.is_file():
        raise EvidenceError(f"hw/{rel} is missing")
    return path


def _hw_relative(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("hw/"):
        raise EvidenceError(f"{value!r} is not a project-relative hw/ path")
    return value[3:]


def acceptance_inventory(hw: Path, record: Any, packets: Iterable[ProducerPacket] = ()) -> dict[str, str]:
    """The acceptance inventory: `hw/<path>` -> sha256 of the verification table, the evidence ledger and
    finding dispositions, every relied-on bundle manifest (cited evidence bundles, dispositioned producer
    bundles and the version's producer packets), every cited evidence artifact and every frozen source.
    Workflow state (flow), decision records, timestamps and the release itself are never part of it.
    EvidenceError when a path is unnormalized, escapes hw/, is a link or is missing."""
    hw = Path(hw)
    rels = {rel for rel in LEDGER_FILES if (hw / rel).is_file() or (hw / rel).is_symlink()}
    for entry in record.evidence:
        for artifact in entry.get("artifacts") or ():
            rels.add(_hw_relative(artifact.get("path")))
        if isinstance(entry.get("bundle"), Mapping):
            rels.add(f"{_hw_relative(entry['bundle'].get('path'))}/{layout.MANIFEST}")
    for disposition in record.dispositions:
        rels.add(f"{_hw_relative((disposition.get('producer') or {}).get('bundle'))}/{layout.MANIFEST}")
    for packet in packets:
        rels.add(f"{_hw_relative(packet.bundle)}/{layout.MANIFEST}")
    rels.update(record.artifacts)
    sources = hw / SOURCES_DIR
    if sources.is_symlink():
        raise EvidenceError(f"hw/{SOURCES_DIR} is a symbolic link")
    for directory, folders, files in os.walk(sources, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise EvidenceError(f"hw/{path.relative_to(hw).as_posix()} is a symbolic link")
        rels.update((Path(directory) / name).relative_to(hw).as_posix() for name in files)
    inventory = {}
    for rel in sorted(rels):
        inventory[f"hw/{rel}"] = _sha256(_safe_file(hw, rel))
    return inventory


def acceptance_digest(inventory: Mapping[str, str]) -> str:
    """The evidence_sha256 of an acceptance inventory: sha256 of its sorted [path, sha256] pairs as
    canonical JSON, framed by the inventory schema name."""
    return _canonical_sha({"schema": INVENTORY_SCHEMA, "files": sorted([path, sha] for path, sha in inventory.items())})


# --- the stamped packet ------------------------------------------------------------------------------------------

_OBLIGATION_GATE_CODES = frozenset({"gate.fail", "gate.stale", "gate.unresolved", "release.waiver_forbidden",
                                    "release.trial_not_permitted"})


def packet_problems(data: Any, *, record: Any, record_sha256: str, stage: str) -> list[str]:
    """Why an evidence packet is not the assessment of `record` (whose record inputs digest to
    `record_sha256`) at `stage`: not an evidence packet, computed from another record (sources changed
    during or after the check), other expected checks, a missing or partial expected check, or another
    stage. Such a packet never stamps, passed or not."""
    if not isinstance(data, Mapping) or data.get("kind") != PACKET_KIND or data.get("schema_version") != PACKET_SCHEMA_VERSION:
        return ["it is not an evidence packet; run `evidence.py check --hw hw --version vN --out .cad-work/evidence.json`"]
    problems = []
    if data.get("record_sha256") != record_sha256:
        problems.append("it was computed from another record: hw/ changed during or after the check; check again")
    expected = mi.required_checks(record)
    if data.get("expected_sha256") != expected_digest(expected) or \
            [o.get("id") for o in data.get("expected") or ()] != [o.id for o in expected]:
        problems.append("its expected checks are not the ones the record implies")
    statuses = _mapping(data.get("statuses"))
    missing = [o.id for o in expected if not isinstance(statuses.get(o.id), Mapping)]
    if missing:
        problems.append(f"missing expected check(s): {', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}")
    if data.get("complete") is not True:
        problems.append("it is partial (complete: false): a producer packet was partial or invalid")
    if data.get("stage") != stage:
        problems.append(f"it was assessed for stage {data.get('stage')}, not {stage}")
    return problems


def packet_blocking(data: Mapping[str, Any], *, record: Any, stage: str, selection_sha256: str | None) -> list[str]:
    """The blocking gate findings of an evidence packet at `stage`: the obligation gate recomputed from
    its statuses (a stale, failed, unknown or missing expected check), the selection gate recomputed from
    `record` and its selection input digest `selection_sha256` (selection.input_digest), plus the packet's
    own blocking findings about the evidence, the use case, the release scope and the approval of a
    confirmed selection. A packet flagged passing cannot override them."""
    statuses = _mapping(data.get("statuses"))
    results = {ident: (status or {}).get("result", "unknown") for ident, status in statuses.items()}
    found = [f"{f.obligation}: {f.message}" for f in obligation_gate([o.as_dict() for o in mi.required_checks(record)],
                                                                     results, stage, _service_class(record))
             if f.severity == "error"]
    found += [f"{f.code}: {f.message}" for f in selection_gate(selection.assess(record, input_sha256=selection_sha256), stage)
              if f.severity == "error"]
    recomputed = _OBLIGATION_GATE_CODES | {"gate.selection"}
    found += [f"{item.get('code')}: {item.get('message')}" for item in ((data.get("gate") or {}).get("findings") or ())
              if isinstance(item, Mapping) and item.get("severity") == "error" and item.get("code") not in recomputed]
    return found


# --- CLI ---------------------------------------------------------------------------------------------------------

def _validate() -> Any:
    return _load_module("cad_record_validate", SCRIPTS / "validate.py")


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def assembly_part_of(assembly: Any) -> Callable[[str], str | None] | None:
    """The PN an assembly part reference (a PN or @occurrence) names in a parsed assembly.yaml, None when
    it names none; None (check_joints' default reading) when the occurrences cannot be normalized."""
    data = _load_module("cad_record_assembly_data", SCRIPTS / "assembly_data.py")
    try:
        inventory = data.normalize(assembly if isinstance(assembly, Mapping) else {})
    except data.InventoryError:
        return None

    def part_of(ref: str) -> str | None:
        try:
            return str(inventory.resolve(ref).pn)
        except data.UnresolvedReference:
            return None

    return part_of


def build_assessment(hw: Path, version: str, today: date | None = None) -> tuple[Assessment, list[ProducerPacket]]:
    """The assessment of the current hw/ record for built `version`, with the inputs the CLI reads."""
    hw = Path(hw).resolve()
    validate = _validate()
    label, folder, snapshot = layout.built_version(hw, version)
    manifest_path = folder / layout.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha = _sha256(manifest_path)
    documents, record = impact.read_inputs(hw)
    built = impact.read_record(snapshot, ledgers_from=hw)
    geometry = impact.current_geometry(hw)
    changed = impact.changed_domains(impact.subject_fingerprints(built, impact.version_geometry(manifest)),
                                     impact.subject_fingerprints(record, geometry))
    stale = impact.stale_obligations(mi.required_checks(record), changed)
    packets, packet_findings = collect_packets(hw, label, manifest_sha)

    def artifact_sha(rel: str) -> str | None:
        try:
            return _sha256(_safe_file(hw, _hw_relative(rel)))
        except EvidenceError:
            return None

    assessment = assess(record, version=label, manifest_sha256=manifest_sha, geometry=geometry, packets=packets,
                        approved=lambda decision: validate.decision_approves(hw, decision), today=today,
                        verification=_rows(hw / "verification.csv"), stale_since_build=stale, artifact_sha=artifact_sha,
                        selection_sha256=selection.input_digest(documents),
                        part_of=assembly_part_of(documents.get("assembly.yaml")))
    if packet_findings:
        assessment = dataclasses.replace(assessment, findings=tuple(packet_findings) + assessment.findings, complete=False)
    return assessment, packets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="print the evidence packet and the stage gate of a built version")
    check.add_argument("--hw", type=Path, required=True)
    check.add_argument("--version", required=True)
    check.add_argument("--stage", choices=STAGES, help="the gate to apply; product.yaml flow.stage when omitted")
    check.add_argument("--out", type=Path, help="also write the packet here, under .cad-work/")
    digest = commands.add_parser("digest", help="print the acceptance inventory and evidence_sha256 of a built version")
    digest.add_argument("--hw", type=Path, required=True)
    digest.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        hw = args.hw.resolve()
        assessment, packets = build_assessment(hw, args.version)
        if args.command == "digest":
            inventory = acceptance_inventory(hw, assessment.record, packets)
            print(json.dumps({"version": assessment.version, "evidence_sha256": acceptance_digest(inventory),
                              "inventory": inventory, "status": "ok"}, indent=2))
            return 0
        flow = impact.plain(assessment.record.product).get("flow") or {}
        stage = args.stage or flow.get("stage")
        if not isinstance(stage, str) or stage not in STAGES:
            raise ValueError(f"product.yaml flow.stage {stage!r} is not a stage; pass --stage")
        packet = assessment.packet(stage, record_digest(record_inputs(hw)))
        text = json.dumps(packet, indent=2, ensure_ascii=False)
        if args.out is not None:
            target = layout.scratch_path(hw, args.out, "--out")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0 if packet["gate"]["passed"] else 1
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
