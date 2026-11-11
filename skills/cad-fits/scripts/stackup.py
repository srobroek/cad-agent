# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""One-dimensional linear tolerance stack-up: worst case, and statistics only where justified.

Usage: uv run --script stackup.py STACK.yaml [--hw hw --version vN [--replace]] [--out .cad-work/...]

Legacy stack file (no intent_version):
    id: STK-001
    title: Pin end float
    units: mm
    requirement: {min: 0.05, max: 0.60, method: worst_case}  # min and/or max; method worst_case|rss
    contributors:
      - id: A
        description: housing bore depth
        nominal: 20.0
        tol_plus: 0.10      # or `tol: 0.10` for a symmetric +/- tolerance
        tol_minus: 0.10     # magnitudes, both >= 0
        direction: 1        # 1 adds to the closing dimension, -1 subtracts

Each contributor is centred on its mid-limit before stacking, so asymmetric tolerances shift the
mean: mean_i = nominal + (tol_plus - tol_minus) / 2 and half_i = (tol_plus + tol_minus) / 2. The
closing dimension is mean = sum(direction_i * mean_i); worst case is mean +/- sum(half_i). The
legacy `rss` result, mean +/- sqrt(sum(half_i ** 2)), is an unqualified quadrature heuristic: no
distribution, independence or coverage stands behind it, so it is never a probabilistic range.

Intent stack file (`intent_version: 2`, the analysis case of a critical interface's
`functional.variation.stack_case`, at hw/analysis/cases/<case>/case.yaml):
    intent_version: 2
    id: STK-014
    title: Cover closing gap
    units: mm
    subject: {interface: IF-001}        # or {PN, feature[, component, occurrence]}
    requirements: [REQ-004]             # optional
    dimension_state: assembled          # the state the closing dimension is judged in
    consequence: collision              # collision|binding|loss_of_function|critical_fit|performance|appearance|other
    relevant: [manufacture, coating, thermal]   # every effect the lead judged relevant; each needs a contributor
    requirement: {min: 0.0, method: worst_case, source: {kind: user, reference: REQ-004, origin: lead}}
    contributors:
      - id: A
        category: manufacture           # manufacture|assembly|thermal|moisture|wear|deformation|coating|adhesive|joining
        subject: {PN: TST-0001, feature: cover_seat}
        state: after_finish             # as_manufactured|after_finish|assembled
        nominal: 12.0
        tol: 0.02
        direction: 1
        source: {kind: calibration, reference: ream calibration, artifact: manufacturing/sources/ream.pdf, sha256: ...}
        capability: {profile: tst_reamer, characteristic: bore_diameter_deviation}   # optional
        operation: ream                 # the route operation that produces this dimension (optional)
        inspection: bore_check          # an inspection that enforces these limits (optional)
        designation: H7                 # set when the limits come from a fit class or general-tolerance label
        process: laser                  # the route kind that produces it, when no capability names it
      - {id: T, category: thermal, status: unknown, reason: no thermal analysis yet}
    statistics:                         # justified_rss only
      decision: DR-012                  # the approved decision that the statistics suit this use
      coverage_factor: 3                # k; optional probability gives its coverage
      independence: {kind: test, ...}   # why contributors not listed in correlations are independent
      correlations: [{between: [A, B], rho: 0.2}]
    # and per contributor: distribution: {sigma | tolerance_divisor, centred: true, stable: true, source}

Intent rules. The worst-case interval is always reported and governs collision, binding, loss of
function and critical fits; nominal clearance is reported separately from the clearance remaining
after tolerances. An unknown contributor, a relevant effect without a contributor, manufacture limits
without calibrated capability, inspection or supplier evidence, limits taken from a fit designation
or general-tolerance label, or a record that disagrees with the stack make the outcome unknown,
never a pass. A zero-width contributor (a perfect dimension), a feature counted twice or in two
states, and a coating added on top of post-finish limits are refused. A justified RSS needs stable,
centred, sourced distributions, an independence statement or a positive semidefinite correlation
set, a coverage factor and an approved decision; it never governs a worst-case-governed consequence.
With --hw and --version the result publishes as hw/analysis/<V>/<case>/ when the stack is the analysis case
hw/analysis/cases/<case>/case.yaml (the bundle cad-record release.py requires for that case; the stack
`id` stays in results.json), else as hw/analysis/<V>/<id>/. An intent stack also binds the version's
record snapshot: the interface's variation,
its requirements, the parts, their route operations and inspections, and the capability profiles'
calibrations. A bound intent stack whose record version 2 snapshot expects its obligation
(interface.stack@IF of a critical interface, feature.fit@PN#feature of a critical surface) also
publishes that obligation's outcome, findings and governing results as its required-check packet at
hw/checks/<V>/stack-<case, or id lowercased>/ (cad-record check_packet, beside the analysis bundle it cites), the one
place cad-record evidence.py reads producer packets; stdout `check_bundle` names it (null when the
record expects no such obligation).

stdout: JSON result. stderr: diagnostics.
Exit 0 = requirement met by the governing method, 1 = not met or (intent stacks) not established,
2 = usage or input error.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

SKILLS = Path(__file__).resolve().parents[2]
LAYOUT_PATH = SKILLS / "cad-record" / "scripts" / "layout.py"
INTENT_PATH = SKILLS / "cad-record" / "scripts" / "manufacturing_intent.py"
SCHEMAS = SKILLS / "cad-record" / "schemas"
MANUFACTURING_SCHEMA_ID = "https://srobroek.com/cad/schemas/manufacturing.schema.json"


def _shared_module(name: str, path: Path) -> Any:
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path} not found")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


LAYOUT = _shared_module("cad_record_layout", LAYOUT_PATH)
CHECK_PACKET = _shared_module("cad_record_check_packet", SKILLS / "cad-record" / "scripts" / "check_packet.py")
# hw/checks/<V>/stack-<case, or id lowercased>/: an intent stack's packet beside its analysis bundle (a bundle.checks
# subject is a PN or a lowercase slug, and a case id or published stack id is one of those)
CHECK_PREFIX = "stack-"

METHODS = ("worst_case", "rss")
RESULT_DECIMALS = 6
PRODUCER = "cad-fits stackup.py"
RSS_QUALIFICATION = ("unqualified quadrature heuristic: no distribution, independence or coverage is established, "
                     "so this is not a probabilistic range")

INTENT_VERSION = 2
CATEGORIES = ("manufacture", "assembly", "thermal", "moisture", "wear", "deformation", "coating", "adhesive", "joining")
STATES = ("as_manufactured", "after_finish", "assembled")
CONSEQUENCES = ("collision", "binding", "loss_of_function", "critical_fit", "performance", "appearance", "other")
WORST_CASE_GOVERNED = frozenset(("collision", "binding", "loss_of_function", "critical_fit"))
GOVERNING_METHODS = ("worst_case", "justified_rss")
QUALIFYING_SOURCES = frozenset(("calibration", "test", "supplier", "datasheet", "standard"))
SHEET_CUTTING = frozenset(("laser", "waterjet", "plasma"))
FINISHING = frozenset(("drilling", "reaming", "tapping", "finishing", "coating", "joining"))
EFFECT_FINISHES = {"coating": "coating", "joining": "joining"}  # finishes counted by their own effect contributor
STACK_KEYS = frozenset(("intent_version", "id", "title", "units", "subject", "requirements", "dimension_state",
                        "consequence", "relevant", "requirement", "statistics", "contributors", "note"))
CONTRIBUTOR_KEYS = frozenset(("id", "category", "description", "status", "reason", "direction", "nominal", "tol",
                              "tol_plus", "tol_minus", "subject", "state", "source", "capability", "operation",
                              "inspection", "designation", "process", "distribution"))
REQUIREMENT_KEYS = frozenset(("min", "max", "method", "source", "id"))
STATISTICS_KEYS = frozenset(("decision", "coverage_factor", "probability", "independence", "correlations"))
DISTRIBUTION_KEYS = frozenset(("sigma", "tolerance_divisor", "centred", "stable", "source"))
CAPABILITY_KEYS = frozenset(("profile", "characteristic", "material"))
SUBJECT_KEYS = frozenset(("PN", "occurrence", "component", "feature", "region"))
DECISION = re.compile(r"^DR-[0-9]{3}$")
REQUIREMENT = re.compile(r"^REQ-[0-9]{3}$")
INTERFACE = re.compile(r"^IF-[0-9]{3}$")
PN = re.compile(r"^[A-Z]{2,5}-[0-9]{4}$")
SEMANTIC = re.compile(r"^[a-z][a-z0-9_]*$")
CONTRIBUTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
LIMITS = (
    "a 1D linear stack of the declared contributors only: 3D form, orientation and position variation are outside it",
    "analytical evidence of the declared limits, not physical validation: parts outside their limits, and the "
    "contributors not declared, are not covered",
)


class StackError(ValueError):
    """The stack definition violates the input contract."""


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StackError(f"{where} must be a finite number, got {value!r}")
    return float(value)


def _tolerance(entry: Mapping[str, Any], where: str) -> tuple[float, float]:
    if "tol" in entry:
        if "tol_plus" in entry or "tol_minus" in entry:
            raise StackError(f"{where}: give either tol or tol_plus/tol_minus, not both")
        plus = minus = _number(entry["tol"], f"{where}.tol")
    else:
        if "tol_plus" not in entry or "tol_minus" not in entry:
            raise StackError(f"{where}: needs tol, or both tol_plus and tol_minus")
        plus = _number(entry["tol_plus"], f"{where}.tol_plus")
        minus = _number(entry["tol_minus"], f"{where}.tol_minus")
    if plus < 0 or minus < 0:
        raise StackError(f"{where}: tolerances are magnitudes and must be >= 0")
    return plus, minus


def _round(value: float) -> float:
    return round(value, RESULT_DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


def _verdict(low: float, high: float, req_min: float | None, req_max: float | None) -> bool:
    return (req_min is None or low >= req_min) and (req_max is None or high <= req_max)


def compute(stack: dict[str, Any], record: Any = None, *, case: str | None = None, on: date | None = None,
            decisions: frozenset[str] | None = None) -> dict[str, Any]:
    """Evaluate a parsed stack definition; raise StackError on invalid input. An intent stack
    (`intent_version: 2`) is bound to `record` (a manufacturing_intent Record) when one is given,
    as the analysis case `case`, with calibrations judged valid `on` that date and `decisions` the
    project's recorded DR ids."""
    if not isinstance(stack, dict):
        raise StackError("stack file must be a mapping")
    if "intent_version" in stack:
        if stack["intent_version"] != INTENT_VERSION or isinstance(stack["intent_version"], bool):
            raise StackError(f"intent_version must be {INTENT_VERSION}, got {stack['intent_version']!r}")
        return _IntentStack(stack, record, case, on, decisions).evaluate()
    if record is not None:
        raise StackError("a legacy stack (no intent_version) cannot be bound to a record; add intent_version: 2")
    return _legacy(stack)


def _legacy(stack: dict[str, Any]) -> dict[str, Any]:
    contributors = stack.get("contributors")
    if not isinstance(contributors, list) or not contributors:
        raise StackError("contributors must be a non-empty list")

    nominal = mean = wc_half = sum_sq = 0.0
    rows = []
    for index, entry in enumerate(contributors):
        where = f"contributors[{index}]"
        if not isinstance(entry, dict):
            raise StackError(f"{where} must be a mapping")
        direction = entry.get("direction")
        if direction not in (1, -1) or isinstance(direction, bool):
            raise StackError(f"{where}.direction must be 1 or -1, got {direction!r}")
        value = _number(entry.get("nominal"), f"{where}.nominal")
        plus, minus = _tolerance(entry, where)
        half = (plus + minus) / 2
        centre = value + (plus - minus) / 2
        nominal += direction * value
        mean += direction * centre
        wc_half += half
        sum_sq += half * half
        rows.append(
            {
                "id": entry.get("id", str(index + 1)),
                "direction": direction,
                "nominal": value,
                "tol_plus": plus,
                "tol_minus": minus,
                "mean": _round(centre),
                "half_tolerance": _round(half),
            }
        )

    rss_half = math.sqrt(sum_sq)
    for row in rows:
        half = row["half_tolerance"]
        row["wc_contribution_pct"] = _round(100 * half / wc_half) if wc_half else 0.0
        row["rss_contribution_pct"] = _round(100 * half * half / sum_sq) if sum_sq else 0.0

    results: dict[str, dict[str, Any]] = {
        "worst_case": {"min": _round(mean - wc_half), "max": _round(mean + wc_half), "half_range": _round(wc_half)},
        "rss": {"min": _round(mean - rss_half), "max": _round(mean + rss_half), "half_range": _round(rss_half),
                "qualification": RSS_QUALIFICATION},
    }

    requirement = stack.get("requirement")
    verdict: dict[str, Any] | None = None
    if requirement is not None:
        if not isinstance(requirement, dict):
            raise StackError("requirement must be a mapping")
        req_min = None if requirement.get("min") is None else _number(requirement["min"], "requirement.min")
        req_max = None if requirement.get("max") is None else _number(requirement["max"], "requirement.max")
        if req_min is None and req_max is None:
            raise StackError("requirement needs min, max, or both")
        method = requirement.get("method", "worst_case")
        if method not in METHODS:
            raise StackError(f"requirement.method must be one of {METHODS}, got {method!r}")
        passes = {name: _verdict(r["min"], r["max"], req_min, req_max) for name, r in results.items()}
        verdict = {"min": req_min, "max": req_max, "method": method, "pass": passes[method], "pass_by_method": passes}

    return {
        "id": stack.get("id"),
        "title": stack.get("title"),
        "units": stack.get("units", "mm"),
        "nominal": _round(nominal),
        "mean": _round(mean),
        **results,
        "contributors": rows,
        "requirement": verdict,
    }


# --------------------------------------------------------------------------- intent stacks (version 2)


@cache
def _validator(definition: str) -> Any:
    """A Draft 2020-12 validator of one shared manufacturing.schema.json definition, offline."""
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - only intent stacks need it
    from referencing import Registry, Resource  # noqa: PLC0415

    resources = [(contents["$id"], Resource.from_contents(contents))
                 for contents in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMAS.glob("*.schema.json")))]
    registry = Registry().with_resources(resources)
    return Draft202012Validator({"$ref": f"{MANUFACTURING_SCHEMA_ID}#/$defs/{definition}"}, registry=registry)


def _check_shared(definition: str, value: Any, where: str) -> None:
    errors = sorted(_validator(definition).iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        raise StackError(f"{where} is not a valid {definition}: {errors[0].message}")


def _keys(entry: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise StackError(f"{where}: unknown key(s) {', '.join(map(str, unknown))}")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StackError(f"{where} must be a mapping")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StackError(f"{where} must be a non-empty string")
    return value


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug if slug and slug[0].isalpha() else f"s_{slug}"


def _intent_module() -> Any:
    return _shared_module("cad_record_manufacturing_intent", INTENT_PATH)


@dataclass
class _Contributor:
    id: str
    category: str
    known: bool
    direction: int = 1
    nominal: float = 0.0
    plus: float = 0.0
    minus: float = 0.0
    reason: str | None = None
    description: str | None = None
    subject: Mapping[str, Any] | None = None
    state: str | None = None
    source: Mapping[str, Any] | None = None
    capability: Mapping[str, Any] | None = None
    operation: str | None = None
    inspection: str | None = None
    designation: str | None = None
    process: str | None = None
    distribution: Mapping[str, Any] | None = None
    qualification: str = "unknown"
    resolved: dict[str, Any] = field(default_factory=dict)

    @property
    def half(self) -> float:
        return (self.plus + self.minus) / 2

    @property
    def centre(self) -> float:
        return self.nominal + (self.plus - self.minus) / 2

    def source_kind(self) -> str:
        """The kind of a known contributor's limits source; parsing refuses a known contributor without one."""
        if self.source is None:
            raise StackError(f"contributor {self.id} names no source of its limits")
        kind: str = self.source["kind"]
        return kind

    def feature_key(self) -> tuple[Any, ...] | None:
        subject = self.subject or {}
        if not subject.get("PN") or not subject.get("feature"):
            return None
        return (subject["PN"], subject.get("occurrence"), subject.get("component"), subject["feature"])

    def row(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "category": self.category, "status": "known" if self.known else "unknown"}
        if self.known:
            data.update(direction=self.direction, nominal=self.nominal, tol_plus=self.plus, tol_minus=self.minus,
                        mean=_round(self.centre), half_tolerance=_round(self.half), qualification=self.qualification)
        else:
            data["reason"] = self.reason
        for key in ("description", "state", "operation", "inspection", "designation", "process"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        for key in ("subject", "source", "capability"):
            if getattr(self, key) is not None:
                data[key] = json.loads(json.dumps(getattr(self, key)))
        if self.resolved:
            data["resolved"] = self.resolved
        return data


class _IntentStack:
    """One intent stack's evaluation: parse, bind to the record, then judge by the governing method."""

    def __init__(self, stack: dict[str, Any], record: Any, case: str | None, on: date | None,
                 decisions: frozenset[str] | None) -> None:
        self.stack, self.record, self.case, self.on, self.decisions = stack, record, case, on, decisions
        self.findings: list[dict[str, Any]] = []
        self.unknown_reasons: list[str] = []
        self.critical = False
        self.required_evidence: list[dict[str, Any]] = []
        self.limits = list(LIMITS)

    # ---------------------------------------------------------------- parsing

    def parse(self) -> None:
        stack = self.stack
        _keys(stack, STACK_KEYS, "stack")
        self.id = _text(stack.get("id"), "id")
        self.title = stack.get("title")
        if stack.get("units", "mm") != "mm":
            raise StackError("an intent stack is in mm")
        self.subject = _mapping(stack.get("subject"), "subject")
        _check_shared("subject", dict(self.subject), "subject")
        if bool(self.subject.get("interface")) == bool(self.subject.get("PN")):
            raise StackError("subject names exactly one interface or one PN")
        if self.subject.get("PN") and not self.subject.get("feature"):
            raise StackError("a part subject names the feature whose fit the stack judges")
        requirements = stack.get("requirements", [])
        if not isinstance(requirements, list) or not all(isinstance(r, str) and REQUIREMENT.match(r) for r in requirements):
            raise StackError("requirements must be a list of REQ-### ids")
        self.requirements = list(requirements)
        self.state = stack.get("dimension_state")
        if self.state not in STATES:
            raise StackError(f"dimension_state must be one of {', '.join(STATES)}")
        consequence = stack.get("consequence")
        if not isinstance(consequence, str) or consequence not in CONSEQUENCES:
            raise StackError(f"consequence must be one of {', '.join(CONSEQUENCES)}")
        self.consequence = consequence
        relevant = stack.get("relevant")
        if (not isinstance(relevant, list) or not relevant or len(set(relevant)) != len(relevant)
                or any(item not in CATEGORIES for item in relevant)):
            raise StackError(f"relevant must list distinct effects from {', '.join(CATEGORIES)}")
        self.relevant = list(relevant)
        self.parse_requirement(_mapping(stack.get("requirement"), "requirement"))
        statistics = stack.get("statistics")
        self.statistics = None if statistics is None else _mapping(statistics, "statistics")
        if self.statistics is not None:
            _keys(self.statistics, STATISTICS_KEYS, "statistics")
        entries = stack.get("contributors")
        if not isinstance(entries, list) or not entries:
            raise StackError("contributors must be a non-empty list")
        self.contributors = [self.parse_contributor(_mapping(entry, f"contributors[{i}]"), f"contributors[{i}]")
                             for i, entry in enumerate(entries)]
        ids = [c.id for c in self.contributors]
        if len(set(ids)) != len(ids):
            raise StackError("contributor ids must be unique")
        self.refuse_double_counting()

    def parse_requirement(self, requirement: Mapping[str, Any]) -> None:
        _keys(requirement, REQUIREMENT_KEYS, "requirement")
        self.req_min = None if requirement.get("min") is None else _number(requirement["min"], "requirement.min")
        self.req_max = None if requirement.get("max") is None else _number(requirement["max"], "requirement.max")
        if self.req_min is None and self.req_max is None:
            raise StackError("requirement needs min, max, or both")
        if self.req_min is not None and self.req_max is not None and self.req_min > self.req_max:
            raise StackError("requirement.min exceeds requirement.max")
        self.method = requirement.get("method", "worst_case")
        if self.method not in GOVERNING_METHODS:
            raise StackError(f"requirement.method must be one of {', '.join(GOVERNING_METHODS)}; legacy rss is "
                             "an unqualified heuristic, never an intent stack's governing method")
        if "source" not in requirement:
            raise StackError("requirement.source is required: where the acceptance limit comes from")
        _check_shared("source", requirement["source"], "requirement.source")
        self.req_source = requirement["source"]
        self.req_id = requirement.get("id")

    def parse_contributor(self, entry: Mapping[str, Any], where: str) -> _Contributor:
        _keys(entry, CONTRIBUTOR_KEYS, where)
        ident = entry.get("id")
        if not isinstance(ident, str) or not CONTRIBUTOR_ID.match(ident):
            raise StackError(f"{where}.id must match {CONTRIBUTOR_ID.pattern}")
        category = entry.get("category")
        if category not in CATEGORIES:
            raise StackError(f"{where}.category must be one of {', '.join(CATEGORIES)}")
        if category not in self.relevant:
            raise StackError(f"{where}: category {category} is not listed in relevant")
        status = entry.get("status", "known")
        subject = entry.get("subject")
        if subject is not None:
            subject = _mapping(subject, f"{where}.subject")
            _keys(subject, SUBJECT_KEYS, f"{where}.subject")
            _check_shared("subject", dict(subject), f"{where}.subject")
        state = entry.get("state")
        if state is not None and state not in STATES:
            raise StackError(f"{where}.state must be one of {', '.join(STATES)}")
        if category == "manufacture" and (subject is None or not subject.get("PN") or not subject.get("feature")
                                          or state is None):
            raise StackError(f"{where}: a manufacture contributor names its subject {{PN, feature}} and its state")
        common = dict(id=ident, category=category, description=entry.get("description"), subject=subject, state=state)
        if status == "unknown":
            extra = sorted(set(entry) & {"nominal", "tol", "tol_plus", "tol_minus", "direction", "source"})
            if extra:
                raise StackError(f"{where}: an unknown contributor gives its reason, not {', '.join(extra)}")
            return _Contributor(known=False, reason=_text(entry.get("reason"), f"{where}.reason"), **common)
        if status != "known":
            raise StackError(f"{where}.status must be known or unknown")
        direction = entry.get("direction")
        if direction not in (1, -1) or isinstance(direction, bool):
            raise StackError(f"{where}.direction must be 1 or -1, got {direction!r}")
        plus, minus = _tolerance(entry, where)
        if plus + minus == 0:
            raise StackError(f"{where}: zero variation claims a perfect dimension; give its qualified limits, "
                             "or its measurement uncertainty for a measured value")
        if "source" not in entry:
            raise StackError(f"{where}.source is required: where the limits come from")
        _check_shared("source", entry["source"], f"{where}.source")
        capability = entry.get("capability")
        if capability is not None:
            capability = _mapping(capability, f"{where}.capability")
            _keys(capability, CAPABILITY_KEYS, f"{where}.capability")
            for key in ("profile", "characteristic"):
                if not isinstance(capability.get(key), str) or not SEMANTIC.match(capability[key]):
                    raise StackError(f"{where}.capability.{key} must be a profile or characteristic id")
        distribution = entry.get("distribution")
        if distribution is not None:
            distribution = _mapping(distribution, f"{where}.distribution")
            _keys(distribution, DISTRIBUTION_KEYS, f"{where}.distribution")
        for key in ("operation", "inspection", "process"):
            if key in entry and (not isinstance(entry[key], str) or not SEMANTIC.match(entry[key])):
                raise StackError(f"{where}.{key} must be an id matching {SEMANTIC.pattern}")
        return _Contributor(known=True, direction=direction, nominal=_number(entry.get("nominal"), f"{where}.nominal"),
                            plus=plus, minus=minus, source=entry["source"], capability=capability,
                            operation=entry.get("operation"), inspection=entry.get("inspection"),
                            designation=None if entry.get("designation") is None else _text(entry["designation"], f"{where}.designation"),
                            process=entry.get("process"), distribution=distribution, **common)

    def refuse_double_counting(self) -> None:
        seen: dict[tuple[Any, ...], _Contributor] = {}
        for item in self.contributors:
            key = item.feature_key()
            if key is None or item.category != "manufacture":
                continue
            if key in seen:
                other = seen[key]
                states = "" if other.state == item.state else f" ({other.state} and {item.state})"
                raise StackError(f"contributors {other.id} and {item.id} both count {key[0]} {key[3]}{states}: "
                                 "a finished feature contributes its post-finish limits once, never with its "
                                 "as-manufactured size as well")
            seen[key] = item
        for item in self.contributors:
            key = item.feature_key()
            base = seen.get(key) if key is not None and item.category == "coating" else None
            if base is not None and base.state == "after_finish":
                raise StackError(f"{item.id} adds a coating to {base.id}, whose post-finish limits already "
                                 "include the finish: that counts the coating twice")

    # ---------------------------------------------------------------- findings

    def finding(self, code: str, suffix: str, *, severity: str, outcome: str, measured: Mapping[str, Any],
                consequence: str, remedy: str, uncertainty: str, subject: Mapping[str, Any] | None = None,
                criterion: Mapping[str, Any] | None = None, alternatives: list[dict[str, str]] | None = None,
                inputs: list[str] | None = None) -> str:
        ident = base = f"{_slug(self.id)}_{_slug(suffix)}"
        taken = {item["id"] for item in self.findings}
        number = 2
        while ident in taken:
            ident, number = f"{base}_{number}", number + 1
        data: dict[str, Any] = {
            "id": ident, "code": code, "subject": json.loads(json.dumps(subject or self.subject)),
            "severity": severity, "outcome": outcome, "measured": dict(measured),
            "criterion": dict(criterion) if criterion is not None else self.criterion(),
            "method": {"name": "1D linear tolerance stack", "tool": PRODUCER},
            "uncertainty": uncertainty, "consequence": consequence, "remedy": remedy,
            "recheck": {"families": ["interface.stack" if self.subject.get("interface") else "feature.fit"],
                        "earliest_stage": "detail", **({"inputs": inputs} if inputs else {})},
        }
        if self.obligation_id():
            data["obligation"] = self.obligation_id()
        if self.requirements:
            data["requirements"] = list(self.requirements)
        if alternatives:
            data["alternatives"] = alternatives
        self.findings.append(data)
        return ident

    def unknown(self, code: str, contributor: _Contributor | None, reason: str, remedy: str, *, consequence: str | None = None,
                inputs: list[str] | None = None) -> None:
        self.unknown_reasons.append(reason)
        subject = contributor.subject if contributor is not None and contributor.subject else None
        self.finding(code, f"{code.split('.')[-1]}_{contributor.id if contributor else 'stack'}",
                     severity=self.severity(), outcome="unknown", measured={"status": "unknown", "reason": reason},
                     subject=subject, consequence=consequence or "the clearance remaining after tolerances is not established",
                     remedy=remedy, uncertainty=reason, inputs=inputs)

    def severity(self) -> str:
        return "critical" if self.critical or self.consequence in WORST_CASE_GOVERNED else "major"

    def criterion(self) -> dict[str, Any]:
        value: float | list[float]
        if self.req_min is not None and self.req_max is not None:
            operator, value = "between", [self.req_min, self.req_max]
        elif self.req_min is not None:
            operator, value = ">=", self.req_min
        elif self.req_max is not None:
            operator, value = "<=", self.req_max
        else:
            raise StackError("requirement needs min, max, or both")
        data = {"property": "closing_dimension", "operator": operator, "value": value, "units": "mm",
                "state": self.state, "source": dict(self.req_source)}
        if self.req_id:
            data["id"] = self.req_id
        return data

    def obligation_id(self) -> str | None:
        if self.subject.get("interface"):
            return f"interface.stack@{self.subject['interface']}"
        anchor = self.subject["PN"] + (f".{self.subject['component']}" if self.subject.get("component") else "")
        return f"feature.fit@{anchor}#{self.subject['feature']}"

    # ---------------------------------------------------------------- qualification and binding

    def qualify(self) -> None:
        for item in self.contributors:
            if not item.known:
                self.unknown("stack.contributor_unknown", item, f"contributor {item.id} ({item.category}) is unknown: {item.reason}",
                             f"establish {item.id}'s limits from calibrated capability, inspection, supplier data or a "
                             "sourced calculation")
                continue
            if item.category != "manufacture":
                item.qualification = f"source:{item.source_kind()}"
                continue
            if item.designation is not None and item.capability is None and item.inspection is None:
                item.qualification = "designation"
                self.unknown("stack.designation_not_capability", item,
                             f"{item.id}'s limits come from {item.designation}, a desired fit or tolerance label, not "
                             "achievable precision of the process that makes it",
                             "cite the calibrated capability of the operation that makes the feature, or the inspection "
                             "that rejects parts outside these limits")
            elif item.capability is not None:
                item.qualification = "capability"
            elif item.inspection is not None:
                item.qualification = "inspection"
            elif item.source_kind() in QUALIFYING_SOURCES:
                item.qualification = f"source:{item.source_kind()}"
            else:
                item.qualification = "unqualified"
                self.unknown("stack.limits_unqualified", item,
                             f"{item.id}'s limits rest on a {item.source_kind()} source, not on calibrated capability, "
                             "inspection or supplier evidence",
                             "cite the calibrated capability, the enforcing inspection or the supplier's stated tolerance")
        present = {item.category for item in self.contributors}
        for category in self.relevant:
            if category not in present:
                self.unknown("stack.effect_missing", None, f"the relevant {category} effect has no contributor",
                             f"add a sourced {category} contributor, or an unknown one with its reason")
        for item in self.contributors:
            if item.known and item.process in SHEET_CUTTING:
                self.require_coupon(item, item.process)

    def require_coupon(self, item: _Contributor, process: str) -> None:
        """One physically_validated entry per sheet-cutting process: its contributors' as-cut fit needs a
        representative fit coupon from the approved stock; a stack interval is not that test."""
        for entry in self.required_evidence:
            if entry["process"] == process:
                if item.id not in entry["contributors"]:
                    entry["contributors"].append(item.id)
                return
        coupon: dict[str, Any] = {
            "level": "physically_validated", "process": process, "contributors": [item.id],
            "reason": (f"{process}-cut kerf, taper and stock variation decide the as-cut fit: a representative fit "
                       "coupon cut from the approved stock must confirm it; this stack interval is not that test")}
        if process == "laser":
            coupon["family"] = "process.laser.fit_coupon"
        self.required_evidence.append(coupon)

    def bind(self) -> None:
        record = self.record
        if record is None:
            self.limits.append("not bound to a record: the interface, parts, operations, inspections and capability "
                               "references are unverified")
            return
        intent = _intent_module()
        for req in self.requirements:
            if req not in record.requirements:
                self.mismatch(f"{req} is not in requirements.yaml")
        interface_id = self.subject.get("interface")
        if interface_id:
            self.bind_interface(intent, interface_id)
        elif self.subject["PN"] not in record.parts:
            self.mismatch(f"{self.subject['PN']} is not a part of the record")
        for item in self.contributors:
            if item.known and item.subject and item.subject.get("PN"):
                self.bind_contributor(intent, item, item.subject)

    def mismatch(self, reason: str) -> None:
        self.unknown("stack.record_mismatch", None, reason, "make the stack and the record agree, then rerun",
                     inputs=["hw/interfaces.yaml"])

    @staticmethod
    def interface_critical(interface: Any) -> bool:
        critical = interface.functional.critical
        return bool(critical.known and critical.value is True)

    def bind_interface(self, intent: Any, interface_id: str) -> None:
        interface = self.record.interfaces.get(interface_id)
        if interface is None:
            self.mismatch(f"{interface_id} is not in interfaces.yaml")
            return
        self.critical = self.interface_critical(interface)
        variation = interface.functional.raw.get("variation")
        if not isinstance(variation, Mapping):
            self.mismatch(f"{interface_id} declares no functional.variation naming this stack")
            return
        if self.case is not None and variation.get("stack_case") != self.case:
            self.mismatch(f"{interface_id} variation.stack_case is {variation.get('stack_case')}, not this case {self.case}")
        missing = [c for c in variation.get("contributors") or () if c not in self.relevant]
        if missing:
            self.mismatch(f"{interface_id} declares {', '.join(missing)} relevant, which the stack omits")
        if variation.get("method") != self.method:
            self.mismatch(f"{interface_id} governs by {variation.get('method')}, the stack by {self.method}")

    def check_finish_counted(self, item: _Contributor, subject: Mapping[str, Any], finishing: list[Any]) -> None:
        """An as-manufactured size of a feature the route finishes before assembly: a machining finish
        replaces it (its post-finish limits count instead), and a coating or joining finish needs its own
        contributor on the same feature, counted once."""
        pn, feature = subject["PN"], subject["feature"]
        for op in finishing:
            effect = EFFECT_FINISHES.get(op.kind)
            if effect is None:
                self.unknown("stack.state_mismatch", item,
                             f"{item.id} counts {pn} {feature} as manufactured, but {op.id} ({op.kind}) finishes it "
                             "before assembly", "use the qualified post-finish limits of the finishing operation")
                return
            counted = any(c.category == effect and c.feature_key() == item.feature_key() for c in self.contributors)
            if not counted:
                self.unknown("stack.state_mismatch", item,
                             f"{op.id} ({op.kind}) changes {pn} {feature} after it is made, and no {effect} contributor "
                             "counts it", f"add a sourced {effect} contributor on {feature}, or use post-finish limits")
                return

    def bind_contributor(self, intent: Any, item: _Contributor, subject: Mapping[str, Any]) -> None:
        pn = subject["PN"]
        part = self.record.parts.get(pn)
        if part is None:
            self.unknown("stack.record_mismatch", item, f"{item.id}: {pn} is not a part of the record",
                         "name a recorded part")
            return
        route = () if intent.is_fact(part.intent.route) else part.intent.route
        operations = {op.id: op for op in route}
        feature = subject.get("feature")
        if item.category == "manufacture":
            finishing = [op for op in route if op.kind in FINISHING
                         and (not op.targets or any(t.get("feature") == feature for t in op.targets))]
            if item.operation is not None and item.operation not in operations:
                self.unknown("stack.record_mismatch", item, f"{item.id}: {pn} has no route operation {item.operation}",
                             "name the operation that produces the dimension")
            if item.state == "as_manufactured" and self.state != "as_manufactured":
                self.check_finish_counted(item, subject, finishing)
            if item.state == "after_finish" and not finishing:
                self.unknown("stack.state_mismatch", item,
                             f"{item.id} counts {pn} {feature} after finishing, but no finishing operation of the "
                             "route targets it", "record the finishing operation, or use the as-manufactured limits")
            producer = operations.get(item.operation) if item.operation else None
            if producer is not None and item.process is None and producer.kind in SHEET_CUTTING:
                self.require_coupon(item, producer.kind)
        if item.inspection is not None:
            inspections = {i.get("id"): i for op in route for i in op.inspections}
            found = inspections.get(item.inspection)
            if found is None:
                self.unknown("stack.record_mismatch", item, f"{item.id}: {pn}'s route has no inspection {item.inspection}",
                             "name the inspection that enforces these limits")
            elif found.get("feature") not in (None, feature) or found.get("state") != item.state:
                self.unknown("stack.record_mismatch", item,
                             f"{item.id}: inspection {item.inspection} checks {found.get('feature')} {found.get('state')}, "
                             f"not {feature} {item.state}", "name the inspection of this feature in this state")
        if item.capability is not None:
            self.bind_capability(intent, item, item.capability, part, operations)

    def bind_capability(self, intent: Any, item: _Contributor, capability: Mapping[str, Any], part: Any,
                        operations: Mapping[str, Any]) -> None:
        profile_id = capability["profile"]
        profile = self.record.capabilities.get(profile_id)
        if profile is None:
            self.unknown("stack.capability_unresolved", item, f"{item.id}: capability profile {profile_id} is not recorded",
                         "record the capability profile and its calibration")
            return
        if item.process is None and profile.process in SHEET_CUTTING:
            self.require_coupon(item, profile.process)
        material = capability.get("material")
        if material is None:
            material = part.intent.material_profile if isinstance(part.intent.material_profile, str) else None
        if material is None:
            self.unknown("stack.capability_unresolved", item, f"{item.id}: {part.pn}'s material profile is unknown, so no "
                                                             "calibration can apply", "record the part's material profile")
            return
        producer = operations.get(item.operation) if item.operation else None
        if producer is not None and producer.capability != profile_id:
            self.unknown("stack.record_mismatch", item, f"{item.id}: operation {producer.id} uses {producer.capability}, "
                                                       f"not {profile_id}", "cite the capability of the operation")
        resolution = intent.capability_limit(profile, capability["characteristic"], material=material, on=self.on,
                                             feature=None)
        if resolution.status != "calibrated":
            self.unknown("stack.capability_unresolved", item,
                         f"{item.id}: {profile_id} {capability['characteristic']} is {resolution.status}: "
                         + "; ".join(resolution.reasons), "calibrate the capability for this material and scope")
            return
        calibration = resolution.calibration
        limits = calibration.limits
        item.resolved = {"calibration": calibration.id, "limits": {"lower": limits.lower, "upper": limits.upper,
                                                                    "value": limits.value, "units": limits.units}}
        if capability["characteristic"].endswith("deviation") and limits.lower is not None:
            low, high = intent.convert(limits.lower, limits.units, "mm"), intent.convert(limits.upper, limits.units, "mm")
            if low < -item.minus - 1e-12 or high > item.plus + 1e-12:
                self.unknown("stack.limits_unqualified", item,
                             f"{item.id}'s limits -{item.minus}/+{item.plus} mm are tighter than calibration "
                             f"{calibration.id}'s demonstrated deviation {low}..{high} mm",
                             "use the demonstrated limits, or a finishing operation and inspection that achieve them")

    # ---------------------------------------------------------------- statistics

    def statistical(self, mean: float) -> dict[str, Any]:
        requested = self.method == "justified_rss" or self.statistics is not None
        if not requested:
            return {"status": "not_requested"}
        reasons: list[str] = []
        stats = self.statistics or {}
        if not stats:
            reasons.append("no statistics block: distributions, independence, coverage and approval are not established")
        if not (isinstance(stats.get("decision"), str) and DECISION.match(stats["decision"])):
            reasons.append("statistics.decision must name the approved DR-### that judges the statistics suitable")
        elif self.decisions is not None and stats["decision"] not in self.decisions:
            reasons.append(f"{stats['decision']} is not a recorded decision")
        k = stats.get("coverage_factor")
        if isinstance(k, bool) or not isinstance(k, (int, float)) or not math.isfinite(k) or k <= 0:
            reasons.append("statistics.coverage_factor must be a positive number")
            k = None
        probability = stats.get("probability")
        if probability is not None and (isinstance(probability, bool) or not isinstance(probability, (int, float))
                                        or not 0 < probability < 1):
            reasons.append("statistics.probability must lie in (0, 1)")
        if "independence" not in stats:
            reasons.append("statistics.independence must source why uncorrelated contributors are independent")
        else:
            try:
                _check_shared("source", stats["independence"], "statistics.independence")
            except StackError as error:
                reasons.append(str(error))
        known = [c for c in self.contributors if c.known]
        if len(known) != len(self.contributors):
            reasons.append("an unknown contributor has no distribution")
        sigmas: dict[str, float] = {}
        for item in known:
            sigma = self.sigma(item, reasons)
            if sigma is not None:
                sigmas[item.id] = sigma
        rho = self.correlations(stats.get("correlations"), {c.id for c in known}, reasons)
        if reasons or k is None:
            return {"status": "refused", "reasons": reasons}
        ids = [c.id for c in known]
        weights = [c.direction * sigmas[c.id] for c in known]
        variance = sum(weights[i] * weights[j] * (1.0 if i == j else rho.get(frozenset((ids[i], ids[j])), 0.0))
                       for i in range(len(ids)) for j in range(len(ids)))
        if not _positive_semidefinite(ids, rho):
            return {"status": "refused", "reasons": ["the correlation set is not positive semidefinite"]}
        sigma_total = math.sqrt(max(variance, 0.0))
        data = {"status": "justified", "mean": _round(mean), "sigma": _round(sigma_total), "coverage_factor": float(k),
                "min": _round(mean - k * sigma_total), "max": _round(mean + k * sigma_total),
                "decision": stats["decision"], "correlated_pairs": len(rho)}
        if probability is not None:
            data["probability"] = probability
        return data

    def sigma(self, item: _Contributor, reasons: list[str]) -> float | None:
        dist = item.distribution
        if dist is None:
            reasons.append(f"{item.id} has no distribution")
            return None
        if dist.get("centred") is not True:
            reasons.append(f"{item.id}: only distributions centred on mid-limit are supported")
        if dist.get("stable") is not True:
            reasons.append(f"{item.id}: the process is not shown stable")
        try:
            _check_shared("source", dist.get("source"), f"{item.id}.distribution.source")
        except StackError as error:
            reasons.append(str(error))
            return None
        if ("sigma" in dist) == ("tolerance_divisor" in dist):
            reasons.append(f"{item.id}: give a measured sigma or a justified tolerance_divisor, exactly one")
            return None
        if "sigma" in dist:
            value = dist["sigma"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                reasons.append(f"{item.id}: sigma must be a positive number in mm")
                return None
            if dist["source"].get("kind") not in ("calibration", "test", "supplier"):
                reasons.append(f"{item.id}: a sigma needs measured calibration, test or supplier data")
            return float(value)
        divisor = dist["tolerance_divisor"]
        if isinstance(divisor, bool) or not isinstance(divisor, (int, float)) or not math.isfinite(divisor) or divisor <= 0:
            reasons.append(f"{item.id}: tolerance_divisor must be a positive number")
            return None
        return item.half / float(divisor)

    @staticmethod
    def correlations(entries: Any, ids: set[str], reasons: list[str]) -> dict[frozenset[str], float]:
        found: dict[frozenset[str], float] = {}
        if entries is None:
            return found
        if not isinstance(entries, list):
            reasons.append("statistics.correlations must be a list")
            return found
        for index, entry in enumerate(entries):
            pair = entry.get("between") if isinstance(entry, Mapping) else None
            rho = entry.get("rho") if isinstance(entry, Mapping) else None
            if (not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1] or not set(pair) <= ids
                    or isinstance(rho, bool) or not isinstance(rho, (int, float)) or not -1 <= rho <= 1):
                reasons.append(f"statistics.correlations[{index}] needs two known contributor ids and rho in [-1, 1]")
                continue
            key = frozenset(pair)
            if key in found:
                reasons.append(f"statistics.correlations[{index}] repeats {pair[0]} / {pair[1]}")
            found[key] = float(rho)
        return found

    # ---------------------------------------------------------------- evaluation

    def evaluate(self) -> dict[str, Any]:
        self.parse()
        if self.record is not None and self.subject.get("interface") in self.record.interfaces:
            self.critical = self.interface_critical(self.record.interfaces[self.subject["interface"]])
        self.qualify()
        self.bind()
        known = [c for c in self.contributors if c.known]
        nominal = sum(c.direction * c.nominal for c in known)
        mean = sum(c.direction * c.centre for c in known)
        half = sum(c.half for c in known)
        complete = len(known) == len(self.contributors)
        worst: dict[str, Any] = {"min": _round(mean - half), "max": _round(mean + half), "half_range": _round(half),
                                 "mean": _round(mean)}
        if not complete:
            worst = {"status": "unknown", "unknown_contributors": [c.id for c in self.contributors if not c.known],
                     "known_only": worst}
        else:
            worst["status"] = "known"
        for item in known:
            item.resolved.setdefault("wc_contribution_pct", _round(100 * item.half / half) if half else 0.0)
        statistical: dict[str, Any] = self.statistical(mean) if complete else (
            {"status": "refused", "reasons": ["an unknown contributor has no distribution"]}
            if self.method == "justified_rss" or self.statistics is not None else {"status": "not_requested"})
        governing = "worst_case"
        if self.method == "justified_rss":
            if statistical["status"] != "justified":
                self.rss_refused(statistical.get("reasons", []), worst)
            elif self.consequence in WORST_CASE_GOVERNED:
                self.rss_refused([f"a {self.consequence} consequence is governed by the worst case: a statistical "
                                  "interval never permits it"], worst)
            else:
                governing = "justified_rss"
        nominal_pass = _verdict(nominal, nominal, self.req_min, self.req_max) if complete else None
        wc_pass = _verdict(worst["min"], worst["max"], self.req_min, self.req_max) if complete else None
        stat_pass = (_verdict(statistical["min"], statistical["max"], self.req_min, self.req_max)
                     if statistical["status"] == "justified" else None)
        if self.unknown_reasons:
            outcome = "unknown"
        else:
            passed = wc_pass if governing == "worst_case" else stat_pass
            outcome = "pass" if passed else "fail"
        if outcome == "fail":
            self.violation(nominal, nominal_pass, worst, statistical, governing)
        elif complete and wc_pass is False and governing == "justified_rss":
            self.limits.append(f"the worst case {worst['min']}..{worst['max']} mm violates the requirement; the "
                               f"approved statistical interval governs this {self.consequence} consequence")
        obligation = self.obligation_id()
        coverage = (f"declared contributors {', '.join(c.id for c in self.contributors)} over effects "
                    f"{', '.join(self.relevant)}; " + "; ".join(LIMITS))
        outcome_row = {"obligation": obligation, "outcome": outcome, "level": "process_planned",
                       "method": "1D worst-case tolerance stack" if governing == "worst_case"
                       else "1D justified statistical tolerance stack",
                       "coverage": coverage, "findings": [f["id"] for f in self.findings]}
        if self.unknown_reasons:
            outcome_row["uncertainty"] = "; ".join(self.unknown_reasons)
        if not outcome_row["findings"]:
            del outcome_row["findings"]
        return {
            "intent_version": INTENT_VERSION, "id": self.id, "title": self.title, "units": "mm",
            "subject": json.loads(json.dumps(self.subject)), "requirements": self.requirements,
            "dimension_state": self.state, "consequence": self.consequence, "relevant": self.relevant,
            "critical": self.critical, "record_bound": self.record is not None, "case": self.case,
            "nominal": _round(nominal) if complete else None,
            "remaining": {"nominal": _round(nominal) if complete else None,
                          "after_tolerances": None if not complete else {"min": worst["min"], "max": worst["max"]}},
            "worst_case": worst, "statistical": statistical,
            "contributors": [c.row() for c in self.contributors],
            "requirement": {"min": self.req_min, "max": self.req_max, "method": self.method, "governing": governing,
                            "nominal_pass": nominal_pass, "worst_case_pass": wc_pass, "statistical_pass": stat_pass,
                            "pass": outcome == "pass", "criterion": self.criterion()},
            "outcome": outcome, "obligations": [outcome_row], "findings": self.findings,
            "required_evidence": self.required_evidence, "limits": self.limits,
            "evaluated_on": None if self.on is None else self.on.isoformat(),
        }

    def rss_refused(self, reasons: list[str], worst: Mapping[str, Any]) -> None:
        self.limits.append("justified RSS refused: " + "; ".join(reasons) + "; the worst case governs")
        if worst.get("status") == "known" and _verdict(worst["min"], worst["max"], self.req_min, self.req_max):
            return  # the worst case meets the requirement by itself; nothing rested on the statistics
        self.finding("stack.rss_refused", "rss_refused", severity=self.severity(),
                     outcome="unknown" if worst.get("status") != "known" else "fail",
                     measured={"text": "; ".join(reasons) or "statistics not established"},
                     consequence="no statistical acceptance: the worst case governs and does not meet the requirement",
                     remedy="meet the requirement by worst case, or establish measured stable centred distributions, "
                            "independence or correlations, a coverage factor and an approved decision",
                     uncertainty="the statistical assumptions are not justified",
                     alternatives=[{"text": "redesign the chain so the worst case clears",
                                    "tradeoffs": "tighter or fewer contributors cost capability, finishing or parts"},
                                   {"text": "inspect the critical contributors and reject parts outside the limits",
                                    "tradeoffs": "inspection effort and scrap; the stack then rests on the inspected limits"}])

    def violation(self, nominal: float, nominal_pass: bool | None, worst: Mapping[str, Any],
                  statistical: Mapping[str, Any], governing: str) -> None:
        interval = worst if governing == "worst_case" else statistical
        top = sorted((c for c in self.contributors if c.known), key=lambda c: c.half, reverse=True)[:3]
        shares = ", ".join(f"{c.id} {c.resolved.get('wc_contribution_pct', 0.0)}%" for c in top)
        lead = (f"nominal {_round(nominal)} mm meets the requirement, but " if nominal_pass else
                f"nominal {_round(nominal)} mm, and ")
        consequence = {"collision": "parts collide", "binding": "the interface binds",
                       "loss_of_function": "the function is lost", "critical_fit": "the critical fit is not achieved"}
        self.finding("stack.worst_case_violation" if governing == "worst_case" else "stack.statistical_violation",
                     "violation", severity=self.severity(), outcome="fail",
                     measured={"lower": interval["min"], "upper": interval["max"], "units": "mm"},
                     consequence=(lead + f"the {governing.replace('_', ' ')} interval {interval['min']}..{interval['max']} mm "
                                  f"does not: {consequence.get(self.consequence, 'the requirement is not met')} at the "
                                  "tolerance extremes"),
                     remedy=f"reduce the largest contributors ({shares}) or change the nominal chain; then rerun the stack",
                     uncertainty="the declared contributor limits; variation outside them is not covered",
                     alternatives=[{"text": "change nominal dimensions to recentre the interval",
                                    "tradeoffs": "moves the other limit; recheck the opposite requirement"},
                                   {"text": "qualify a finishing operation with inspection for the dominant feature",
                                    "tradeoffs": "adds an operation, allowance and inspection"},
                                   {"text": "add an adjustment or a selective-fit step at assembly",
                                    "tradeoffs": "assembly effort and a recorded adjustment procedure"}])


def _positive_semidefinite(ids: list[str], rho: Mapping[frozenset[str], float]) -> bool:
    """Whether the correlation matrix is positive semidefinite (Cholesky with a small tolerance)."""
    n = len(ids)
    matrix = [[1.0 if i == j else rho.get(frozenset((ids[i], ids[j])), 0.0) for j in range(n)] for i in range(n)]
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = matrix[i][j] - sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if total < -1e-9:
                    return False
                lower[i][i] = math.sqrt(max(total, 0.0))
            elif lower[j][j] > 1e-12:
                lower[i][j] = total / lower[j][j]
            elif abs(total) > 1e-9:
                return False
    return True


# --------------------------------------------------------------------------- command line


def load_stack(path: Path) -> Any:
    import yaml  # imported here so `compute` stays importable without the script environment

    with path.open(encoding="utf-8") as handle:
        try:
            return yaml.safe_load(handle)
        except yaml.YAMLError as error:
            raise StackError(f"invalid YAML: {error}") from error


def _scratch_hw(path: Path) -> Path:
    """Locate the project anchor of a standalone .cad-work output for layout.scratch_path."""
    parts = path.resolve().parts
    index = parts.index(".cad-work") if ".cad-work" in parts else len(parts) - 1
    return Path(*parts[:index]) / "hw"


def recorded_decisions(hw: Path) -> frozenset[str]:
    """The DR ids with a decision record under hw/decisions/."""
    return frozenset(path.stem for path in (Path(hw) / "decisions").glob("DR-[0-9][0-9][0-9].md") if path.is_file())


def status_of(result: Mapping[str, Any]) -> str:
    if "outcome" in result:
        outcome: str = result["outcome"]
        return outcome
    requirement = result["requirement"]
    return "fail" if requirement is not None and not requirement["pass"] else "pass"


def check_packet(result: Mapping[str, Any], record: Any, version: str,
                 dependencies: Mapping[str, str]) -> dict[str, Any] | None:
    """The required-check packet of an intent stack bound to a record version 2 snapshot: the outcome of the
    interface.stack or feature.fit obligation it judges, as its governing method judged it (unknown stays
    unknown), and that obligation's findings. None for a legacy stack or record, or when the record expects
    no such obligation (an interface that is not critical, a feature that is not a critical surface)."""
    intent = _intent_module()
    if record is None or record.version != intent.RECORD_VERSION or "obligations" not in result:
        return None
    judged = {row["obligation"] for row in result["obligations"]}
    expected = [o for o in intent.required_checks(record) if o.id in judged]
    if not expected:
        return None
    ids = {o.id for o in expected}
    outcomes = [dict(row) for row in result["obligations"] if row["obligation"] in ids]
    findings = [dict(item) for item in result["findings"] if item.get("obligation") in ids]
    packet: dict[str, Any] = CHECK_PACKET.build(record, expected, PRODUCER, version, outcomes, findings, dependencies)
    return packet


def publish_check(hw: Path, version: str, subject: str, result: Mapping[str, Any], packet: Mapping[str, Any],
                  replace: bool) -> str:
    """Seal `packet` as hw/checks/<V>/stack-<subject, lowercased>/ beside the published (or reused) analysis
    bundle `result["bundle"]` of that subject, with that bundle and the governing results as its evidence; reused while it holds
    the packet of that very analysis bundle. Its project-relative path."""
    bundle = Path(result["bundle"])
    analysis = {"bundle": LAYOUT.relative(hw, bundle),
                "manifest_sha256": hashlib.sha256((bundle / LAYOUT.MANIFEST).read_bytes()).hexdigest()}
    detail = {"analysis": analysis, "worst_case": result["worst_case"], "statistical": result["statistical"],
              "requirement": result["requirement"]}
    output, _ = CHECK_PACKET.emit(LAYOUT, hw, version, CHECK_PREFIX + subject.lower(), PRODUCER, packet, detail,
                                  "analysis.json", {"analysis": analysis}, out=None, replace=replace,
                                  record_version=_intent_module().RECORD_VERSION)
    path: str = LAYOUT.relative(hw, Path(output["bundle"]))
    return path


def packet_dependencies(hw: Path, stack_path: Path, stack: Mapping[str, Any]) -> dict[str, str]:
    """sha256 of the inputs beyond the version's record snapshot: the stack case and, for statistics,
    the decision that approves them."""
    found = {"stack_case": hashlib.sha256(stack_path.read_bytes()).hexdigest()}
    statistics = stack.get("statistics")
    decision = statistics.get("decision") if isinstance(statistics, Mapping) else None
    if isinstance(decision, str) and DECISION.match(decision):
        path = Path(hw) / "decisions" / f"{decision}.md"
        if path.is_file():
            found[f"decision_{decision.lower().replace('-', '_')}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1D worst-case tolerance stack-up; statistics only where justified")
    parser.add_argument("stack", type=Path, help="stack definition YAML")
    parser.add_argument("--hw", type=Path, help="hardware project hw/; required with --version")
    parser.add_argument("--version", help="built version to bind into hw/analysis/vN/<case or stack-id>/")
    parser.add_argument("--replace", action="store_true", help="retain and replace a different recipe")
    parser.add_argument("--out", type=Path, help="scratch result JSON path, only under .cad-work")
    args = parser.parse_args(argv)
    if bool(args.hw) != bool(args.version):
        parser.error("--hw and --version must be provided together")
    try:
        stack_path = args.stack.resolve()
        stack = load_stack(stack_path)
        record = case = decisions = None
        if args.hw:
            version, _, record_dir = LAYOUT.built_version(args.hw, args.version)
            # a stack at hw/analysis/cases/<case>/case.yaml is that analysis case: it publishes as the case
            case = LAYOUT.analysis_case(args.hw.resolve(), stack_path)
            if isinstance(stack, dict) and "intent_version" in stack:
                record = _intent_module().load_record(record_dir)
                decisions = recorded_decisions(args.hw)
        result = compute(stack, record, case=case, on=date.today() if record is not None else None, decisions=decisions)
        status = status_of(result)
        if args.hw:
            result["status"] = status
            stack_id = result.get("id")
            if not isinstance(stack_id, str) or not stack_id:
                raise StackError("stack id is required for versioned publication")
            subject = case if case is not None else stack_id
            params = {"V": version, "S": subject}
            if args.out is not None:
                raise StackError("--out is only allowed for standalone scratch output")
            options: dict[str, Any] = {"result_decimals": RESULT_DECIMALS,
                                       "method": (stack.get("requirement") or {}).get("method", "worst_case")}
            if "intent_version" in stack:
                options.update(intent_version=INTENT_VERSION, case=case)
            recipe = {"source_yaml_sha256": hashlib.sha256(stack_path.read_bytes()).hexdigest(), "options": options}
            reused = LAYOUT.reusable(args.hw, "bundle.analysis", params, recipe, replace=args.replace)
            if reused is not None:
                result = json.loads((reused / "results.json").read_text(encoding="utf-8"))
                result["bundle"] = str(reused)
                result["reused"] = True
                status = result["status"]
            else:
                stage = LAYOUT.staging(args.hw, "bundle.analysis")
                output = stage / "results.json"
                output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                published = LAYOUT.publish(args.hw, "bundle.analysis", params, stage,
                                           recipe=recipe, producer=PRODUCER, status=status,
                                           replace=args.replace)
                result["bundle"] = str(published.path)
                result["reused"] = published.reused
            if record is not None:
                packet = check_packet(result, record, version, packet_dependencies(args.hw, stack_path, stack))
                result["check_bundle"] = (None if packet is None else
                                          publish_check(args.hw, version, subject, result, packet, args.replace))
        elif args.out is not None:
            target = LAYOUT.scratch_path(_scratch_hw(args.out), args.out, "--out")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if status != "pass":
            governing = result["requirement"].get("governing", result["requirement"].get("method"))
            print(f"stackup: requirement {'not met' if status == 'fail' else 'not established'} ({governing})",
                  file=sys.stderr)
        return 0 if status == "pass" else 1
    except OSError as error:
        print(f"stackup: cannot read {args.stack}: {error}", file=sys.stderr)
        return 2
    except (StackError, LAYOUT.LayoutError, CHECK_PACKET.PacketError) as error:
        print(f"stackup: {args.stack}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
