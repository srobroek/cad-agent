#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Assess a design record's use case and compare its material, process and architecture candidates.

Read-only: it reads a record version 2 hw/ and reports; it never writes the design record. A
selection becomes a decision only when the lead records it in hw/manufacturing/selection.yaml with an
approving decision (status confirmed), through the existing approval gate.

The order of judgement, per decision (the candidates that share a part):
1. The use case comes first. manufacturing_intent.assess_use_case says whether a final choice may be
   made; while facts that could change the choice or create a safety risk are missing, the report stays
   conditional and names them.
2. Hard constraints eliminate before anything ranks. A mandatory requirement's structured acceptance
   criteria are computed from the candidate's own material profile when the criterion is a material
   property (under the route's form, the use case's operating temperature and, for production and
   safety-critical service, a design allowable), and otherwise come from an authored evaluation that
   states the same criterion. The route's capability profiles and material approvals and, for a
   transparent or optical part, the optical route are hard constraints too: only a purchased
   transparent part or identified transparent stock on an approved route meets an optical requirement,
   so additive (opaque) processes are eliminated. A fail eliminates; an unknown keeps a candidate
   conditional; neither is ever a pass.
3. Preferences rank feasible candidates first, then conditional ones, never eliminated ones, by the
   weighted scores over the preferences every candidate of the tier was scored on.
4. The proposal names a primary, a fallback, an end-use route once the use case allows a final choice
   and, when a cheaper candidate may serve as one, a prototype route with what it can and cannot
   validate, plus change triggers and limits. selection.yaml's recorded assessment and recommendation
   are checked against it: a stale input digest, a final choice the facts do not support, or a
   recommended candidate a mandatory constraint eliminates.

Usage: selection.py <project or hw> [--json] [--out .cad-work/<report>.json]
Exit status: 0 every decision may be confirmed final; 1 conditional, not assessed, or blocked (a
decision with no surviving candidate, or a recorded selection the assessment contradicts); 2 no valid
record, an output path outside .cad-work, or an infrastructure error.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent


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
layout = _load_module("cad_record_layout", SCRIPTS / "layout.py")

REPORT_VERSION = 1
FEASIBLE, CONDITIONAL, ELIMINATED = "feasible", "conditional", "eliminated"
# Service classes whose stress acceptance needs a justified design allowable; a typical value cannot pass it.
ALLOWABLE_SERVICE = frozenset({"production", "safety_critical"})
# Mechanical properties that change with temperature: they resolve only at the use case's operating
# temperature when it is known. Temperatures themselves (heat deflection, glass transition) do not.
_TEMPERATURE_QUALIFIED = frozenset(
    name for name, dimensions in mi.MATERIAL_PROPERTIES.items()
    if {"stress", "energy_per_area", "energy_per_length"} & set(dimensions)) | {"elongation_at_yield", "elongation_at_break"}
# The material form whose properties describe a part shaped by a route kind; other kinds take stock.
_FORM_OF_KIND = {"fdm": "printed", "resin": "printed", "casting": "cast", "moulding": "moulded"}
PROPERTY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("strength", ("yield_strength", "tensile_strength", "flexural_strength", "compressive_strength", "shear_strength",
                  "interlayer_strength", "elongation_at_yield", "elongation_at_break", "impact_strength_charpy_notched",
                  "impact_strength_charpy_unnotched", "impact_strength_izod_notched")),
    ("stiffness", ("youngs_modulus", "flexural_modulus", "shear_modulus", "poisson_ratio")),
    ("fatigue", ("fatigue_strength",)),
    ("creep", ("creep_modulus",)),
    ("heat", ("heat_deflection_temperature", "vicat_softening_temperature", "glass_transition_temperature",
              "melting_temperature", "max_service_temperature", "thermal_conductivity")),
    ("wear", ("coefficient_of_friction", "hardness_shore_d", "hardness_rockwell", "hardness_brinell")),
    ("stability", ("water_absorption", "moisture_absorption_equilibrium", "shrinkage", "thermal_expansion")),
    ("optical", ("light_transmission", "haze", "refractive_index")),
    ("mass", ("density",)),
)
SERVICE_GROUPS = ("strength", "stiffness", "fatigue", "creep", "heat", "wear")
# Route families, so a report can name the credible alternatives no candidate covers yet.
ROUTE_FAMILIES = {"fdm": "additive", "resin": "additive", "cnc": "machined", "laser": "sheet", "waterjet": "sheet",
                  "plasma": "sheet", "sheet_forming": "sheet", "casting": "formed", "moulding": "formed",
                  "extrusion": "formed", "purchased": "purchased"}
_FAMILY_ALTERNATIVES = {
    "additive": ("a printed polymer route on a calibrated printer",
                 "lowest tooling cost; printed properties are orientation dependent and need coupons and service evidence"),
    "machined": ("a machined metal or engineering-polymer grade (CNC)",
                 "wrought or bulk properties and calibrated accuracy; higher part cost and set-up, needs workholding and tool access"),
    "sheet": ("a sheet construction: laser, waterjet or plasma cut, formed and joined",
              "approved stock and thickness, cheap at volume; adds bends, joints and folded tolerance stacks"),
    "formed": ("a cast, moulded or extruded route",
               "tooling cost amortised over quantity; draft, parting and shrink rules and secondary machining"),
    "purchased": ("a standard purchased component with identified ratings",
                  "catalogue ratings replace made-part checks; adds an interface, a supplier and availability risk"),
    "hybrid": ("a hybrid construction, such as a printed or sheet housing with purchased or machined wear and load parts",
               "each part gets the process its function needs; adds interfaces, fits and assembly operations"),
}
COLUMNS: tuple[tuple[str, str], ...] = (
    ("candidate", "Candidate"),
    ("material_process", "Material family/grade and process"),
    ("suitability", "Suitability for the intended use"),
    ("service_properties", "Strength, stiffness, fatigue, creep, heat and wear"),
    ("accuracy_finish_stability", "Accuracy, surface finish and dimensional stability"),
    ("equipment_finishing_inspection", "Equipment, finishing and inspection"),
    ("design_changes_interfaces", "Design changes and assembly interfaces"),
    ("cost_lead_time", "Cost and lead time at quantity"),
    ("maintenance", "Maintenance, repair and replacement"),
    ("evidence", "Evidence confidence and outstanding validation"),
    ("pros", "Pros"),
    ("cons", "Cons"),
    ("tradeoffs", "Accepted tradeoffs"),
    ("preference", "Preference ranking"),
)
APPROVAL_GATE = ("a proposal only: it becomes a decision when the lead records it in hw/manufacturing/selection.yaml "
                 "(status confirmed, recommendation status final, assessment input_sha256 and decision) through the approval gate")
# The record inputs a selection reads, digested so a recorded assessment can be checked for staleness.
DIGEST_SCOPE = ("product.yaml use_case", "requirements.yaml", "interfaces.yaml",
                "parts/*/part.yaml PN, make_buy, procurement, process, material, transparency and intent",
                "manufacturing/capabilities.yaml", "manufacturing/materials.yaml", "manufacturing/selection.yaml candidates")
_PART_FIELDS = ("PN", "make_buy", "procurement", "process", "material", "transparency", "intent")


# --- inputs ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """One route step of a candidate part: its kind, capability profile id (or a Fact) and provider."""

    kind: str
    capability: Any  # a capability profile id, or a Fact (unknown when the step records none)
    provider: str | None

    def describe(self) -> str:
        capability = self.capability if isinstance(self.capability, str) else f"capability {self.capability.status}"
        return f"{self.kind} [{capability}{', ' + self.provider if self.provider else ''}]"


@dataclass(frozen=True)
class CandidatePart:
    pn: str
    material: Any  # a material profile id, or a Fact (unknown when the part records none)
    route: tuple[Step, ...]
    note: str | None

    @property
    def shaping_kind(self) -> str | None:
        """The kind that gives the part its shape: the first primary kind; purchased when it is only bought."""
        for step in self.route:
            if step.kind not in mi.SECONDARY_KINDS and step.kind != "purchased":
                return step.kind
        if any(step.kind == "purchased" for step in self.route):
            return "purchased"
        return self.route[0].kind if self.route else None

    @property
    def material_label(self) -> str:
        return self.material if isinstance(self.material, str) else f"material {self.material.status.replace('_', ' ')}"


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str | None
    architecture: str | None
    parts: tuple[CandidatePart, ...]
    interfaces: tuple[str, ...]
    authored: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]

    @property
    def pns(self) -> tuple[str, ...]:
        return tuple(part.pn for part in self.parts)

    def part(self, pn: str) -> CandidatePart | None:
        return next((part for part in self.parts if part.pn == pn), None)

    def fact(self, key: str) -> Any:
        """The candidate's cost, quantity, lead_time or maintenance Fact; unknown when not recorded."""
        data = self.raw.get(key)
        return mi.fact(data) if isinstance(data, Mapping) else mi.Fact.unknown_because(f"the candidate records no {key}")

    def texts(self, key: str) -> tuple[str, ...]:
        return tuple(self.raw.get(key) or ())


def _open_choice(value: Any, what: str) -> Any:
    """A candidate's profile id as itself and a recorded fact as its Fact. Anything else (absent, or not a fact,
    which the schema reports) is unknown, so the assessment stays conditional on it, never feasible or a crash."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        try:
            return mi.fact(value)
        except mi.IntentError as error:
            return mi.Fact.unknown_because(f"the candidate's {what} is not a recorded fact: {error}")
    return mi.Fact.unknown_because(f"the candidate records no {what}")


def _candidate(ident: str, raw: Mapping[str, Any]) -> Candidate:
    parts = []
    for entry in raw.get("parts") or ():
        steps = [Step(step.get("kind"), _open_choice(step.get("capability"), "capability"), step.get("provider"))
                 for step in entry.get("route") or ()]
        parts.append(CandidatePart(entry.get("PN"), _open_choice(entry.get("material_profile"), "material profile"),
                                   tuple(steps), entry.get("note")))
    return Candidate(ident, raw.get("title"), raw.get("architecture"), tuple(parts), tuple(raw.get("interfaces") or ()),
                     tuple(raw.get("evaluations") or ()), raw)


def input_digest(documents: Mapping[str, Any]) -> str:
    """The sha256 of the record inputs a selection reads (DIGEST_SCOPE), as canonical JSON of the parsed
    documents, so comments and formatting do not count. selection.yaml assessment.input_sha256 records it."""
    scoped: dict[str, Any] = {}
    product = documents.get("product.yaml")
    if isinstance(product, Mapping):
        scoped["product.yaml#use_case"] = product.get("use_case")
    for rel in ("requirements.yaml", "interfaces.yaml", "manufacturing/capabilities.yaml", "manufacturing/materials.yaml"):
        if rel in documents:
            scoped[rel] = documents[rel]
    selection = documents.get("manufacturing/selection.yaml")
    if isinstance(selection, Mapping):
        scoped["manufacturing/selection.yaml#candidates"] = selection.get("candidates")
    for rel, data in documents.items():
        if re.fullmatch(r"parts/[^/]+/part\.yaml", rel) and isinstance(data, Mapping):
            scoped[rel] = {key: data.get(key) for key in _PART_FIELDS}
    text = json.dumps(scoped, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- evaluations -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Evaluation:
    """One hard-constraint or requirement result of one candidate: pass, fail, unknown or not_applicable."""

    constraint: str  # a REQ id, or material_form, capability, material_approval, process_coverage, optical_route, part
    mandatory: bool
    outcome: str
    subject: Mapping[str, Any]
    method: str
    reasons: tuple[str, ...] = ()
    criterion: Mapping[str, Any] | None = None
    measured: Mapping[str, Any] | None = None
    basis: str | None = None
    sources: tuple[Mapping[str, Any], ...] = ()
    uncertainty: str | None = None
    remedy: str | None = None
    blocks_prototype: bool = False  # a failure that rules the candidate out even as a limited prototype
    authored: bool = False  # from the candidate's evaluations, not computed here

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"constraint": self.constraint, "mandatory": self.mandatory, "outcome": self.outcome,
                                "subject": dict(self.subject), "method": self.method, "reasons": list(self.reasons)}
        for key in ("criterion", "measured", "basis", "uncertainty", "remedy"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.sources:
            data["sources"] = list(self.sources)
        data["authored"] = self.authored
        return data

    @property
    def requirement(self) -> str | None:
        return self.constraint if self.constraint.startswith("REQ-") else None


def _source_dict(source: Any) -> dict[str, Any]:
    return {key: value for key, value in dataclasses.asdict(source).items() if value is not None}


def _criterion_dict(criterion: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"property": criterion.property, "operator": criterion.operator, "value": mi.thaw(criterion.value)}
    for key in ("units", "id", "state", "conditions"):
        if getattr(criterion, key):
            data[key] = getattr(criterion, key)
    data["source"] = _source_dict(criterion.source)
    return data


def _record_source(path: str, what: str) -> dict[str, Any]:
    """The source of a constraint the design record itself states, such as a part's transparency."""
    return {"kind": "user", "reference": f"{path}: {what}", "origin": "the design record"}


def _policy_source(text: str) -> dict[str, Any]:
    return {"kind": "user", "reference": text, "origin": "cad-agent manufacturing-intent policy (the user's specification)"}


def _criterion_text(criterion: Mapping[str, Any]) -> str:
    value = criterion.get("value")
    if isinstance(value, (list, tuple)):
        value = "..".join(f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v) for v in value)
    elif isinstance(value, float):
        value = f"{value:g}"
    units = f" {criterion['units']}" if criterion.get("units") else ""
    return f"{criterion.get('property')} {criterion.get('operator')} {value}{units}"


def _quantity_text(data: Mapping[str, Any]) -> str:
    if "value" in data:
        return f"{data['value']:g} {data['units']}"
    return f"{data['lower']:g}..{data['upper']:g} {data['units']}"


def _numbers(value: Any) -> list[float] | None:
    items = value if isinstance(value, (list, tuple)) else [value]
    if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in items):
        return [float(item) for item in items]
    return None


def _same_criterion(required: Any, stated: Any) -> bool:
    """Whether an evaluation states the requirement's criterion: same property and operator and the same
    value, converted to the requirement's units. A pass against another criterion never satisfies it."""
    if (required.property, required.operator) != (stated.property, stated.operator):
        return False
    wanted, given = mi.thaw(required.value), mi.thaw(stated.value)
    if required.units == stated.units:
        return bool(wanted == given)
    first, second = _numbers(wanted), _numbers(given)
    if required.units is None or stated.units is None or first is None or second is None or len(first) != len(second):
        return False
    try:
        converted = [mi.convert(number, stated.units, required.units) for number in second]
    except mi.IntentError:
        return False
    return all(abs(a - b) <= 1e-9 * max(1.0, abs(a)) for a, b in zip(first, converted, strict=True))


def _compare(value: Any, criterion: Any) -> tuple[str, str]:
    """pass, fail or unknown of a value or interval against a numeric criterion, and why. An interval that
    straddles the limit is unknown, never a pass."""
    if criterion.units is None:
        return "unknown", f"{criterion.property} {criterion.operator} {criterion.value} is categorical, not a material value"
    try:
        lower, upper = value.to(criterion.units).bounds
    except mi.IntentError as error:
        return "unknown", str(error)
    shown = f"{value} against {_criterion_text(_criterion_dict(criterion))}"
    tolerance = 1e-9 * max(1.0, abs(lower), abs(upper))
    limits = _numbers(mi.thaw(criterion.value))
    if limits is None:
        return "unknown", f"{shown}: the criterion is not numeric"
    operator = criterion.operator
    if operator == ">=":
        verdict = "pass" if lower >= limits[0] - tolerance else "fail" if upper < limits[0] - tolerance else "unknown"
    elif operator == "<=":
        verdict = "pass" if upper <= limits[0] + tolerance else "fail" if lower > limits[0] + tolerance else "unknown"
    elif operator == "between":
        low, high = limits
        inside = lower >= low - tolerance and upper <= high + tolerance
        outside = upper < low - tolerance or lower > high + tolerance
        verdict = "pass" if inside else "fail" if outside else "unknown"
    elif operator in ("equals", "one_of"):
        exact = upper - lower <= tolerance and any(abs(lower - limit) <= tolerance for limit in limits)
        possible = any(lower - tolerance <= limit <= upper + tolerance for limit in limits)
        verdict = "pass" if exact else "fail" if not possible else "unknown"
    else:
        return "unknown", f"{shown}: operator {operator} is not comparable"
    if verdict == "unknown":
        return verdict, f"{shown}: the interval straddles the limit"
    return verdict, shown


def _variants(profile: Any, name: str) -> list[tuple[str, str | None]]:
    """The distinct (conditioning, stated orientation) of `profile`'s known values of `name`."""
    keys: list[tuple[str, str | None]] = []
    for prop in profile.properties:
        if prop.name != name or not prop.value.known:
            continue
        stated = prop.orientation.value if prop.orientation is not None and prop.orientation.known else None
        key = (prop.conditioning, stated if isinstance(stated, str) else None)
        if key not in keys:
            keys.append(key)
    return keys


def _judge(profile: Any, criterion: Any, *, form: str | None, temperature: Any,
           allowable: bool) -> tuple[str, list[Any], list[str]]:
    """The outcome of a material-property criterion on `profile`, the resolutions it rests on and why.
    Every recorded conditioning and orientation that resolves under the requested form and temperature
    must pass for a pass and fail for a fail. With `allowable`, a typical value can only fail it."""
    name = criterion.property

    def resolve(want_allowable: bool) -> tuple[list[Any], list[str]]:
        variants: list[tuple[str | None, str | None]] = list(_variants(profile, name)) or [(None, None)]
        found, why = [], []
        for conditioning, orientation in variants:
            result = mi.resolve_property(profile, name, form=form, orientation=orientation, conditioning=conditioning,
                                         temperature=temperature, allowable=want_allowable)
            if result.resolved:
                found.append(result)
            else:
                why.extend(result.reasons)
        return found, why

    found, reasons = resolve(allowable)
    if not found and allowable:
        typical, _ = resolve(False)
        verdicts = [_compare(result.value, criterion) for result in typical]
        if typical and all(verdict == "fail" for verdict, _ in verdicts):
            return "fail", typical, [text for _, text in verdicts] + [
                "even the typical value fails, so no allowable of this profile can pass it"]
        return "unknown", typical, reasons + [text for _, text in verdicts] + [
            "this service class needs a justified design allowable; a typical value cannot pass it"]
    if not found:
        return "unknown", [], reasons
    verdicts = [_compare(result.value, criterion) for result in found]
    texts = []
    for result, (_, text) in zip(found, verdicts, strict=True):
        tested = ", ".join(f"{key} {value}" for key, value in (result.conditions or {}).items() if value is not None)
        texts.append(f"{text} ({result.basis}; {tested})" if tested else f"{text} ({result.basis})")
    outcomes = {verdict for verdict, _ in verdicts}
    if outcomes == {"fail"}:
        return "fail", found, texts
    if outcomes == {"pass"}:
        if profile.level == "family":
            return "unknown", found, texts + [f"{profile.id} is family guidance: only a grade profile with sourced "
                                              "evidence can pass a requirement"]
        return "pass", found, texts
    return "unknown", found, texts + ["the recorded variants disagree or straddle the limit: the part's actual "
                                      "orientation and conditioning decide"]


def _safety_relevant(requirement: Any) -> bool:
    risk = requirement.risk or {}
    return risk.get("severity") in ("major", "critical") or requirement.waiver_policy == "forbidden"


def _needs_optical(part: Any) -> bool:
    functions = part.intent.functions
    names = {entry.function for entry in functions} if not mi.is_fact(functions) else set()
    names |= {entry.function for component in part.intent.components for entry in component.functions}
    return "optical" in names or part.transparency in ("translucent", "transparent")


def _requirement_links(record: Any) -> dict[str, set[str]]:
    """Requirement id -> the parts whose intent or interfaces name it (functions, service, loads, surfaces, route)."""
    links: dict[str, set[str]] = {}
    loads = record.use_case.loads
    load_requirements = {} if mi.is_fact(loads) else {load.id: load.requirements for load in loads}
    for pn, part in record.parts.items():
        intent = part.intent
        found: set[str] = set()
        if not mi.is_fact(intent.functions):
            for entry in intent.functions:
                found.update(entry.requirements)
        for component in intent.components:
            for entry in component.functions:
                found.update(entry.requirements)
        if not mi.is_fact(intent.critical_surfaces):
            for surface in intent.critical_surfaces:
                found.update(surface.requirements)
        if not mi.is_fact(intent.service):
            found.update(intent.service.get("requirements") or ())
            for load in intent.service.get("loads") or ():
                found.update(load_requirements.get(load, ()))
        if not mi.is_fact(intent.route):
            for op in intent.route:
                found.update(op.requirements)
        for ident in found:
            links.setdefault(ident, set()).add(pn)
    for interface in record.interfaces.values():
        for ident in interface.functional.requirements:
            links.setdefault(ident, set()).update(_ends(interface))
    return links


def _ends(interface: Any) -> tuple[str, ...]:
    return tuple(pn for pn in (interface.raw.get("from"), interface.raw.get("to")) if isinstance(pn, str))


# --- the assessment --------------------------------------------------------------------------------------------

@dataclass
class _Assessed:
    candidate: Candidate
    evaluations: list[Evaluation]
    status: str = CONDITIONAL
    rank: int | None = None
    score: float | None = None
    compared_on: tuple[str, ...] = ()
    not_compared: tuple[str, ...] = ()
    unscored: tuple[str, ...] = ()
    recorded: dict[str, bool] = field(default_factory=dict)  # PN -> the record's intent is this candidate's route

    @property
    def id(self) -> str:
        return self.candidate.id

    def mandatory(self, outcome: str) -> list[Evaluation]:
        return [e for e in self.evaluations if e.mandatory and e.outcome == outcome]

    @property
    def prototype_eligible(self) -> bool:
        return not any(e.blocks_prototype for e in self.mandatory("fail"))


class _Assessor:
    """One selection pass over a normalized record. Pure: it reads the record and returns the report."""

    def __init__(self, record: Any) -> None:
        self.record = record
        use_case = record.use_case
        self.service = use_case.service_class.value if use_case.service_class.known else None
        temperature = use_case.facts.get("environment.temperature")
        self.temperature = (temperature.value if temperature is not None and temperature.known
                            and isinstance(temperature.value, mi.Quantity) else None)
        self.equipment = None if mi.is_fact(use_case.equipment) else frozenset(use_case.equipment)
        self.links = _requirement_links(record)
        self.issues: list[dict[str, Any]] = []

    # -- scope --
    def scope(self, requirement: Any, candidate: Candidate) -> tuple[bool, tuple[str, ...], bool]:
        """Whether `requirement` applies to `candidate`, the candidate parts its material criteria apply to, and
        whether those parts are named (by a subject, a part intent or an interface) rather than implied by a
        product-level requirement. A requirement without subjects that part intents or interfaces name applies to
        those parts; one that nothing names applies to the product, so to every part of the candidate."""
        pns = set(candidate.pns)
        linked = set(self.links.get(requirement.id, ()))
        subject_pns, subject_interfaces = set(), set()
        product = not requirement.subjects and not linked
        for subject in requirement.subjects:
            if subject.get("product") or subject.get("assembly_operation"):
                product = True
            if subject.get("PN"):
                subject_pns.add(subject["PN"])
            if subject.get("interface"):
                subject_interfaces.add(subject["interface"])
        for ident in subject_interfaces:
            interface = self.record.interfaces.get(ident)
            if interface is not None:
                linked.update(_ends(interface))
        scoped = (subject_pns | linked) & pns
        applies = product or bool(scoped) or bool(subject_interfaces & set(candidate.interfaces))
        return applies, tuple(sorted(scoped or (pns if product else set()))), bool(scoped)

    # -- one candidate --
    def assess(self, candidate: Candidate) -> _Assessed:
        evaluations: list[Evaluation] = []
        recorded: dict[str, bool] = {}
        for cpart in candidate.parts:
            part = self.record.parts.get(cpart.pn)
            if part is None:
                evaluations.append(Evaluation("part", True, "unknown", {"PN": cpart.pn}, "record lookup",
                                              (f"{cpart.pn} has no hw/parts/{cpart.pn}/part.yaml",)))
                continue
            recorded[cpart.pn] = self.matches_record(cpart, part)
            evaluations.extend(self.route_constraints(cpart))
            if _needs_optical(part):
                evaluations.append(self.optical(cpart, part))
        for ident in sorted(self.record.requirements):
            evaluations.extend(self.requirement(candidate, self.record.requirements[ident]))
        assessed = _Assessed(candidate, evaluations, recorded=recorded)
        assessed.status = (ELIMINATED if assessed.mandatory("fail") else CONDITIONAL if assessed.mandatory("unknown")
                           else FEASIBLE)
        return assessed

    def matches_record(self, cpart: CandidatePart, part: Any) -> bool:
        route = part.intent.route
        if mi.is_fact(route) or tuple(op.kind for op in route) != tuple(step.kind for step in cpart.route):
            return False
        recorded, proposed = part.intent.material_profile, cpart.material
        if isinstance(recorded, str) or isinstance(proposed, str):
            return bool(recorded == proposed)
        return bool(recorded.not_applicable and proposed.not_applicable)

    # -- route constraints --
    def route_constraints(self, cpart: CandidatePart) -> list[Evaluation]:
        subject = {"PN": cpart.pn}
        found = [self.material_form(cpart)]
        for index, step in enumerate(cpart.route):
            if step.kind == "purchased":
                continue
            where = f"route step {index + 1} ({step.kind})"
            if step.kind not in mi.KNOWN_ROUTE_KINDS:
                found.append(Evaluation(
                    "process_coverage", True, "unknown", subject, f"{where}: manufacturing_intent.KNOWN_ROUTE_KINDS",
                    (f"{step.kind} has no check contract: its manufacturability coverage is unknown, never a pass",),
                    remedy=f"add a check profile for {step.kind}, or choose a route kind cad-agent checks"))
                continue
            found.extend(self.capability(cpart, step, where))
        return found

    def material_form(self, cpart: CandidatePart) -> Evaluation:
        subject = {"PN": cpart.pn}
        method = "material profile form against the shaping route"
        if not isinstance(cpart.material, str):
            outcome = "not_applicable" if cpart.material.not_applicable else "unknown"
            return Evaluation("material_form", True, outcome, subject, method,
                              (f"{cpart.pn} names no material profile: {cpart.material.reason}",),
                              remedy="select a material profile (grade, form and process state) for this route")
        profile = self.record.materials.get(cpart.material)
        if profile is None:
            return Evaluation("material_form", True, "unknown", subject, method, (f"{cpart.material} is not declared",))
        shaping = cpart.shaping_kind
        additive = shaping in mi.ADDITIVE_KINDS
        if profile.form == "unknown":
            outcome, why = "unknown", f"{profile.id} does not state its form"
        elif additive and profile.form != "printed":
            outcome, why = "unknown", (f"{profile.id} holds {profile.form} properties, which do not describe a {shaping} "
                                       "printed part; select a printed-form profile")
        elif not additive and profile.form == "printed":
            outcome, why = "unknown", f"{profile.id} holds printed properties, which do not describe {shaping} stock"
        else:
            outcome, why = "pass", f"{profile.id} is {profile.form} material for a {shaping} route"
        return Evaluation("material_form", True, outcome, subject, method, (why,),
                          remedy=None if outcome == "pass" else "select a profile whose form matches the route")

    def capability(self, cpart: CandidatePart, step: Step, where: str) -> list[Evaluation]:
        subject = {"PN": cpart.pn}
        method = f"{where}: capability profile"
        not_assumed = "unavailable tooling is never assumed"
        if not isinstance(step.capability, str):
            return [Evaluation("capability", True, "unknown", subject, method,
                               (f"no capability profile: {step.capability.reason}; {not_assumed}",),
                               remedy=f"record the actual {step.kind} machine or supplier as a capability profile")]
        profile = self.record.capabilities.get(step.capability)
        if profile is None:
            return [Evaluation("capability", True, "unknown", subject, method, (f"{step.capability} is not declared",))]
        source = _record_source("hw/manufacturing/capabilities.yaml", f"{profile.id} ({profile.process}, {profile.provider})")
        criterion = {"property": "capability_process", "operator": "equals", "value": step.kind, "source": source}
        if profile.template:
            return [Evaluation("capability", True, "unknown", subject, method,
                               (f"{profile.id} is a template, not a capability; {not_assumed}",), criterion=criterion,
                               remedy="copy the template, record the actual machine's values and calibrations, then remove template")]
        if profile.process != step.kind:
            return [Evaluation("capability", True, "fail", subject, method,
                               (f"{profile.id} is a {profile.process} capability; the step is {step.kind}",),
                               criterion=criterion, measured={"text": profile.process}, blocks_prototype=True,
                               remedy=f"name a {step.kind} capability profile for this step")]
        reasons = [f"{profile.id} is a {profile.process} capability ({profile.provider})"]
        if step.provider and step.provider != profile.provider:
            reasons.append(f"the step says {step.provider}, the profile is {profile.provider}")
        if profile.provider == "local" and self.equipment is not None and profile.id not in self.equipment:
            return [Evaluation("capability", True, "unknown", subject, method,
                               (f"{profile.id} is not listed in use_case.capability.equipment; {not_assumed}",),
                               criterion=criterion, remedy=f"confirm {profile.id} is available, or choose another capability")]
        return [Evaluation("capability", True, "pass", subject, method, tuple(reasons), criterion=criterion,
                           measured={"text": profile.process}),
                self.approval(cpart, step, profile, where)]

    def approval(self, cpart: CandidatePart, step: Step, profile: Any, where: str) -> Evaluation:
        subject = {"PN": cpart.pn}
        method = f"{where}: {profile.id} material approvals"
        source = _record_source("hw/manufacturing/capabilities.yaml", f"{profile.id} materials")
        criterion = {"property": "material_approval", "operator": "equals", "value": "approved", "source": source}
        if not isinstance(cpart.material, str):
            return Evaluation("material_approval", True, "unknown", subject, method,
                              (f"no material selected, so {profile.id}'s approval cannot be checked",), criterion=criterion,
                              remedy="select an identified material profile")
        approvals = profile.materials
        if mi.is_fact(approvals):
            if approvals.not_applicable and step.kind not in mi.SHEET_CUTTING_KINDS:
                return Evaluation("material_approval", True, "pass", subject, method,
                                  (f"{profile.id} has no material scope: {approvals.reason}",), criterion=criterion)
            return Evaluation("material_approval", True, "unknown", subject, method,
                              (f"{profile.id}'s approved materials are {approvals.status.replace('_', ' ')}: {approvals.reason}",),
                              criterion=criterion, remedy=f"record which exact materials {profile.id} is approved for")
        entries = [entry for entry in approvals if entry.get("material") == cpart.material]
        forbidden = [entry for entry in entries if entry.get("status") == "forbidden"]
        if forbidden:
            entry = forbidden[0]
            return Evaluation("material_approval", True, "fail", subject, method,
                              (f"{profile.id} forbids {cpart.material}: {entry.get('reason')}",),
                              criterion={**criterion, "source": mi.thaw(entry.get("source"))}, measured={"text": "forbidden"},
                              blocks_prototype=True,
                              remedy="choose an approved, identified material; never an unidentified or forbidden one")
        approved = [entry for entry in entries if entry.get("status") == "approved"]
        if not approved:
            return Evaluation("material_approval", True, "unknown", subject, method,
                              (f"{profile.id} does not list {cpart.material}: an unlisted material is never assumed approved",),
                              criterion=criterion,
                              remedy=f"record {profile.id}'s approval of {cpart.material} with its source, or choose a listed material")
        entry = approved[0]
        if step.kind == "laser":
            extraction = entry.get("extraction")
            if not isinstance(extraction, Mapping) or not mi.fact(extraction).known:
                return Evaluation("material_approval", True, "unknown", subject, method,
                                  (f"{profile.id} approves {cpart.material} but its extraction approval is not recorded",),
                                  criterion=criterion, remedy="record the extraction approved for this material")
        return Evaluation("material_approval", True, "pass", subject, method, (f"{profile.id} approves {cpart.material}",),
                          criterion={**criterion, "source": mi.thaw(entry.get("source"))}, measured={"text": "approved"})

    # -- optical --
    def optical(self, cpart: CandidatePart, part: Any) -> Evaluation:
        pn = cpart.pn
        why = f"transparency {part.transparency}" if part.transparency in ("translucent", "transparent") else "an optical function"
        criterion = {"property": "optical_route", "operator": "one_of",
                     "value": ["purchased_transparent_part", "identified_transparent_stock"],
                     "source": _record_source(f"hw/parts/{pn}/part.yaml", why)}
        subject = {"PN": pn}
        method = "optical route: a purchased transparent part or identified transparent stock on an approved route"
        remedy = ("use a purchased transparent part or identified transparent stock (such as cast acrylic or polycarbonate "
                  "sheet) on an approved laser or CNC route, and record its purchase criteria: material identity and "
                  "grade, optical grade, thickness and supplier evidence; never an unidentified clear plastic")
        additive = sorted({step.kind for step in cpart.route} & mi.ADDITIVE_KINDS)
        if additive:
            return Evaluation("optical_route", True, "fail", subject, method,
                              (f"{pn} needs {why}; {', '.join(additive)} is an opaque process and cannot make it",),
                              criterion=criterion, measured={"text": f"{', '.join(additive)} route"}, remedy=remedy,
                              blocks_prototype=True)
        criteria = self.optical_criteria(cpart, part)
        missing = [entry["name"] for entry in criteria if entry["required"] and entry["status"] != "known"]
        route = "purchased transparent part" if cpart.shaping_kind == "purchased" else "transparent stock"
        if missing:
            return Evaluation("optical_route", True, "unknown", subject, method,
                              (f"{route}: {', '.join(missing)} not recorded",), criterion=criterion, remedy=remedy)
        return Evaluation("optical_route", True, "pass", subject, method,
                          (f"identified {route}: " + "; ".join(f"{e['name']} {e['value']}" for e in criteria if e["status"] == "known"),),
                          criterion=criterion, measured={"text": route})

    def optical_criteria(self, cpart: CandidatePart, part: Any) -> list[dict[str, Any]]:
        """The traceable purchase criteria of a transparent part on this candidate's route: material identity
        and grade, optical grade, thickness and supplier evidence, each known (with its value) or unknown."""
        def entry(name: str, required: bool, value: str | None, reason: str, sources: Sequence[Any] = ()) -> dict[str, Any]:
            data: dict[str, Any] = {"name": name, "required": required, "status": "known" if value else "unknown"}
            if value:
                data["value"] = value
            else:
                data["reason"] = reason
            if sources:
                data["sources"] = [_source_dict(s) for s in sources]
            return data

        route = part.intent.route
        operations = () if mi.is_fact(route) else route
        if cpart.shaping_kind == "purchased":
            purchase = next((op.purchase for op in operations if op.kind == "purchased" and op.purchase), None)
            if purchase is None:
                missing = "the part record holds no purchase criteria for this route"
                return [entry(name, required, None, missing) for name, required in
                        (("material_identity_grade", True), ("optical_grade", True), ("thickness", False),
                         ("supplier_evidence", True))]
            facts = {key: mi.fact(purchase[key]) for key in ("identity", "specification", "optical", "thickness")
                     if isinstance(purchase.get(key), Mapping)}
            identity, specification = facts.get("identity"), facts.get("specification")
            identity_text = (f"{identity.value}; {specification.value}" if identity is not None and identity.known
                             and specification is not None and specification.known else None)
            optical, thickness = facts.get("optical"), facts.get("thickness")
            supplier_evidence = [mi.Source.from_data(s) for s in purchase.get("supplier_evidence") or ()]
            return [entry("material_identity_grade", True, identity_text, "identity and specification are not both known"),
                    entry("optical_grade", True, str(optical.value) if optical is not None and optical.known else None,
                          "purchase.optical is not known", optical.sources if optical is not None else ()),
                    entry("thickness", False, str(thickness.value) if thickness is not None and thickness.known else None,
                          "purchase.thickness is not known"),
                    entry("supplier_evidence", True,
                          "; ".join(s.reference for s in supplier_evidence) if supplier_evidence else None,
                          "no purchase.supplier_evidence", supplier_evidence)]
        profile = self.record.materials.get(cpart.material) if isinstance(cpart.material, str) else None
        identity = None
        if profile is not None and profile.level == "grade" and profile.grade.known:
            identity = f"{profile.family} {profile.grade.value}"
        optical = None
        evidence: list[Any] = []
        if profile is not None:
            values = []
            for name in ("light_transmission", "haze"):
                result = mi.resolve_property(profile, name)
                if result.resolved:
                    values.append(f"{name} {result.value} ({result.basis})")
                    evidence.extend(s for s in result.sources if s.kind in ("datasheet", "supplier", "test"))
            optical = "; ".join(values) or None
        thickness = None
        for op in operations:
            stock = (op.setup or {}).get("stock") if op.kind == cpart.shaping_kind else None
            if isinstance(stock, Mapping) and isinstance(stock.get("thickness"), Mapping):
                stated = mi.fact(stock["thickness"])
                if stated.known:
                    thickness = str(stated.value)
        return [entry("material_identity_grade", True, identity,
                      "the stock's grade profile is not identified" if profile is not None else "no material profile"),
                entry("optical_grade", True, optical, "no sourced light_transmission resolves for the stock profile"),
                entry("thickness", False, thickness, "the part route states no stock thickness"),
                entry("supplier_evidence", True, "; ".join(dict.fromkeys(s.reference for s in evidence)) or None,
                      "no datasheet, supplier or test source backs the optical grade", list({s: None for s in evidence}))]

    # -- requirements --
    def requirement(self, candidate: Candidate, requirement: Any) -> list[Evaluation]:
        applies, pns, named = self.scope(requirement, candidate)
        if not applies:
            return []
        blocks = _safety_relevant(requirement)
        authored = [data for data in candidate.authored if data.get("requirement") == requirement.id]
        if len(pns) == 1:
            subject: dict[str, Any] = {"PN": pns[0]}
        elif len(requirement.subjects) == 1 and not requirement.subjects[0].get("PN"):
            subject = {key: value for key, value in mi.thaw(requirement.subjects[0]).items() if key != "requirements"}
        else:
            subject = {"product": True}
        subject["requirements"] = [requirement.id]
        blanket = [data for data in authored if data.get("outcome") == "not_applicable" and "criterion" not in data]
        results: list[Evaluation] = []
        matched: set[int] = set()
        for criterion in requirement.acceptance:
            here = [i for i, data in enumerate(authored)
                    if isinstance(data.get("criterion"), Mapping) and _same_criterion(criterion, mi.Criterion.from_data(data["criterion"]))]
            matched.update(here)
            written = [self.authored(requirement, authored[i], subject, blocks) for i in here]
            computed = []
            if criterion.property in mi.MATERIAL_PROPERTIES and pns:
                computed = [self.material_criterion(requirement, criterion, cpart, blocks, named)
                            for pn in pns if (cpart := candidate.part(pn)) is not None]
            if not computed and not written:
                if blanket:
                    results.append(self.authored(requirement, blanket[0], subject, blocks))
                    continue
                results.append(Evaluation(
                    requirement.id, requirement.mandatory, "unknown", subject, "no evaluation",
                    (f"no evaluation states {_criterion_text(_criterion_dict(criterion))}: an unevaluated criterion is never a pass",),
                    criterion=_criterion_dict(criterion), blocks_prototype=blocks,
                    remedy="evaluate the criterion with a sourced method and record it in the candidate's evaluations"))
                continue
            computed_fail = any(e.outcome == "fail" for e in computed)
            if computed_fail and any(e.outcome == "pass" for e in written):
                self.issues.append({"kind": "conflict", "candidate": candidate.id, "requirement": requirement.id,
                                    "criterion": _criterion_dict(criterion), "subject": subject})
            if not computed_fail and any(e.outcome == "pass" for e in written):
                computed = [e for e in computed if e.outcome != "unknown"]
            results.extend(computed + written)
        for index, data in enumerate(authored):
            if index in matched or (requirement.acceptance and data in blanket):
                continue
            if requirement.acceptance and data.get("outcome") == "pass":
                self.issues.append({"kind": "mismatch", "candidate": candidate.id, "requirement": requirement.id,
                                    "criterion": mi.thaw(data.get("criterion")), "subject": subject,
                                    "required": [_criterion_dict(c) for c in requirement.acceptance]})
                continue
            if requirement.acceptance and data.get("outcome") == "not_applicable":
                continue
            results.append(self.authored(requirement, data, subject, blocks))
        if not requirement.acceptance and not authored:
            results.append(Evaluation(
                requirement.id, requirement.mandatory, "unknown", subject, "no evaluation",
                (f"{requirement.id} has no structured acceptance criteria and the candidate records no evaluation of it",),
                blocks_prototype=blocks,
                remedy=f"add structured acceptance to {requirement.id} or record a sourced evaluation for this candidate"))
        return results

    def authored(self, requirement: Any, data: Mapping[str, Any], subject: Mapping[str, Any], blocks: bool) -> Evaluation:
        value = data.get("value")
        measured = mi.thaw(value) if isinstance(value, Mapping) else {"text": value} if isinstance(value, str) else None
        source = data.get("source")
        stated = data.get("outcome")
        outcome = stated if isinstance(stated, str) else "unknown"  # an evaluation without an outcome states none
        return Evaluation(requirement.id, requirement.mandatory, outcome, dict(subject), f"authored: {data.get('method')}",
                          (data["note"],) if data.get("note") else (),
                          criterion=mi.thaw(data["criterion"]) if isinstance(data.get("criterion"), Mapping) else None,
                          measured=measured, sources=(mi.thaw(source),) if isinstance(source, Mapping) else (),
                          uncertainty=data.get("uncertainty"), blocks_prototype=blocks, authored=True,
                          remedy={"fail": f"choose a candidate that meets {requirement.id} with sourced evidence; never relax "
                                          f"{requirement.id} to make this one pass",
                                  "unknown": f"evaluate {requirement.id} for this candidate with a sourced method"}.get(outcome))

    def material_criterion(self, requirement: Any, criterion: Any, cpart: CandidatePart, blocks: bool, named: bool) -> Evaluation:
        subject = {"PN": cpart.pn, "requirements": [requirement.id]}
        common = dict(constraint=requirement.id, mandatory=requirement.mandatory, subject=subject,
                      criterion=_criterion_dict(criterion), blocks_prototype=blocks)
        if not isinstance(cpart.material, str):
            if cpart.material.not_applicable and not named:
                return Evaluation(outcome="not_applicable", method="material profile",
                                  reasons=(f"{cpart.pn} has no material profile ({cpart.material.reason}): its rating governs, "
                                           "checked as a purchase",), **common)
            return Evaluation(outcome="unknown", method="material profile",
                              reasons=(f"{cpart.pn} names no material profile ({cpart.material.reason}), so "
                                       f"{requirement.id} cannot be computed for it",),
                              remedy="select a material profile with sourced properties, or record an evaluation of the "
                                     "item's rating against the same criterion", **common)
        profile = self.record.materials.get(cpart.material)
        if profile is None:
            return Evaluation(outcome="unknown", method="material profile", reasons=(f"{cpart.material} is not declared",), **common)
        name = criterion.property
        form = _FORM_OF_KIND.get(cpart.shaping_kind or "")
        allowable = self.service in ALLOWABLE_SERVICE and "stress" in mi.MATERIAL_PROPERTIES[name]
        temperature = self.temperature if name in _TEMPERATURE_QUALIFIED else None
        conditions = [f"form {form}" if form else "any stock form", f"at {temperature}" if temperature else "any temperature",
                      "design allowable" if allowable else "typical or allowable"]
        method = f"resolve_property({profile.id}, {name}; {', '.join(conditions)})"
        outcome, resolutions, reasons = _judge(profile, criterion, form=form, temperature=temperature, allowable=allowable)
        first = resolutions[0] if resolutions else None
        uncertainty = None
        if first is not None:
            notes = [f"{first.basis} value from a {profile.level} profile"]
            if profile.typical_not_allowable:
                notes.append("typical, not a design allowable")
            if temperature is None and name in _TEMPERATURE_QUALIFIED:
                notes.append("the operating temperature is unknown, so the tested temperature governs")
            uncertainty = "; ".join(notes)
        remedy = None
        if outcome == "fail":
            remedy = (f"choose a material or route whose sourced {name} meets {_criterion_text(_criterion_dict(criterion))}, "
                      f"such as another candidate; never relax {requirement.id} to make this one pass")
        elif outcome == "unknown":
            remedy = f"record a sourced {name} of {profile.id} under the service conditions, or a grade-level profile"
        return Evaluation(outcome=outcome, method=method, reasons=tuple(reasons),
                          measured=first.value.as_dict() if first is not None else None,
                          basis=first.basis if first is not None else None,
                          sources=tuple(_source_dict(s) for s in first.sources) if first is not None else (),
                          uncertainty=uncertainty, remedy=remedy, **common)

    # -- preferences --
    def rank(self, group: list[_Assessed]) -> None:
        """Rank feasible candidates, then conditional ones, by weighted preference score over the
        preferences every candidate of that tier was scored on; eliminated candidates are never ranked."""
        preferences = self.record.preferences
        position = 1
        for tier in (FEASIBLE, CONDITIONAL):
            members = [a for a in group if a.status == tier]
            scores = {a.id: {s.get("preference"): s.get("score") for s in a.candidate.raw.get("preference_scores") or ()
                             if s.get("preference") in preferences} for a in members}
            common = set(preferences)
            for a in members:
                common &= set(scores[a.id])
            for a in members:
                a.compared_on = tuple(sorted(common))
                a.not_compared = tuple(sorted(set(scores[a.id]) - common))
                a.unscored = tuple(sorted(set(preferences) - set(scores[a.id])))
                a.score = sum(preferences[p].weight * scores[a.id][p] for p in common) if common else None
            for a in sorted(members, key=lambda item: (-(item.score or 0.0), item.id)):
                a.rank = position
                position += 1

    # -- comparison --
    def pairs(self, candidate: Candidate) -> list[dict[str, Any]]:
        """The contacting pairs this candidate touches: interfaces with a contact record or a rotating or
        sliding function, the pair's materials on this candidate and the contact evidence recorded for them."""
        found = []
        pns = set(candidate.pns)
        for ident in sorted(self.record.interfaces):
            interface = self.record.interfaces[ident]
            ends = _ends(interface)
            if len(ends) != 2 or not set(ends) & pns:
                continue
            functional = interface.functional
            names = sorted({e.function for e in functional.functions}) if not mi.is_fact(functional.functions) else []
            contact = functional.raw.get("contact")
            if not isinstance(contact, Mapping) and not {"rotating", "sliding"} & set(names):
                continue
            materials = []
            for pn in ends:
                cpart = candidate.part(pn)
                material = cpart.material if cpart is not None else self.record.parts[pn].intent.material_profile if pn in self.record.parts else None
                materials.append(material if isinstance(material, str) else "unknown")
            critical = functional.critical
            evidence = "no contact record"
            if isinstance(contact, Mapping):
                recorded = sorted(m if isinstance(m, str) else "unknown" for m in contact.get("materials") or ())
                if recorded != sorted(materials):
                    evidence = f"the contact record is for {'/'.join(recorded)}, not this pair: its evidence does not apply"
                elif contact.get("evidence"):
                    evidence = f"evidence {', '.join(contact['evidence'])}"
                else:
                    evidence = "contact recorded, no evidence"
            found.append({"interface": ident, "parts": list(ends), "materials": materials, "functions": names,
                          "critical": critical.value if critical.known else None, "contact_evidence": evidence})
        return found

    def row(self, assessed: _Assessed) -> dict[str, str]:
        candidate = assessed.candidate
        cells: dict[str, str] = {"candidate": f"{candidate.id}: {candidate.title} ({candidate.architecture})"}
        materials = []
        for cpart in candidate.parts:
            profile = self.record.materials.get(cpart.material) if isinstance(cpart.material, str) else None
            if profile is not None:
                grade = profile.grade.value if profile.grade.known else f"grade {profile.grade.status.replace('_', ' ')}"
                label = f"{profile.id} ({profile.family}, {grade}, {profile.level}, {profile.form})"
            else:
                label = cpart.material_label
            materials.append(f"{cpart.pn}: {label}; " + " -> ".join(step.describe() for step in cpart.route))
        cells["material_process"] = "; ".join(materials)
        cells["suitability"] = self.suitability(assessed)
        cells["service_properties"] = "; ".join(f"{group}: {self.properties(candidate, group)}" for group in SERVICE_GROUPS)
        cells["accuracy_finish_stability"] = self.accuracy(candidate)
        equipment = [f"{cpart.pn}: " + ", ".join(step.describe() for step in cpart.route) for cpart in candidate.parts]
        changes = candidate.raw.get("changes") or {}
        for key in ("equipment", "finishing", "inspection"):
            equipment += [f"{key}: {text}" for text in changes.get(key) or ()]
        cells["equipment_finishing_inspection"] = "; ".join(equipment)
        design = [f"{key}: {text}" for key in ("design", "interfaces", "assembly") for text in changes.get(key) or ()]
        design += [f"interface {ident}" for ident in candidate.interfaces]
        design += [f"{p['interface']} {'/'.join(p['parts'])} {'/'.join(p['materials'])} ({', '.join(p['functions']) or 'contact'}): "
                   f"{p['contact_evidence']}" for p in self.pairs(candidate)]
        cells["design_changes_interfaces"] = "; ".join(design) or "none recorded"
        cells["cost_lead_time"] = self.cost(candidate)
        cells["maintenance"] = _fact_text(candidate.fact("maintenance"))
        cells["evidence"] = self.evidence(assessed)
        for key in ("pros", "cons", "tradeoffs"):
            cells[key] = "; ".join(candidate.texts(key)) or "none recorded"
        if assessed.rank is None:
            cells["preference"] = "not ranked: eliminated by a mandatory constraint, which no score can compensate"
        else:
            score = f"score {assessed.score:g} over {', '.join(assessed.compared_on)}" if assessed.score is not None else "no common preference scores"
            extra = f"; not compared: {', '.join(assessed.not_compared)}" if assessed.not_compared else ""
            cells["preference"] = f"rank {assessed.rank} ({assessed.status}); {score}{extra}"
        return cells

    def suitability(self, assessed: _Assessed) -> str:
        def describe(evaluation: Evaluation) -> str:
            where = _subject_text(evaluation.subject)
            detail = "; ".join(evaluation.reasons) or evaluation.method
            return f"{evaluation.constraint} ({where}): {detail}"

        if assessed.status == ELIMINATED:
            return "eliminated: " + " | ".join(describe(e) for e in assessed.mandatory("fail"))
        if assessed.status == CONDITIONAL:
            return "conditional: " + " | ".join(describe(e) for e in assessed.mandatory("unknown"))
        passed = sorted({e.constraint for e in assessed.evaluations if e.mandatory and e.outcome == "pass"})
        return f"feasible: every mandatory constraint assessed passes ({', '.join(passed) or 'none applies'})"

    def properties(self, candidate: Candidate, group: str) -> str:
        names = dict(PROPERTY_GROUPS)[group]
        found = []
        for cpart in candidate.parts:
            profile = self.record.materials.get(cpart.material) if isinstance(cpart.material, str) else None
            for prop in profile.properties if profile is not None else ():
                if prop.name in names and prop.value.known:
                    tested = [prop.conditioning]
                    if prop.orientation is not None and prop.orientation.known:
                        tested.append(str(prop.orientation.value))
                    if prop.temperature.known:
                        tested.append(f"at {prop.temperature.value}")
                    found.append(f"{cpart.pn} {prop.name} {prop.value.value} ({prop.value.basis}, {', '.join(tested)})")
        return ", ".join(found) or "not recorded"

    def accuracy(self, candidate: Candidate) -> str:
        found = []
        for cpart in candidate.parts:
            for step in cpart.route:
                profile = self.record.capabilities.get(step.capability) if isinstance(step.capability, str) else None
                if step.kind == "purchased":
                    continue
                if profile is None or profile.template:
                    found.append(f"{cpart.pn} {step.kind}: capability unknown, so accuracy is unknown")
                    continue
                calibrated = [c for c in profile.calibrations if c.scope.get("material") == cpart.material]
                if not calibrated:
                    found.append(f"{cpart.pn} {step.kind} {profile.id}: no calibration for {cpart.material_label}; "
                                 "accuracy is unknown, never inferred from resolution")
                for c in calibrated:
                    scope = ", ".join(f"{k} {v}" for k, v in sorted(c.scope.items()) if k != "material")
                    found.append(f"{cpart.pn} {step.kind} {profile.id}: {c.characteristic} {c.limits}"
                                 f" ({c.samples} samples{', ' + scope if scope else ''})")
        stability = self.properties(candidate, "stability")
        found.append(f"stability: {stability}")
        return "; ".join(found)

    def cost(self, candidate: Candidate) -> str:
        quantity = candidate.fact("quantity")
        at = f" at {quantity.value}" if quantity.known else f" at an {quantity.status.replace('_', ' ')} quantity"
        return f"cost {_fact_text(candidate.fact('cost'))}{at}; lead time {_fact_text(candidate.fact('lead_time'))}"

    def evidence(self, assessed: _Assessed) -> str:
        candidate = assessed.candidate
        parts = []
        if candidate.raw.get("evidence_confidence"):
            parts.append(candidate.raw["evidence_confidence"])
        computed = [e for e in assessed.evaluations if e.requirement and not e.authored and e.basis]
        if computed:
            bases = sorted({e.basis for e in computed if e.basis})
            parts.append(f"{len(computed)} requirement results computed from profile values ({', '.join(bases)})")
        written = [e for e in assessed.evaluations if e.authored]
        if written:
            parts.append(f"{len(written)} authored evaluations")
        unknown = sorted({e.constraint for e in assessed.mandatory("unknown")})
        if unknown:
            parts.append(f"unknown: {', '.join(unknown)}")
        required = []
        for ident in sorted({e.requirement for e in assessed.evaluations if e.requirement}):
            levels = self.record.requirements[ident].evidence_required
            if levels:
                required.append(f"{ident} needs {', '.join(levels)}")
        if required:
            parts.append("outstanding: " + ", ".join(required))
        parts += [f"outstanding: {text}" for text in candidate.texts("obligations")]
        for pair in self.pairs(candidate):
            if pair["critical"] and set(pair["functions"]) & {"rotating", "sliding"} and not pair["contact_evidence"].startswith("evidence"):
                parts.append(f"outstanding: {pair['interface']} wear and friction evidence for {'/'.join(pair['materials'])}")
        parts.append("selection evidence is not physical validation")
        return "; ".join(parts)


def _fact_text(fact: Any) -> str:
    if fact.known:
        value = fact.value
        if isinstance(value, Mapping) and {"amount", "currency"} <= set(value):
            value = f"{value['amount']:g} {value['currency']}"
        reference = fact.sources[0].reference if fact.sources else "no source"
        uncertainty = f", {fact.uncertainty['note']}" if fact.uncertainty else ""
        return f"{value} ({fact.basis}{uncertainty}; {reference})"
    return f"{fact.status.replace('_', ' ')} ({fact.reason})"


def _open_fact(fact: Any) -> dict[str, Any]:
    """The schema form of an unknown or not-applicable Fact."""
    data = {"status": fact.status, "reason": fact.reason}
    if fact.question:
        data["question"] = fact.question
    return data


# --- decisions, recommendations and findings -------------------------------------------------------------------

def _groups(candidates: Sequence[Candidate]) -> list[list[Candidate]]:
    """Candidates that share a part are alternatives for one decision."""
    parent = list(range(len(candidates)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owner: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        for pn in candidate.pns:
            if pn in owner:
                parent[root(index)] = root(owner[pn])
            else:
                owner[pn] = index
    groups: dict[int, list[Candidate]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(root(index), []).append(candidate)
    return sorted(groups.values(), key=lambda group: sorted({pn for c in group for pn in c.pns}))


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug if slug[:1].isalpha() else f"x_{slug}"


def _subject_text(subject: Mapping[str, Any]) -> str:
    return subject.get("PN") or subject.get("interface") or (f"op:{subject['assembly_operation']}"
                                                           if subject.get("assembly_operation") else "product")


class _Findings:
    """Collects findings in the manufacturing.schema.json#/$defs/finding shape with unique, stable ids."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.ids: set[str] = set()

    def add(self, code: str, key: str, subject: Mapping[str, Any], severity: str, outcome: str, *, measured: Mapping[str, Any],
            criterion: Mapping[str, Any], method: str, uncertainty: str, consequence: str, remedy: str,
            inputs: Sequence[str], requirements: Sequence[str] = (), alternatives: Sequence[Mapping[str, str]] = ()) -> None:
        ident = base = _slug(f"{code.split('.', 1)[1]}_{key}")
        counter = 2
        while ident in self.ids:
            ident, counter = f"{base}_{counter}", counter + 1
        self.ids.add(ident)
        finding: dict[str, Any] = {"id": ident, "code": code, "subject": dict(subject), "severity": severity, "outcome": outcome,
                                   "measured": dict(measured), "criterion": dict(criterion)}
        if requirements:
            finding["requirements"] = sorted(set(requirements))
        finding.update({"method": {"name": method}, "uncertainty": uncertainty or "none stated", "consequence": consequence,
                        "remedy": remedy})
        if alternatives:
            finding["alternatives"] = [dict(a) for a in alternatives]
        finding["recheck"] = {"families": ["selection"], "inputs": list(inputs), "earliest_stage": "intent"}
        self.items.append(finding)


_INPUTS = ("hw/manufacturing/selection.yaml", "hw/requirements.yaml", "hw/manufacturing/materials.yaml",
           "hw/manufacturing/capabilities.yaml", "hw/product.yaml use_case")


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason}


# Findings that the recorded selection contradicts the assessment: they block until selection.yaml is corrected.
_CONTRADICTIONS = frozenset({"selection.recorded_recommendation_eliminated", "selection.final_unsupported"})


def assess(record: Any, *, problems: Sequence[Any] | None = None, input_sha256: str | None = None) -> dict[str, Any]:
    """The deterministic, JSON-ready selection report of a normalized record (manufacturing_intent.Record).
    `problems` are the record's problems (default record.problems); an error other than
    record.migration_required makes the report invalid, and nothing is compared on invalid input. A valid
    report's `coverage` names the record's made parts and those no candidate covers (see `_coverage`)."""
    problems = list(record.problems if problems is None else problems)
    assessment = mi.assess_use_case(record)
    findings = _Findings()
    report: dict[str, Any] = {"report": "cad-record selection", "report_version": REPORT_VERSION, "status": "not_assessed",
                              "record_version": record.version, "input_sha256": input_sha256, "digest_scope": list(DIGEST_SCOPE),
                              "use_case": _use_case_section(record, assessment), "decisions": [],
                              "recorded": _recorded_section(record, input_sha256), "findings": findings.items,
                              "problems": [p.finding() for p in problems], "approval": APPROVAL_GATE}
    if any(p.severity == "error" and p.code != mi.MIGRATION_REQUIRED for p in problems):
        report["status"] = "invalid"
        return report
    if not assessment.final_ready:
        _use_case_finding(findings, assessment, report["use_case"])
    candidates = [_candidate(ident, raw) for ident, raw in record.selection.candidates.items()]
    if not candidates:
        if record.migrated:
            findings.add("selection.not_assessed", "selection", {"product": True}, "minor", "unknown",
                         measured=_unknown("hw/manufacturing/selection.yaml records no candidates"),
                         criterion={"property": "candidates", "operator": ">=", "value": 1, "units": "count",
                                    "source": _policy_source("evaluate credible material, process and architecture combinations")},
                         method="selection.yaml candidates", uncertainty="no candidate is recorded",
                         consequence="no material, process or architecture is recommended",
                         remedy="record whole candidate routes (material profile, route and capability per part), including "
                                "metal, purchased, sheet and hybrid alternatives where credible", inputs=_INPUTS)
        report["coverage"] = _coverage(record, [])
        return report
    assessor = _Assessor(record)
    statuses = []
    for group in _groups(candidates):
        decision = _decision(assessor, group, assessment, findings)
        report["decisions"].append(decision)
        statuses.append("blocked" if decision["recommendation"] is None else "final_ready" if decision["final_allowed"] else "conditional")
    for issue in assessor.issues:
        _issue_finding(findings, issue)
    _recorded_findings(findings, report)
    if any(f["code"] in _CONTRADICTIONS for f in findings.items):
        statuses.append("blocked")
    report["status"] = ("blocked" if "blocked" in statuses else "conditional" if "conditional" in statuses else "final_ready")
    report["coverage"] = _coverage(record, report["decisions"])
    return report


def _coverage(record: Any, decisions: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The record's made parts and those no decision covers. A made part no candidate names has an unknown
    material and process choice: exploration before the design freeze, a refusal from it (evidence.selection_gate)."""
    made = sorted(pn for pn, part in record.parts.items() if part.made)
    covered = {pn for decision in decisions for pn in decision["parts"]}
    return {"made": made, "unselected": [pn for pn in made if pn not in covered]}


def _use_case_section(record: Any, assessment: Any) -> dict[str, Any]:
    use_case = record.use_case
    fields = dict(use_case.facts)
    fields["service_class"] = use_case.service_class
    for name, value in (("loads", use_case.loads), ("capability.equipment", use_case.equipment),
                        ("evidence_policy", use_case.evidence_policy)):
        if mi.is_fact(value):
            fields[name] = value
    missing = []
    for name in assessment.unknown:
        fact = fields[name]
        question = record.questions.get(fact.question) if fact.question else None
        entry: dict[str, Any] = {"field": f"use_case.{name}", "reason": fact.reason, "question": fact.question}
        if question is not None:
            entry.update(question_text=question.question, question_status=question.status,
                         material_to_choice=question.material_to_choice, safety_relevant=question.safety_relevant)
        entry["could_change_choice_or_safety"] = question is None or question.material_to_choice or question.safety_relevant
        missing.append(entry)
    questions = [{"id": q.id, "question": q.question, "material_to_choice": q.material_to_choice,
                  "safety_relevant": q.safety_relevant} for q in sorted(record.questions.values(), key=lambda q: q.id)
                 if q.blocks_final_choice]
    return {"status": assessment.status, "service_class": assessment.service_class, "final_ready": assessment.final_ready,
            "blocking": list(assessment.blocking), "missing": missing, "open_questions": questions}


def _use_case_finding(findings: _Findings, assessment: Any, section: Mapping[str, Any]) -> None:
    critical = [m["field"] for m in section["missing"] if m["could_change_choice_or_safety"]]
    findings.add("selection.use_case_incomplete", "use_case", {"product": True}, "major", "unknown",
                 measured=_unknown("; ".join(assessment.blocking)),
                 criterion={"property": "use_case_readiness", "operator": "equals", "value": "final_ready",
                            "source": _policy_source("a final material and process recommendation needs a confirmed use case "
                                                     "with every material-to-choice and safety fact known or tracked")},
                 method="manufacturing_intent.assess_use_case",
                 uncertainty=f"missing facts that could change the choice or create a safety risk: {', '.join(critical) or 'none'}",
                 consequence="only a conditional recommendation with explicit limits is possible; no final choice",
                 remedy="answer the open questions "
                        f"({', '.join(q['id'] for q in section['open_questions']) or 'none open'}) and confirm the use case "
                        "with an approving decision", inputs=("hw/product.yaml use_case", "hw/requirements.yaml open_questions"))


def _decision(assessor: _Assessor, group: list[Candidate], assessment: Any, findings: _Findings) -> dict[str, Any]:
    assessed = [assessor.assess(candidate) for candidate in group]
    assessor.rank(assessed)
    ordered = sorted(assessed, key=lambda a: (a.rank is None, a.rank or 0, a.id))
    ranked = [a for a in ordered if a.rank is not None]
    primary = ranked[0] if ranked else None
    fallback = ranked[1] if len(ranked) > 1 else None
    prototype = _prototype(ordered, primary, fallback)
    final_allowed = assessment.final_ready and primary is not None and primary.status == FEASIBLE
    refused = [] if final_allowed else list(assessment.blocking)
    if primary is None:
        refused.append("every candidate is eliminated by a mandatory constraint")
    elif primary.status == CONDITIONAL:
        refused.append(f"the primary {primary.id} is conditional: "
                       + ", ".join(sorted({e.constraint for e in primary.mandatory('unknown')})) + " unknown")
    parts = sorted({pn for candidate in group for pn in candidate.pns})
    decision: dict[str, Any] = {"parts": parts, "final_allowed": final_allowed, "refused_because": refused,
                                "candidates": [], "comparison": [assessor.row(a) for a in ordered]}
    for a in ordered:
        decision["candidates"].append({
            "id": a.id, "title": a.candidate.title, "architecture": a.candidate.architecture, "status": a.status,
            "rank": a.rank, "families": sorted(_families(a.candidate)), "recorded_route": dict(sorted(a.recorded.items())),
            "prototype_eligible": a.prototype_eligible,
            "eliminated_by": sorted({e.constraint for e in a.mandatory("fail")}),
            "unknown": sorted({e.constraint for e in a.mandatory("unknown")}),
            "preference": {"score": a.score, "compared_on": list(a.compared_on), "not_compared": list(a.not_compared),
                           "unscored": list(a.unscored)},
            "parts": [{"PN": p.pn, "material_profile": p.material if isinstance(p.material, str) else _open_fact(p.material),
                       "shaping_kind": p.shaping_kind, "route": [{"kind": s.kind, "capability": s.capability if isinstance(s.capability, str)
                                                                 else s.capability.status, "provider": s.provider} for s in p.route]}
                      for p in a.candidate.parts],
            "pairs": assessor.pairs(a.candidate),
            "evaluations": [e.as_dict() for e in a.evaluations]})
    _candidate_findings(findings, ordered, primary, assessor)
    if primary is None or primary.status != FEASIBLE:
        _coverage_finding(findings, parts, ordered)
    decision["optical"] = _optical_section(assessor, primary)
    decision["prototype"] = _prototype_section(prototype, primary)
    decision["change_triggers"] = _change_triggers(assessor, ranked, primary) if primary is not None else []
    decision["recommendation"] = None if primary is None else _recommendation(
        assessor, ordered, primary, fallback, prototype, final_allowed, refused, decision)
    decision["approval"] = APPROVAL_GATE
    return decision


def _families(candidate: Candidate) -> set[str]:
    found = {ROUTE_FAMILIES.get(cpart.shaping_kind or "", "unsupported") for cpart in candidate.parts}
    if len(found) > 1:
        found.add("hybrid")
    return found


def _cost(assessed: _Assessed) -> tuple[str, tuple[float, float], float] | None:
    cost, quantity = assessed.candidate.fact("cost"), assessed.candidate.fact("quantity")
    if not (cost.known and quantity.known and isinstance(quantity.value, mi.Quantity)):
        return None
    return cost.value["currency"], quantity.value.to("count").bounds, float(cost.value["amount"])


def _lead_seconds(assessed: _Assessed) -> float:
    lead = assessed.candidate.fact("lead_time")
    return lead.value.to("s").bounds[1] if lead.known and isinstance(lead.value, mi.Quantity) else float("inf")


def _prototype(ordered: list[_Assessed], primary: _Assessed | None, fallback: _Assessed | None) -> _Assessed | None:
    """A cheaper candidate that may serve as a limited prototype: never the primary or the fallback, never one a
    safety, capability or optical failure rules out, and only at a cost comparable with the primary's (same
    currency and quantity)."""
    primary_cost = _cost(primary) if primary is not None else None
    if primary_cost is None:
        return None
    currency, quantity, amount = primary_cost
    options = []
    for a in ordered:
        cost = _cost(a)
        if a is primary or a is fallback or not a.prototype_eligible or cost is None or cost[:2] != (currency, quantity) \
                or cost[2] >= amount:
            continue
        options.append((cost[2], _lead_seconds(a), a.id, a))
    return min(options)[3] if options else None


def _prototype_section(prototype: _Assessed | None, primary: _Assessed | None) -> dict[str, Any] | None:
    """What a prototype route can and cannot validate for the primary: a different material or route never
    establishes the primary's strength, life or thermal performance, and its own unmet constraints stay unmet."""
    if prototype is None or primary is None:
        return None
    can, cannot = [], []
    same_parts = set(prototype.candidate.pns) == set(primary.candidate.pns)
    for cpart in prototype.candidate.parts:
        end = primary.candidate.part(cpart.pn)
        if end is None:
            cannot.append(f"{cpart.pn}: the primary has no such part, so the prototype does not represent the primary's architecture")
            continue
        if end.material == cpart.material and [s.kind for s in end.route] == [s.kind for s in cpart.route]:
            can.append(f"{cpart.pn}: the same material and route as the primary; its own process evidence still applies")
            continue
        can.append(f"{cpart.pn}: geometry, fit, assembly order and handling of the shape, as made by "
                   f"{' -> '.join(s.kind for s in cpart.route)}, not with the primary's accuracy")
        cannot.append(f"{cpart.pn}: strength, stiffness, fatigue, creep, heat, wear, service life and dimensional stability of "
                      f"{end.material_label} on {' -> '.join(s.kind for s in end.route)}; a {cpart.shaping_kind} prototype "
                      "does not establish them")
    for evaluation in prototype.evaluations:
        if evaluation.mandatory and evaluation.outcome in ("fail", "unknown"):
            cannot.append(f"{evaluation.constraint}: {evaluation.outcome} on the prototype route")
    if not same_parts:
        cannot.append("the primary's parts " + ", ".join(sorted(set(primary.candidate.pns) - set(prototype.candidate.pns)) or ["(none)"])
                      + " are not in the prototype")
    return {"candidate": prototype.id, "status": prototype.status,
            "role": "a low-cost prototype only: it can never be the primary, end-use or fallback route",
            "can_validate": sorted(dict.fromkeys(can)), "cannot_validate": sorted(dict.fromkeys(cannot))}


def _optical_section(assessor: _Assessor, primary: _Assessed | None) -> list[dict[str, Any]]:
    """For every transparent or optical part of the primary: the traceable purchase criteria to record."""
    if primary is None:
        return []
    found = []
    for cpart in primary.candidate.parts:
        part = assessor.record.parts.get(cpart.pn)
        if part is not None and _needs_optical(part):
            route = "purchased transparent part" if cpart.shaping_kind == "purchased" else "identified transparent stock"
            found.append({"PN": cpart.pn, "route": route, "purchase_criteria": assessor.optical_criteria(cpart, part)})
    return found


def _change_triggers(assessor: _Assessor, ranked: list[_Assessed], primary: _Assessed) -> list[dict[str, str]]:
    """What would change the recommendation: loads, temperature, life, tolerances, equipment, volume and
    the margins of the computed requirement results."""
    record = assessor.record
    use_case = record.use_case
    found: list[dict[str, str]] = []

    def add(kind: str, text: str) -> None:
        found.append({"kind": kind, "text": text})

    def missing(fact: Any, what: str) -> str:
        question = f" ({fact.question})" if fact.question else ""
        return f"{what} is {fact.status.replace('_', ' ')}{question}: stating it can change the choice"

    def without(capability_id: str) -> str:
        others = [a.id for a in ranked if a is not primary
                  and not any(s.capability == capability_id for p in a.candidate.parts for s in p.route)]
        return f"; ranked candidates that do not use it: {', '.join(others)}" if others else "; no ranked candidate avoids it"

    loads = use_case.loads
    if mi.is_fact(loads):
        add("loads", missing(loads, "the load case"))
    else:
        for load in loads:
            if load.magnitude.known:
                bears = ", ".join(load.requirements) or "the requirements it bears on"
                add("loads", f"a {load.kind} load {load.id} above {load.magnitude.value} re-opens {bears} for {primary.id}")
            else:
                add("loads", missing(load.magnitude, f"the magnitude of load {load.id}"))
    temperature = use_case.facts["environment.temperature"]
    if temperature.known:
        add("temperature", f"a measured operating temperature outside {temperature.value} re-opens every temperature-qualified "
                           f"result of {primary.id}")
    else:
        add("temperature", missing(temperature, "the operating temperature"))
    life = use_case.facts["motion.service_life"]
    if life.known:
        add("life", f"a service life beyond {life.value} needs fatigue, creep and wear evidence beyond this selection")
    else:
        add("life", missing(life, "the service life"))
    tolerance = use_case.facts["accuracy.tolerance"]
    if tolerance.known:
        add("tolerances", f"tolerances tighter than {tolerance.value} need calibrations of the route's capabilities")
    else:
        add("tolerances", missing(tolerance, "the required accuracy"))
    for cpart in primary.candidate.parts:
        for step in cpart.route:
            if isinstance(step.capability, str):
                add("equipment", f"if {step.capability} becomes unavailable or withdraws its approval of {cpart.material_label}, "
                                 f"{primary.id} is no longer feasible for {cpart.pn}{without(step.capability)}")
    quantity = primary.candidate.fact("quantity")
    if quantity.known:
        add("volume", f"cost and lead time are stated at {quantity.value}; another production volume changes them and can favour "
                      "tooled (casting, moulding) or purchased routes")
    else:
        add("volume", "the quantity the cost is stated at is unknown, so a production volume can change the ranking")
    for evaluation in primary.evaluations:
        measured = evaluation.measured
        if (evaluation.outcome != "pass" or not evaluation.requirement or evaluation.authored or measured is None
                or "units" not in measured or not evaluation.criterion):
            continue
        criterion = evaluation.criterion
        kind = "temperature" if mi.MATERIAL_PROPERTIES.get(criterion["property"]) == ("temperature",) else "requirement"
        add(kind, f"{evaluation.constraint} {evaluation.subject.get('PN')}: {criterion['property']} "
                  f"{_quantity_text(measured)} meets {_criterion_text(criterion)}; a requirement beyond "
                  f"{_quantity_text(measured)} eliminates {primary.id}")
    order = ("loads", "temperature", "life", "tolerances", "equipment", "volume", "requirement")
    return sorted({(t["kind"], t["text"]): t for t in found}.values(), key=lambda t: (order.index(t["kind"]), t["text"]))


def _recommendation(assessor: _Assessor, ordered: list[_Assessed], primary: _Assessed, fallback: _Assessed | None,
                    prototype: _Assessed | None, final_allowed: bool, refused: list[str],
                    decision: Mapping[str, Any]) -> dict[str, Any]:
    """The proposal, in the manufacturing-selection.schema.json recommendation shape."""
    reasons = []
    if primary.status == FEASIBLE:
        passed = sorted({e.constraint for e in primary.evaluations if e.mandatory and e.outcome == "pass"})
        reasons.append(f"{primary.id} meets every mandatory constraint it was assessed against: {', '.join(passed) or 'none applies'}")
    else:
        reasons.append(f"no candidate is feasible yet; {primary.id} is the best-ranked conditional candidate")
    if primary.score is not None:
        reasons.append(f"{primary.id} ranks first among {primary.status} candidates on {', '.join(primary.compared_on)} "
                       f"(weighted score {primary.score:g})")
    else:
        reasons.append(f"no preference score separates the {primary.status} candidates; the order falls back to the candidate id")
    governing: set[str] = set()
    for a in ordered:
        for evaluation in a.mandatory("fail"):
            if evaluation.requirement:
                governing.add(evaluation.requirement)
        if a.status == ELIMINATED:
            causes = "; ".join(f"{e.constraint} ({_subject_text(e.subject)}): {'; '.join(e.reasons) or e.method}"
                               for e in a.mandatory("fail"))
            reasons.append(f"{a.id} is eliminated: {causes}")
    limits = list(refused)
    limits += [f"{primary.id} {e.constraint} ({_subject_text(e.subject)}) is unknown: {'; '.join(e.reasons) or e.method}"
               for e in primary.mandatory("unknown")]
    limits.append("the selection compares recorded evidence (typical, estimated or authored values); it is not physical validation")
    record = assessor.record
    for ident in sorted({e.requirement for e in primary.evaluations if e.requirement}):
        levels = record.requirements[ident].evidence_required
        if levels:
            limits.append(f"{ident} still needs {', '.join(levels)} evidence on the chosen route")
    changes = primary.candidate.raw.get("changes") or {}
    proposed = [f"{key}: {text}" for key in ("design", "interfaces", "assembly", "equipment", "finishing", "inspection")
                for text in changes.get(key) or ()]
    for cpart in primary.candidate.parts:
        if not primary.recorded.get(cpart.pn, True):
            part = record.parts[cpart.pn]
            route = part.intent.route
            now = "unknown" if mi.is_fact(route) else " -> ".join(op.kind for op in route)
            current = part.intent.material_profile if isinstance(part.intent.material_profile, str) else "unknown"
            sourcing = ("; make_buy becomes buy" if cpart.shaping_kind == "purchased" and part.sourcing == "make" else
                        "; make_buy becomes make" if cpart.shaping_kind != "purchased" and part.sourcing != "make" else "")
            proposed.append(f"{cpart.pn}: record material profile {cpart.material_label} and route "
                            f"{' -> '.join(s.kind for s in cpart.route)} (now {current}, {now}){sourcing} through the approval gate")
    for entry in decision["optical"]:
        missing = [c["name"] for c in entry["purchase_criteria"] if c["status"] != "known"]
        if missing:
            proposed.append(f"{entry['PN']}: record the purchase criteria {', '.join(missing)} of the {entry['route']}")
    recommendation: dict[str, Any] = {"status": "final" if final_allowed else "conditional", "primary": primary.id}
    if prototype is not None:
        recommendation["prototype"] = prototype.id
    if final_allowed:
        recommendation["end_use"] = primary.id  # an end-use route needs the confirmed use case it serves
    if fallback is not None:
        recommendation["fallback"] = fallback.id
    if governing:
        recommendation["governing_requirements"] = sorted(governing)
    recommendation["reasons"] = reasons
    recommendation["limits"] = limits
    if decision["prototype"] is not None:
        recommendation["prototype_validates"] = ([f"can validate: {t}" for t in decision["prototype"]["can_validate"]]
                                                 + [f"cannot validate: {t}" for t in decision["prototype"]["cannot_validate"]])
    recommendation["what_would_change"] = [t["text"] for t in decision["change_triggers"]] or [
        "any change to the use case, requirements, profiles or candidates re-opens the selection"]
    if proposed:
        recommendation["proposed_changes"] = proposed
    return recommendation


def _alternatives(ordered: list[_Assessed], exclude: _Assessed) -> list[dict[str, str]]:
    return [{"text": f"{a.id}: {a.candidate.title} ({a.status})", "tradeoffs": "; ".join(a.candidate.texts("tradeoffs")) or "not stated"}
            for a in ordered if a is not exclude and a.status != ELIMINATED]


def _candidate_findings(findings: _Findings, ordered: list[_Assessed], primary: _Assessed | None, assessor: _Assessor) -> None:
    for a in ordered:
        for evaluation in a.evaluations:
            if not evaluation.mandatory or evaluation.outcome not in ("fail", "unknown"):
                continue
            pn = evaluation.subject.get("PN")
            recorded = a.recorded.get(pn, False) if pn else bool(a.recorded) and all(a.recorded.values())
            if evaluation.outcome == "fail":
                if recorded:
                    severe = assessor.service == "safety_critical" or evaluation.blocks_prototype
                    code, severity = "selection.recorded_route_eliminated", "critical" if severe else "major"
                    consequence = (f"the route the record holds for {_subject_text(evaluation.subject)} is {a.id}'s, which cannot "
                                   f"credibly meet {evaluation.constraint}: the current design is unsuitable for its intended service")
                else:
                    code, severity = "selection.candidate_eliminated", "info"
                    consequence = f"{a.id} is eliminated before any preference ranks it; no score can revive it"
            elif a is primary:
                code, severity = "selection.primary_unknown", "major"
                consequence = f"{a.id} stays conditional: it cannot be a final recommendation until {evaluation.constraint} is known"
            else:
                code, severity = "selection.candidate_unknown", "minor"
                consequence = f"{a.id} stays conditional and ranks below every feasible candidate"
            reason = "; ".join(evaluation.reasons) or evaluation.method
            uncertainty = evaluation.uncertainty or (reason if evaluation.outcome == "unknown"
                                                     else "none recorded beyond the cited criterion, source and method")
            findings.add(code, f"{a.id}_{evaluation.constraint}_{_subject_text(evaluation.subject)}", evaluation.subject, severity,
                         evaluation.outcome, measured=evaluation.measured or _unknown(reason),
                         criterion=evaluation.criterion or _unknown(f"{evaluation.constraint} states no structured criterion"),
                         method=evaluation.method, uncertainty=uncertainty, consequence=f"{consequence} ({reason})",
                         remedy=evaluation.remedy or f"resolve {evaluation.constraint} with sourced evidence",
                         inputs=_INPUTS, requirements=[evaluation.requirement] if evaluation.requirement else (),
                         alternatives=_alternatives(ordered, a))


def _coverage_finding(findings: _Findings, parts: list[str], ordered: list[_Assessed]) -> None:
    considered = set().union(*(_families(a.candidate) for a in ordered))
    open_families = [name for name in ("machined", "purchased", "sheet", "formed", "additive", "hybrid") if name not in considered]
    eliminated = [a.id for a in ordered if a.status == ELIMINATED]
    conditional = [a.id for a in ordered if a.status == CONDITIONAL]
    alternatives = [{"text": _FAMILY_ALTERNATIVES[name][0], "tradeoffs": _FAMILY_ALTERNATIVES[name][1]} for name in open_families]
    subject = {"PN": parts[0]} if len(parts) == 1 else {"product": True}
    findings.add("selection.no_feasible_candidate", "_".join(parts), subject, "major", "fail" if not conditional else "unknown",
                 measured={"text": f"{len(ordered)} candidates: eliminated {', '.join(eliminated) or 'none'}; "
                                   f"conditional {', '.join(conditional) or 'none'}"},
                 criterion={"property": "feasible_candidates", "operator": ">=", "value": 1, "units": "count",
                            "source": _policy_source("recommend a route that credibly meets every mandatory requirement")},
                 method="selection hard-constraint elimination",
                 uncertainty=f"route families not yet considered: {', '.join(open_families) or 'none'}",
                 consequence=f"no feasible material, process and architecture is known for {', '.join(parts)}",
                 remedy="add candidates from route families not yet considered (metal, purchased, sheet, formed or hybrid "
                        "constructions) with sourced profiles, or resolve the conditional candidates' unknowns; never relax a "
                        "mandatory requirement to make a candidate pass",
                 inputs=_INPUTS, alternatives=alternatives)


def _issue_finding(findings: _Findings, issue: Mapping[str, Any]) -> None:
    requirement = issue["requirement"]
    if issue["kind"] == "conflict":
        findings.add("selection.evaluation_conflict", f"{issue['candidate']}_{requirement}", issue["subject"], "major", "fail",
                     measured={"text": "an authored pass contradicts the result computed from the candidate's own profile"},
                     criterion=issue["criterion"], method="authored evaluation against the computed material result",
                     uncertainty="the authored evaluation may rest on other conditions or another grade",
                     consequence=f"{issue['candidate']} stays eliminated: an authored pass never overrides a sourced failure",
                     remedy="correct the candidate's material profile or its conditions, or withdraw the authored pass",
                     inputs=_INPUTS, requirements=[requirement])
        return
    findings.add("selection.evaluation_mismatch", f"{issue['candidate']}_{requirement}", issue["subject"], "major", "unknown",
                 measured={"text": f"an authored pass against {_criterion_text(issue['criterion'])}"},
                 criterion=issue["required"][0],
                 method="authored evaluation criterion against the requirement's acceptance",
                 uncertainty="the authored criterion differs from every acceptance criterion of the requirement",
                 consequence=f"the pass does not count: {requirement} stays unevaluated for {issue['candidate']}",
                 remedy=f"evaluate {requirement}'s own acceptance criterion; changing the criterion needs an approved "
                        "requirement change, never a silent substitution", inputs=_INPUTS, requirements=[requirement])


def _recorded_section(record: Any, current: str | None) -> dict[str, Any]:
    selection = record.selection
    assessment = selection.raw.get("assessment") if isinstance(selection.raw.get("assessment"), Mapping) else None
    section: dict[str, Any] = {"status": selection.status}
    if assessment is not None:
        recorded = assessment.get("input_sha256")
        section["assessment"] = {**mi.thaw(assessment), "current": None if current is None else recorded == current}
    if selection.recommendation is not None:
        section["recommendation"] = mi.thaw(selection.recommendation)
    return section


def _recorded_findings(findings: _Findings, report: Mapping[str, Any]) -> None:
    """Check what selection.yaml records against this assessment: a stale digest, a final recommendation the use
    case or the candidates do not support, and a recommended candidate a mandatory constraint eliminates."""
    section = report["recorded"]
    assessment = section.get("assessment")
    if assessment is not None and assessment.get("current") is False:
        findings.add("selection.assessment_stale", "assessment", {"product": True}, "major", "unknown",
                     measured={"text": f"recorded input_sha256 {assessment.get('input_sha256')}"},
                     criterion={"property": "input_sha256", "operator": "equals", "value": report["input_sha256"],
                                "source": _policy_source("a recorded assessment holds only for the inputs it read")},
                     method="input_digest of the selection inputs", uncertainty=f"digest scope: {', '.join(DIGEST_SCOPE)}",
                     consequence="the recorded assessment and recommendation may rest on superseded inputs",
                     remedy="re-run the selection, review the changes and record the new assessment through the approval gate",
                     inputs=_INPUTS)
    recommendation = section.get("recommendation")
    if recommendation is None:
        return
    statuses = {c["id"]: (c["status"], c["prototype_eligible"], decision)
                for decision in report["decisions"] for c in decision["candidates"]}
    final = section["status"] == "confirmed" or recommendation.get("status") == "final"
    primary = recommendation.get("primary")
    criterion = {"property": "recorded_recommendation", "operator": "equals", "value": "supported",
                 "source": _policy_source("a final recommendation needs a ready use case and a candidate that passes every "
                                          "mandatory constraint")}
    for role in ("primary", "end_use", "fallback", "prototype"):
        ident = recommendation.get(role)
        if ident not in statuses:
            continue
        status, eligible, decision = statuses[ident]
        bad = status == ELIMINATED and (role != "prototype" or not eligible)
        if bad:
            findings.add("selection.recorded_recommendation_eliminated", f"{role}_{ident}", {"product": True}, "critical", "fail",
                         measured={"text": f"recorded {role} {ident} is {status}"}, criterion=criterion,
                         method="recorded recommendation against the assessed candidates",
                         uncertainty="the candidate's eliminating evaluations are in the decision report",
                         consequence=f"selection.yaml names an eliminated candidate as {role}: a preference cannot revive it",
                         remedy=f"record a feasible candidate as {role} through the approval gate", inputs=_INPUTS)
    if final and primary in statuses:
        status, _, decision = statuses[primary]
        if not decision["final_allowed"] or status != FEASIBLE:
            findings.add("selection.final_unsupported", f"final_{primary}", {"product": True}, "critical", "fail",
                         measured={"text": f"recorded final recommendation of {primary}, which is {status}"}, criterion=criterion,
                         method="recorded recommendation against assess_use_case and the candidates",
                         uncertainty="; ".join(decision["refused_because"]) or "none",
                         consequence="a final material and process choice is recorded without the facts or evidence it needs",
                         remedy="return the selection to conditional, answer the blocking questions and evaluate the unknowns",
                         inputs=_INPUTS)
    if primary in statuses:
        computed = statuses[primary][2]["recommendation"]
        if computed is not None and computed["primary"] != primary and statuses[primary][0] != ELIMINATED:
            findings.add("selection.recommendation_differs", f"differs_{primary}", {"product": True}, "minor", "unknown",
                         measured={"text": f"recorded primary {primary}, assessed primary {computed['primary']}"},
                         criterion=criterion, method="recorded recommendation against the preference ranking",
                         uncertainty="a human decision may weigh tradeoffs the recorded preferences omit",
                         consequence="the recorded choice differs from the ranking of the recorded preferences",
                         remedy="record the tradeoff that justifies the choice, or the preference that expresses it", inputs=_INPUTS)


# --- entry points ----------------------------------------------------------------------------------------------

def assess_hw(hw: Path) -> dict[str, Any]:
    """Read, normalize and schema-check the record at `hw`, then assess it (see `assess`)."""
    hw = Path(hw)
    documents, problems = mi.read_documents(hw)
    record = mi.normalize_record(documents, hw=hw)
    problems = [*problems, *mi.schema_problems(documents), *record.problems]
    return assess(record, problems=problems, input_sha256=input_digest(documents))


def exit_status(report: Mapping[str, Any]) -> int:
    """0 when every decision may be confirmed final, 2 for an invalid record, otherwise 1."""
    return {"final_ready": 0, "invalid": 2}.get(report["status"], 1)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", "<br>")


def _yes(flag: Any) -> str:
    return "unknown" if flag is None else "yes" if flag else "no"


def render_markdown(report: Mapping[str, Any]) -> str:
    """The report as Markdown: use-case readiness, a comparison table per decision, the proposal and the findings."""
    lines = ["# Material, process and architecture selection", "",
             f"Status: **{report['status']}** (exit {exit_status(report)}); record version {report['record_version']}.",
             f"Read-only: {report['approval']}.", ""]
    use_case = report["use_case"]
    lines += ["## Use case", "",
              f"Status {use_case['status']}, service class {use_case['service_class'] or 'unknown'}; final recommendation "
              f"{'allowed' if use_case['final_ready'] else 'refused'}."]
    lines += [f"- {reason}" for reason in use_case["blocking"]]
    critical = [m for m in use_case["missing"] if m["could_change_choice_or_safety"]]
    if critical:
        lines += ["", "Missing facts that could change the material or process choice or create a safety risk:", "",
                  "| Field | Question | Material to choice | Safety relevant |", "|---|---|---|---|"]
        for m in critical:
            lines.append(f"| {_cell(m['field'])} | {_cell(m.get('question') or 'none tracks it')}"
                         f" | {_yes(m.get('material_to_choice'))} | {_yes(m.get('safety_relevant'))} |")
    lines.append("")
    for decision in report["decisions"]:
        lines += [f"## Decision: {', '.join(decision['parts'])}", "",
                  "| " + " | ".join(header for _, header in COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
        for row in decision["comparison"]:
            lines.append("| " + " | ".join(_cell(row[key]) for key, _ in COLUMNS) + " |")
        lines.append("")
        recommendation = decision["recommendation"]
        if recommendation is None:
            lines += ["No recommendation: every candidate is eliminated.", ""]
        else:
            lines += [f"### Recommendation ({recommendation['status']})", "",
                      f"- Primary: {recommendation['primary']}"]
            for role in ("end_use", "fallback", "prototype"):
                if role in recommendation:
                    lines.append(f"- {role.replace('_', '-').capitalize()}: {recommendation[role]}")
            if "governing_requirements" in recommendation:
                lines.append(f"- Governing requirements: {', '.join(recommendation['governing_requirements'])}")
            for title, key in (("Reasons", "reasons"), ("Limits", "limits"), ("What the prototype validates", "prototype_validates"),
                               ("What would change it", "what_would_change"), ("Proposed changes", "proposed_changes")):
                if recommendation.get(key):
                    lines += ["", f"{title}:"] + [f"- {_cell(text)}" for text in recommendation[key]]
            lines.append("")
        for entry in decision["optical"]:
            lines += [f"Purchase criteria for {entry['PN']} ({entry['route']}):"]
            lines += [f"- {c['name']}: {c.get('value') or 'unknown: ' + c.get('reason', '')}" for c in entry["purchase_criteria"]]
            lines.append("")
    if report["findings"]:
        lines += ["## Findings", ""]
        for finding in report["findings"]:
            subject = _subject_text(finding["subject"])
            lines.append(f"- **{finding['severity']}** `{finding['code']}` {subject}: {_cell(finding['consequence'])}. "
                         f"Remedy: {_cell(finding['remedy'])}")
        lines.append("")
    if report["problems"]:
        lines += ["## Record problems", ""]
        lines += [f"- {p['severity']} `{p['code']}` {p['path']}: {_cell(p['message'])}" for p in report["problems"]]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_dir", type=Path, help="project directory containing hw/, or the hw/ directory itself")
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of Markdown")
    parser.add_argument("--out", type=Path, help="also write the JSON report here, under the project's .cad-work/")
    args = parser.parse_args(argv)
    hw = (args.project_dir / "hw" if (args.project_dir / "hw").is_dir() else args.project_dir).resolve()
    if not (hw / "product.yaml").is_file():
        print(json.dumps({"status": "error", "message": f"{hw} holds no design record (no product.yaml)"}))
        return 2
    target = None
    if args.out is not None:
        try:
            target = layout.scratch_path(hw, args.out, "--out")
        except layout.LayoutError as error:
            print(json.dumps({"status": "error", "message": str(error)}))
            return 2
    report = assess_hw(hw)
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    print(text if args.json else render_markdown(report))
    return exit_status(report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001 - one JSON report for any infrastructure failure
        print(json.dumps({"status": "error", "message": f"infrastructure error: {error}"}))
        raise SystemExit(2)
