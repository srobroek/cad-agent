# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""The vocabulary every process handler shares: results, finding drafts, the per-operation context and
the geometry facts adapter.

Nothing here imports a CAD kernel. Geometry arrives as the plain probe facts form_check.py measures on
the exported manufacturing component (see `Geometry`); without them every geometric check is UNKNOWN.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re
import sys
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

INTENT_PATH = Path(__file__).resolve().parents[3] / "cad-record" / "scripts" / "manufacturing_intent.py"
INTENT_MODULE = "cad_record_manufacturing_intent"  # the shared name cad-model and cad-record load it under
ASSEMBLY_DATA_PATH = INTENT_PATH.with_name("assembly_data.py")
ASSEMBLY_DATA_MODULE = "cad_record_assembly_data"  # the shared name cad-motion and cad-record load it under
GUIDELINES_PATH = INTENT_PATH.with_name("guidelines.py")
GUIDELINES_MODULE = "cad_record_guidelines"  # the shared name manufacturing_intent loads it under

GUIDELINE_PROCESSES = frozenset({"fdm"})  # the processes the shipped guideline sets cover (resin and the rest have none)

# The service classes whose checks take a guideline value as a pass (the user's rule). Every other class, unknown
# included, needs a calibrated or a waived value: a guideline pass is unknown there. A guideline fail fails in every class.
GUIDELINE_PASS_CLASSES = frozenset({"appearance_concept", "fit_assembly", "functional_demo"})

PASS, FAIL, UNKNOWN, NOT_APPLICABLE = "pass", "fail", "unknown", "not_applicable"
IMPLICIT = "main"  # the component id of a sole solid (cad-model components.IMPLICIT)
PROBE_FORMAT = 1  # the probe facts form_check.py writes; bump when a key's meaning changes
DIRECTIONS = {"+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0), "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
              "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0)}
PARALLEL_COS = math.cos(math.radians(1.0))  # a feature axis within 1 degree of an access direction is on it

# The plugin rules this module applies are policy, never product facts or capability numbers.
POLICY = types.MappingProxyType({
    "kind": "policy", "reference": "benchhand manufacturing-intent dispatch policy (cad-design process_checks)",
    "origin": "benchhand plugin", "document": "skills/cad-design/scripts/process_checks/__init__.py"})


def _load(name: str, path: Path) -> Any:
    """The cad-record module at `path`, loaded once and registered in sys.modules as `name`."""
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def intent() -> Any:
    """cad-record manufacturing_intent.py under its shared module name."""
    return _load(INTENT_MODULE, INTENT_PATH)


def assembly_data() -> Any:
    """cad-record assembly_data.py (the occurrence inventory) under its shared module name."""
    return _load(ASSEMBLY_DATA_MODULE, ASSEMBLY_DATA_PATH)


def guidelines() -> Any:
    """cad-record guidelines.py (discovery and resolution of guideline limits) under its shared module name."""
    return _load(GUIDELINES_MODULE, GUIDELINES_PATH)


# --- results ----------------------------------------------------------------------------------------------------

@dataclass
class Draft:
    """A finding before the dispatcher gives it its id and obligation: every field the finding schema needs."""

    code: str
    severity: str
    outcome: str
    measured: Mapping[str, Any]
    criterion: Mapping[str, Any]
    method: str
    uncertainty: str
    consequence: str
    remedy: str | None = None
    alternatives: Sequence[Mapping[str, str]] = ()
    recheck: Sequence[str] = ()
    earliest_stage: str = "detail"
    feature: str | None = None
    coverage: str | None = None
    assumptions: Sequence[str] = ()
    requirements: Sequence[str] = ()
    link: bool = True  # False: the finding names evidence another producer or the ledger owns


@dataclass
class Result:
    """One obligation's outcome with what the method covered and the findings behind it."""

    outcome: str
    method: str
    coverage: str
    findings: list[Draft] = field(default_factory=list)
    uncertainty: str | None = None
    source: Mapping[str, Any] | None = None


def combine(method: str, parts: Iterable[tuple[str, str]], findings: list[Draft], *,
            uncertainty: str | None = None, source: Mapping[str, Any] | None = None) -> Result:
    """A Result from sub-check (outcome, coverage) pairs: fail if any fails, else unknown if any is
    unknown, else pass; not_applicable sub-checks drop out, and all of them makes it not_applicable."""
    parts = list(parts)
    outcomes = [o for o, _ in parts if o != NOT_APPLICABLE]
    if not outcomes:
        outcome = NOT_APPLICABLE
    elif FAIL in outcomes:
        outcome = FAIL
    elif UNKNOWN in outcomes:
        outcome = UNKNOWN
    else:
        outcome = PASS
    coverage = "; ".join(c for _, c in parts if c) or "nothing to assess"
    return Result(outcome, method, coverage, findings, uncertainty, source)


def text(value: str) -> dict[str, str]:
    return {"text": value}


def length(value: float) -> dict[str, Any]:
    return {"value": round(float(value), 6) + 0.0, "units": "mm"}


def angle(value: float) -> dict[str, Any]:
    return {"value": round(float(value), 4) + 0.0, "units": "deg"}


def unknown(reason: str) -> dict[str, str]:
    return {"status": "unknown", "reason": reason}


def source_dict(source: Any) -> dict[str, Any]:
    """A Source DTO (or mapping) in its schema form, without empty fields."""
    if isinstance(source, Mapping):
        return {k: v for k, v in source.items() if v is not None}
    return {name: getattr(source, name) for name in source.__dataclass_fields__ if getattr(source, name) is not None}


def policy(prop: str, operator: str, value: Any, units: str | None = None, note: str | None = None) -> dict[str, Any]:
    """A criterion this plugin's dispatch policy sets (never a capability number)."""
    data: dict[str, Any] = {"property": prop, "operator": operator, "value": value, "source": dict(POLICY)}
    if units:
        data["units"] = units
    if note:
        data["note"] = note
    return data


def stable_id(*parts: Any) -> str:
    """A finding id that stays the same for the same code, subject and detail on every run."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]
    return f"f_{digest}"


def _noted(coverage: str, note: str) -> str:
    """`coverage` with `(note)` appended, once however many limits a check graded."""
    tag = f"({note})"
    return coverage if coverage.endswith(tag) else f"{coverage} {tag}"


# --- calibrations -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Calibrated:
    """A capability_limit answer with its criterion form, or why there is none."""

    status: str  # calibrated, unknown or refused
    characteristic: str
    limits: Any  # a Quantity when calibrated
    reasons: tuple[str, ...]
    calibration: Any = None

    @property
    def known(self) -> bool:
        return self.status == "calibrated"

    def bounds(self, units: str = "mm") -> tuple[float, float]:
        bounds: tuple[float, float] = self.limits.to(units).bounds  # manufacturing_intent.Quantity.bounds
        return bounds

    def criterion(self, prop: str | None = None, operator: str = "between", value: Any = None,
                  units: str | None = None) -> dict[str, Any]:
        """The calibration as a finding criterion; unknown with the reasons when it is not calibrated."""
        if not self.known:
            return unknown("; ".join(self.reasons) or f"no calibration of {self.characteristic}")
        cal = self.calibration
        limits = self.limits
        units = units or limits.units
        if value is None:
            lower, upper = limits.to(units).bounds
            if operator == "between":
                value = [lower, upper]
            elif operator in (">=", ">", "<=", "<"):  # a minimum must hold at the calibration's worst (upper) end
                value = upper if operator in (">=", ">") else lower
            else:
                raise ValueError(f"a calibration states no criterion value for operator {operator!r}")
        scope = ", ".join(f"{k} {v}" for k, v in sorted(cal.scope.items()) if not isinstance(v, Mapping))
        return {"id": cal.id, "property": prop or self.characteristic, "operator": operator, "value": value,
                "units": units, "source": source_dict(cal.source),
                "conditions": f"calibration {cal.id} ({cal.samples} samples, {cal.date}): {scope}"}


MINIMUM, MAXIMUM = "minimum", "maximum"
_SIDE_OPERATORS = {MINIMUM: (">=", ">"), MAXIMUM: ("<=", "<")}
EPS = 1e-9


@dataclass(frozen=True)
class Limit:
    """The limit a check judges one characteristic against: the calibration of the operation's capability profile,
    else the value the guideline sets give for that profile and material (cad-record guidelines.resolve_guidelines),
    else unknown with every reason. `side` is how the check reads it: a minimum the measured value must reach
    (min_wall, min_feature, min_hole) or a maximum it must stay within (overhang_angle, bridge_span)."""

    characteristic: str
    side: str
    calibrated: Calibrated
    resolved: Any = None  # guidelines.Resolved; None when no catalog was given or the operation has no capability profile
    base_mm: float | None = None  # the profile's line width, else its nozzle diameter; None when it states neither

    @property
    def status(self) -> str:
        """calibrated, guideline or unknown. A guideline limit on the other side of the check (a maximum for a
        minimum check, an interval) is no answer to it, so it is unknown."""
        if self.calibrated.known:
            return "calibrated"
        if self.resolved is not None and self.resolved.status == "guideline" and \
                self.resolved.operator in _SIDE_OPERATORS[self.side]:
            return "guideline"
        return "unknown"

    @property
    def known(self) -> bool:
        return self.status != "unknown"

    @property
    def warnings(self) -> tuple[str, ...]:
        """What the resolver warned about while resolving this limit (a relaxation, a pin that matched nothing)."""
        return tuple(self.resolved.warnings) if self.resolved is not None else ()

    @property
    def reasons(self) -> tuple[str, ...]:
        found = list(self.calibrated.reasons)
        if self.resolved is None:
            found.append(f"no guideline set was consulted for {self.characteristic}")
        elif self.resolved.status != "guideline":
            found.extend(self.resolved.reasons or (f"no guideline states {self.characteristic}",))
        elif self.status == "unknown":
            found.append(f"the guideline {self.characteristic} limit is {self.resolved.operator}, not a {self.side}")
        found.extend(f"guideline resolver warning: {w}" for w in self.warnings)
        return tuple(found)

    @property
    def operator(self) -> str:
        if self.status == "guideline":
            operator: str = self.resolved.operator
            return operator
        return _SIDE_OPERATORS[self.side][0]

    def threshold(self, units: str) -> float:
        """The value a measurement is compared with: a calibration's worst end (upper for a minimum, lower for a
        maximum), or the guideline value. Only for a known limit."""
        if self.status == "calibrated":
            lower, upper = self.calibrated.bounds(units)
            return upper if self.side == MINIMUM else lower
        assert self.status == "guideline", f"{self.characteristic} is unknown"
        return float(self.resolved.value.to(units).value)

    def breaks(self, measured: float, units: str) -> bool:
        """Whether `measured` misses the limit. > and < are strict: a value equal to the limit misses them."""
        limit = self.threshold(units)
        strict = self.operator in (">", "<")
        if self.side == MINIMUM:
            return measured <= limit + EPS if strict else measured < limit - EPS
        return measured >= limit - EPS if strict else measured > limit + EPS

    def criterion(self, prop: str, units: str) -> dict[str, Any]:
        """The limit as a finding criterion: the calibration's, the guideline's with its source (id, revision and
        sha256 of the set) and where it comes from, or unknown with the reasons."""
        if self.status == "calibrated":
            return self.calibrated.criterion(prop, self.operator, self.threshold(units), units)
        if self.status == "unknown":
            return unknown("; ".join(self.reasons))
        winner = self.resolved.provenance[0]
        conditions = f"guideline {winner.guideline} ({winner.layer} layer, {winner.path}): a design guideline, " \
                     "not a calibration of this machine and material"
        if self.calibrated.status == "refused":
            conditions += f"; calibrations not used: {'; '.join(self.calibrated.reasons)}"
        if self.warnings:
            conditions += f"; guideline resolver warnings: {'; '.join(self.warnings)}"
        return {"property": prop, "operator": self.operator, "value": self.threshold(units), "units": units,
                "source": dict(self.resolved.source), "conditions": conditions}


# --- the geometry facts adapter ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Geometry:
    """The probe facts of one exported manufacturing component, in its exported frame.

    `facts` is form_check.py's probe dict (PROBE_FORMAT): `frame` (bed: exported in its approved additive
    pose, build direction +Z, bed at z 0; model: the native frame), `solids`, `shells`, `volume_mm3`,
    `bbox_mm{min,max}`, `centre_mm`, `bed{z_mm, contact_area_mm2, contact_hull, contact_extent}`, `faces[{id, type,
    area_mm2, features, down{max_angle_deg, area_mm2, angles[[deg, mm2]], kind, span_mm, start{bed_mm2,
    part_mm2, features}}, normals[[x,y,z]], visible{direction: fraction}}]`, `holes[{id, diameter_mm, axis,
    position, depth_mm, through, features, floor_mm}]`, `corners[{radius_mm, axis, length_mm, features}]`,
    `pins[{diameter_mm, features}]`, `bends[{inner_radius_mm, thickness_mm, features}]`, `walls{required_mm,
    thickness_mm, location_mm, features, measured}`, `features{id: {kind, faces, mapped, width_mm?}}` and
    `unmapped` (locator ids that matched no face). `evidence` holds the file_hash rows of the files measured.
    """

    pn: str
    component: str
    facts: Mapping[str, Any]
    evidence: tuple[Mapping[str, str], ...] = ()

    @property
    def frame(self) -> str:
        return str(self.facts.get("frame"))

    def faces(self) -> list[Mapping[str, Any]]:
        return list(self.facts.get("faces") or ())

    def holes(self) -> list[Mapping[str, Any]]:
        return list(self.facts.get("holes") or ())

    def corners(self) -> list[Mapping[str, Any]]:
        return list(self.facts.get("corners") or ())

    def pins(self) -> list[Mapping[str, Any]]:
        return list(self.facts.get("pins") or ())

    def bends(self) -> list[Mapping[str, Any]]:
        return list(self.facts.get("bends") or ())

    def feature(self, ident: str) -> Mapping[str, Any] | None:
        found = (self.facts.get("features") or {}).get(ident)
        return found if isinstance(found, Mapping) and found.get("mapped") else None

    def feature_faces(self, ident: str) -> list[Mapping[str, Any]]:
        found = self.feature(ident)
        names = set(found.get("faces") or ()) if found else set()
        return [face for face in self.faces() if face.get("id") in names]

    def method(self, what: str) -> str:
        return f"{what} on the exported {self.frame}-frame manufacturing STEP of {self.pn} {self.component} (form_check probe)"


# --- the context of one obligation ------------------------------------------------------------------------------

def component_ids(part: Any) -> list[str]:
    """The component ids of `part`: its declared components, else the implicit sole solid."""
    return [c.id for c in part.intent.components] or [IMPLICIT]


def component_contexts(record: Any, pn: str, component: str, catalog: Any, today: date,
                       calibration: bool = True) -> list[Context]:
    """The context of every route operation that acts on `component` of part `pn`, in route order: the operations
    a component-wide scan (probe_plan) reads. A part the record does not hold has none."""
    part = record.parts.get(pn)
    if part is None or intent().is_fact(part.intent.route):
        return []
    return [Context(record, pn, component, op, None, today, catalog=catalog, calibration=calibration)
            for op in part.intent.route if not op.components or component in op.components]


def guideline_scope(record: Any, pn: str, catalog: Any, today: date) -> tuple[list[Any], list[str]]:
    """(guideline scopes, reasons) of part `pn`: the guidelines.Context of each route operation on each of its
    components that has one (Context.guideline_context: capability profile and stock material), in route order. With
    none, the scopes are empty and the reasons say why each operation, or the part, has none. The reading of the scope
    that form_check's part-level diagnostic shares with the process checks."""
    part = record.parts.get(pn)
    if part is None:
        return [], [f"{pn} is not a part of the record"]
    route = part.intent.route
    if intent().is_fact(route):
        return [], [f"the route of {pn} is not recorded"]
    ids = component_ids(part)
    scopes: list[Any] = []
    reasons: list[str] = []
    for component in ids:
        for ctx in component_contexts(record, pn, component, catalog, today):
            scope = ctx.guideline_context()
            if scope is not None:
                scopes.append(scope)
                continue
            op = ctx.operation
            if ctx.profile is not None:
                reasons.append(f"{ctx.label}: {op.capability} is a {ctx.profile.process} capability"
                               + (", which no guideline set covers" if catalog is not None else " and no guideline catalog is given"))
            elif isinstance(op.capability, str):
                reasons.append(f"{ctx.label} names capability {op.capability}, which the record does not hold")
            else:
                reasons.append(f"{ctx.label} names no capability profile")
    if scopes:
        return scopes, []
    if not reasons:
        reasons = [f"none of its {len(route)} route operations acts on a component of {pn} ({', '.join(ids)})" if route
                   else f"{pn} has no route operation"]
    return [], list(dict.fromkeys(reasons))


def parse_side(value: str) -> tuple[float, float, float] | None:
    """An access side as an axis direction of the native manufacturing frame (+X .. -Z); None otherwise."""
    key = re.sub(r"\s+", "", str(value)).lower()
    if re.fullmatch(r"[xyz][+-]", key):
        key = key[1] + key[0]
    return DIRECTIONS.get(key)


@dataclass(frozen=True)
class Context:
    """One obligation's view of the record: the part, its component, the route operation (None for a
    component- or part-level check), the version geometry of the component and the date calibrations
    must still be valid on.

    Operation-scoped handlers (process.<kind>.*, purchase.*, route.operation_plan) read `operation`, which
    the dispatcher guarantees; checks behind `capability_gate` read `capability`; geometric checks take
    their geometry from `measured`, which answers UNKNOWN when the component was not measured."""

    record: Any
    pn: str
    component: str
    op: Any | None
    geometry: Geometry | None
    today: date
    geometries: Mapping[tuple[str, str], Geometry] = field(default_factory=dict)
    catalog: Any = None  # cad-record guidelines.Catalog; None: no guideline set is consulted, so only calibrations count
    resolutions: list[Any] = field(default_factory=list, compare=False)  # guidelines.Resolved of every limit looked up (shared)
    consulted: list[Any] = field(default_factory=list, compare=False)  # the same, for this obligation alone
    calibration: bool = True  # the calibration flag, read once per assessment (assess); off ignores FDM calibrations
    ignored: list[Any] = field(default_factory=list, compare=False)  # recorded FDM calibrations skipped with it off (shared)

    # record views ---------------------------------------------------------------------------------------------
    @property
    def part(self) -> Any:
        return self.record.parts[self.pn]

    @property
    def mi(self) -> Any:
        return intent()

    @property
    def operation(self) -> Any:
        """The route operation an operation-scoped check judges (`op`, which the dispatcher sets for every
        obligation whose subject names an operation)."""
        if self.op is None:
            raise LookupError(f"{self.label}: an operation check ran for an obligation that names no operation")
        return self.op

    @property
    def declared(self) -> Any | None:
        """The declared component (None for the implicit sole solid)."""
        return next((c for c in self.part.intent.components if c.id == self.component), None)

    @property
    def route(self) -> tuple[Any, ...]:
        route = self.part.intent.route
        return () if self.mi.is_fact(route) else tuple(route)

    def shaping(self) -> list[Any]:
        """The route operations that act on this component, in route order."""
        return [op for op in self.route
                if not op.components or self.component in op.components]

    def before(self, op: Any | None = None) -> list[Any]:
        """The operations on this component that `op` (default: this operation) transitively depends on."""
        try:
            predecessors = self.mi.part_route(self.record, self.pn).predecessors
        except self.mi.IntentError:
            return []
        seen: list[str] = []
        stack = list(predecessors.get((op or self.operation).id, ()))
        while stack:
            ident = stack.pop()
            if ident not in seen:
                seen.append(ident)
                stack.extend(predecessors.get(ident, ()))
        mine = {o.id for o in self.shaping()}
        return [o for o in self.route if o.id in seen and o.id in mine]

    def after(self, op: Any | None = None) -> list[Any]:
        """The operations on this component that depend (transitively) on `op`."""
        op = op or self.operation
        return [o for o in self.shaping() if o.id != op.id and op.id in {b.id for b in self.before(o)}]

    @property
    def subject(self) -> dict[str, Any]:
        data: dict[str, Any] = {"PN": self.pn}
        if self.declared is not None:
            data["component"] = self.component
        if self.op is not None:
            data["operation"] = self.op.id
        return data

    @property
    def label(self) -> str:
        where = self.pn if self.declared is None else f"{self.pn} {self.component}"
        return f"{where} {self.op.id}" if self.op is not None else where

    @property
    def material_id(self) -> str | None:
        declared = self.declared
        chosen = declared.material_profile if declared is not None and declared.material_profile is not None \
            else self.part.intent.material_profile
        return chosen if isinstance(chosen, str) else None

    @property
    def material(self) -> Any | None:
        ident = self.material_id
        return self.record.materials.get(ident) if ident else None

    def stock_material_id(self) -> str | None:
        """The stock profile the operation's setup names, else the component's material profile."""
        stock = ((self.op.setup or {}).get("stock") or {}) if self.op is not None else {}
        named = stock.get("material_profile")
        return named if isinstance(named, str) else self.material_id

    @property
    def profile(self) -> Any | None:
        capability = self.op.capability if self.op is not None else None
        return self.record.capabilities.get(capability) if isinstance(capability, str) else None

    @property
    def capability(self) -> Any:
        """The operation's capability profile, for a check that runs behind `capability_gate` (which answers
        the obligation itself when there is none)."""
        profile = self.profile
        if profile is None:
            raise LookupError(f"{self.label}: a capability check ran without a capability profile")
        return profile

    def functions(self) -> set[str]:
        intent_ = self.part.intent
        found = set() if self.mi.is_fact(intent_.functions) else {f.function for f in intent_.functions}
        declared = self.declared
        if declared is not None:
            found |= {f.function for f in declared.functions}
        return found

    def surfaces(self) -> list[Any]:
        """The critical surfaces of this component (a surface without a component is the part's)."""
        surfaces = self.part.intent.critical_surfaces
        if self.mi.is_fact(surfaces):
            return []
        return [s for s in surfaces if s.component is None or s.component == self.component]

    def surface_raw(self, surface: Any) -> Mapping[str, Any]:
        """The schema form of a critical surface (criteria and finish live only there)."""
        for item in ((self.part.raw.get("intent") or {}).get("critical_surfaces") or ()):
            if isinstance(item, Mapping) and item.get("id") == surface.id:
                return item
        return {}

    def split_criteria(self, surface: Any, prop: str, operators: Iterable[str]) -> tuple[list[Mapping[str, Any]], list[str]]:
        """The criteria of `prop` on `surface` whose operator is in `operators`, and the operators of its other criteria.
        A check evaluates the first and turns a non-empty second into UNKNOWN: a criterion is never dropped silently."""
        items = [c for c in self.surface_raw(surface).get("criteria") or () if c.get("property") == prop]
        return ([c for c in items if c.get("operator") in operators],
                sorted({str(c.get("operator")) for c in items if c.get("operator") not in operators}))

    def critical_features(self) -> dict[str, Any]:
        return {s.feature: s for s in self.surfaces()}

    def targets(self, op: Any | None = None) -> set[str]:
        op = op or self.op
        return {t.get("feature") for t in (op.targets if op is not None else ()) if t.get("feature")}

    def inspections(self) -> dict[str, Mapping[str, Any]]:
        """Every inspection entry of the route by id."""
        return {item["id"]: item for op in self.route for item in op.inspections if item.get("id")}

    def allowances(self, feature: str, ops: Iterable[Any]) -> list[tuple[Any, Mapping[str, Any]]]:
        return [(op, a) for op in ops for a in op.allowances if a.get("feature") == feature]

    # capability ---------------------------------------------------------------------------------------------------
    def calibrated(self, characteristic: str, *, profile: Any | None = None, material: str | None = None,
                   orientation: str | None = None, feature: str | None = None, tool: str | None = None) -> Calibrated:
        """The calibration of `characteristic` on this operation's capability profile for its material:
        never a machine parameter, a nominal resolution or another material's calibration."""
        profile = profile or self.profile
        material = material or self.stock_material_id()
        if profile is None:
            return Calibrated("unknown", characteristic, None, (f"{self.label}: no capability profile",))
        if material is None:
            return Calibrated("unknown", characteristic, None, (f"{self.label}: the material profile is unknown",))
        found = self.mi.capability_limit(profile, characteristic, material=material, on=self.today,
                                         orientation=orientation, feature=feature, tool=tool)
        if self.calibration_off(profile):
            if found.status == "calibrated" and found.calibration.id not in self.ignored:
                self.ignored.append(found.calibration.id)
            return Calibrated("unknown", characteristic, None,
                              (f"{self.label}: printer calibration is off, and no guideline value covers {characteristic}",))
        if found.status == "calibrated":
            return Calibrated("calibrated", characteristic, found.calibration.limits, (), found.calibration)
        return Calibrated(found.status, characteristic, None, tuple(found.reasons))

    def calibration_off(self, profile: Any | None = None) -> bool:
        """Whether the FDM calibration flag is off for this operation's capability profile (only FDM has the flag)."""
        profile = profile or self.profile
        return not self.calibration and profile is not None and profile.process in GUIDELINE_PROCESSES

    def guideline_context(self) -> Any | None:
        """The guideline scope (guidelines.Context) of this operation's capability profile and material, or None
        without a catalog, a profile or a process in GUIDELINE_PROCESSES."""
        profile = self.profile
        if self.catalog is None or profile is None or profile.process not in GUIDELINE_PROCESSES:
            return None
        material_id = self.stock_material_id()
        return guidelines().context_for(profile, self.record.materials.get(material_id) if material_id else None)

    def limit(self, characteristic: str, side: str, *, orientation: str | None = None) -> Limit:
        """The limit of `characteristic` for this operation: its calibration (`calibrated`), else the guideline value
        for its profile and material, else unknown. A refused calibration (another material, an expired one) lets the
        guideline apply and keeps its reasons."""
        cal = self.calibrated(characteristic, orientation=orientation)
        context = self.guideline_context()
        if cal.known or context is None:
            return Limit(characteristic, side, cal)
        found = guidelines().resolve_guidelines(self.catalog, context, characteristic, orientation=orientation)
        self.consulted.append(found)
        if found not in self.resolutions:
            self.resolutions.append(found)
        return Limit(characteristic, side, cal, found, context.base_mm)

    @property
    def service_class(self) -> str | None:
        fact = self.record.use_case.service_class
        return str(fact.value) if fact.known else None

    def grade(self, limit: Limit, coverage: str, findings: list[Draft], *, check: str, measured: Mapping[str, Any],
              prop: str, units: str, method: str, feature: str | None = None) -> tuple[str, str]:
        """A sub-check that met `limit`: a pass for a calibrated limit, or a guideline value when the service class is one
        of GUIDELINE_PASS_CLASSES. A guideline value in another class is unknown, with a finding a calibration or an
        approved waiver settles. An unknown limit is unknown too (its callers report why)."""
        service = self.service_class
        if limit.status == "calibrated" or (limit.status == "guideline" and (service in GUIDELINE_PASS_CLASSES
                                                                             or self.calibration_off())):
            return PASS, coverage
        if limit.status == "unknown":
            return UNKNOWN, _noted(coverage, "limit unknown")
        kind = self.operation.kind
        findings.append(Draft(f"process.{kind}.{check}_guideline_only", "minor", UNKNOWN, measured,
                              limit.criterion(prop, units), method,
                              f"{limit.characteristic} is a design-guideline value, not a calibration of this machine and material",
                              f"a {service or 'unknown'} service class needs a calibrated or waived {limit.characteristic}: "
                              "a guideline value alone does not pass it",
                              remedy=f"calibrate {limit.characteristic} for this material and machine, or waive this finding "
                                     "with an approved decision",
                              recheck=[f"process.{kind}"], feature=feature))
        return UNKNOWN, _noted(coverage, "guideline value only")

    def capability_gate(self, kinds: Iterable[str] | None = None) -> Result | None:
        """UNKNOWN or FAIL when the operation has no usable capability profile of its own kind."""
        op = self.operation
        kinds = set(kinds or {op.kind})
        method = "capability profile lookup"
        if self.mi.is_fact(op.capability):
            reason = op.capability.reason or "not recorded"
            draft = Draft(f"process.{op.kind}.capability_unknown", "major", UNKNOWN, text(f"capability unknown: {reason}"),
                          unknown(f"no capability profile names what {self.label} can do"),
                          method, f"the capability of {self.label} is unknown: {reason}",
                          "no check of this operation can use calibrated criteria",
                          remedy="record the capability profile of the actual machine, operator or supplier, with its calibrations",
                          recheck=[f"process.{op.kind}"], earliest_stage="parts")
            if op.provider == "outsourced":
                draft.consequence += "; outsourcing is no exemption: the supplier's capability and inspection are required"
            return Result(UNKNOWN, method, "capability unknown", [draft])
        profile = self.profile
        if profile is None:
            draft = Draft("route.capability_reference", "major", FAIL, text(f"capability {op.capability}"),
                          policy("capability_profile", "equals", "a profile of hw/manufacturing/capabilities.yaml"),
                          method, "none", f"{self.label} names capability {op.capability}, which the record does not hold",
                          remedy="add the profile or correct the reference", recheck=[f"process.{op.kind}"],
                          earliest_stage="parts")
            return Result(FAIL, method, "capability reference", [draft])
        if profile.template or profile.process not in kinds:
            why = "is a plugin template, not a capability" if profile.template else \
                f"is a {profile.process} profile, not {' or '.join(sorted(kinds))}"
            draft = Draft("route.capability_mismatch", "major", FAIL, text(f"{profile.id} {why}"),
                          policy("capability_process", "one_of", sorted(kinds)), method, "none",
                          f"{self.label}: profile {profile.id} {why}; no {op.kind} assumption is inherited from another process",
                          remedy=f"name a {op.kind} capability profile of the actual equipment or supplier",
                          recheck=[f"process.{op.kind}"], earliest_stage="parts")
            return Result(FAIL, method, "capability process", [draft])
        return None

    def measured(self, what: str) -> Geometry | Result:
        """The component's measured geometry, or the UNKNOWN result of assessing `what` without it."""
        if self.geometry is not None:
            return self.geometry
        draft = Draft(f"process.{self.op.kind if self.op else 'route'}.geometry_unassessed", "minor", UNKNOWN,
                      unknown("no manufacturing geometry was assessed"),
                      unknown("geometric criteria need the version's manufacturing component"),
                      "record-only assessment", "the manufacturing STEP of this component was not measured",
                      f"{self.label}: {what} not assessed",
                      remedy="build the version (cad-model run) and run form_check.py --version", recheck=["process"],
                      earliest_stage="parts")
        return Result(UNKNOWN, "record-only assessment", f"{what} not measured", [draft])


def access_directions(sides: Sequence[str]) -> tuple[list[tuple[str, tuple[float, float, float]]], list[str]]:
    """(parsed sides, unparseable sides)."""
    parsed, bad = [], []
    for side in sides:
        direction = parse_side(side)
        if direction is None:
            bad.append(str(side))
        else:
            parsed.append((str(side), direction))
    return parsed, bad


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def direction_key(direction: Sequence[float]) -> str | None:
    """The probe key (+x .. -z) of an axis direction, or None for an oblique one."""
    for key, axis in DIRECTIONS.items():
        if dot(axis, direction) > PARALLEL_COS:
            return key
    return None


Handler = Callable[[Context], Result]


def gated(handler: Handler, kinds: tuple[str, ...] | None = None) -> Handler:
    """`handler` behind `capability_gate`: it runs only when the operation has a usable capability profile
    of its own kind (or of one of `kinds`)."""
    def run(ctx: Context) -> Result:
        return ctx.capability_gate(kinds) or handler(ctx)
    run.__name__ = handler.__name__
    return run
