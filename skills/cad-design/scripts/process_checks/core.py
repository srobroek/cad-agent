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

PASS, FAIL, UNKNOWN, NOT_APPLICABLE = "pass", "fail", "unknown", "not_applicable"
IMPLICIT = "main"  # the component id of a sole solid (cad-model components.IMPLICIT)
PROBE_FORMAT = 1  # the probe facts form_check.py writes; bump when a key's meaning changes
DIRECTIONS = {"+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0), "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
              "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0)}
PARALLEL_COS = math.cos(math.radians(1.0))  # a feature axis within 1 degree of an access direction is on it

# The plugin rules this module applies are policy, never product facts or capability numbers.
POLICY = types.MappingProxyType({
    "kind": "policy", "reference": "cad-agent manufacturing-intent dispatch policy (cad-design process_checks)",
    "origin": "cad-agent plugin", "document": "skills/cad-design/scripts/process_checks/__init__.py"})


def intent() -> Any:
    """cad-record manufacturing_intent.py under its shared module name."""
    module = sys.modules.get(INTENT_MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(INTENT_MODULE, INTENT_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[INTENT_MODULE] = module
        spec.loader.exec_module(module)
    return module


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
            value = [lower, upper] if operator == "between" else (upper if operator == ">=" else lower)
        scope = ", ".join(f"{k} {v}" for k, v in sorted(cal.scope.items()) if not isinstance(v, Mapping))
        return {"id": cal.id, "property": prop or self.characteristic, "operator": operator, "value": value,
                "units": units, "source": source_dict(cal.source),
                "conditions": f"calibration {cal.id} ({cal.samples} samples, {cal.date}): {scope}"}


# --- the geometry facts adapter ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Geometry:
    """The probe facts of one exported manufacturing component, in its exported frame.

    `facts` is form_check.py's probe dict (PROBE_FORMAT): `frame` (bed: exported in its approved additive
    pose, build direction +Z, bed at z 0; model: the native frame), `solids`, `shells`, `volume_mm3`,
    `bbox_mm{min,max}`, `centre_mm`, `bed{z_mm, contact_area_mm2, contact_min, contact_max}`, `faces[{id, type,
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
        if found.status == "calibrated":
            return Calibrated("calibrated", characteristic, found.calibration.limits, (), found.calibration)
        return Calibrated(found.status, characteristic, None, tuple(found.reasons))

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
