"""Material conditions and evidence of a static FEA case: where E, nu, density and yield come from, and
what a completed solve can support.

A versioned case of a record version 2 project takes its material only from the analysed part's material
profile (cad-record `manufacturing_intent.resolve_property`), under the conditions the case states: the
conditioning, the temperature and, for orientation-dependent material, the orientation. Before any solve it
refuses numeric case values (copied, unsourced properties), a part whose material is only a name, a family
or density-only profile, a profile whose form or process contradicts the part's route, a property tested
under other conditions, and orientation-dependent material without an approved effective isotropic model:
the solver is static, linear and isotropic. Nothing is derived from density or inherited from another
case, part or profile.

Explicit numbers stay readable in a standalone case and in a record version 1 version: they are labelled
unsourced and the result is exploratory, never service evidence. A completed sourced solve is
simulation-supported evidence at most, never physical validation; `engineering.*` outcomes pass only with a
sourced acceptance criterion, service-grade properties, covered service loads and demonstrated mesh
convergence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

INTENT_PATH = Path(__file__).resolve().parents[2] / "cad-record" / "scripts" / "manufacturing_intent.py"
PRODUCER = "cad-analysis fea_static.py"
EXPLICIT_KEYS = ("E", "nu", "density", "yield")
PROFILE_KEYS = ("profile", "conditions", "effective_isotropic")
CONDITIONINGS = ("dry", "conditioned", "as_received")
# solver input -> (material profile property, the units the solver takes)
PROPERTIES = {"E": ("youngs_modulus", "MPa"), "nu": ("poisson_ratio", "dimensionless"),
              "density": ("density", "kg_m3"), "yield": ("yield_strength", "MPa")}
# A yield allowable (not a typical value) is required for these service classes.
ALLOWABLE_SERVICE = frozenset({"production", "safety_critical"})
# Bases that may support service evidence; estimates, assumptions and bare user statements may not.
SERVICE_BASES = frozenset({"measured", "supplier_typical", "allowable", "standard"})
# The required checks a static isotropic solve can answer, all at simulation_supported.
ENGINEERING_FAMILIES = ("engineering.strength", "engineering.stiffness", "engineering.anisotropy")
# Route kinds that make the material state itself, and the profile form their properties have. A subtractive or
# cutting route keeps its stock's state, so the stock profile's own process lies upstream of the part's route.
FORMING = {"fdm": "printed", "resin": "printed", "casting": "cast", "moulding": "moulded"}
LEVEL = "simulation_supported"
DECISION = re.compile(r"DR-[0-9]{3}")
REQUIREMENT = re.compile(r"REQ-[0-9]{3}")
METHOD = "static linear elastic isotropic FEA (FreeCAD FEM, Gmsh second-order tetrahedra, CalculiX)"
LIMITS = (
    "static, linear elastic, small displacement and isotropic: no contact, plasticity, buckling, dynamics or thermal load",
    "face loads only: no self-weight or inertia load",
    "peak von Mises stress at load faces, constraints and sharp re-entrant corners depends on the mesh",
    "a yield safety factor is not a fatigue, creep, wear, impact, buckling or service-life margin",
    "simulation-supported evidence at most, never physical validation",
)


class Refused(ValueError):
    """The case cannot be solved as stated: every problem is a {code, message} row."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__("; ".join(problem["message"] for problem in problems))
        self.problems = problems


def intent() -> Any:
    """cad-record manufacturing_intent.py under the name every CAD skill shares."""
    name = "cad_record_manufacturing_intent"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, INTENT_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"{INTENT_PATH} not found")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def material_form(material: Mapping[str, Any]) -> str:
    """`explicit` for legacy numbers, `profile` for the conditions of the part's material profile."""
    return "explicit" if any(key in material for key in EXPLICIT_KEYS) else "profile"


@dataclass
class Assessment:
    """What a case's material and evidence rest on, settled before the solve."""

    material: dict[str, Any]  # the report block: source, profile, conditions, properties
    evidence_gaps: list[str]  # why a completed solve cannot be service evidence; empty when it can
    version: str | None = None
    subject: dict[str, Any] | None = None
    binding: dict[str, Any] | None = None
    expected: list[dict[str, Any]] = field(default_factory=list)  # required_checks rows this case answers
    criteria: dict[str, dict[str, Any]] = field(default_factory=dict)  # strength/stiffness -> criterion row
    load_gaps: list[str] = field(default_factory=list)  # why the case does not cover the part's service loads
    convergence: dict[str, Any] = field(default_factory=dict)
    effective_isotropic: dict[str, Any] | None = None
    dependencies: dict[str, str] = field(default_factory=dict)  # semantic name -> sha256

    @property
    def sourced(self) -> bool:
        return self.material.get("source") == "profile"

    def recipe(self) -> dict[str, Any]:
        """The recipe fields that bind a published bundle to this material, criterion and configuration."""
        return {"material": self.material, "binding": self.binding, "acceptance": self.criteria,
                "mesh_convergence": self.convergence, "dependencies": self.dependencies}


def explicit(case: Mapping[str, Any], why: str) -> Assessment:
    """Legacy explicit numbers: readable, unsourced, exploratory, never service evidence."""
    material = case["material"]
    report = {"source": "unsourced_explicit", "name": material.get("name"),
              "values": {key: material[key] for key in EXPLICIT_KEYS},
              "note": f"{why}: explicit case numbers are not a material source; the result is exploratory and never "
                      "service evidence"}
    return Assessment(report, [report["note"]])


def read_record(record_dir: Path) -> tuple[Any, dict[str, Any]]:
    """The normalized record of a version's record snapshot and its parsed documents."""
    mi = intent()
    documents, problems = mi.read_documents(record_dir)
    if problems:
        raise Refused([{"code": "record.unreadable", "message": problem.message} for problem in problems])
    return mi.normalize_record(documents), documents


def assess_version(case: dict[str, Any], *, record_dir: Path, hw: Path, version: str) -> Assessment:
    """The material and evidence basis of a versioned case; Refused before the solve when it has none."""
    record, documents = read_record(record_dir)
    form = case["_material_form"]
    if record.version == 1:
        if form != "explicit":
            raise Refused([{"code": "material.legacy_record",
                            "message": f"version {version} is a record version 1 snapshot without material profiles; "
                                       "migrate the record and build a new version to analyse a profile's material"}])
        assessment = explicit(case, f"version {version} predates material profiles (record version 1)")
        assessment.version = version
        return assessment
    if record.version != 2:
        raise Refused([{"code": "record.version_unsupported",
                        "message": f"version {version}: the record snapshot's record_version is not supported"}])
    if form == "explicit":
        raise Refused([{"code": "material.inherited",
                        "message": "a record version 2 case takes E, nu, density and yield only from the part's material "
                                   "profile: numeric case values are copied, unsourced properties; replace them with "
                                   "material.conditions"}])
    return _Assessor(record, documents, case, record_dir, Path(hw), version).run()


class _Assessor:
    """One versioned record version 2 case: every refusal is collected, then raised together."""

    def __init__(self, record: Any, documents: Mapping[str, Any], case: dict[str, Any], record_dir: Path, hw: Path,
                 version: str) -> None:
        self.mi = intent()
        self.record, self.documents, self.case = record, documents, case
        self.record_dir, self.hw, self.version = record_dir, hw, version
        self.problems: list[dict[str, str]] = []
        self.gaps: list[str] = []
        self.dependencies: dict[str, str] = {}

    def refuse(self, code: str, message: str) -> None:
        self.problems.append({"code": code, "message": message})

    def check(self) -> None:
        if self.problems:
            raise Refused(self.problems)

    def run(self) -> Assessment:
        subject = self.subject()
        self.check()
        pn = subject["PN"]
        part = self.record.parts.get(pn)
        if part is None:
            self.refuse("material.subject", f"version {self.version}: the record snapshot has no part {pn}")
            self.check()
        self.depend("part_record", self.record_dir / "parts" / pn / "part.yaml")
        for name, rel in (("product_record", "product.yaml"), ("requirements_record", "requirements.yaml"),
                          ("assembly_record", "assembly.yaml"), ("materials_record", "manufacturing/materials.yaml")):
            self.depend(name, self.record_dir / rel)
        profile = self.profile(part)
        self.check()
        route = self.route(part, profile)
        conditions = self.conditions(profile)
        self.check()
        anisotropy = self.anisotropy(profile, conditions)
        allowable = self.allowable_required()
        properties, values = self.properties(profile, conditions, allowable)
        self.check()
        self.service_conditions(conditions)
        criteria = self.criteria()
        load_gaps = self.load_coverage(part)
        convergence = self.convergence()
        self.check()
        profile_sha = _digest(self.mi.thaw(profile.raw))
        self.dependencies["material_profile"] = profile_sha
        material = {
            "source": "profile", "name": profile.raw.get("title") or profile.id,
            "profile": {"id": profile.id, "revision": profile.revision, "sha256": profile_sha, "level": profile.level,
                        "family": profile.family, "grade": profile.grade.value if profile.grade.known else None,
                        "form": profile.form, "process": route["profile_process"],
                        "typical_not_allowable": profile.typical_not_allowable},
            "conditions": {"conditioning": conditions["conditioning"], "temperature": conditions["temperature"].as_dict(),
                           "orientation": conditions["orientation"]},
            "allowable_required": allowable,
            "isotropy": anisotropy,
            "properties": properties,
        }
        self.case["material"] = {"name": material["name"], **values}
        binding = {"version": self.version, "record_version": 2, **subject, "part_revision": part.raw.get("rev"),
                   "material_profile": {"id": profile.id, "revision": profile.revision, "sha256": profile_sha},
                   "route": route["kinds"], "conditions": material["conditions"]}
        expected = [ob.as_dict() for ob in self.mi.required_checks(self.record)
                    if ob.family in ENGINEERING_FAMILIES and ob.subject.get("PN") == pn]
        return Assessment(material, self.gaps, self.version, subject, binding, expected, criteria, load_gaps, convergence,
                          anisotropy if anisotropy["model"] != "isotropic" else None, self.dependencies)

    # -- inputs ----------------------------------------------------------------------------------------------

    def depend(self, name: str, path: Path) -> str | None:
        if not path.is_file():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.dependencies[name] = digest
        return digest

    def decision(self, ref: str, name: str, code: str, what: str) -> None:
        """An approved decision record under hw/decisions/, bound by its sha256."""
        if self.depend(name, self.hw / "decisions" / f"{ref}.md") is None:
            self.refuse(code, f"{what} names {ref}, but hw/decisions/{ref}.md does not exist")

    def subject(self) -> dict[str, Any]:
        """The analysed PN, from `part` or from `occurrence` through the version's assembly.yaml."""
        if self.case.get("part"):
            return {"PN": self.case["part"]}
        occurrence = self.case.get("occurrence")
        if occurrence:
            assembly = self.documents.get("assembly.yaml")
            occurrences = assembly.get("occurrences") if isinstance(assembly, Mapping) else None
            placed = {item.get("id"): item.get("PN") for item in occurrences or () if isinstance(item, Mapping)}
            pn = placed.get(occurrence[1:])
            if isinstance(pn, str):
                return {"PN": pn, "occurrence": occurrence}
            self.refuse("material.subject", f"occurrence {occurrence} is not an occurrence of version {self.version}'s "
                                            "assembly.yaml, so its part and material profile are unknown")
            return {}
        self.refuse("material.subject", "a record version 2 case names its `part` or `occurrence`: the material profile "
                                        "is the analysed part's, never chosen by solid index or STEP content")
        return {}

    def profile(self, part: Any) -> Any:
        """The part's material profile: never its material name, another part's or a copied case's profile."""
        pn, reference = part.pn, part.intent.material_profile
        asserted = self.case["material"].get("profile")
        if self.mi.is_fact(reference):
            why = reference.reason or reference.status
            self.refuse("material.name_only",
                        f"{pn}'s material profile is {reference.status} ({why}); its material name {part.material!r} is "
                        "never a property source")
            return None
        if asserted is not None and asserted != reference:
            self.refuse("material.inherited",
                        f"the case names profile {asserted}, but {pn}'s intent names {reference}: a case copied from "
                        "another part or profile never supplies this part's properties")
        others = sorted({c.material_profile for c in part.intent.components
                         if isinstance(c.material_profile, str) and c.material_profile != reference})
        if others:
            self.refuse("material.component",
                        f"{pn}'s components use profiles {', '.join(others)} besides {reference}: one isotropic material "
                        "per solve cannot represent them")
        profile = self.record.materials.get(reference)
        if profile is None:
            self.refuse("material.name_only", f"{pn}'s profile {reference} is not in the version's "
                                              "manufacturing/materials.yaml")
            return None
        if profile.level != "grade":
            self.refuse("material.grade", f"{reference} is {profile.level} guidance, never a grade specification: "
                                          "name the grade profile the part is made of")
        broken = [p.message for p in self.record.problems
                  if p.path == "hw/manufacturing/materials.yaml" and p.message.startswith((f"{reference}:", f"{reference} "))]
        for message in broken:
            self.refuse("material.profile", message)
        return profile

    def route(self, part: Any, profile: Any) -> dict[str, Any]:
        """The profile's form and process against the part's route: printed, cast or moulded properties only for
        a part its route prints, casts or moulds by that process, and never bulk values for such a part."""
        state = profile.raw.get("process_state") or {}
        made_by = state.get("process") if isinstance(state.get("process"), str) else None
        if profile.form == "printed" and made_by is None:
            self.refuse("material.form_process", f"{profile.id} is printed material whose process is unknown: printed "
                                                 "properties depend on the process that made them")
        route = part.intent.route
        if self.mi.is_fact(route):
            self.gaps.append(f"{part.pn}'s route is {route.status}: the material state the profile describes is unverified")
            return {"kinds": None, "profile_process": made_by}
        kinds = [op.kind for op in route]
        forming = [kind for kind in kinds if kind in FORMING]
        for kind in forming:
            if profile.form != FORMING[kind]:
                self.refuse("material.form_process",
                            f"{part.pn} is made by {kind}, but {profile.id} holds {profile.form} properties: only "
                            f"{FORMING[kind]} values describe that material")
            elif made_by is not None and made_by != kind:
                self.refuse("material.form_process", f"{profile.id} was tested as {made_by} material, but {part.pn} is made "
                                                     f"by {kind}")
        if profile.form in FORMING.values() and not any(FORMING[kind] == profile.form for kind in forming):
            self.refuse("material.form_process", f"{profile.id} holds {profile.form} properties, but {part.pn}'s route "
                                                 f"({', '.join(kinds) or 'empty'}) makes no {profile.form} material")
        return {"kinds": kinds, "profile_process": made_by}

    def conditions(self, profile: Any) -> dict[str, Any]:
        stated = self.case["material"]["conditions"]
        try:
            temperature = self.mi.quantity(stated["temperature"])
        except (self.mi.IntentError, TypeError, AttributeError) as exc:
            self.refuse("material.condition", f"material.conditions.temperature: {exc}")
            return {}
        if temperature.dimension != "temperature":
            self.refuse("material.condition", f"material.conditions.temperature is {temperature}, not a temperature")
        orientation = stated.get("orientation")
        oriented = profile.form == "printed" or any(p.orientation is not None and p.orientation.known
                                                    for p in profile.properties if p.name in _PROFILE_NAMES)
        if oriented and orientation is None:
            self.refuse("material.condition", f"{profile.id} has orientation-dependent properties: state "
                                              "material.conditions.orientation, the specimen direction the analysis takes")
        return {"conditioning": stated["conditioning"], "temperature": temperature, "orientation": orientation}

    def anisotropy(self, profile: Any, conditions: Mapping[str, Any]) -> dict[str, Any]:
        """Isotropic, or an approved effective isotropic model of orientation-dependent material at one orientation."""
        orientations = {str(p.orientation.value).strip().lower() for p in profile.properties
                        if p.name in _PROFILE_NAMES and p.orientation is not None and p.orientation.known}
        layup = (profile.raw.get("process_state") or {}).get("layup")
        reasons = ([f"{profile.id} is printed"] if profile.form == "printed" else []) + \
                  ([f"{profile.id} has a layup ({layup})"] if layup else []) + \
                  ([f"{profile.id} states properties at {len(orientations)} orientations"] if len(orientations) > 1 else [])
        if not reasons:
            return {"model": "isotropic", "reasons": []}
        model = self.case["material"].get("effective_isotropic")
        if model is None:
            self.refuse("material.anisotropy",
                        f"{'; '.join(reasons)}: the static solver is isotropic, so orientation-dependent material needs "
                        "material.effective_isotropic with the approved decision justifying it")
            return {"model": "unsupported", "reasons": reasons}
        self.decision(model["decision"], f"decision_{model['decision'].lower().replace('-', '_')}", "material.anisotropy",
                      "material.effective_isotropic")
        return {"model": "effective_isotropic", "reasons": reasons, "orientation": conditions["orientation"],
                "decision": model["decision"], "justification": model["justification"]}

    def allowable_required(self) -> bool:
        service = self.record.use_case.service_class
        if not service.known or service.value == "unknown":
            self.gaps.append("the use case's service class is unknown, so whether a yield allowable is required is unknown")
            return False
        return service.value in ALLOWABLE_SERVICE

    def properties(self, profile: Any, conditions: Mapping[str, Any], allowable: bool) -> tuple[list[dict[str, Any]], dict[str, float]]:
        rows, values = [], {}
        for key, (name, units) in PROPERTIES.items():
            needs_allowable = allowable and key == "yield"
            found = self.mi.resolve_property(profile, name, orientation=conditions["orientation"],
                                             conditioning=conditions["conditioning"], temperature=conditions["temperature"],
                                             allowable=needs_allowable)
            row: dict[str, Any] = {"input": key, "property": name, "status": found.status, "reasons": list(found.reasons),
                                   "allowable_requested": needs_allowable}
            rows.append(row)
            if not found.resolved:
                code = ("material.allowable" if needs_allowable and found.status == "refused"
                        else "material.condition" if found.status == "refused" else "material.property")
                self.refuse(code, f"{profile.id} {name} is {found.status}: {'; '.join(found.reasons) or 'no value'}")
                continue
            lower, upper = found.value.to(units).bounds
            if lower != upper and key != "yield":
                self.refuse("material.property", f"{profile.id} {name} is the interval {found.value}: the solver takes one "
                                                 "value, never a default inside it")
                continue
            used = lower  # an interval yield is taken at its lower bound, the conservative end for a yield margin
            if lower != upper:
                row["bound"] = "lower"
            sources = [_plain(source) for source in found.sources]
            row.update(value=found.value.as_dict(), used=used, units=units, basis=found.basis, sources=sources,
                       conditions={k: v.as_dict() if isinstance(v, self.mi.Quantity) else v
                                   for k, v in (found.conditions or {}).items()})
            values[key] = used
            if key != "density" and found.basis not in SERVICE_BASES:
                self.gaps.append(f"{profile.id} {name} is a {found.basis} value, not service evidence")
            for source in found.sources:
                self.artifact(source, f"{profile.id} {name}")
        names = [row["property"] for row in rows if row["status"] == "resolved"]
        if names == ["density"]:
            self.refuse("material.density_only", f"{profile.id} resolves only a density: density is never stiffness or "
                                                 "strength, and no property is derived from it")
        valid = {"E": lambda v: v > 0, "nu": lambda v: 0 < v < 0.5, "density": lambda v: v > 0, "yield": lambda v: v > 0}
        for key, value in values.items():
            if not valid[key](value):
                self.refuse("material.property", f"{profile.id} {PROPERTIES[key][0]} {value:g} is outside the range a "
                                                 "linear elastic material card accepts")
        return rows, values

    def artifact(self, source: Any, what: str) -> None:
        if not source.artifact:
            return
        path = self.record_dir / source.artifact
        if not path.is_file():
            self.gaps.append(f"{what}: source artifact {source.artifact} is not in the version's record snapshot")
            return
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if source.sha256 and digest != source.sha256:
            self.refuse("material.source_artifact", f"{what}: {source.artifact} does not match its recorded sha256")
        self.dependencies[f"source_{digest[:16]}"] = digest

    def service_conditions(self, conditions: Mapping[str, Any]) -> None:
        service = self.record.use_case.facts.get("environment.temperature")
        analysed = conditions["temperature"]
        if service is None or not service.known or not isinstance(service.value, self.mi.Quantity):
            self.gaps.append("the use case's service temperature is unknown")
        elif not analysed.contains(service.value):
            self.gaps.append(f"the analysed temperature {analysed} does not cover the service temperature {service.value}")

    def criteria(self) -> dict[str, dict[str, Any]]:
        """The sourced acceptance criteria the case names, from requirements.yaml; never a bare number."""
        named = self.case.get("acceptance") or {}
        found = {}
        for check, prop, operator, units in (("strength", "yield_safety_factor", ">=", "dimensionless"),
                                             ("stiffness", "max_displacement", "<=", "mm")):
            ref = named.get(check)
            if ref is None:
                continue
            requirement = self.record.requirements.get(ref)
            if requirement is None:
                self.refuse("analysis.criterion", f"acceptance.{check} names {ref}, which is not in requirements.yaml")
                continue
            matches = [c for c in requirement.acceptance if c.property == prop]
            if len(matches) != 1 or matches[0].operator != operator or matches[0].units is None:
                self.refuse("analysis.criterion", f"acceptance.{check} names {ref}, which needs exactly one sourced "
                                                  f"acceptance criterion {prop} {operator} <value> with units")
                continue
            criterion = matches[0]
            try:
                value = self.mi.convert(float(criterion.value), criterion.units, units)
            except (self.mi.IntentError, TypeError, ValueError) as exc:
                self.refuse("analysis.criterion", f"{ref} {prop}: {exc}")
                continue
            found[check] = {"requirement": ref, "property": prop, "operator": operator, "value": value, "units": units,
                            "source": _plain(criterion.source)}
        if "strength" in found:
            required = self.case.get("required_safety_factor")
            if required is not None and abs(required - found["strength"]["value"]) > 1e-12:
                self.refuse("analysis.criterion", f"required_safety_factor {required:g} contradicts "
                                                  f"{found['strength']['requirement']}'s {found['strength']['value']:g}")
            self.case["required_safety_factor"] = found["strength"]["value"]
        return found

    def load_coverage(self, part: Any) -> list[str]:
        """Why the case's loads do not cover the part's service loads (empty when they do)."""
        named = [load.get("load") for load in self.case["loads"] if load.get("load")]
        loads = self.record.use_case.loads
        known = {} if self.mi.is_fact(loads) else {load.id: load for load in loads}
        for ref in named:
            if ref not in known:
                self.refuse("analysis.load", f"a case load names {ref!r}, which is not a load of the use case")
        service = part.intent.service
        if self.mi.is_fact(service):
            return [f"{part.pn}'s service is {service.status}: the loads it must carry are unknown"]
        wanted = list(service.get("loads") or ())
        if not wanted or self.mi.is_fact(loads):
            return [f"{part.pn}'s service loads are unknown"]
        gaps = []
        for ref in wanted:
            load = known.get(ref)
            magnitude = load.magnitude if load is not None else None
            if magnitude is None or not magnitude.known or not isinstance(magnitude.value, self.mi.Quantity) \
                    or magnitude.value.dimension != "force":
                gaps.append(f"service load {ref}'s force magnitude is unknown")
                continue
            applied = sum(math.sqrt(sum(c * c for c in item["force"])) for item in self.case["loads"] if item.get("load") == ref)
            peak = magnitude.value.to("N").bounds[1]
            if applied < peak * (1 - 1e-9):
                gaps.append(f"the case applies {applied:g} N for service load {ref}, which reaches {peak:g} N")
        return gaps

    def convergence(self) -> dict[str, Any]:
        stated = self.case.get("mesh_convergence")
        if stated is None:
            return {"status": "not_demonstrated",
                    "reason": "one mesh: refine mesh_size until peak stress and displacement settle, then record the study "
                              "in an approved decision named by mesh_convergence.decision"}
        self.decision(stated["decision"], f"decision_{stated['decision'].lower().replace('-', '_')}", "analysis.convergence",
                      "mesh_convergence")
        return {"status": "demonstrated", "decision": stated["decision"]}


_PROFILE_NAMES = frozenset(name for name, _units in PROPERTIES.values())


def _plain(source: Any) -> dict[str, Any]:
    return {key: value for key, value in vars(source).items() if value is not None}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- after the solve -------------------------------------------------------------------------------------------

def report(result: dict[str, Any], assessment: Assessment, case: Mapping[str, Any]) -> dict[str, Any]:
    """Add the material, evidence, mesh and (record version 2) engineering packet to a completed result."""
    convergence = assessment.convergence or {"status": "not_demonstrated", "reason": "one mesh; no convergence study"}
    result["material"] = assessment.material
    result["mesh"] = {"size_mm": case["mesh_size"], "element_order": "second", "nodes": result.get("node_count"),
                      "elements": result.get("element_count"), "convergence": convergence}
    gaps = list(assessment.evidence_gaps)
    result["evidence"] = {
        "level": LEVEL if assessment.sourced else None,
        "exploratory": not assessment.sourced,
        "service_evidence": assessment.sourced and not gaps,
        "physical_validation": False,
        "gaps": gaps,
        "limits": list(LIMITS),
    }
    if assessment.binding is not None:
        result["binding"] = assessment.binding
    if assessment.sourced:
        result["engineering"] = packet(result, assessment)
    return result


def packet(result: Mapping[str, Any], a: Assessment) -> dict[str, Any]:
    """The engineering.* check-result packet (manufacturing-check-result.schema.json) of one solve."""
    if a.subject is None:
        raise ValueError("only a record version 2 case, which names its analysed part, has an engineering packet")
    subject = a.subject
    outcomes, findings = [], []
    for obligation in a.expected:
        outcome, finding = _outcome(obligation, result, a, subject)
        outcomes.append(outcome)
        if finding is not None:
            findings.append(finding)
    states = {row["outcome"] for row in outcomes}
    verdict = ("fail" if "fail" in states else "unknown" if "unknown" in states else
               "pass" if "pass" in states else "not_applicable")
    return {"schema_version": 1, "record_version": 2, "producer": PRODUCER, "version": a.version, "scope": [subject],
            "expected": a.expected, "outcomes": outcomes, "findings": findings, "dependencies": dict(a.dependencies),
            "complete": len(outcomes) == len(a.expected), "result": verdict}


_CHECKS = {
    "engineering.strength": ("strength", "safety_factor_yield", "dimensionless",
                             "the yield safety factor at the peak von Mises stress"),
    "engineering.stiffness": ("stiffness", "max_displacement_mm", "mm", "the peak displacement"),
}


def _outcome(obligation: Mapping[str, Any], result: Mapping[str, Any], a: Assessment,
             subject: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    family = obligation["family"]
    gaps = list(a.evidence_gaps)
    if a.convergence.get("status") != "demonstrated":
        gaps.append("mesh convergence is not demonstrated")
    if family == "engineering.anisotropy":
        model = a.effective_isotropic
        if model is None:
            # Additive routes need an effective model or were refused, so this is an obligation without one.
            return _row(obligation, "unknown", a, subject,
                        ["the case records no effective isotropic model for the orientation-dependent material"],
                        None, None, "the orientation-dependent material model")
        reasons = list(a.evidence_gaps) + a.load_gaps
        if reasons:
            return _row(obligation, "unknown", a, subject, reasons, None, None,
                        f"effective isotropic model {model['decision']}")
        return _row(obligation, "pass", a, subject, [], None, None,
                    f"orientation-specific properties at {model['orientation']} under the effective isotropic model "
                    f"{model['decision']}; interlayer failure is outside a von Mises check")
    check, key, units, what = _CHECKS[family]
    criterion = a.criteria.get(check)
    measured = result.get(key)
    gaps += a.load_gaps
    if criterion is None:
        return _row(obligation, "unknown", a, subject, [f"no sourced acceptance criterion: name the requirement under "
                                                        f"acceptance.{check}"], measured, units, what)
    if not isinstance(measured, (int, float)) or isinstance(measured, bool) or not math.isfinite(measured):
        return _row(obligation, "unknown", a, subject, [f"the solve reported no {what}"], None, units, what, criterion)
    meets = measured >= criterion["value"] if criterion["operator"] == ">=" else measured <= criterion["value"]
    if not meets:
        # A coarser mesh under-reports peak stress, so a miss stands without a convergence study; its basis gaps stay visible.
        return _row(obligation, "fail", a, subject, gaps, measured, units, what, criterion)
    return _row(obligation, "pass" if not gaps else "unknown", a, subject, gaps, measured, units, what, criterion)


def _row(obligation: Mapping[str, Any], outcome: str, a: Assessment, subject: Mapping[str, Any], reasons: list[str],
         measured: Any, units: str | None, what: str,
         criterion: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    family = obligation["family"]
    uncertainty = "; ".join(reasons) if reasons else (
        "linear static idealisation; peak values depend on the mesh within the demonstrated convergence")
    row: dict[str, Any] = {"obligation": obligation["id"], "outcome": outcome, "level": LEVEL, "method": METHOD,
                           "uncertainty": uncertainty,
                           "coverage": f"{what} of one static load case on {subject['PN']}; never fatigue, creep, wear, "
                                       "impact, buckling or life"}
    if criterion is not None:
        row["source"] = criterion["source"]
    if outcome == "pass":
        return row, None
    ident = family.replace(".", "_")
    row["findings"] = [ident]
    finding = {
        "id": ident, "code": f"fea.{family.split('.', 1)[1]}.{'margin' if outcome == 'fail' else 'unverified'}",
        "obligation": obligation["id"], "subject": subject, "severity": "critical" if outcome == "fail" else "major",
        "outcome": outcome,
        "measured": ({"value": measured, "units": units} if isinstance(measured, (int, float)) and units else
                     {"status": "unknown", "reason": uncertainty}),
        "criterion": (_criterion(criterion) if criterion is not None else
                      {"status": "unknown", "reason": "the case names no sourced acceptance criterion"}),
        "method": {"name": METHOD}, "uncertainty": uncertainty,
        "consequence": (f"{subject['PN']} does not meet {criterion['requirement']} under this load case"
                        if outcome == "fail" and criterion else
                        f"{subject['PN']}'s {family.split('.', 1)[1]} is not established by this analysis"),
        "remedy": ("change the geometry, the material grade or the load path, then rerun the analysis"
                   if outcome == "fail" else
                   "close the listed gaps (sourced criterion, service-grade profile values, covered service loads, "
                   "demonstrated mesh convergence), then rerun the analysis"),
        "recheck": {"families": [family], "earliest_stage": "analysis"},
    }
    if criterion is not None:
        finding["requirements"] = [criterion["requirement"]]
    return row, finding


def _criterion(criterion: Mapping[str, Any]) -> dict[str, Any]:
    return {key: criterion[key] for key in ("property", "operator", "value", "units", "source")}
