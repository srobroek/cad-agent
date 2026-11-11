#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Record version 2 manufacturing intent: the typed, immutable view of a design record's use case,
classified statements, per-part intent, interface functions and capability, material and selection
profiles, and the policy every other skill reads it through.

The standard library is enough to normalize and check already-parsed record documents, so CAD
environments without jsonschema can load this module by path; register it in `sys.modules` under
`cad_record_manufacturing_intent` before executing it (dataclasses look it up there). `read_documents`
needs PyYAML and `schema_problems` needs jsonschema; both import them only when called.

Invariants this module protects: an unknown is never a default, a zero or a pass; requirements stay
apart from capability; a property resolves only under matching form, orientation, conditioning,
temperature and basis; achievable accuracy comes only from calibrations; every expected check is
enumerated before any producer runs. The field contracts are schemas/manufacturing*.schema.json.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import types
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

if TYPE_CHECKING:
    from typing_extensions import TypeIs

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

RECORD_VERSION = 2
LEGACY_RECORD_VERSION = 1  # product.yaml without record_version
MIGRATE_SCRIPT = "cad-record/scripts/migrate.py"
MIGRATE_COMMAND = f'uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" {MIGRATE_SCRIPT} <project>'
LEGACY_REASON = "the record predates record version 2 and was never migrated; run migrate.py"

# --- problem codes this module reports (validate.py maps them to report findings) -------------------------------
MIGRATION_REQUIRED = "record.migration_required"
VERSION_UNSUPPORTED = "record.version_unsupported"
INCOMPLETE = "record.incomplete"
LEGACY_FREEZE = "record.legacy_freeze"
CODES: Mapping[str, str] = types.MappingProxyType({
    MIGRATION_REQUIRED: "a record version 1 record: readable, but gated actions refuse until migrate.py migrates it",
    VERSION_UNSUPPORTED: "a record_version this cad-agent cannot read",
    INCOMPLETE: "a record version 2 record lacks a block or file version 2 requires",
    LEGACY_FREEZE: "flow.frozen anchors no evidence_sha256: a freeze from before migration, which no release accepts",
    "intent.schema": "a document violates its JSON schema",
    "intent.reference": "a reference names no question, assumption, requirement, interface, part, load, profile, operation, inspection, evidence, case or decision",
    "intent.duplicate": "an id is declared twice",
    "intent.units": "a quantity's unit is unknown or of the wrong dimension",
    "intent.number": "a non-finite number, a reversed interval or an improper rotation",
    "intent.route": "a route cycle, a dependency on an unknown operation, or a route that contradicts make_buy and procurement",
    "intent.dual_authority": "installation operations in both assembly.yaml operations and drawing.operations",
    "intent.allowable": "an allowable without justification, or in a typical_not_allowable profile",
    "intent.profile": "a profile that contradicts its level, form or process, a template used as a capability, or an unknown parameter or property name",
    "intent.source_artifact": "a source artifact that is missing or whose bytes do not match its sha256",
    "intent.coverage": "a route operation kind with no check contract; its coverage is unknown",
})

# --- closed vocabularies ---------------------------------------------------------------------------------------
EVIDENCE_LEVELS = ("cad_valid", "geometrically_manufacturable", "process_planned", "simulation_supported", "physically_validated")
OUTCOMES = ("pass", "fail", "unknown", "not_applicable")
SEVERITIES = ("critical", "major", "minor", "info")
SERVICE_CLASSES = ("appearance_concept", "fit_assembly", "functional_demo", "working_prototype", "production", "safety_critical", "unknown")
FUNCTIONS = ("structural", "cosmetic", "locating", "sliding", "rotating", "sealing", "spring", "thermal", "electrical", "optical", "other")
PRIMARY_PROCESSES = ("fdm", "resin", "cnc", "laser", "waterjet", "plasma", "sheet_forming", "casting", "moulding", "extrusion", "purchased", "other")
ADDITIVE_KINDS = frozenset({"fdm", "resin"})
SHEET_CUTTING_KINDS = frozenset({"laser", "waterjet", "plasma"})
SECONDARY_KINDS = frozenset({"drilling", "reaming", "tapping", "finishing", "coating", "joining", "inspection"})
KNOWN_ROUTE_KINDS = frozenset(set(PRIMARY_PROCESSES) - {"other"}) | SECONDARY_KINDS
STAGES = ("ground", "intent", "specify", "concept", "layout", "parts", "detail", "bom", "analysis", "critique", "reconcile",
          "design_freeze", "release")

# Units: name -> (dimension, scale to the dimension's base unit, offset). base = value * scale + offset.
UNITS: Mapping[str, tuple[str, float, float]] = types.MappingProxyType({
    "mm": ("length", 1e-3, 0.0), "um": ("length", 1e-6, 0.0), "m": ("length", 1.0, 0.0),
    "mm3": ("volume", 1e-9, 0.0),
    "deg": ("angle", math.pi / 180.0, 0.0), "rad": ("angle", 1.0, 0.0),
    "N": ("force", 1.0, 0.0), "kN": ("force", 1e3, 0.0),
    "N_mm": ("torque", 1e-3, 0.0), "N_m": ("torque", 1.0, 0.0),
    "MPa": ("stress", 1e6, 0.0), "GPa": ("stress", 1e9, 0.0), "bar": ("stress", 1e5, 0.0),
    "g": ("mass", 1e-3, 0.0), "kg": ("mass", 1.0, 0.0),
    "kg_m3": ("mass_density", 1.0, 0.0), "g_cm3": ("mass_density", 1e3, 0.0),
    "deg_C": ("temperature", 1.0, 273.15), "K": ("temperature", 1.0, 0.0),
    "s": ("time", 1.0, 0.0), "min": ("time", 60.0, 0.0), "h": ("time", 3600.0, 0.0), "d": ("time", 86400.0, 0.0),
    "cycles": ("count", 1.0, 0.0), "count": ("count", 1.0, 0.0),
    "rpm": ("frequency", 1.0 / 60.0, 0.0), "Hz": ("frequency", 1.0, 0.0),
    "mm_s": ("speed", 1e-3, 0.0), "m_s": ("speed", 1.0, 0.0),
    "percent": ("ratio", 0.01, 0.0), "dimensionless": ("ratio", 1.0, 0.0),
    "J": ("energy", 1.0, 0.0), "J_m": ("energy_per_length", 1.0, 0.0), "kJ_m2": ("energy_per_area", 1e3, 0.0),
    "W": ("power", 1.0, 0.0), "kW": ("power", 1e3, 0.0),
    "W_m_K": ("thermal_conductivity", 1.0, 0.0),
    "um_m_K": ("thermal_expansion", 1e-6, 0.0), "per_K": ("thermal_expansion", 1.0, 0.0),
})

# Material property name -> the dimensions its value may have. Extend here, never per project.
MATERIAL_PROPERTIES: Mapping[str, tuple[str, ...]] = types.MappingProxyType({
    "density": ("mass_density",),
    "youngs_modulus": ("stress",), "flexural_modulus": ("stress",), "shear_modulus": ("stress",), "creep_modulus": ("stress",),
    "poisson_ratio": ("ratio",),
    "yield_strength": ("stress",), "tensile_strength": ("stress",), "flexural_strength": ("stress",),
    "compressive_strength": ("stress",), "shear_strength": ("stress",), "interlayer_strength": ("stress",),
    "fatigue_strength": ("stress",),
    "elongation_at_yield": ("ratio",), "elongation_at_break": ("ratio",),
    "impact_strength_charpy_notched": ("energy_per_area",), "impact_strength_charpy_unnotched": ("energy_per_area",),
    "impact_strength_izod_notched": ("energy_per_area", "energy_per_length"),
    "hardness_shore_d": ("ratio",), "hardness_rockwell": ("ratio",), "hardness_brinell": ("ratio",),
    "coefficient_of_friction": ("ratio",),
    "thermal_expansion": ("thermal_expansion",), "thermal_conductivity": ("thermal_conductivity",),
    "heat_deflection_temperature": ("temperature",), "vicat_softening_temperature": ("temperature",),
    "glass_transition_temperature": ("temperature",), "melting_temperature": ("temperature",),
    "max_service_temperature": ("temperature",),
    "water_absorption": ("ratio",), "moisture_absorption_equilibrium": ("ratio",), "shrinkage": ("ratio",),
    "light_transmission": ("ratio",), "haze": ("ratio",), "refractive_index": ("ratio",),
})

TEXT = "text"  # a capability parameter whose value is text or a boolean, never a quantity
_ENVELOPE = {"bed_x": ("length",), "bed_y": ("length",), "max_thickness": ("length",)}
_TOOLING = {"tool_set": TEXT, "range": ("length",), "resolution": ("length",)}
# Capability parameter name -> dimensions (or TEXT) per process kind. These describe the machine; achievable
# accuracy is never a parameter, it is a calibration.
CAPABILITY_PARAMETERS: Mapping[str, Mapping[str, Any]] = types.MappingProxyType({
    "fdm": {"nozzle_diameter": ("length",), "nozzle_material": TEXT, "filament_diameter": ("length",), "layer_height": ("length",),
            "line_width": ("length",), "hotend_max_temperature": ("temperature",), "bed_max_temperature": ("temperature",),
            "enclosure": TEXT, "chamber_temperature": ("temperature",), "filament_drying": TEXT, "support_materials": TEXT,
            "max_speed": ("speed",)},
    "resin": {"layer_height": ("length",), "xy_resolution": ("length",), "light_source": TEXT, "wash": TEXT, "post_cure": TEXT,
              "resin_heating": ("temperature",)},
    "cnc": {"axes": ("count",), "travel_x": ("length",), "travel_y": ("length",), "travel_z": ("length",),
            "spindle_max_speed": ("frequency",), "spindle_power": ("power",), "tool_holder": TEXT, "max_tool_length": ("length",),
            "coolant": TEXT, "probing": TEXT},
    "laser": {"laser_type": TEXT, "power": ("power",), "wavelength": ("length",), "assist_gas": TEXT, "extraction": TEXT,
              "operations": TEXT, **_ENVELOPE},
    "waterjet": {"pump_pressure": ("stress",), "abrasive": TEXT, **_ENVELOPE},
    "plasma": {"source": TEXT, "gas": TEXT, **_ENVELOPE},
    "sheet_forming": {"press_brake_force": ("force",), "max_bend_length": ("length",), "tooling": TEXT, "max_thickness": ("length",)},
    "casting": {"method": TEXT, "max_part_size": ("length",), "tooling": TEXT},
    "moulding": {"method": TEXT, "clamp_force": ("force",), "shot_volume": ("volume",), "tooling": TEXT},
    "extrusion": {"die": TEXT, "max_profile": ("length",)},
    "drilling": dict(_TOOLING), "reaming": dict(_TOOLING), "tapping": dict(_TOOLING), "finishing": dict(_TOOLING),
    "coating": {"coating_type": TEXT, "method": TEXT, "max_part_size": ("length",)},
    "joining": {"method": TEXT, "consumables": TEXT},
    "inspection": {"instrument": TEXT, "range": ("length",), "resolution": ("length",)},
})

# Expected dimensions of the use-case facts that hold quantities.
USE_CASE_FACTS = (
    "purpose", "users",
    "environment.temperature", "environment.heat_sources", "environment.moisture", "environment.chemicals", "environment.uv",
    "environment.corrosion",
    "motion.speed", "motion.range", "motion.duty_cycle", "motion.friction", "motion.wear", "motion.service_life",
    "accuracy.tolerance", "accuracy.repeatability", "accuracy.surface_finish", "accuracy.dimensional_stability",
    "consequences.failure", "consequences.severity", "consequences.forbidden_outcomes", "consequences.standards",
    "consequences.supervision", "consequences.approval_authority",
    "logistics.quantity", "logistics.budget", "logistics.lead_time", "logistics.maintenance", "logistics.repair",
    "capability.operator", "capability.outsourcing",
    "release_scope.purpose", "release_scope.operating_limits", "release_scope.exclusions",
)
_USE_CASE_DIMENSIONS = {
    "environment.temperature": ("temperature",), "motion.speed": ("speed", "frequency"), "motion.range": ("length", "angle"),
    "motion.service_life": ("time", "count"), "logistics.quantity": ("count",), "logistics.lead_time": ("time",),
}

# The use-case questions migrate.py and the project template ask: (question, use-case paths it answers,
# material_to_choice, safety_relevant). One authority, so the template and every migration ask the same.
USE_CASE_QUESTIONS: tuple[tuple[str, tuple[str, ...], bool, bool], ...] = (
    ("Which service class applies: appearance or concept model, fit or assembly prototype, functional demonstration with "
     "limited loads and supervised operation, working prototype, production or end-use product, or safety-critical, "
     "regulated or high-consequence application?", ("service_class",), True, True),
    ("What must the product do, and who uses it?", ("purpose", "users"), True, False),
    ("What operating temperature range and heat sources apply?", ("environment.temperature", "environment.heat_sources"), True, True),
    ("What moisture, chemical, UV and corrosion exposure applies?",
     ("environment.moisture", "environment.chemicals", "environment.uv", "environment.corrosion"), True, False),
    ("Which loads act on it: magnitude, direction, static or sustained, cyclic, impact and vibration?", ("loads",), True, True),
    ("Which motions occur: speed, range, duty cycle, friction, wear and expected service life?",
     ("motion.speed", "motion.range", "motion.duty_cycle", "motion.friction", "motion.wear", "motion.service_life"), True, False),
    ("What accuracy, repeatability, surface finish and dimensional stability are required?",
     ("accuracy.tolerance", "accuracy.repeatability", "accuracy.surface_finish", "accuracy.dimensional_stability"), True, False),
    ("What are the consequences of failure, the forbidden outcomes, the applicable standards or safety requirements, the "
     "supervision and the approving authority?",
     ("consequences.failure", "consequences.severity", "consequences.forbidden_outcomes", "consequences.standards",
      "consequences.supervision", "consequences.approval_authority"), True, True),
    ("What quantity, budget, lead time, maintenance and repair needs apply?",
     ("logistics.quantity", "logistics.budget", "logistics.lead_time", "logistics.maintenance", "logistics.repair"), True, False),
    ("Which equipment and operators are available, and is outsourcing acceptable?",
     ("capability.equipment", "capability.operator", "capability.outsourcing"), True, False),
    ("What may a release claim: its purpose, operating limits, exclusions and the evidence each requirement needs?",
     ("release_scope.purpose", "release_scope.operating_limits", "release_scope.exclusions", "evidence_policy"), False, True),
)

# Documents of the record, by path below hw/, and the schema each follows.
DOCUMENT_SCHEMAS: tuple[tuple[str, str], ...] = (
    ("product.yaml", "product.schema.json"),
    ("requirements.yaml", "requirements.schema.json"),
    ("interfaces.yaml", "interfaces.schema.json"),
    ("assembly.yaml", "assembly.schema.json"),
    ("parts/*/part.yaml", "part.schema.json"),
    ("manufacturing/capabilities.yaml", "manufacturing-capabilities.schema.json"),
    ("manufacturing/materials.yaml", "manufacturing-materials.schema.json"),
    ("manufacturing/selection.yaml", "manufacturing-selection.schema.json"),
    ("manufacturing/findings.yaml", "manufacturing-findings.schema.json"),
    ("manufacturing/evidence.yaml", "manufacturing-evidence.schema.json"),
)
REQUIRED_DESIGN_FILES = ("manufacturing/capabilities.yaml", "manufacturing/materials.yaml", "manufacturing/selection.yaml")


class IntentError(ValueError):
    """A call this module refuses: an unknown PN, a cyclic route, an unknown property or unit."""


class UnsupportedRecordVersion(IntentError):
    """product.yaml names a record_version this cad-agent cannot read."""


@dataclass(frozen=True)
class Problem:
    """One finding about the record. `path` is project-relative (hw/...); `pointer` locates it in the document."""

    code: str
    path: str
    pointer: str
    message: str
    severity: str = "error"  # error or warning
    action: str | None = None

    def finding(self) -> dict[str, Any]:
        """The validate.py report item: path, message, severity, code, canonical_location and action."""
        location = f"{self.path}#/{self.pointer}" if self.pointer else self.path
        return {"path": location, "message": self.message, "severity": self.severity, "code": self.code,
                "canonical_location": None, "action": self.action}


def freeze(value: Any) -> Any:
    """`value` with every mapping read-only and every list a tuple, recursively."""
    if isinstance(value, Mapping):
        return freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """`value` read-only, its items frozen (see freeze)."""
    return types.MappingProxyType({key: freeze(item) for key, item in value.items()})


def thaw(value: Any) -> Any:
    """The plain dict/list form of a frozen value, such as for canonical JSON."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    if isinstance(value, Quantity):
        return value.as_dict()
    return value


_EMPTY: Mapping[str, Any] = types.MappingProxyType({})


def _mapping(value: object) -> Mapping[str, Any]:
    """`value` when it is a mapping, else an empty one: an absent or malformed block reads as empty
    (schema_problems reports a malformed one)."""
    return value if isinstance(value, Mapping) else _EMPTY


# --- quantities ------------------------------------------------------------------------------------------------

def _unknown_unit(name: object) -> IntentError:
    return IntentError(f"unknown unit {name!r}; the units are {', '.join(UNITS)}")


def unit(name: str) -> tuple[str, float, float]:
    try:
        return UNITS[name]
    except KeyError:
        raise _unknown_unit(name) from None


def convert(value: float, from_units: str, to_units: str) -> float:
    """`value` in `from_units` expressed in `to_units`; IntentError across dimensions."""
    source, target = unit(from_units), unit(to_units)
    if source[0] != target[0]:
        raise IntentError(f"cannot convert {from_units} ({source[0]}) to {to_units} ({target[0]})")
    return (value * source[1] + source[2] - target[2]) / target[1]


@dataclass(frozen=True)
class Quantity:
    """A value or a closed interval in one unit."""

    units: str
    value: float | None = None
    lower: float | None = None
    upper: float | None = None

    @property
    def dimension(self) -> str:
        return unit(self.units)[0]

    @property
    def bounds(self) -> tuple[float, float]:
        if self.value is not None:
            return (self.value, self.value)
        assert self.lower is not None and self.upper is not None
        return (self.lower, self.upper)

    def to(self, units: str) -> Quantity:
        if self.value is not None:
            return Quantity(units, value=convert(self.value, self.units, units))
        lower, upper = self.bounds
        return Quantity(units, lower=convert(lower, self.units, units), upper=convert(upper, self.units, units))

    def contains(self, other: Quantity) -> bool:
        """Whether `other`'s bounds lie inside these bounds (after conversion); False across dimensions."""
        if self.dimension != other.dimension:
            return False
        lower, upper = self.bounds
        other_lower, other_upper = other.to(self.units).bounds
        tolerance = 1e-9 * max(1.0, abs(lower), abs(upper))
        return lower - tolerance <= other_lower and other_upper <= upper + tolerance

    def as_dict(self) -> dict[str, Any]:
        if self.value is not None:
            return {"value": self.value, "units": self.units}
        return {"lower": self.lower, "upper": self.upper, "units": self.units}

    def __str__(self) -> str:
        if self.value is not None:
            return f"{self.value:g} {self.units}"
        return f"{self.lower:g}..{self.upper:g} {self.units}"


def quantity(data: Mapping[str, Any]) -> Quantity:
    """A Quantity from its schema form; IntentError for an unknown unit, non-finite number or reversed interval."""
    units = data.get("units")
    if not isinstance(units, str):
        raise _unknown_unit(units)
    unit(units)
    numbers = [data.get(key) for key in ("value", "lower", "upper") if key in data]
    if not numbers or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in numbers):
        raise IntentError(f"a quantity needs finite numbers, got {dict(data)!r}")
    if "value" in data:
        return Quantity(units, value=float(data["value"]))
    lower, upper = float(data["lower"]), float(data["upper"])
    if lower > upper:
        raise IntentError(f"interval {lower:g}..{upper:g} {units} is reversed")
    return Quantity(units, lower=lower, upper=upper)


def _is_quantity(value: object) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(value, Mapping) and "units" in value and ("value" in value or "lower" in value)


# --- facts -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    kind: str | None  # None only in a schema-invalid document; schema_problems reports it
    reference: str | None
    revision: str | None = None
    url: str | None = None
    accessed: str | None = None
    artifact: str | None = None  # relative to hw/
    sha256: str | None = None
    conditions: str | None = None
    origin: str | None = None  # who stated a user value or sets a policy
    reason: str | None = None
    decision: str | None = None
    date: str | None = None
    document: str | None = None  # the document that holds a policy

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Source:
        return cls(**{name: data.get(name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Fact:
    """known (value, sources, basis), unknown (reason, question) or not_applicable (reason, decision)."""

    status: str
    value: Any = None  # a Quantity for a quantity fact, else the frozen value
    sources: tuple[Source, ...] = ()
    basis: str | None = None
    reason: str | None = None
    question: str | None = None
    decision: str | None = None
    uncertainty: Mapping[str, Any] | None = None
    conditions: str | None = None
    assumption: str | None = None
    derivation: str | None = None
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.status == "known"

    @property
    def unknown(self) -> bool:
        return self.status == "unknown"

    @property
    def not_applicable(self) -> bool:
        return self.status == "not_applicable"

    @classmethod
    def unknown_because(cls, reason: str, question: str | None = None) -> Fact:
        return cls("unknown", reason=reason, question=question)


def is_fact(value: object) -> TypeIs[Fact]:
    """Whether a field holds a Fact (unknown or not applicable) instead of its known list or id."""
    return isinstance(value, Fact)


def fact(data: Mapping[str, Any]) -> Fact:
    """A Fact from its schema form (structure as the schema checks it; see normalize_record for the rest)."""
    status = data.get("status")
    if status == "known":
        value = data.get("value")
        value = quantity(value) if _is_quantity(value) else freeze(value)
        return Fact("known", value, tuple(Source.from_data(s) for s in data.get("sources") or ()), data.get("basis"),
                    uncertainty=freeze(data.get("uncertainty")), conditions=data.get("conditions"),
                    assumption=data.get("assumption"), derivation=data.get("derivation"), decision=data.get("decision"),
                    note=data.get("note"))
    if status == "unknown":
        return Fact("unknown", reason=data.get("reason"), question=data.get("question"))
    if status == "not_applicable":
        return Fact("not_applicable", reason=data.get("reason"), decision=data.get("decision"))
    raise IntentError(f"a fact's status is known, unknown or not_applicable, got {status!r}")


def _looks_like_fact(value: object) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(value, Mapping) and value.get("status") in ("known", "unknown", "not_applicable")


# --- statements ------------------------------------------------------------------------------------------------
# A field typed `X | None` that the schema requires holds None only in a schema-invalid document, which
# schema_problems reports; the reader keeps it absent rather than raising or inventing a value.

@dataclass(frozen=True)
class Criterion:
    property: str
    operator: str
    value: Any
    source: Source
    units: str | None = None
    id: str | None = None
    state: str | None = None
    conditions: str | None = None

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Criterion:
        return cls(data["property"], data["operator"], freeze(data["value"]), Source.from_data(data["source"]),
                   data.get("units"), data.get("id"), data.get("state"), data.get("conditions"))


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str | None
    priority: str  # must, should or could
    method: str | None
    criteria_text: str | None
    acceptance: tuple[Criterion, ...]
    evidence_required: tuple[str, ...]
    waiver_policy: str | None
    risk: Mapping[str, Any] | None
    subjects: tuple[Mapping[str, Any], ...]

    @property
    def mandatory(self) -> bool:
        return self.priority.lower() == "must"


@dataclass(frozen=True)
class Preference:
    id: str
    text: str | None
    weight: float | None
    source: Source | None
    criterion: Criterion | None


@dataclass(frozen=True)
class Assumption:
    id: str
    text: str | None
    status: str | None
    owner: str | None
    validation: str | None
    requirements: tuple[str, ...]


@dataclass(frozen=True)
class OpenQuestion:
    id: str
    question: str | None
    status: str | None  # open or resolved
    material_to_choice: bool
    safety_relevant: bool
    subjects: tuple[Mapping[str, Any], ...]

    @property
    def blocks_final_choice(self) -> bool:
        return self.status == "open" and (self.material_to_choice or self.safety_relevant)


@dataclass(frozen=True)
class Load:
    id: str | None
    kind: str | None  # static, sustained, cyclic, impact or vibration
    magnitude: Fact
    requirements: tuple[str, ...]


@dataclass(frozen=True)
class UseCase:
    status: str | None  # unknown, draft or confirmed
    decision: str | None
    service_class: Fact
    facts: Mapping[str, Fact]  # USE_CASE_FACTS path -> Fact
    loads: tuple[Load, ...] | Fact
    equipment: tuple[str, ...] | Fact
    evidence_policy: tuple[Mapping[str, Any], ...] | Fact
    evidence_policy_decision: str | None

    @classmethod
    def unknown(cls, reason: str) -> UseCase:
        blank = Fact.unknown_because(reason)
        return cls("unknown", None, blank, types.MappingProxyType({path: blank for path in USE_CASE_FACTS}), blank, blank, blank, None)


@dataclass(frozen=True)
class FunctionEntry:
    function: str | None
    description: str | None
    requirements: tuple[str, ...]


@dataclass(frozen=True)
class Component:
    id: str | None
    witness: tuple[float, float, float] | None
    locator: Mapping[str, Any] | None
    material_profile: str | Fact | None
    functions: tuple[FunctionEntry, ...]
    multiplicity: int


@dataclass(frozen=True)
class CriticalSurface:
    id: str | None
    feature: str | None
    role: str | None
    dimension_state: str | None
    region: str | None
    component: str | None
    interfaces: tuple[str, ...]
    requirements: tuple[str, ...]
    inspection: str | None


@dataclass(frozen=True)
class Operation:
    """One route operation. `capability` is a profile id or an unknown Fact (None for a purchase)."""

    id: str | None
    kind: str | None
    capability: str | Fact | None
    provider: str | None
    components: tuple[str, ...]  # empty: every component
    depends_on: tuple[str, ...]  # explicit dependencies; empty: the previous operation
    input_state: str | None
    output_state: str | None
    targets: tuple[Mapping[str, Any], ...]
    setup: Mapping[str, Any] | None
    orientation: Mapping[str, Any] | Fact | None
    supports: Mapping[str, Any] | Fact | None
    allowances: tuple[Mapping[str, Any], ...]
    inspections: tuple[Mapping[str, Any], ...]
    requirements: tuple[str, ...]
    purchase: Mapping[str, Any] | None
    raw: Mapping[str, Any]

    @property
    def known_kind(self) -> bool:
        return self.kind in KNOWN_ROUTE_KINDS


@dataclass(frozen=True)
class PartIntent:
    functions: tuple[FunctionEntry, ...] | Fact
    material_profile: str | Fact | None  # None when the intent names none
    selection: str | None
    service: Mapping[str, Any] | Fact
    components: tuple[Component, ...]
    critical_surfaces: tuple[CriticalSurface, ...] | Fact
    route: tuple[Operation, ...] | Fact
    rationale: Mapping[str, Any] | None

    @classmethod
    def unknown(cls, reason: str) -> PartIntent:
        blank = Fact.unknown_because(reason)
        return cls(blank, blank, None, blank, (), blank, blank, None)


@dataclass(frozen=True)
class Part:
    pn: str
    title: str | None
    material: str | None  # the legacy declaration, never a property source
    process: str | None  # the scalar primary-process label
    sourcing: str  # make, made_to_drawing or catalogue
    transparency: str | None
    intent: PartIntent
    raw: Mapping[str, Any]

    @property
    def made(self) -> bool:
        """Custom-made (make or made_to_drawing); a catalogue buy is not."""
        return self.sourcing in ("make", "made_to_drawing")


@dataclass(frozen=True)
class InterfaceFunctional:
    critical: Fact
    functions: tuple[FunctionEntry, ...] | Fact
    requirements: tuple[str, ...]
    raw: Mapping[str, Any]

    @classmethod
    def unknown(cls, reason: str) -> InterfaceFunctional:
        blank = Fact.unknown_because(reason)
        return cls(blank, blank, (), types.MappingProxyType({}))


@dataclass(frozen=True)
class Interface:
    id: str
    type: str | None
    functional: InterfaceFunctional
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class Calibration:
    id: str | None
    characteristic: str | None
    scope: Mapping[str, Any]
    limits: Quantity
    uncertainty: Quantity | None
    samples: int
    source: Source
    date: str | None
    valid_until: str | None


@dataclass(frozen=True)
class CapabilityProfile:
    id: str
    revision: int
    process: str | None
    provider: str | None
    template: bool
    parameters: Mapping[str, Fact]
    materials: tuple[Mapping[str, Any], ...] | Fact
    calibrations: tuple[Calibration, ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class MaterialProperty:
    name: str | None
    value: Fact  # value is a Quantity when known
    test_standard: Fact
    temperature: Fact  # value is a Quantity when known
    conditioning: str
    orientation: Fact | None


@dataclass(frozen=True)
class MaterialProfile:
    id: str
    revision: int
    level: str | None  # family or grade
    family: str | None
    grade: Fact
    form: str | None
    conditioning: str
    typical_not_allowable: bool
    properties: tuple[MaterialProperty, ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ProposedPart:
    """A part a selection candidate introduces that the record does not hold yet (`parts[].proposed`).
    Never a Record.parts entry and never a source of check obligations: it becomes a part only when the
    approved change creates its part.yaml."""

    pn: str
    candidate: str
    title: str | None
    sourcing: str | None  # make, made_to_drawing or catalogue
    reason: str | None
    functions: tuple[FunctionEntry, ...]  # empty when the proposal states none
    transparency: str | None


@dataclass(frozen=True)
class Selection:
    status: str  # not_assessed, assessed or confirmed
    candidates: Mapping[str, Mapping[str, Any]]
    recommendation: Mapping[str, Any] | None
    raw: Mapping[str, Any]
    proposed: Mapping[str, Mapping[str, ProposedPart]] = types.MappingProxyType({})  # candidate id -> PN -> proposal


@dataclass(frozen=True)
class Record:
    """A normalized record. A version 1 record carries LEGACY_REASON unknowns and a migration problem."""

    version: int
    product: Mapping[str, Any]
    use_case: UseCase
    requirements: Mapping[str, Requirement]
    preferences: Mapping[str, Preference]
    assumptions: Mapping[str, Assumption]
    questions: Mapping[str, OpenQuestion]
    parts: Mapping[str, Part]
    interfaces: Mapping[str, Interface]
    assembly_operations: tuple[Mapping[str, Any], ...] | Fact
    capabilities: Mapping[str, CapabilityProfile]
    materials: Mapping[str, MaterialProfile]
    selection: Selection
    evidence: tuple[Mapping[str, Any], ...]
    dispositions: tuple[Mapping[str, Any], ...]
    artifacts: Mapping[str, str]  # source artifact path (relative to hw/) -> sha256 the record names
    problems: tuple[Problem, ...]

    @property
    def migrated(self) -> bool:
        return self.version == RECORD_VERSION


# --- record version --------------------------------------------------------------------------------------------

def record_version(product: Mapping[str, Any]) -> int:
    """1 for a product.yaml without record_version, 2 for the current version; UnsupportedRecordVersion otherwise."""
    if "record_version" not in product:
        return LEGACY_RECORD_VERSION
    version = product["record_version"]
    if isinstance(version, bool) or version != RECORD_VERSION:
        raise UnsupportedRecordVersion(f"product.yaml record_version {version!r} is not supported; this cad-agent reads "
                                       f"record version {RECORD_VERSION} and migrates version {LEGACY_RECORD_VERSION}")
    return RECORD_VERSION


def migration_problem(product: Mapping[str, Any] | None, path: str = "hw/product.yaml") -> Problem | None:
    """The blocking record.migration_required (version 1) or record.version_unsupported problem, or None
    for a current record. validate.py, status and gated commands report it beside every other finding."""
    if not isinstance(product, Mapping):
        return None
    try:
        version = record_version(product)
    except UnsupportedRecordVersion as error:
        return Problem(VERSION_UNSUPPORTED, path, "record_version", str(error),
                       action="Use the cad-agent release that wrote this record; nothing was changed.")
    if version == RECORD_VERSION:
        return None
    return Problem(MIGRATION_REQUIRED, path, "",
                   f"the record is record version {LEGACY_RECORD_VERSION}; manufacturing intent (use case, part routes, "
                   f"profiles) is unknown and stage advance, freeze, release and record-writing builds refuse it",
                   action=f"Review `{MIGRATE_COMMAND} --dry-run`, then run it with --apply. It adds explicit unknowns and "
                          "open questions and changes nothing else; nothing was changed.")


# --- reading documents -----------------------------------------------------------------------------------------

def read_documents(hw: Path) -> tuple[dict[str, Any], list[Problem]]:
    """Every record document below `hw` by its path below hw/ (DOCUMENT_SCHEMAS), parsed with PyYAML,
    and a problem for each one that does not parse. Absent files are absent."""
    import yaml  # noqa: PLC0415 - optional dependency, needed only to read files

    hw = Path(hw)
    documents: dict[str, Any] = {}
    problems: list[Problem] = []
    for pattern, _schema in DOCUMENT_SCHEMAS:
        for path in sorted(hw.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(hw).as_posix()
            try:
                documents[rel] = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
                problems.append(Problem("intent.schema", f"hw/{rel}", "", f"cannot read YAML: {error}"))
    return documents, problems


def schema_for(rel: str) -> str | None:
    """The schema file a document below hw/ follows, or None."""
    for pattern, schema in DOCUMENT_SCHEMAS:
        if re.fullmatch(pattern.replace(".", r"\.").replace("*", "[^/]+"), rel):
            return schema
    return None


def schema_problems(documents: Mapping[str, Any]) -> list[Problem]:
    """JSON-schema problems of every document (needs jsonschema), offline: every bundled schema resolves by $id."""
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - optional dependency
    from referencing import Registry, Resource  # noqa: PLC0415

    resources = [(contents["$id"], Resource.from_contents(contents))
                 for contents in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMAS.glob("*.schema.json")))]
    registry = Registry().with_resources(resources)
    problems = []
    for rel, document in sorted(documents.items()):
        name = schema_for(rel)
        if name is None:
            continue
        schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, registry=registry)
        for error in sorted(validator.iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path]):
            pointer = "/".join(str(part) for part in error.absolute_path)
            problems.append(Problem("intent.schema", f"hw/{rel}", pointer, f"{pointer + ': ' if pointer else ''}{error.message}"))
    return problems


def load_record(hw: Path) -> Record:
    """The normalized record at `hw`, with the problems of reading and checking it (schema aside)."""
    documents, problems = read_documents(hw)
    record = normalize_record(documents, hw=hw)
    return _with_problems(record, problems + list(record.problems))


def check_record(hw: Path) -> tuple[Record, list[Problem]]:
    """load_record plus schema_problems: everything this contract reports about the record at `hw`."""
    documents, problems = read_documents(hw)
    record = normalize_record(documents, hw=hw)
    return record, problems + schema_problems(documents) + list(record.problems)


def _with_problems(record: Record, problems: list[Problem]) -> Record:
    values = {name: getattr(record, name) for name in record.__dataclass_fields__}
    values["problems"] = tuple(problems)
    return Record(**values)


# --- normalization ---------------------------------------------------------------------------------------------

def normalize_record(documents: Mapping[str, Any], hw: Path | None = None) -> Record:
    """The Record of parsed `documents` (keyed by path below hw/, as read_documents returns them). With
    `hw`, decision, case and source-artifact references are also checked against the files."""
    return _Reader(documents, Path(hw) if hw is not None else None).record()


class _Reader:
    """One normalization pass: builds the DTOs and collects every problem, never raising for record content."""

    def __init__(self, documents: Mapping[str, Any], hw: Path | None) -> None:
        self.documents = documents
        self.hw = hw
        self.problems: list[Problem] = []
        self.defined: dict[str, set[str]] = {}
        self.refs: list[tuple[str, str, str, str]] = []  # (kind, id, path, pointer)
        self.artifacts: dict[str, str] = {}

    # -- reporting --
    def problem(self, code: str, path: str, pointer: str, message: str, severity: str = "error") -> None:
        self.problems.append(Problem(code, path, pointer, message, severity))

    def define(self, kind: str, ident: Any, path: str, pointer: str) -> None:
        if not isinstance(ident, str):
            return
        names = self.defined.setdefault(kind, set())
        if ident in names:
            self.problem("intent.duplicate", path, pointer, f"{kind.split(':', 1)[0].replace('_', ' ')} {ident} is declared twice")
        names.add(ident)

    def ref(self, kind: str, ident: Any, path: str, pointer: str) -> None:
        if isinstance(ident, str):
            self.refs.append((kind, ident, path, pointer))

    def doc(self, rel: str) -> Mapping[str, Any] | None:
        data = self.documents.get(rel)
        return data if isinstance(data, Mapping) else None

    # -- numbers and facts --
    def numbers(self, value: Any, path: str, pointer: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                self.numbers(item, path, f"{pointer}/{key}" if pointer else str(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.numbers(item, path, f"{pointer}/{index}" if pointer else str(index))
        elif isinstance(value, float) and not math.isfinite(value):
            self.problem("intent.number", path, pointer, f"{pointer}: {value} is not a finite number")

    def quantity(self, data: Any, path: str, pointer: str, dimensions: tuple[str, ...] | None = None) -> Quantity | None:
        if not isinstance(data, Mapping):
            return None
        try:
            value = quantity(data)
        except IntentError as error:
            code = "intent.units" if "unit" in str(error) else "intent.number"
            self.problem(code, path, pointer, f"{pointer}: {error}")
            return None
        if dimensions and value.dimension not in dimensions:
            self.problem("intent.units", path, pointer, f"{pointer}: {value.units} is a {value.dimension}; "
                                                      f"this needs a {' or '.join(dimensions)}")
        return value

    def source(self, data: Any, path: str, pointer: str) -> Source | None:
        if not isinstance(data, Mapping):
            return None
        source = Source.from_data(data)
        if source.artifact and source.sha256:
            known = self.artifacts.setdefault(source.artifact, source.sha256)
            if known != source.sha256:
                self.problem("intent.source_artifact", path, pointer,
                             f"{pointer}: {source.artifact} is named with two different sha256 values")
        self.ref("decision", source.decision, path, pointer)
        return source

    def fact(self, data: Any, path: str, pointer: str, dimensions: tuple[str, ...] | None = None) -> Fact:
        if not _looks_like_fact(data):
            return Fact.unknown_because(f"{pointer} is not a recorded fact")
        self.ref("question", data.get("question"), path, pointer)
        self.ref("assumption", data.get("assumption"), path, pointer)
        self.ref("decision", data.get("decision"), path, pointer)
        sources = data.get("sources")
        for index, item in enumerate(sources if isinstance(sources, list) else ()):
            self.source(item, path, f"{pointer}/sources/{index}")
        if data.get("status") == "known" and _is_quantity(data.get("value")):
            if self.quantity(data["value"], path, f"{pointer}/value", dimensions) is None:
                return Fact.unknown_because(f"{pointer} holds an invalid quantity")
            uncertainty = data.get("uncertainty")
            if isinstance(uncertainty, Mapping) and "lower" in uncertainty and "upper" in uncertainty:
                lower, upper = uncertainty["lower"], uncertainty["upper"]
                if all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in (lower, upper)) and lower > upper:
                    self.problem("intent.number", path, f"{pointer}/uncertainty", f"{pointer}/uncertainty: {lower} > {upper}")
        try:
            return fact(data)
        except IntentError as error:
            self.problem("intent.number", path, pointer, f"{pointer}: {error}")
            return Fact.unknown_because(str(error))

    def subject(self, data: Any, path: str, pointer: str) -> Mapping[str, Any]:
        if not isinstance(data, Mapping):
            return types.MappingProxyType({})
        self.ref("part", data.get("PN"), path, pointer)
        self.ref("interface", data.get("interface"), path, pointer)
        self.ref("assembly_operation", data.get("assembly_operation"), path, pointer)
        for requirement in data.get("requirements") or ():
            self.ref("requirement", requirement, path, pointer)
        pn = data.get("PN")
        if isinstance(pn, str):
            if data.get("component"):
                self.ref(f"component:{pn}", data["component"], path, pointer)
            if data.get("operation"):
                self.ref(f"operation:{pn}", data["operation"], path, pointer)
        return freeze_mapping(data)

    def criterion(self, data: Any, path: str, pointer: str) -> Criterion | None:
        if not isinstance(data, Mapping) or not {"property", "operator", "value", "source"} <= set(data):
            return None
        self.source(data["source"], path, f"{pointer}/source")
        units = data.get("units")
        if units is not None and units not in UNITS:
            self.problem("intent.units", path, pointer, f"{pointer}: unknown unit {units!r}")
        value = data["value"]
        if data["operator"] == "between" and isinstance(value, list) and len(value) == 2 and all(
                isinstance(n, (int, float)) and not isinstance(n, bool) for n in value) and value[0] > value[1]:
            self.problem("intent.number", path, f"{pointer}/value", f"{pointer}/value: interval {value[0]}..{value[1]} is reversed")
        return Criterion.from_data(data)

    def functions(self, data: Any, path: str, pointer: str) -> tuple[FunctionEntry, ...] | Fact:
        if _looks_like_fact(data):
            return self.fact(data, path, pointer)
        return self.function_entries(data, path, pointer)

    def function_entries(self, data: Any, path: str, pointer: str) -> tuple[FunctionEntry, ...]:
        """A function list; a component's and a proposal's functions are always one (the schema allows no fact)."""
        entries = []
        for index, item in enumerate(data if isinstance(data, list) else ()):
            if not isinstance(item, Mapping):
                continue
            for requirement in item.get("requirements") or ():
                self.ref("requirement", requirement, path, f"{pointer}/{index}")
            entries.append(FunctionEntry(item.get("function"), item.get("description"), tuple(item.get("requirements") or ())))
        return tuple(entries)

    def id_or_fact(self, data: Any, kind: str, path: str, pointer: str) -> str | Fact | None:
        """A profile id, its recorded fact, or None when absent (or neither, which the schema reports)."""
        if _looks_like_fact(data):
            return self.fact(data, path, pointer)
        if not isinstance(data, str):
            return None
        self.ref(kind, data, path, pointer)
        return data

    # -- documents --
    def record(self) -> Record:
        product_rel = "product.yaml"
        product = self.doc(product_rel) or {}
        for rel, data in self.documents.items():
            self.numbers(data, f"hw/{rel}", "")
        try:
            version = record_version(product)
        except UnsupportedRecordVersion:
            version = 0
        problem = migration_problem(product)
        if problem is not None:
            self.problems.append(problem)
        if version != RECORD_VERSION:
            return self.legacy(product, version)
        self.completeness()
        use_case = self.use_case(product.get("use_case"), f"hw/{product_rel}")
        requirements, preferences, assumptions, questions = self.statements()
        capabilities = self.capabilities()
        materials = self.materials()
        selection = self.selection()
        parts = self.parts()
        interfaces = self.interfaces()
        operations = self.assembly_operations()
        evidence, dispositions = self.ledgers()
        self.check_profile_use(parts, capabilities, materials)
        self.resolve()
        return Record(RECORD_VERSION, freeze(product), use_case, requirements, preferences, assumptions, questions, parts,
                      interfaces, operations, capabilities, materials, selection, evidence, dispositions,
                      types.MappingProxyType(dict(self.artifacts)), tuple(self.problems))

    def legacy(self, product: Mapping[str, Any], version: int) -> Record:
        """A version 1 (or unreadable-version) record: every manufacturing-intent field unknown, never defaulted."""
        reason = LEGACY_REASON if version == LEGACY_RECORD_VERSION else "the record version is not supported"
        parts = {}
        for rel in sorted(self.documents):
            match = re.fullmatch(r"parts/([^/]+)/part\.yaml", rel)
            data = self.doc(rel)
            pn = data.get("PN") if data is not None else None
            if match and data is not None and isinstance(pn, str):
                parts[pn] = self.part_fields(pn, data, PartIntent.unknown(reason))
        interfaces = {}
        for item in (self.doc("interfaces.yaml") or {}).get("interfaces") or ():
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                interfaces[item["id"]] = Interface(item["id"], item.get("type"), InterfaceFunctional.unknown(reason), freeze(item))
        assembly = self.doc("assembly.yaml") or {}
        drawing = _mapping(assembly.get("drawing"))
        legacy_operations = drawing.get("operations")
        operations: tuple[Mapping[str, Any], ...] | Fact = (
            freeze(legacy_operations) if isinstance(legacy_operations, list) else Fact.unknown_because(reason))
        requirements = self.requirements_only()
        return Record(version, freeze(product), UseCase.unknown(reason), requirements, types.MappingProxyType({}),
                      types.MappingProxyType({}), types.MappingProxyType({}), types.MappingProxyType(parts),
                      types.MappingProxyType(interfaces), operations, types.MappingProxyType({}), types.MappingProxyType({}),
                      Selection("not_assessed", types.MappingProxyType({}), None, types.MappingProxyType({})), (), (),
                      types.MappingProxyType({}), tuple(self.problems))

    def completeness(self) -> None:
        requirements = self.doc("requirements.yaml")
        if requirements is not None:
            missing = [key for key in ("preferences", "assumptions", "open_questions") if key not in requirements]
            if missing:
                self.problem(INCOMPLETE, "hw/requirements.yaml", "", f"record version 2 needs {', '.join(missing)} "
                             "(empty lists when there are none)")
        for rel in sorted(self.documents):
            if re.fullmatch(r"parts/[^/]+/part\.yaml", rel) and (part := self.doc(rel)) is not None and "intent" not in part:
                self.problem(INCOMPLETE, f"hw/{rel}", "", "record version 2 needs intent; record unknowns explicitly")
        for index, item in enumerate((self.doc("interfaces.yaml") or {}).get("interfaces") or ()):
            if isinstance(item, Mapping) and "functional" not in item:
                self.problem(INCOMPLETE, "hw/interfaces.yaml", f"interfaces/{index}",
                             f"{item.get('id')}: record version 2 needs functional; record unknowns explicitly")
        assembly = self.doc("assembly.yaml")
        if assembly is not None:
            if "operations" not in assembly:
                self.problem(INCOMPLETE, "hw/assembly.yaml", "", "record version 2 needs operations, or an unknown with its reason")
            drawing = assembly.get("drawing")
            if isinstance(drawing, Mapping) and "operations" in drawing:
                self.problem("intent.dual_authority", "hw/assembly.yaml", "drawing/operations",
                             "record version 2 keeps installation operations in top-level operations only")
        for rel in REQUIRED_DESIGN_FILES:
            if rel not in self.documents:
                self.problem(INCOMPLETE, f"hw/{rel}", "", f"record version 2 needs hw/{rel}")
        flow = (self.doc("product.yaml") or {}).get("flow")
        frozen = flow.get("frozen") if isinstance(flow, Mapping) else None
        if isinstance(frozen, Mapping) and "evidence_sha256" not in frozen:
            self.problem(LEGACY_FREEZE, "hw/product.yaml", "flow/frozen",
                         f"the freeze of {frozen.get('version')} anchors no evidence_sha256; a release needs a new record version 2 "
                         "freeze that records its acceptance digest", severity="warning")

    def use_case(self, data: Any, path: str) -> UseCase:
        if not isinstance(data, Mapping):
            return UseCase.unknown("product.yaml has no use_case")
        facts = {}
        for dotted in USE_CASE_FACTS:
            value: Any = data
            for key in dotted.split("."):
                value = value.get(key) if isinstance(value, Mapping) else None
            facts[dotted] = self.fact(value, path, "use_case/" + dotted.replace(".", "/"), _USE_CASE_DIMENSIONS.get(dotted))
        service_class = self.fact(data.get("service_class"), path, "use_case/service_class")
        loads_data = data.get("loads")
        loads: tuple[Load, ...] | Fact
        if isinstance(loads_data, list):
            entries = []
            for index, item in enumerate(loads_data):
                if not isinstance(item, Mapping):
                    continue
                pointer = f"use_case/loads/{index}"
                self.define("load", item.get("id"), path, pointer)
                for key, dimensions in (("duration", ("time",)), ("frequency", ("frequency",)), ("cycles", ("count",)),
                                        ("direction", None)):
                    if key in item:
                        self.fact(item[key], path, f"{pointer}/{key}", dimensions)
                if "application" in item:
                    self.subject(item["application"], path, f"{pointer}/application")
                for requirement in item.get("requirements") or ():
                    self.ref("requirement", requirement, path, pointer)
                entries.append(Load(item.get("id"), item.get("kind"),
                                    self.fact(item.get("magnitude"), path, f"{pointer}/magnitude", ("force", "torque")),
                                    tuple(item.get("requirements") or ())))
            loads = tuple(entries)
        else:
            loads = self.fact(loads_data, path, "use_case/loads")
        capability = _mapping(data.get("capability"))
        equipment_data = capability.get("equipment")
        equipment: tuple[str, ...] | Fact
        if isinstance(equipment_data, list):
            for index, ident in enumerate(equipment_data):
                self.ref("capability", ident, path, f"use_case/capability/equipment/{index}")
            equipment = tuple(equipment_data)
        else:
            equipment = self.fact(equipment_data, path, "use_case/capability/equipment")
        policy_data = data.get("evidence_policy")
        policy: tuple[Mapping[str, Any], ...] | Fact
        policy_decision = None
        if isinstance(policy_data, Mapping) and "obligations" in policy_data:
            for index, item in enumerate(policy_data.get("obligations") or ()):
                if isinstance(item, Mapping):
                    self.ref("requirement", item.get("requirement"), path, f"use_case/evidence_policy/obligations/{index}")
                    if "subject" in item:
                        self.subject(item["subject"], path, f"use_case/evidence_policy/obligations/{index}/subject")
            policy = freeze(policy_data.get("obligations") or [])
            policy_decision = policy_data.get("decision")
            self.ref("decision", policy_decision, path, "use_case/evidence_policy/decision")
        else:
            policy = self.fact(policy_data, path, "use_case/evidence_policy")
        self.ref("decision", data.get("decision"), path, "use_case/decision")
        return UseCase(data.get("status"), data.get("decision"), service_class, types.MappingProxyType(facts), loads,
                       equipment, policy, policy_decision)

    def requirements_only(self) -> Mapping[str, Requirement]:
        found: dict[str, Requirement] = {}
        data = self.doc("requirements.yaml") or {}
        for index, item in enumerate(data.get("requirements") or ()):
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                continue
            pointer = f"requirements/{index}"
            self.define("requirement", item["id"], "hw/requirements.yaml", pointer)
            verification = _mapping(item.get("verification"))
            acceptance = tuple(c for c in (self.criterion(entry, "hw/requirements.yaml", f"{pointer}/acceptance/{i}")
                                           for i, entry in enumerate(item.get("acceptance") or ())) if c is not None)
            subjects = tuple(self.subject(s, "hw/requirements.yaml", f"{pointer}/subjects/{i}")
                             for i, s in enumerate(item.get("subjects") or ()))
            found[item["id"]] = Requirement(item["id"], item.get("text"), str(item.get("priority", "")), verification.get("method"),
                                            verification.get("criteria"), acceptance, tuple(item.get("evidence_required") or ()),
                                            item.get("waiver_policy"), freeze(item.get("risk")), subjects)
        return types.MappingProxyType(found)

    def statements(self) -> tuple[Mapping[str, Requirement], Mapping[str, Preference], Mapping[str, Assumption],
                                  Mapping[str, OpenQuestion]]:
        path = "hw/requirements.yaml"
        requirements = self.requirements_only()
        data = self.doc("requirements.yaml") or {}
        preferences: dict[str, Preference] = {}
        for index, item in enumerate(data.get("preferences") or ()):
            if not isinstance(item, Mapping):
                continue
            pointer = f"preferences/{index}"
            ident = item.get("id")
            self.define("preference", ident, path, pointer)
            for i, subject in enumerate(item.get("subjects") or ()):
                self.subject(subject, path, f"{pointer}/subjects/{i}")
            source = self.source(item.get("source"), path, f"{pointer}/source")
            criterion = self.criterion(item.get("criterion"), path, f"{pointer}/criterion")
            if isinstance(ident, str):  # an entry without an id is unreferenceable; the schema reports it
                preferences[ident] = Preference(ident, item.get("text"), item.get("weight"), source, criterion)
        assumptions: dict[str, Assumption] = {}
        for index, item in enumerate(data.get("assumptions") or ()):
            if not isinstance(item, Mapping):
                continue
            pointer = f"assumptions/{index}"
            ident = item.get("id")
            self.define("assumption", ident, path, pointer)
            for requirement in item.get("requirements") or ():
                self.ref("requirement", requirement, path, pointer)
            self.ref("decision", item.get("decision"), path, pointer)
            if isinstance(item.get("bounds"), Mapping):
                self.quantity(item["bounds"], path, f"{pointer}/bounds")
            if isinstance(ident, str):
                assumptions[ident] = Assumption(ident, item.get("text"), item.get("status"), item.get("owner"),
                                                item.get("validation"), tuple(item.get("requirements") or ()))
        questions: dict[str, OpenQuestion] = {}
        for index, item in enumerate(data.get("open_questions") or ()):
            if not isinstance(item, Mapping):
                continue
            pointer = f"open_questions/{index}"
            ident = item.get("id")
            self.define("question", ident, path, pointer)
            subjects = tuple(self.subject(s, path, f"{pointer}/subjects/{i}") for i, s in enumerate(item.get("subjects") or ()))
            resolution = item.get("resolution")
            if isinstance(resolution, Mapping):
                self.ref("decision", resolution.get("decision"), path, f"{pointer}/resolution")
                if "source" in resolution:
                    self.source(resolution["source"], path, f"{pointer}/resolution/source")
            if isinstance(ident, str):
                questions[ident] = OpenQuestion(ident, item.get("question"), item.get("status"),
                                                bool(item.get("material_to_choice")), bool(item.get("safety_relevant")),
                                                subjects)
        return (requirements, types.MappingProxyType(preferences), types.MappingProxyType(assumptions),
                types.MappingProxyType(questions))

    def part_fields(self, pn: str, data: Mapping[str, Any], intent: PartIntent) -> Part:
        make_buy = data.get("make_buy")
        procurement = _mapping(data.get("procurement"))
        sourcing = ("make" if make_buy == "make" else
                    "made_to_drawing" if procurement.get("mode") == "made_to_drawing" else "catalogue")
        return Part(pn, data.get("title"), data.get("material"), data.get("process"), sourcing,
                    data.get("transparency"), intent, freeze(data))

    def parts(self) -> Mapping[str, Part]:
        found: dict[str, Part] = {}
        for rel in sorted(self.documents):
            if not re.fullmatch(r"parts/[^/]+/part\.yaml", rel):
                continue
            data = self.doc(rel)
            pn = data.get("PN") if data is not None else None
            if data is None or not isinstance(pn, str):
                continue
            path = f"hw/{rel}"
            self.define("part", pn, path, "PN")
            intent_data = data.get("intent")
            intent = self.intent(pn, intent_data, path) if isinstance(intent_data, Mapping) else \
                PartIntent.unknown("part.yaml has no intent")
            part = self.part_fields(pn, data, intent)
            self.check_route_sourcing(part, path)
            found[part.pn] = part
        return types.MappingProxyType(found)

    def intent(self, pn: str, data: Mapping[str, Any], path: str) -> PartIntent:
        functions = self.functions(data.get("functions"), path, "intent/functions")
        material = self.id_or_fact(data.get("material_profile"), "material", path, "intent/material_profile")
        self.ref("candidate", data.get("selection"), path, "intent/selection")
        service_data = data.get("service")
        service: Mapping[str, Any] | Fact
        if _looks_like_fact(service_data):
            service = self.fact(service_data, path, "intent/service")
        else:
            service_data = service_data if isinstance(service_data, Mapping) else {}
            for requirement in service_data.get("requirements") or ():
                self.ref("requirement", requirement, path, "intent/service/requirements")
            for load in service_data.get("loads") or ():
                self.ref("load", load, path, "intent/service/loads")
            if "life" in service_data:
                self.fact(service_data["life"], path, "intent/service/life", ("time", "count"))
            service = freeze(service_data)
        components = []
        for index, item in enumerate(data.get("components") or ()):
            if not isinstance(item, Mapping):
                continue
            pointer = f"intent/components/{index}"
            self.define(f"component:{pn}", item.get("id"), path, pointer)
            witness = item.get("witness")
            point = ((float(witness[0]), float(witness[1]), float(witness[2]))
                     if isinstance(witness, list) and len(witness) == 3 else None)
            components.append(Component(
                item.get("id"), point,
                freeze(item.get("locator")), self.id_or_fact(item.get("material_profile"), "material", path, f"{pointer}/material_profile"),
                self.function_entries(item.get("functions"), path, f"{pointer}/functions"), int(item.get("multiplicity", 1))))
        surfaces_data = data.get("critical_surfaces")
        surfaces: tuple[CriticalSurface, ...] | Fact
        if _looks_like_fact(surfaces_data):
            surfaces = self.fact(surfaces_data, path, "intent/critical_surfaces")
        else:
            entries = []
            for index, item in enumerate(surfaces_data if isinstance(surfaces_data, list) else ()):
                if not isinstance(item, Mapping):
                    continue
                pointer = f"intent/critical_surfaces/{index}"
                self.define(f"critical_surface:{pn}", item.get("id"), path, pointer)
                if item.get("component"):
                    self.ref(f"component:{pn}", item["component"], path, pointer)
                for interface in item.get("interfaces") or ():
                    self.ref("interface", interface, path, pointer)
                for requirement in item.get("requirements") or ():
                    self.ref("requirement", requirement, path, pointer)
                if item.get("inspection"):
                    self.ref(f"inspection:{pn}", item["inspection"], path, pointer)
                for i, criterion in enumerate(item.get("criteria") or ()):
                    self.criterion(criterion, path, f"{pointer}/criteria/{i}")
                entries.append(CriticalSurface(item.get("id"), item.get("feature"), item.get("role"), item.get("dimension_state"),
                                               item.get("region"), item.get("component"), tuple(item.get("interfaces") or ()),
                                               tuple(item.get("requirements") or ()), item.get("inspection")))
            surfaces = tuple(entries)
        route = self.route(pn, data.get("route"), path)
        rationale = data.get("rationale")
        if isinstance(rationale, Mapping):
            self.ref("decision", rationale.get("decision"), path, "intent/rationale/decision")
            self.ref("candidate", rationale.get("selection"), path, "intent/rationale/selection")
        return PartIntent(functions, material, data.get("selection"), service, tuple(components), surfaces, route,
                          freeze(rationale) if isinstance(rationale, Mapping) else None)

    def route(self, pn: str, data: Any, path: str) -> tuple[Operation, ...] | Fact:
        if _looks_like_fact(data):
            return self.fact(data, path, "intent/route")
        operations = []
        for index, item in enumerate(data if isinstance(data, list) else ()):
            if not isinstance(item, Mapping):
                continue
            pointer = f"intent/route/{index}"
            ident, kind = item.get("id"), item.get("kind")
            self.define(f"operation:{pn}", ident, path, pointer)
            if isinstance(kind, str) and kind not in KNOWN_ROUTE_KINDS:
                self.problem("intent.coverage", path, f"{pointer}/kind",
                             f"{pn} {ident}: route kind {kind!r} has no check contract; its coverage is unknown, never a pass",
                             severity="warning")
            for component in item.get("components") or ():
                self.ref(f"component:{pn}", component, path, pointer)
            for requirement in item.get("requirements") or ():
                self.ref("requirement", requirement, path, pointer)
            for evidence in item.get("evidence") or ():
                self.ref("evidence", evidence, path, pointer)
            for i, criterion in enumerate(item.get("criteria") or ()):
                self.criterion(criterion, path, f"{pointer}/criteria/{i}")
            capability = self.id_or_fact(item.get("capability"), "capability", path, f"{pointer}/capability")
            setup = item.get("setup") if isinstance(item.get("setup"), Mapping) else None
            if setup is not None:
                self.setup(setup, path, f"{pointer}/setup")
            orientation = self.orientation(item.get("orientation"), path, f"{pointer}/orientation")
            supports = item.get("supports")
            if _looks_like_fact(supports):
                supports = self.fact(supports, path, f"{pointer}/supports")
            elif isinstance(supports, Mapping):
                if "allowance" in supports:
                    self.quantity(supports["allowance"], path, f"{pointer}/supports/allowance", ("length",))
                for inspection in supports.get("inspection") or ():
                    self.ref(f"inspection:{pn}", inspection, path, f"{pointer}/supports/inspection")
                supports = freeze(supports)
            for i, allowance in enumerate(item.get("allowances") or ()):
                if isinstance(allowance, Mapping):
                    self.quantity(allowance.get("amount"), path, f"{pointer}/allowances/{i}/amount", ("length",))
                    self.source(allowance.get("source"), path, f"{pointer}/allowances/{i}/source")
            for i, inspection in enumerate(item.get("inspection") or ()):
                if isinstance(inspection, Mapping):
                    self.define(f"inspection:{pn}", inspection.get("id"), path, f"{pointer}/inspection/{i}")
                    self.ref("requirement", inspection.get("requirement"), path, f"{pointer}/inspection/{i}")
                    self.ref("capability", inspection.get("capability"), path, f"{pointer}/inspection/{i}")
                    for evidence in inspection.get("evidence") or ():
                        self.ref("evidence", evidence, path, f"{pointer}/inspection/{i}")
                    if "criterion" in inspection:
                        self.criterion(inspection["criterion"], path, f"{pointer}/inspection/{i}/criterion")
            purchase = self.purchase(item.get("purchase"), path, f"{pointer}/purchase")
            operations.append(Operation(
                ident, kind, capability, item.get("provider"), tuple(item.get("components") or ()),
                tuple(item.get("depends_on") or ()), item.get("input_state"), item.get("output_state"),
                freeze(item.get("targets") or []), freeze(setup), orientation, supports,
                freeze(item.get("allowances") or []), freeze(item.get("inspection") or []),
                tuple(item.get("requirements") or ()), freeze(purchase), freeze(item)))
        self.check_dependencies(pn, operations, path)
        return tuple(operations)

    def purchase(self, data: Any, path: str, pointer: str) -> Mapping[str, Any] | None:
        """Check the facts and supplier evidence of a purchase block (a part operation's or a candidate step's)."""
        if not isinstance(data, Mapping):
            return None
        for key in ("identity", "specification", "ratings", "optical", "availability"):
            if key in data:
                self.fact(data[key], path, f"{pointer}/{key}")
        if "thickness" in data:
            self.fact(data["thickness"], path, f"{pointer}/thickness", ("length",))
        for i, source in enumerate(data.get("supplier_evidence") or ()):
            self.source(source, path, f"{pointer}/supplier_evidence/{i}")
        return data

    def setup(self, setup: Mapping[str, Any], path: str, pointer: str) -> None:
        stock = _mapping(setup.get("stock"))
        self.id_or_fact(stock.get("material_profile"), "material", path, f"{pointer}/stock/material_profile")
        for key in ("grade", "form", "dimensions"):
            if key in stock:
                self.fact(stock[key], path, f"{pointer}/stock/{key}")
        if "thickness" in stock:
            self.fact(stock["thickness"], path, f"{pointer}/stock/thickness", ("length",))
        workholding = _mapping(setup.get("workholding"))
        if "method" in workholding:
            self.fact(workholding["method"], path, f"{pointer}/workholding/method")
        for key, dimensions in (("clamping_force", ("force",)), ("allowable_deformation", ("length",))):
            if key in workholding:
                self.fact(workholding[key], path, f"{pointer}/workholding/{key}", dimensions)
        access = _mapping(setup.get("access"))
        for index, tool in enumerate(access.get("tools") or ()):
            if isinstance(tool, Mapping):
                self.ref("capability", tool.get("capability"), path, f"{pointer}/access/tools/{index}")
                for key in ("diameter", "reach"):
                    if key in tool:
                        self.fact(tool[key], path, f"{pointer}/access/tools/{index}/{key}", ("length",))
        if "inspection_access" in setup:
            self.fact(setup["inspection_access"], path, f"{pointer}/inspection_access")

    def orientation(self, data: Any, path: str, pointer: str) -> Mapping[str, Any] | Fact | None:
        if data is None:
            return None
        if _looks_like_fact(data):
            return self.fact(data, path, pointer)
        if not isinstance(data, Mapping):
            return None
        rotation = _mapping(data.get("rotation"))
        quaternion = rotation.get("quaternion")
        if isinstance(quaternion, list) and len(quaternion) == 4 and all(isinstance(q, (int, float)) for q in quaternion):
            norm = math.sqrt(sum(float(q) ** 2 for q in quaternion))
            if not math.isfinite(norm) or abs(norm - 1.0) > 1e-6:
                self.problem("intent.number", path, f"{pointer}/rotation/quaternion",
                             f"{pointer}: quaternion norm {norm:.9g} is not 1; a rotation needs a unit quaternion")
        axis = rotation.get("axis")
        if isinstance(axis, list) and len(axis) == 3 and all(isinstance(a, (int, float)) for a in axis):
            if math.sqrt(sum(float(a) ** 2 for a in axis)) == 0.0:
                self.problem("intent.number", path, f"{pointer}/rotation/axis", f"{pointer}: a rotation axis cannot be zero")
        self.ref("decision", data.get("decision"), path, f"{pointer}/decision")
        self.ref("candidate", data.get("selection"), path, f"{pointer}/selection")
        return freeze_mapping(data)

    def check_dependencies(self, pn: str, operations: list[Operation], path: str) -> None:
        ids = [op.id for op in operations]
        graph: dict[str | None, tuple[str | None, ...]] = {}
        for index, op in enumerate(operations):
            for dependency in op.depends_on:
                if dependency not in ids:
                    self.problem("intent.route", path, f"intent/route/{index}/depends_on",
                                 f"{pn} {op.id} depends on {dependency}, which is not an operation of its route")
            graph[op.id] = op.depends_on or ((ids[index - 1],) if index else ())
        state: dict[str | None, int] = {}

        def visit(node: str | None) -> bool:
            if state.get(node) == 1:
                return True
            if state.get(node) == 2:
                return False
            state[node] = 1
            cyclic = any(visit(n) for n in graph.get(node, ()) if n in graph)
            state[node] = 2
            return cyclic

        if any(visit(node) for node in graph):
            self.problem("intent.route", path, "intent/route", f"{pn}: the route's depends_on form a cycle")

    def check_route_sourcing(self, part: Part, path: str) -> None:
        route = part.intent.route
        if is_fact(route):
            return
        kinds = [op.kind for op in route]
        if part.sourcing == "catalogue":
            made = sorted({str(k) for k in kinds if k not in ("purchased", "inspection")})
            if made:
                self.problem("intent.route", path, "intent/route",
                             f"{part.pn} is a catalogue buy but its route makes it ({', '.join(made)}); a modified purchased "
                             "item is procurement.mode made_to_drawing")
            if "purchased" not in kinds:
                self.problem("intent.route", path, "intent/route", f"{part.pn} is a catalogue buy; its route needs its purchase")
        elif "purchased" in kinds:
            self.problem("intent.route", path, "intent/route",
                         f"{part.pn} is custom-made ({part.sourcing}); record bought stock in setup.stock, not a purchased operation, "
                         "and list the operations the maker performs")

    def interfaces(self) -> Mapping[str, Interface]:
        path = "hw/interfaces.yaml"
        found: dict[str, Interface] = {}
        for index, item in enumerate((self.doc("interfaces.yaml") or {}).get("interfaces") or ()):
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                continue
            pointer = f"interfaces/{index}"
            self.define("interface", item["id"], path, pointer)
            data = item.get("functional")
            if not isinstance(data, Mapping):
                functional = InterfaceFunctional.unknown("interfaces.yaml has no functional block")
            else:
                base = f"{pointer}/functional"
                critical = self.fact(data.get("critical"), path, f"{base}/critical")
                functions = self.functions(data.get("functions"), path, f"{base}/functions")
                for requirement in data.get("requirements") or ():
                    self.ref("requirement", requirement, path, base)
                for load in data.get("loads") or ():
                    self.ref("load", load, path, f"{base}/loads")
                if "failure_consequence" in data:
                    self.fact(data["failure_consequence"], path, f"{base}/failure_consequence")
                for i, surface in enumerate(data.get("surfaces") or ()):
                    if isinstance(surface, Mapping):
                        self.ref("part", surface.get("PN"), path, f"{base}/surfaces/{i}")
                        if surface.get("component") and surface.get("PN"):
                            self.ref(f"component:{surface['PN']}", surface["component"], path, f"{base}/surfaces/{i}")
                        if "source" in surface:
                            self.source(surface["source"], path, f"{base}/surfaces/{i}/source")
                for i, acceptance in enumerate(data.get("acceptance") or ()):
                    if isinstance(acceptance, Mapping):
                        self.criterion(acceptance.get("criterion"), path, f"{base}/acceptance/{i}/criterion")
                variation = _mapping(data.get("variation"))
                self.ref("case", variation.get("stack_case"), path, f"{base}/variation/stack_case")
                self.ref("decision", variation.get("decision"), path, f"{base}/variation/decision")
                contact = _mapping(data.get("contact"))
                for i, material in enumerate(contact.get("materials") or ()):
                    self.id_or_fact(material, "material", path, f"{base}/contact/materials/{i}")
                for key, dimensions in (("load", ("force", "torque")), ("speed", ("speed", "frequency")),
                                        ("life", ("time", "count")), ("finish", None), ("lubrication", None), ("duty", None)):
                    if key in contact:
                        self.fact(contact[key], path, f"{base}/contact/{key}", dimensions)
                for evidence in contact.get("evidence") or ():
                    self.ref("evidence", evidence, path, f"{base}/contact/evidence")
                play = _mapping(data.get("play"))
                compliance = _mapping(play.get("compliance"))
                for block, prefix, keys in ((play, "play", ("axial", "radial", "angular", "backlash")),
                                            (compliance, "play/compliance", ("axial", "radial", "angular"))):
                    for key in keys:
                        if key in block:
                            play_dimensions = {"axial": ("length",), "radial": ("length",), "angular": ("angle",)}.get(key, ("length", "angle"))
                            self.fact(block[key], path, f"{base}/{prefix}/{key}", play_dimensions)
                for case in play.get("scenario_cases") or ():
                    self.ref("motion_case", case, path, f"{base}/play/scenario_cases")
                for operation in data.get("assembly_operations") or ():
                    self.ref("assembly_operation", operation, path, f"{base}/assembly_operations")
                for decision in data.get("decisions") or ():
                    self.ref("decision", decision, path, f"{base}/decisions")
                functional = InterfaceFunctional(critical, functions, tuple(data.get("requirements") or ()), freeze(data))
            found[item["id"]] = Interface(item["id"], item.get("type"), functional, freeze(item))
        return types.MappingProxyType(found)

    def assembly_operations(self) -> tuple[Mapping[str, Any], ...] | Fact:
        path = "hw/assembly.yaml"
        data = (self.doc("assembly.yaml") or {}).get("operations")
        if _looks_like_fact(data):
            return self.fact(data, path, "operations")
        if not isinstance(data, list):
            return Fact.unknown_because("assembly.yaml has no operations")
        for index, item in enumerate(data):
            if not isinstance(item, Mapping):
                continue
            pointer = f"operations/{index}"
            self.define("assembly_operation", item.get("id"), path, pointer)
            self.ref("interface", item.get("interface"), path, pointer)
            for key, dimensions in (("engagement", ("length",)), ("torque", ("torque",)), ("retention", None), ("service_access", None)):
                if key in item:
                    self.fact(item[key], path, f"{pointer}/{key}", dimensions)
            edge = item.get("edge_distance")
            if isinstance(edge, Mapping):
                criterion = edge.get("criterion")
                self.criterion(criterion, path, f"{pointer}/edge_distance/criterion")
                if isinstance(criterion, Mapping) and criterion.get("units") in UNITS and unit(criterion["units"])[0] != "length":
                    self.problem("intent.units", path, f"{pointer}/edge_distance/criterion",
                                 f"{pointer}/edge_distance/criterion: {criterion['units']} is not a length")
                self.fact(edge.get("distance"), path, f"{pointer}/edge_distance/distance", ("length",))
            for i, tool in enumerate(item.get("tool_access") or ()):
                if isinstance(tool, Mapping):
                    self.ref("capability", tool.get("capability"), path, f"{pointer}/tool_access/{i}")
                    if "reach" in tool:
                        self.fact(tool["reach"], path, f"{pointer}/tool_access/{i}/reach", ("length",))
                    if "envelope" in tool:
                        self.fact(tool["envelope"], path, f"{pointer}/tool_access/{i}/envelope")
        for index, item in enumerate(data):
            if isinstance(item, Mapping):
                for predecessor in item.get("predecessors") or ():
                    self.ref("assembly_operation", predecessor, path, f"operations/{index}/predecessors")
        return tuple(freeze(item) for item in data)

    def capabilities(self) -> Mapping[str, CapabilityProfile]:
        path = "hw/manufacturing/capabilities.yaml"
        found: dict[str, CapabilityProfile] = {}
        for index, item in enumerate((self.doc("manufacturing/capabilities.yaml") or {}).get("profiles") or ()):
            if isinstance(item, Mapping):
                profile = self.capability_profile(item, path, f"profiles/{index}")
                if profile is not None:
                    found[profile.id] = profile
        return types.MappingProxyType(found)

    def capability_profile(self, item: Mapping[str, Any], path: str, pointer: str, register: bool = True) -> CapabilityProfile | None:
        ident, process = item.get("id"), item.get("process")
        if register:
            self.define("capability", ident, path, pointer)
        vocabulary = CAPABILITY_PARAMETERS.get(process) if isinstance(process, str) else None

        def parameters(data: Any, where: str) -> Mapping[str, Fact]:
            result = {}
            for name, value in (data.items() if isinstance(data, Mapping) else ()):
                expected = vocabulary.get(name) if vocabulary is not None else None
                if vocabulary is not None and name not in vocabulary:
                    self.problem("intent.profile", path, f"{where}/{name}",
                                 f"{ident}: {name} is not a {process} parameter ({', '.join(sorted(vocabulary))}); achievable "
                                 "accuracy belongs in calibrations")
                dimensions = expected if isinstance(expected, tuple) else None
                parsed = self.fact(value, path, f"{where}/{name}", dimensions)
                if expected == TEXT and parsed.known and isinstance(parsed.value, Quantity):
                    self.problem("intent.units", path, f"{where}/{name}", f"{ident}: {name} is text, not a quantity")
                if isinstance(expected, tuple) and parsed.known and not isinstance(parsed.value, Quantity):
                    self.problem("intent.units", path, f"{where}/{name}", f"{ident}: {name} needs a quantity in "
                                                                          f"{' or '.join(expected)}")
                result[name] = parsed
            return types.MappingProxyType(result)

        params = parameters(item.get("parameters"), f"{pointer}/parameters")
        for index, tool in enumerate(item.get("tools") or ()):
            if isinstance(tool, Mapping):
                parameters(tool.get("parameters"), f"{pointer}/tools/{index}/parameters")
        for key in ("supplier", "operator", "workholding"):
            if key in item:
                self.fact(item[key], path, f"{pointer}/{key}")
        machine = _mapping(item.get("machine"))
        if "identity" in machine:
            self.fact(machine["identity"], path, f"{pointer}/machine/identity")
        envelope = _mapping(item.get("envelope"))
        for axis in ("x", "y", "z"):
            if axis in envelope:
                self.fact(envelope[axis], path, f"{pointer}/envelope/{axis}", ("length",))
        if "source" in item:
            self.source(item["source"], path, f"{pointer}/source")
        materials_data = item.get("materials")
        materials: tuple[Mapping[str, Any], ...] | Fact
        if _looks_like_fact(materials_data):
            materials = self.fact(materials_data, path, f"{pointer}/materials")
        else:
            for i, approval in enumerate(materials_data if isinstance(materials_data, list) else ()):
                if isinstance(approval, Mapping):
                    where = f"{pointer}/materials/{i}"
                    if register:
                        self.ref("material", approval.get("material"), path, where)
                    self.source(approval.get("source"), path, f"{where}/source")
                    if "thickness" in approval:
                        self.fact(approval["thickness"], path, f"{where}/thickness", ("length",))
                    for key in ("coating", "extraction"):
                        if key in approval:
                            self.fact(approval[key], path, f"{where}/{key}")
            materials = freeze(materials_data if isinstance(materials_data, list) else [])
        calibrations = []
        for index, data in enumerate(item.get("calibrations") or ()):
            if not isinstance(data, Mapping):
                continue
            where = f"{pointer}/calibrations/{index}"
            self.define(f"calibration:{ident}", data.get("id"), path, where)
            scope = _mapping(data.get("scope"))
            if register:
                self.ref("material", scope.get("material"), path, f"{where}/scope/material")
            for key in ("size", "thickness"):
                if key in scope:
                    self.quantity(scope[key], path, f"{where}/scope/{key}")
            result = _mapping(data.get("result"))
            limits = self.quantity(result.get("limits"), path, f"{where}/result/limits")
            uncertainty = self.quantity(result.get("uncertainty"), path, f"{where}/result/uncertainty") if "uncertainty" in result else None
            source = self.source(data.get("source"), path, f"{where}/source")
            if source is not None and source.kind not in ("calibration", "test", "supplier"):
                self.problem("intent.profile", path, f"{where}/source",
                             f"{ident} {data.get('id')}: a calibration rests on calibration, test or supplier evidence, not {source.kind}")
            self.ref("decision", data.get("decision"), path, where)
            if limits is not None and source is not None:
                calibrations.append(Calibration(data.get("id"), data.get("characteristic"), freeze(scope), limits, uncertainty,
                                                int(result.get("samples", 0)), source, data.get("date"), data.get("valid_until")))
        if not isinstance(ident, str):
            return None
        return CapabilityProfile(ident, int(item.get("revision", 0)), process, item.get("provider"), bool(item.get("template")),
                                 params, materials, tuple(calibrations), freeze(item))

    def materials(self) -> Mapping[str, MaterialProfile]:
        path = "hw/manufacturing/materials.yaml"
        found: dict[str, MaterialProfile] = {}
        for index, item in enumerate((self.doc("manufacturing/materials.yaml") or {}).get("profiles") or ()):
            if isinstance(item, Mapping):
                profile = self.material_profile(item, path, f"profiles/{index}")
                if profile is not None:
                    found[profile.id] = profile
        return types.MappingProxyType(found)

    def material_profile(self, item: Mapping[str, Any], path: str, pointer: str, register: bool = True) -> MaterialProfile | None:
        ident = item.get("id")
        if register:
            self.define("material", ident, path, pointer)
        level, form = item.get("level"), item.get("form")
        grade = self.fact(item.get("grade"), path, f"{pointer}/grade")
        if level == "grade" and not grade.known:
            self.problem("intent.profile", path, f"{pointer}/grade", f"{ident}: a grade profile names its grade")
        if level == "family" and grade.known:
            self.problem("intent.profile", path, f"{pointer}/grade", f"{ident}: a family profile has no grade; family guidance "
                                                                       "is never a grade specification")
        for key in ("manufacturer", "specification"):
            if key in item:
                self.fact(item[key], path, f"{pointer}/{key}")
        state = _mapping(item.get("process_state"))
        if _looks_like_fact(state.get("process")):
            self.fact(state["process"], path, f"{pointer}/process_state/process")
        typical_only = bool(item.get("typical_not_allowable"))
        properties = []
        for index, data in enumerate(item.get("properties") or ()):
            if not isinstance(data, Mapping):
                continue
            where = f"{pointer}/properties/{index}"
            name = data.get("name")
            dimensions = MATERIAL_PROPERTIES.get(name) if isinstance(name, str) else None
            if dimensions is None:
                self.problem("intent.profile", path, f"{where}/name",
                             f"{ident}: {name!r} is not a material property name ({', '.join(MATERIAL_PROPERTIES)})")
            value = self.fact(data.get("value"), path, f"{where}/value", dimensions)
            if value.known and value.basis == "allowable" and typical_only:
                self.problem("intent.allowable", path, f"{where}/value",
                             f"{ident} {name}: a typical_not_allowable profile holds no allowables")
            if value.known and value.basis == "allowable" and level == "family":
                self.problem("intent.allowable", path, f"{where}/value", f"{ident} {name}: family guidance is never an allowable")
            conditions = _mapping(data.get("conditions"))
            orientation = (self.fact(conditions["orientation"], path, f"{where}/conditions/orientation")
                           if "orientation" in conditions else None)
            if form == "printed" and orientation is None:
                self.problem("intent.profile", path, f"{where}/conditions",
                             f"{ident} {name}: a printed property states its orientation, or why it is unknown")
            properties.append(MaterialProperty(
                name, value, self.fact(conditions.get("test_standard"), path, f"{where}/conditions/test_standard"),
                self.fact(conditions.get("temperature"), path, f"{where}/conditions/temperature", ("temperature",)),
                conditions.get("conditioning", "unknown"), orientation))
        if not isinstance(ident, str):
            return None
        return MaterialProfile(ident, int(item.get("revision", 0)), level, item.get("family"), grade, form,
                               state.get("conditioning", "unknown"), typical_only, tuple(properties), freeze(item))

    def selection(self) -> Selection:
        path = "hw/manufacturing/selection.yaml"
        data = self.doc("manufacturing/selection.yaml") or {}
        candidates: dict[str, Mapping[str, Any]] = {}
        proposed: dict[str, Mapping[str, ProposedPart]] = {}
        for index, item in enumerate(data.get("candidates") or ()):
            if not isinstance(item, Mapping):
                continue
            pointer = f"candidates/{index}"
            ident = item.get("id")
            self.define("candidate", ident, path, pointer)
            introduced: dict[str, ProposedPart] = {}
            for i, part in enumerate(item.get("parts") or ()):
                if not isinstance(part, Mapping):
                    continue
                where = f"{pointer}/parts/{i}"
                self.define(f"candidate_part:{index}", part.get("PN"), path, where)
                proposal = part.get("proposed")
                if isinstance(proposal, Mapping):
                    self.ref("proposed_part", part.get("PN"), path, where)
                    entries = self.function_entries(proposal.get("functions"), path, f"{where}/proposed/functions")
                    pn = part.get("PN")
                    if isinstance(pn, str) and isinstance(ident, str):
                        introduced[pn] = ProposedPart(pn, ident, proposal.get("title"), proposal.get("sourcing"),
                                                      proposal.get("reason"), entries, proposal.get("transparency"))
                else:
                    self.ref("part", part.get("PN"), path, where)
                self.id_or_fact(part.get("material_profile"), "material", path, f"{where}/material_profile")
                for j, step in enumerate(part.get("route") or ()):
                    if isinstance(step, Mapping):
                        self.id_or_fact(step.get("capability"), "capability", path, f"{where}/route/{j}/capability")
                        self.purchase(step.get("purchase"), path, f"{where}/route/{j}/purchase")
            if introduced and isinstance(ident, str):
                proposed[ident] = types.MappingProxyType(introduced)
            for interface in item.get("interfaces") or ():
                self.ref("interface", interface, path, pointer)
            for i, evaluation in enumerate(item.get("evaluations") or ()):
                if isinstance(evaluation, Mapping):
                    self.ref("requirement", evaluation.get("requirement"), path, f"{pointer}/evaluations/{i}")
                    if "criterion" in evaluation:
                        self.criterion(evaluation["criterion"], path, f"{pointer}/evaluations/{i}/criterion")
                    if "source" in evaluation:
                        self.source(evaluation["source"], path, f"{pointer}/evaluations/{i}/source")
                    if isinstance(evaluation.get("value"), Mapping):
                        self.quantity(evaluation["value"], path, f"{pointer}/evaluations/{i}/value")
            for key, dimensions in (("quantity", ("count",)), ("cost", None), ("lead_time", ("time",)), ("maintenance", None)):
                if key in item:
                    self.fact(item[key], path, f"{pointer}/{key}", dimensions)
            for i, score in enumerate(item.get("preference_scores") or ()):
                if isinstance(score, Mapping):
                    self.ref("preference", score.get("preference"), path, f"{pointer}/preference_scores/{i}")
            if isinstance(ident, str):  # an id-less candidate is unreferenceable; the schema reports it
                candidates[ident] = freeze_mapping(item)
        recommendation = data.get("recommendation")
        if isinstance(recommendation, Mapping):
            for key in ("primary", "prototype", "end_use", "fallback"):
                self.ref("candidate", recommendation.get(key), path, f"recommendation/{key}")
            for requirement in recommendation.get("governing_requirements") or ():
                self.ref("requirement", requirement, path, "recommendation/governing_requirements")
        assessment = _mapping(data.get("assessment"))
        self.ref("decision", assessment.get("decision"), path, "assessment/decision")
        return Selection(data.get("status", "not_assessed"), types.MappingProxyType(candidates),
                         freeze(recommendation) if isinstance(recommendation, Mapping) else None, freeze(data),
                         types.MappingProxyType(proposed))

    def ledgers(self) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
        evidence_path, findings_path = "hw/manufacturing/evidence.yaml", "hw/manufacturing/findings.yaml"
        entries = [e for e in (self.doc("manufacturing/evidence.yaml") or {}).get("entries") or () if isinstance(e, Mapping)]
        for index, entry in enumerate(entries):
            pointer = f"entries/{index}"
            self.define("evidence", entry.get("id"), evidence_path, pointer)
            self.ref("requirement", entry.get("requirement"), evidence_path, pointer)
            self.subject(entry.get("subject"), evidence_path, f"{pointer}/subject")
            if "criterion" in entry:
                self.criterion(entry["criterion"], evidence_path, f"{pointer}/criterion")
            if "source" in entry:
                self.source(entry["source"], evidence_path, f"{pointer}/source")
            for decision in entry.get("decisions") or ():
                self.ref("decision", decision, evidence_path, pointer)
            waiver = _mapping(entry.get("waiver"))
            self.ref("decision", waiver.get("decision"), evidence_path, f"{pointer}/waiver")
        dispositions = [d for d in (self.doc("manufacturing/findings.yaml") or {}).get("dispositions") or () if isinstance(d, Mapping)]
        for index, disposition in enumerate(dispositions):
            pointer = f"dispositions/{index}"
            self.ref("decision", disposition.get("decision"), findings_path, pointer)
            for evidence in disposition.get("replacement_evidence") or ():
                self.ref("evidence", evidence, findings_path, pointer)
            waiver = _mapping(disposition.get("waiver"))
            self.ref("decision", waiver.get("decision"), findings_path, f"{pointer}/waiver")
            if waiver.get("subject"):
                self.subject(waiver["subject"], findings_path, f"{pointer}/waiver/subject")
        return freeze(entries), freeze(dispositions)

    def check_profile_use(self, parts: Mapping[str, Part], capabilities: Mapping[str, CapabilityProfile],
                          materials: Mapping[str, MaterialProfile]) -> None:
        for pn, part in parts.items():
            path = f"hw/parts/{pn}/part.yaml"
            route = part.intent.route
            if is_fact(route):
                continue
            for index, op in enumerate(route):
                profile = capabilities.get(op.capability) if isinstance(op.capability, str) else None
                if profile is None:
                    continue
                if profile.template:
                    self.problem("intent.profile", path, f"intent/route/{index}/capability",
                                 f"{pn} {op.id}: {profile.id} is a template, not a capability; record the actual machine or supplier")
                if profile.process != op.kind:
                    self.problem("intent.profile", path, f"intent/route/{index}/capability",
                                 f"{pn} {op.id}: {profile.id} is a {profile.process} capability, the operation is {op.kind}")

    def resolve(self) -> None:
        for kind, ident, path, pointer in self.refs:
            if kind == "decision":
                if self.hw is not None and not (self.hw / "decisions" / f"{ident}.md").is_file():
                    self.problem("intent.reference", path, pointer, f"{pointer}: decision {ident} has no hw/decisions/{ident}.md")
                continue
            if kind in ("case", "motion_case"):
                folder = "analysis" if kind == "case" else "motion"
                name = "case.yaml" if kind == "case" else "plan.yaml"
                if self.hw is not None and not (self.hw / folder / "cases" / ident / name).is_file():
                    self.problem("intent.reference", path, pointer, f"{pointer}: {kind} {ident} has no hw/{folder}/cases/{ident}/{name}")
                continue
            if kind == "proposed_part":
                if ident in self.defined.get("part", set()):
                    self.problem("intent.duplicate", path, pointer, f"{pointer}: proposed part {ident} is already declared under "
                                 f"hw/parts/{ident}; reference the declared part, or propose an unused PN")
                continue
            if ident not in self.defined.get(kind, set()):
                label = kind.split(":", 1)[0].replace("_", " ")
                self.problem("intent.reference", path, pointer, f"{pointer}: {label} {ident} is not declared")
        if self.hw is not None:
            for artifact, digest in sorted(self.artifacts.items()):
                target = self.hw / artifact
                if not target.is_file() or target.is_symlink():
                    self.problem("intent.source_artifact", f"hw/{artifact}", "", f"hw/{artifact} is named as a source but is missing")
                elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    self.problem("intent.source_artifact", f"hw/{artifact}", "",
                                 f"hw/{artifact} does not match the sha256 its source names")


# --- policy ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class UseCaseAssessment:
    status: str | None
    service_class: str | None  # the known service class, or None
    final_ready: bool  # a final material/process recommendation may be made
    blocking: tuple[str, ...]  # why it may not
    unknown: tuple[str, ...]  # use-case fields that are unknown
    open_questions: tuple[str, ...]  # open questions that are material to the choice or safety-relevant


def assess_use_case(record: Record) -> UseCaseAssessment:
    """Whether the use case supports a final material/process recommendation. Conditional exploration
    is always allowed; a final choice needs a confirmed use case, a known service class, no open
    material-to-choice or safety question, and no unknown that no open question tracks. A resolved
    question does not track an unknown: its answer belongs in the fact, so the pair is a contradiction."""
    use_case = record.use_case
    blocking: list[str] = []
    if record.version != RECORD_VERSION:
        blocking.append(f"the record is record version {record.version}; run migrate.py")
    if use_case.status != "confirmed" or not use_case.decision:
        blocking.append("the use case is not confirmed by a decision")
    service = use_case.service_class.value if use_case.service_class.known else None
    if service in (None, "unknown"):
        blocking.append("the service class is unknown")
    fields = dict(use_case.facts)
    fields["service_class"] = use_case.service_class
    for name, value in (("loads", use_case.loads), ("capability.equipment", use_case.equipment),
                        ("evidence_policy", use_case.evidence_policy)):
        if is_fact(value):
            fields[name] = value
    unknown = tuple(sorted(name for name, value in fields.items() if value.unknown))
    for name in unknown:
        tracking = fields[name].question
        question = record.questions.get(tracking) if tracking else None
        if question is None:
            blocking.append(f"use_case.{name} is unknown and no open question tracks it")
        elif question.status != "open":
            blocking.append(f"use_case.{name} is unknown but its question {question.id} is {question.status}; "
                            "record the answer in the fact or reopen the question")
    open_questions = tuple(sorted(q.id for q in record.questions.values() if q.blocks_final_choice))
    if open_questions:
        blocking.append(f"open questions material to the choice or safety: {', '.join(open_questions)}")
    if service == "safety_critical":
        for name in ("consequences.standards", "consequences.approval_authority"):
            if not fields[name].known:
                blocking.append(f"a safety-critical use case needs use_case.{name}")
    return UseCaseAssessment(use_case.status, service, not blocking, tuple(blocking), unknown, open_questions)


@dataclass(frozen=True)
class PartRoute:
    pn: str
    sourcing: str  # make, made_to_drawing or catalogue
    operations: tuple[Operation, ...]  # route order; empty while the route is unknown
    # An id is None only for an operation or component a schema-invalid record leaves unnamed.
    predecessors: Mapping[str | None, tuple[str | None, ...]]  # operation id -> the operations it depends on
    unknown: Fact | None  # the route fact while it is not recorded
    unsupported: tuple[str | None, ...]  # operation ids whose kind has no check contract
    components: tuple[str | None, ...]  # component ids; ("main",) for a sole solid


def part_route(record: Record, pn: str) -> PartRoute:
    """The ordered route of part `pn` with each operation's predecessors. IntentError for an unknown PN
    or a route whose dependencies are unknown or cyclic."""
    part = record.parts.get(pn)
    if part is None:
        raise IntentError(f"no part {pn}")
    components: tuple[str | None, ...] = tuple(c.id for c in part.intent.components) or ("main",)
    route = part.intent.route
    if is_fact(route):
        return PartRoute(pn, part.sourcing, (), types.MappingProxyType({}), route, (), components)
    ids = [op.id for op in route]
    predecessors: dict[str | None, tuple[str | None, ...]] = {}
    for index, op in enumerate(route):
        missing = [d for d in op.depends_on if d not in ids]
        if missing:
            raise IntentError(f"{pn} {op.id} depends on {', '.join(missing)}, not operations of its route")
        predecessors[op.id] = op.depends_on or ((ids[index - 1],) if index else ())
    _topological(pn, predecessors)
    return PartRoute(pn, part.sourcing, route, types.MappingProxyType(predecessors), None,
                     tuple(op.id for op in route if not op.known_kind), components)


def _topological(pn: str, predecessors: Mapping[str | None, tuple[str | None, ...]]) -> list[str | None]:
    order: list[str | None] = []
    state: dict[str | None, int] = {}

    def visit(node: str | None) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise IntentError(f"{pn}: the route's depends_on form a cycle through {node}")
        state[node] = 1
        for previous in predecessors.get(node, ()):
            visit(previous)
        state[node] = 2
        order.append(node)

    for node in predecessors:
        visit(node)
    return order


@dataclass(frozen=True)
class PropertyResolution:
    status: str  # resolved, unknown or refused
    profile: str
    name: str
    value: Quantity | None
    basis: str | None
    sources: tuple[Source, ...]
    conditions: Mapping[str, Any] | None
    reasons: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"


def resolve_property(profile: MaterialProfile, name: str, *, form: str | None = None, orientation: str | None = None,
                     conditioning: str | None = None, temperature: Quantity | None = None,
                     allowable: bool = False) -> PropertyResolution:
    """Property `name` of `profile` under the requested conditions: resolved only when exactly one sourced
    value matches the form, orientation, conditioning, temperature (its tested range contains the
    requested one) and, with `allowable`, a justified allowable basis. Never another property, another
    profile or a default: unknown when no value exists, refused when values exist but none applies."""
    if name not in MATERIAL_PROPERTIES:
        raise IntentError(f"{name!r} is not a material property name")

    def answer(status: str, reasons: list[str], match: MaterialProperty | None = None) -> PropertyResolution:
        if match is None:
            return PropertyResolution(status, profile.id, name, None, None, (), None, tuple(reasons))
        conditions = {"test_standard": match.test_standard.value if match.test_standard.known else None,
                      "temperature": match.temperature.value if match.temperature.known else None,
                      "conditioning": match.conditioning,
                      "orientation": match.orientation.value if match.orientation is not None and match.orientation.known else None}
        return PropertyResolution(status, profile.id, name, match.value.value, match.value.basis, match.value.sources,
                                  types.MappingProxyType(conditions), tuple(reasons))

    reasons: list[str] = []
    if form is not None and profile.form != form:
        return answer("refused", [f"{profile.id} is {profile.form} material; {form} properties were requested"])
    if allowable and (profile.level == "family" or profile.typical_not_allowable):
        guidance = "family guidance" if profile.level == "family" else "typical_not_allowable"
        return answer("refused", [f"{profile.id} is {guidance}: it holds no design allowables"])
    candidates = [p for p in profile.properties if p.name == name and p.value.known]
    if not candidates:
        return answer("unknown", [f"{profile.id} records no known {name}"])
    matches = []
    for candidate in candidates:
        why: list[str] = []
        if conditioning is not None and candidate.conditioning != conditioning:
            why.append(f"tested {candidate.conditioning}, {conditioning} requested")
        if orientation is not None:
            stated = candidate.orientation.value if candidate.orientation is not None and candidate.orientation.known else None
            if not isinstance(stated, str) or stated.strip().lower() != orientation.strip().lower():
                why.append(f"orientation {stated or 'not stated'}, {orientation} requested")
        if temperature is not None:
            tested = candidate.temperature.value if candidate.temperature.known else None
            if not isinstance(tested, Quantity) or not tested.contains(temperature):
                why.append(f"tested at {tested or 'an unstated temperature'}, {temperature} requested")
        if allowable and candidate.value.basis != "allowable":
            why.append(f"{candidate.value.basis} value, an allowable requested")
        if why:
            reasons.append("; ".join(why))
        else:
            matches.append(candidate)
    if len(matches) > 1:
        values = {str(m.value.value) for m in matches}
        if len(values) > 1:
            return answer("refused", [f"{len(matches)} {name} values match with different values: {', '.join(sorted(values))}"])
    if matches:
        return answer("resolved", [], matches[0])
    return answer("refused", reasons)


@dataclass(frozen=True)
class CapabilityResolution:
    status: str  # calibrated, unknown or refused
    profile: str
    characteristic: str
    calibration: Calibration | None
    reasons: tuple[str, ...]


def capability_limit(profile: CapabilityProfile, characteristic: str, *, material: str, on: date | None = None,
                     tool: str | None = None, orientation: str | None = None, feature: str | None = None) -> CapabilityResolution:
    """The calibration that demonstrates `characteristic` for `material` (and the tool, orientation and
    feature when given), valid on `on`. Machine parameters and nominal resolution never answer it."""
    if profile.template:
        return CapabilityResolution("refused", profile.id, characteristic, None, (f"{profile.id} is a template, not a capability",))
    reasons = []
    for calibration in profile.calibrations:
        if calibration.characteristic != characteristic:
            continue
        scope = calibration.scope
        why = []
        if scope.get("material") != material:
            why.append(f"{calibration.id} is for {scope.get('material')}")
        for key, wanted in (("tool", tool), ("orientation", orientation), ("feature", feature)):
            if wanted is not None and scope.get(key) != wanted:
                why.append(f"{calibration.id} {key} is {scope.get(key) or 'not stated'}, {wanted} requested")
        if on is not None and calibration.valid_until and calibration.valid_until < on.isoformat():
            why.append(f"{calibration.id} expired on {calibration.valid_until}")
        if why:
            reasons.extend(why)
            continue
        return CapabilityResolution("calibrated", profile.id, characteristic, calibration, ())
    if not reasons:
        reasons.append(f"{profile.id} has no calibration of {characteristic}")
        return CapabilityResolution("unknown", profile.id, characteristic, None, tuple(reasons))
    return CapabilityResolution("refused", profile.id, characteristic, None, tuple(reasons))


# --- required checks -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Obligation:
    id: str  # family@anchor
    family: str
    subject: Mapping[str, Any]
    level: str
    stage: str
    basis: str
    requirement: str | None = None
    criterion: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The schema form (manufacturing.schema.json#/$defs/obligation)."""
        data: dict[str, Any] = {"id": self.id, "family": self.family, "subject": thaw(self.subject), "level": self.level,
                                "stage": self.stage, "basis": self.basis}
        if self.requirement:
            data["requirement"] = self.requirement
        if self.criterion:
            data["criterion"] = self.criterion
        return data


# Per route kind: (check family, evidence level, stage due). An unknown kind gets process.<kind>.coverage only.
PROCESS_CHECKS: Mapping[str, tuple[tuple[str, str, str], ...]] = types.MappingProxyType({
    "fdm": (("orientation", "process_planned", "detail"), ("geometry", "geometrically_manufacturable", "parts"),
            ("support", "process_planned", "detail"), ("thermal_distortion", "geometrically_manufacturable", "detail"),
            ("trial", "physically_validated", "release")),
    "resin": (("orientation", "process_planned", "detail"), ("geometry", "geometrically_manufacturable", "parts"),
              ("support", "process_planned", "detail"), ("drainage", "geometrically_manufacturable", "detail"),
              ("post_cure", "process_planned", "detail"), ("trial", "physically_validated", "release")),
    "cnc": (("stock", "process_planned", "parts"), ("setup", "process_planned", "detail"),
            ("access", "geometrically_manufacturable", "detail"), ("features", "geometrically_manufacturable", "parts"),
            ("precision", "process_planned", "detail")),
    "laser": (("material", "process_planned", "parts"), ("kerf", "geometrically_manufacturable", "detail"),
              ("features", "geometrically_manufacturable", "parts"), ("thermal", "process_planned", "detail"),
              ("nesting", "process_planned", "detail"), ("fit_coupon", "physically_validated", "release")),
    "waterjet": (("material", "process_planned", "parts"), ("kerf", "geometrically_manufacturable", "detail"),
                 ("features", "geometrically_manufacturable", "parts"), ("edge", "process_planned", "detail")),
    "plasma": (("material", "process_planned", "parts"), ("kerf", "geometrically_manufacturable", "detail"),
               ("features", "geometrically_manufacturable", "parts"), ("thermal", "process_planned", "detail")),
    "sheet_forming": (("bend", "geometrically_manufacturable", "detail"), ("sequence", "process_planned", "detail"),
                      ("distortion", "process_planned", "detail")),
    "casting": (("draft", "geometrically_manufacturable", "detail"), ("walls", "geometrically_manufacturable", "parts"),
                ("tooling", "process_planned", "detail"), ("defects", "process_planned", "detail")),
    "moulding": (("draft", "geometrically_manufacturable", "detail"), ("walls", "geometrically_manufacturable", "parts"),
                 ("tooling", "process_planned", "detail"), ("defects", "process_planned", "detail")),
    "extrusion": (("profile", "geometrically_manufacturable", "parts"), ("secondary", "process_planned", "detail")),
    "drilling": (("access", "geometrically_manufacturable", "detail"), ("feature", "geometrically_manufacturable", "detail")),
    "reaming": (("access", "geometrically_manufacturable", "detail"), ("allowance", "process_planned", "detail")),
    "tapping": (("access", "geometrically_manufacturable", "detail"), ("engagement", "geometrically_manufacturable", "detail")),
    "finishing": (("allowance", "process_planned", "detail"), ("access", "geometrically_manufacturable", "detail")),
    "coating": (("allowance", "process_planned", "detail"), ("access", "process_planned", "detail")),
    "joining": (("access", "geometrically_manufacturable", "detail"), ("distortion", "process_planned", "detail")),
    "inspection": (("access", "geometrically_manufacturable", "detail"), ("capability", "process_planned", "detail")),
    "purchased": (("identity", "process_planned", "parts"), ("rating", "process_planned", "parts"),
                  ("dimensions", "geometrically_manufacturable", "parts"), ("mounting", "process_planned", "detail"),
                  ("availability", "process_planned", "parts")),
})
_LEVEL_STAGE = {"cad_valid": "layout", "geometrically_manufacturable": "parts", "process_planned": "detail",
                "simulation_supported": "analysis", "physically_validated": "release"}
# The assembly operation kinds that make a joint, each with its assembly.joint evidence obligation
# (cad-record assembly_operations.JOINING is the same set).
JOINING_KINDS = frozenset(("press_fit", "heat_set", "retaining_ring", "insert", "fasten", "join"))


def obligation_anchor(subject: Mapping[str, Any]) -> str:
    """The anchor of an obligation id for `subject`: product, PN[.component][/operation][#feature], IF[#feature],
    REQ or op:<assembly operation>."""
    if subject.get("PN"):
        anchor = f"{subject['PN']}"
        if subject.get("component"):
            anchor += f".{subject['component']}"
        if subject.get("operation"):
            anchor += f"/{subject['operation']}"
        if subject.get("feature"):
            anchor += f"#{subject['feature']}"
        return anchor
    if subject.get("interface"):
        return f"{subject['interface']}" + (f"#{subject['feature']}" if subject.get("feature") else "")
    if subject.get("assembly_operation"):
        return f"op:{subject['assembly_operation']}"
    if subject.get("requirements"):
        return f"{subject['requirements'][0]}"
    return "product"


def required_checks(record: Record) -> tuple[Obligation, ...]:
    """Every check obligation the record implies, enumerated before any producer runs, sorted by id:
    each made component x route operation x its kind's check families; purchase checks for bought items;
    function-driven engineering checks; critical interface fit, stack, acceptance, contact and play (play for
    every critical interface that declares it or rotates or slides); assembly order and access, and joint
    evidence for every joining operation; requirement evidence levels; and an intent.* obligation for every unknown
    input that hides checks. A producer cannot shrink this list by omission; an unknown route kind gets
    process.<kind>.coverage, which no handler can pass."""
    found: dict[str, Obligation] = {}

    def add(family: str, subject: Mapping[str, Any], level: str, stage: str, basis: str, *,
            requirement: str | None = None, criterion: str | None = None) -> None:
        clean = {key: value for key, value in subject.items() if value is not None}
        ident = f"{family}@{obligation_anchor(clean)}"
        if ident not in found:
            found[ident] = Obligation(ident, family, types.MappingProxyType(clean), level, stage, basis, requirement, criterion)

    use_case = record.use_case
    add("intent.use_case", {"product": True}, "process_planned", "intent",
        "a final material and process recommendation needs a confirmed use case")
    for pn, part in sorted(record.parts.items()):
        _part_checks(record, pn, part, add)
    for if_id, interface in sorted(record.interfaces.items()):
        _interface_checks(if_id, interface, add)
    operations = record.assembly_operations
    if is_fact(operations):
        if not operations.not_applicable:
            add("intent.assembly_operations", {"product": True}, "process_planned", "detail",
                f"the assembly operations are {operations.status}: installation order and access stay unchecked")
    else:
        for operation in operations:
            ident = operation.get("id")
            if isinstance(ident, str):
                add("assembly.order", {"assembly_operation": ident}, "process_planned", "detail",
                    f"assembly operation {ident}: installation order and closure")
                add("assembly.access", {"assembly_operation": ident}, "geometrically_manufacturable", "detail",
                    f"assembly operation {ident}: part path and tool reach")
                if operation.get("kind") in JOINING_KINDS:
                    add("assembly.joint", {"assembly_operation": ident}, "process_planned", "detail",
                        f"assembly operation {ident}: engagement, edge distance, retention, torque and host suitability")
    for req_id, requirement in sorted(record.requirements.items()):
        for level in requirement.evidence_required:
            add(f"requirement.{level}", {"product": True, "requirements": (req_id,)}, level, _LEVEL_STAGE[level],
                f"{req_id} evidence_required {level}", requirement=req_id)
    if not is_fact(use_case.evidence_policy):
        for item in use_case.evidence_policy:
            policy_level = item.get("level")
            subject = item.get("subject") or (
                {"product": True, "requirements": (item["requirement"],)} if item.get("requirement") else None)
            if subject is not None and policy_level in _LEVEL_STAGE:
                add(f"policy.{policy_level}", subject, policy_level, _LEVEL_STAGE[policy_level], "the approved evidence policy",
                    requirement=item.get("requirement"))
    return tuple(found[key] for key in sorted(found))


def _part_checks(record: Record, pn: str, part: Part, add: Any) -> None:
    intent = part.intent
    made = part.made
    base = {"PN": pn}
    if is_fact(intent.functions):
        add("intent.functions", base, "process_planned", "parts", f"{pn} functions are {intent.functions.status}")
    if made and is_fact(intent.material_profile) and not intent.material_profile.not_applicable:
        add("intent.material", base, "process_planned", "parts", f"{pn} material profile is {intent.material_profile.status}")
    if made and is_fact(intent.critical_surfaces) and intent.critical_surfaces.unknown:
        add("intent.critical_surfaces", base, "process_planned", "detail", f"{pn} critical surfaces are unknown")
    route = intent.route
    if is_fact(route):
        add("intent.route", base, "process_planned", "parts", f"{pn} route is {route.status}: its process checks are unknown")
    components = tuple(c.id for c in intent.components) or (None,)
    if made:
        for component in components:
            add("cad.readback", {**base, "component": component}, "cad_valid", "layout",
                f"{pn}: B-rep validity, readback, manufacturing state and component identity")
            add("route.consistency", {**base, "component": component}, "process_planned", "parts",
                f"{pn}: route, profile, material and capability consistency")
    if not is_fact(route):
        for op in route:
            targets = [c for c in components if c is None or not op.components or c in op.components]
            kind = op.kind
            checks = PROCESS_CHECKS.get(kind) if kind is not None and op.known_kind else None
            for component in targets if kind != "purchased" else (None,):
                subject = {**base, "component": component, "operation": op.id}
                if checks is None:
                    add(f"process.{kind}.coverage", subject, "process_planned", "parts",
                        f"{pn} {op.id}: route kind {kind} has no check contract")
                    continue
                family_prefix = "purchase" if kind == "purchased" else f"process.{kind}"
                for family, level, stage in checks:
                    add(f"{family_prefix}.{family}", subject, level, stage, f"{pn} {op.id}: {kind} operation")
                if op.kind != "purchased":
                    add("route.operation_plan", subject, "process_planned", "detail",
                        f"{pn} {op.id}: setup, orientation, supports, allowances and inspection")
    functions = () if is_fact(intent.functions) else tuple(f.function for f in intent.functions)
    functions += tuple(f.function for c in intent.components for f in c.functions)
    if "optical" in functions or part.transparency in ("translucent", "transparent"):
        add("optical.route", base, "process_planned", "parts",
            f"{pn} has an optical requirement: only a purchased transparent part or identified transparent stock on a suitable "
            "route qualifies; opaque processes are eliminated")
        add("optical.transmission", base, "physically_validated", "release", f"{pn}: sourced optical grade and transmission")
    if not made:
        return  # a catalogue item's function is its rating: the purchase checks cover it, never made-part checks
    loads = _part_loads(record, part)
    if "structural" in functions:
        add("engineering.strength", base, "simulation_supported", "analysis", f"{pn} is structural")
        add("engineering.stiffness", base, "simulation_supported", "analysis", f"{pn} is structural")
        if loads is None:
            add("engineering.load_cases", base, "simulation_supported", "analysis",
                f"{pn} is structural and its loads are unknown: creep, fatigue and impact applicability is unknown")
        else:
            kinds = {load.kind for load in loads}
            if "sustained" in kinds:
                add("engineering.creep", base, "physically_validated", "analysis", f"{pn} carries a sustained load")
            if kinds & {"cyclic", "vibration"}:
                add("engineering.fatigue", base, "physically_validated", "analysis", f"{pn} carries a cyclic load")
            if "impact" in kinds:
                add("engineering.impact", base, "physically_validated", "analysis", f"{pn} carries an impact load")
        if not is_fact(route) and any(op.kind in ADDITIVE_KINDS for op in route):
            add("engineering.anisotropy", base, "simulation_supported", "analysis",
                f"{pn} is structural and additive: layer direction against its loads")
    surfaces = () if is_fact(intent.critical_surfaces) else intent.critical_surfaces
    for surface in surfaces:
        subject = {**base, "component": surface.component, "feature": surface.feature}
        add("feature.fit", subject, "process_planned", "detail", f"{pn} {surface.id}: {surface.role} finished fit")
        add("feature.inspection", subject, "physically_validated", "release", f"{pn} {surface.id}: inspected finished dimensions")
    for function, family, level in _FUNCTION_CHECKS:
        if function in functions:
            add(family, base, level, "detail" if level == "process_planned" else "analysis", f"{pn} function {function}")


_FUNCTION_CHECKS = (
    ("rotating", "contact.wear", "physically_validated"), ("sliding", "contact.wear", "physically_validated"),
    ("rotating", "motion.play", "simulation_supported"), ("sliding", "motion.play", "simulation_supported"),
    ("locating", "feature.datum", "process_planned"), ("sealing", "function.sealing", "physically_validated"),
    ("spring", "function.spring", "physically_validated"), ("thermal", "function.thermal", "physically_validated"),
    ("electrical", "function.electrical", "physically_validated"), ("other", "function.other", "physically_validated"),
)


def _part_loads(record: Record, part: Part) -> tuple[Load, ...] | None:
    """The use-case loads `part` carries, or None when they are unknown."""
    service = part.intent.service
    loads = record.use_case.loads
    if is_fact(service) or is_fact(loads):
        return None
    wanted = service.get("loads")
    if not wanted:
        return None
    return tuple(load for load in loads if load.id in wanted)


def _interface_checks(if_id: str, interface: Interface, add: Any) -> None:
    functional = interface.functional
    base = {"interface": if_id}
    critical = functional.critical
    if not critical.known:
        if not critical.not_applicable:
            add("intent.interface", base, "process_planned", "parts", f"{if_id} criticality is unknown")
        return
    if critical.value is not True:
        return
    raw = functional.raw
    add("interface.fit", base, "process_planned", "detail", f"{if_id} is critical: intended fit after tolerances")
    add("interface.stack", base, "process_planned", "detail",
        f"{if_id} is critical: worst-case stack with manufacturing, assembly, thermal, moisture, wear and deformation contributors")
    for index, acceptance in enumerate(raw.get("acceptance") or ()):
        criterion = acceptance.get("criterion") or {}
        name = criterion.get("id") or f"criterion_{index + 1}"
        for level in (acceptance.get("verification") or {}).get("evidence_required") or ():
            add(f"interface.acceptance.{level}", {**base, "feature": name}, level, _LEVEL_STAGE.get(level, "detail"),
                f"{if_id} acceptance {name}", criterion=name)
    if raw.get("contact"):
        add("contact.compatibility", base, "physically_validated", "analysis", f"{if_id}: contacting pair friction, wear and life")
    functions = set() if is_fact(functional.functions) else {entry.function for entry in functional.functions}
    if raw.get("play") or functions & {"rotating", "sliding"}:
        add("motion.play", base, "simulation_supported", "analysis", f"{if_id}: function under play and tolerance extremes")


# --- source artifacts and blank records --------------------------------------------------------------------------

def source_artifacts(record: Record) -> Mapping[str, str]:
    """Every source artifact (relative to hw/) the record's facts name, with the sha256 they name."""
    return record.artifacts


def profile_document(data: Mapping[str, Any], kind: str, path: str) -> tuple[tuple[Any, ...], tuple[Problem, ...]]:
    """The profiles of a standalone capability (`kind` "capabilities") or material ("materials") document,
    such as the plugin's capability templates or starter material profiles, and their problems beside the
    schema: vocabularies, units, numbers, levels, forms and allowables. References to project records are
    not resolved; a project resolves them when it copies a profile in."""
    if kind not in ("capabilities", "materials"):
        raise IntentError(f"a profile document is capabilities or materials, not {kind!r}")
    reader = _Reader({}, None)
    reader.numbers(data, path, "")
    profiles = []
    for index, item in enumerate(data.get("profiles") or () if isinstance(data, Mapping) else ()):
        if not isinstance(item, Mapping):
            continue
        pointer = f"profiles/{index}"
        reader.define(kind, item.get("id"), path, pointer)
        build = reader.capability_profile if kind == "capabilities" else reader.material_profile
        profile = build(item, path, pointer, register=False)
        if profile is not None:
            profiles.append(profile)
    return tuple(profiles), tuple(reader.problems)


def blank_use_case(reason: str, questions: Mapping[str, str]) -> dict[str, Any]:
    """A use_case block whose every field is unknown for `reason`, each naming the question id
    `questions` maps its USE_CASE_QUESTIONS path to. Used by migrate.py, the template and fixtures."""
    def unknown(path: str) -> dict[str, Any]:
        item = {"status": "unknown", "reason": reason}
        if questions.get(path):
            item["question"] = questions[path]
        return item

    block: dict[str, Any] = {"status": "unknown", "service_class": unknown("service_class")}
    for dotted in ("purpose", "users"):
        block[dotted] = unknown(dotted)
    for dotted in USE_CASE_FACTS[2:]:
        group, name = dotted.split(".")
        block.setdefault(group, {})[name] = unknown(dotted)
        if dotted == "environment.corrosion":
            block["loads"] = unknown("loads")
    capability = block.pop("capability")
    block["capability"] = {"equipment": unknown("capability.equipment"), **capability}
    release = block.pop("release_scope")
    block["release_scope"] = release
    block["evidence_policy"] = unknown("evidence_policy")
    order = ["status", "service_class", "purpose", "users", "environment", "loads", "motion", "accuracy", "consequences",
             "logistics", "capability", "release_scope", "evidence_policy"]
    return {key: block[key] for key in order}


def use_case_questions(first_number: int, owner: str = "lead") -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The USE_CASE_QUESTIONS as open_questions entries numbered from Q-<first_number>, and the map of each
    use-case path to its question id, for blank_use_case."""
    entries, paths = [], {}
    for offset, (text, covered, material, safety) in enumerate(USE_CASE_QUESTIONS):
        ident = f"Q-{first_number + offset:03d}"
        entries.append({"id": ident, "question": text, "subjects": [{"product": True}], "owner": owner,
                        "material_to_choice": material, "safety_relevant": safety, "status": "open"})
        for path in covered:
            paths[path] = ident
    return entries, paths
