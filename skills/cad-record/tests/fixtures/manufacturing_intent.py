"""Shared record version 2 fixtures for manufacturing-intent tests in every CAD skill.

Load it by path (it is not a test module) and build explicit synthetic TST records:

    spec = importlib.util.spec_from_file_location("cad_record_mi_fixtures", <skills>/cad-record/tests/fixtures/manufacturing_intent.py)
    fixtures = importlib.util.module_from_spec(spec); sys.modules[spec.name] = fixtures; spec.loader.exec_module(fixtures)
    hw = fixtures.write_record(Path(tmp) / "project" / "hw", fixtures.tst_record())

Every number here is a synthetic test value with a synthetic source; none is material, machine or
supplier data, and no example project's assumptions are copied. Builders return plain dicts, so a test
keeps the inputs it asserts on visible and can edit them before writing.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SKILL = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL / "scripts"
LAMP = SKILL.parent.parent / "examples" / "lamp-arm" / "hw"
SYNTHETIC = "synthetic test fixture value, not material, machine or supplier data"


def _module(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def intent_module() -> Any:
    """cad-record manufacturing_intent.py under its shared name."""
    return _module("cad_record_manufacturing_intent", SCRIPTS / "manufacturing_intent.py")


def migrate_module() -> Any:
    """cad-record migrate.py (needs PyYAML and jsonschema)."""
    return _module("cad_record_migrate", SCRIPTS / "migrate.py")


# --- facts ------------------------------------------------------------------------------------------------------

def source(kind: str = "estimate", reference: str = "TST fixture", **extra: Any) -> dict[str, Any]:
    """A source; an estimate or calculation gets the synthetic reason, a user statement the TST origin, a
    policy the TST owner and policy document unless a decision or document is given."""
    data: dict[str, Any] = {"kind": kind, "reference": reference}
    if kind in ("estimate", "calculation"):
        data["reason"] = SYNTHETIC
    if kind == "user":
        data["origin"] = "TST fixture user"
    if kind == "policy":
        data["origin"] = "TST fixture policy owner"
        if "decision" not in extra and "document" not in extra:
            data["document"] = "TST fixture policy document"
    if kind in ("datasheet", "standard", "calibration", "test", "supplier") and "url" not in extra and "artifact" not in extra:
        data.update(url="https://example.invalid/tst-fixture", accessed="2026-01-01")
    data.update(extra)
    return data


def quantity(value: float | None = None, units: str = "mm", *, lower: float | None = None, upper: float | None = None) -> dict[str, Any]:
    if value is not None:
        return {"value": value, "units": units}
    return {"lower": lower, "upper": upper, "units": units}


def known(value: Any, basis: str = "user_declared", sources: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """A known fact; an estimate gets a synthetic uncertainty unless one is given."""
    data: dict[str, Any] = {"status": "known", "value": value, "sources": sources or [source("user")], "basis": basis}
    if basis == "estimate" and "uncertainty" not in extra:
        data["uncertainty"] = {"note": SYNTHETIC}
    data.update(extra)
    return data


def unknown(reason: str = "not recorded in this fixture", question: str | None = None) -> dict[str, Any]:
    data = {"status": "unknown", "reason": reason}
    if question:
        data["question"] = question
    return data


def not_applicable(reason: str = "not applicable in this fixture") -> dict[str, Any]:
    return {"status": "not_applicable", "reason": reason}


def criterion(prop: str, operator: str, value: Any, units: str | None = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"property": prop, "operator": operator, "value": value, "source": source("user")}
    if units:
        data["units"] = units
    data.update(extra)
    return data


# --- product and statements -------------------------------------------------------------------------------------

def use_case(status: str = "unknown", **overrides: Any) -> dict[str, Any]:
    """A use case whose every field is unknown and tracked by the standard questions Q-001..Q-011
    (open_questions() writes them); `overrides` replace top-level fields, such as loads or service_class."""
    module = intent_module()
    _entries, paths = module.use_case_questions(1)
    block = module.blank_use_case("not recorded in this fixture", paths)
    block["status"] = status
    block.update(copy.deepcopy(overrides))
    return block


def open_questions(extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The standard use-case questions Q-001..Q-011, then `extra`."""
    entries, _paths = intent_module().use_case_questions(1)
    return entries + list(extra or [])


def question(ident: str, text: str = "TST fixture question?", *, material: bool = False, safety: bool = False,
             status: str = "open", **extra: Any) -> dict[str, Any]:
    data = {"id": ident, "question": text, "owner": "lead", "material_to_choice": material, "safety_relevant": safety,
            "status": status}
    data.update(extra)
    return data


def product(use: dict[str, Any] | None = None, stage: str = "parts", **extra: Any) -> dict[str, Any]:
    data = {"record_version": 2, "id": "tst-product", "rigor": "pro", "units": "mm", "regime": "iso",
            "general_tolerance": "ISO 2768-m", "flow": {"stage": stage, "recheck_from": stage},
            "use_case": use if use is not None else use_case()}
    data.update(extra)
    return data


def requirement(ident: str = "REQ-001", text: str = "TST fixture requirement", **extra: Any) -> dict[str, Any]:
    data = {"id": ident, "text": text, "rationale": "TST fixture", "source": "TST fixture", "priority": "must",
            "verification": {"method": "T", "criteria": "TST fixture criteria"}}
    data.update(extra)
    return data


def requirements(items: list[dict[str, Any]] | None = None, questions: list[dict[str, Any]] | None = None,
                 preferences: list[dict[str, Any]] | None = None, assumptions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"requirements": items or [requirement()], "preferences": preferences or [], "assumptions": assumptions or [],
            "open_questions": questions if questions is not None else open_questions()}


# --- parts ------------------------------------------------------------------------------------------------------

def operation(ident: str, kind: str, capability: Any = None, **extra: Any) -> dict[str, Any]:
    """A route operation; a made one gets an unknown capability unless one is given."""
    data: dict[str, Any] = {"id": ident, "kind": kind}
    if kind == "purchased":
        data["purchase"] = extra.pop("purchase", {"identity": known("TST catalogue item 1"),
                                                  "specification": known("TST specification"),
                                                  "ratings": unknown("ratings not recorded in this fixture")})
    else:
        data["capability"] = capability if capability is not None else unknown("no capability in this fixture")
    data.update(extra)
    return data


def intent(functions: Any = None, material_profile: Any = None, route: Any = None, *, service: Any = None,
           critical_surfaces: Any = None, **extra: Any) -> dict[str, Any]:
    """Part intent: unknown fields stay unknown unless given."""
    data = {"functions": functions if functions is not None else unknown(),
            "material_profile": material_profile if material_profile is not None else unknown(),
            "service": service if service is not None else unknown(),
            "critical_surfaces": critical_surfaces if critical_surfaces is not None else unknown(),
            "route": route if route is not None else unknown()}
    data.update(extra)
    return data


def part(pn: str, title: str = "TST part", *, process: str = "fdm", make_buy: str = "make", material: str = "TST material",
         part_intent: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    data = {"PN": pn, "rev": "X1", "title": title, "material": material, "process": process, "finish": "as made",
            "mass_target": None, "make_buy": make_buy, "supplier": None, "intent": part_intent if part_intent is not None else intent()}
    data.update(extra)
    return data


def interface(ident: str, frm: str, to: str, kind: str = "mounting", functional: dict[str, Any] | None = None,
              **extra: Any) -> dict[str, Any]:
    data = {"id": ident, "from": frm, "to": to, "type": kind, "description": "TST fixture interface",
            "functional": functional if functional is not None else {"critical": unknown(), "functions": unknown()}}
    data.update(extra)
    return data


# --- profiles ---------------------------------------------------------------------------------------------------

def material_property(name: str, value: float, units: str, *, basis: str = "estimate", temperature: dict[str, Any] | None = None,
                      conditioning: str = "dry", orientation: Any = None, sources: list[dict[str, Any]] | None = None,
                      **extra: Any) -> dict[str, Any]:
    """A material property; its value cites an estimate (a calculation for an allowable) unless `sources` are given."""
    conditions: dict[str, Any] = {"test_standard": known("TST-STD-1", "standard", [source("standard", "TST-STD-1")]),
                                  "temperature": known(temperature or quantity(23, "deg_C"), "measured", [source("test")]),
                                  "conditioning": conditioning}
    if orientation is not None:
        conditions["orientation"] = orientation
    fact_extra = {"conditions": "TST fixture conditions", "derivation": SYNTHETIC} if basis == "allowable" else {}
    cited = sources or [source("calculation" if basis == "allowable" else "estimate")]
    data = {"name": name, "value": known(quantity(value, units), basis, cited, **fact_extra),
            "conditions": conditions}
    data.update(extra)
    return data


def material_profile(ident: str, *, level: str = "grade", form: str = "bulk", family: str = "TST family",
                     properties: list[dict[str, Any]] | None = None, typical_not_allowable: bool = True, **extra: Any) -> dict[str, Any]:
    data = {"id": ident, "revision": 1, "title": f"{ident} (TST fixture)", "level": level, "family": family,
            "grade": known(f"{ident} grade", "user_declared") if level == "grade" else not_applicable("family guidance only"),
            "form": form, "process_state": {"process": unknown(), "conditioning": "dry"},
            "typical_not_allowable": typical_not_allowable, "properties": properties or []}
    data.update(extra)
    return data


def calibration(ident: str, characteristic: str, material: str, limits: dict[str, Any], **scope: Any) -> dict[str, Any]:
    return {"id": ident, "characteristic": characteristic, "scope": {"material": material, **scope},
            "result": {"limits": limits, "samples": 5}, "source": source("calibration", "TST calibration coupon"),
            "date": "2026-01-01"}


def capability_profile(ident: str, process: str, *, parameters: dict[str, Any] | None = None,
                       calibrations: list[dict[str, Any]] | None = None, materials: Any = None, provider: str = "local",
                       **extra: Any) -> dict[str, Any]:
    data = {"id": ident, "revision": 1, "title": f"{ident} (TST fixture)", "process": process, "provider": provider,
            "parameters": parameters or {}, "materials": materials if materials is not None else unknown(),
            "calibrations": calibrations or []}
    data.update(extra)
    return data


# --- whole records ----------------------------------------------------------------------------------------------

def record(*, parts: list[dict[str, Any]] | None = None, interfaces: list[dict[str, Any]] | None = None,
           product_data: dict[str, Any] | None = None, requirements_data: dict[str, Any] | None = None,
           operations: Any = None, capabilities: list[dict[str, Any]] | None = None,
           materials: list[dict[str, Any]] | None = None, selection: dict[str, Any] | None = None,
           decisions: tuple[str, ...] = ("DR-001",), sources: dict[str, bytes] | None = None) -> dict[str, Any]:
    """A record version 2 record as {path below hw/: document}; write_record writes it."""
    parts = parts if parts is not None else [part("TST-0001")]
    documents: dict[str, Any] = {
        "product.yaml": product_data if product_data is not None else product(),
        "requirements.yaml": requirements_data if requirements_data is not None else requirements(),
        "interfaces.yaml": {"interfaces": interfaces or []},
        "assembly.yaml": {"subassemblies": [{"id": "main", "title": "Main", "parts": [p["PN"] for p in parts]}], "joints": [],
                          "operations": operations if operations is not None else unknown()},
        "manufacturing/capabilities.yaml": {"profiles": capabilities or []},
        "manufacturing/materials.yaml": {"profiles": materials or []},
        "manufacturing/selection.yaml": selection or {"status": "not_assessed", "candidates": []},
    }
    for item in parts:
        documents[f"parts/{item['PN']}/part.yaml"] = item
    documents["_decisions"] = list(decisions)
    documents["_sources"] = dict(sources or {})
    return documents


def current_documents(documents: dict[str, Any]) -> dict[str, Any]:
    """A hand-written record version 1 test fixture ({path below hw/: document}) as record version 2 in migrate.py's
    shape: every manufacturing-intent field an explicit unknown, legacy drawing.operations moved to operations.
    For fixtures about other record contracts, which read the same in both versions."""
    out = copy.deepcopy(documents)
    out["product.yaml"] = {**out["product.yaml"], "record_version": 2}
    out["product.yaml"].setdefault("use_case", use_case())
    reqs = out.setdefault("requirements.yaml", {"requirements": [requirement()]})
    reqs.setdefault("preferences", [])
    reqs.setdefault("assumptions", [])
    reqs.setdefault("open_questions", open_questions())
    for item in (out.setdefault("interfaces.yaml", {"interfaces": []}).get("interfaces") or []):
        item.setdefault("functional", {"critical": unknown(), "functions": unknown()})
    assembly = out["assembly.yaml"]
    legacy = (assembly.get("drawing") or {}).pop("operations", None) if isinstance(assembly.get("drawing"), dict) else None
    if isinstance(assembly.get("drawing"), dict) and not assembly["drawing"]:
        del assembly["drawing"]
    assembly.setdefault("operations", legacy if legacy is not None else unknown())
    out.setdefault("manufacturing/capabilities.yaml", {"profiles": []})
    out.setdefault("manufacturing/materials.yaml", {"profiles": []})
    out.setdefault("manufacturing/selection.yaml", {"status": "not_assessed", "candidates": []})
    for rel, document in out.items():
        if rel.startswith("parts/") and rel.endswith("/part.yaml"):
            document.setdefault("intent", intent())
    return out


def upgrade_record(hw: Path) -> Path:
    """Rewrite the hand-written record version 1 test fixture below `hw` as record version 2 (current_documents)."""
    hw = Path(hw)
    names = ["product.yaml", "requirements.yaml", "interfaces.yaml", "assembly.yaml",
             *(p.relative_to(hw).as_posix() for p in sorted(hw.glob("parts/*/part.yaml")))]
    documents = {name: yaml.safe_load((hw / name).read_text(encoding="utf-8")) for name in names if (hw / name).is_file()}
    for rel, document in current_documents(documents).items():
        (hw / rel).parent.mkdir(parents=True, exist_ok=True)
        (hw / rel).write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return hw


def write_record(hw: Path, documents: dict[str, Any]) -> Path:
    """Write `documents` (from record()) as a valid project below `hw`, with a matching BOM and verification CSV."""
    hw = Path(hw)
    documents = dict(documents)
    decisions = documents.pop("_decisions", [])
    sources = documents.pop("_sources", {})
    for rel, document in documents.items():
        path = hw / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    for ident in decisions:
        path = hw / "decisions" / f"{ident}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {ident}\n\nTST fixture decision, approved.\n", encoding="utf-8")
    for rel, data in sources.items():
        path = hw / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    parts = [doc for rel, doc in documents.items() if rel.startswith("parts/")]
    with (hw / "bom.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["level", "PN", "rev", "qty", "title", "material", "process", "make_buy", "supplier", "mass"])
        for item in parts:
            writer.writerow([1, item["PN"], item["rev"], 1, item["title"], item["material"], item["process"], item["make_buy"],
                             item.get("supplier") or "", ""])
    with (hw / "verification.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["requirement_id", "method", "criteria", "result", "evidence"])
        for item in documents["requirements.yaml"]["requirements"]:
            writer.writerow([item["id"], item["verification"]["method"], item["verification"]["criteria"], "planned", ""])
    return hw


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tst_record() -> dict[str, Any]:
    """A small synthetic product covering the main routes: TST-0001 a structural printed bracket under a
    sustained load (fdm, calibrated profile, approved orientation, supports on non-critical surfaces);
    TST-0002 a made-to-drawing CNC shaft with a reamed journal; TST-0003 a catalogue bearing;
    TST-0004 a laser-cut plate from identified stock; IF-001 a critical rotating interface."""
    datasheet = b"%PDF-1.4 TST fixture datasheet\n"
    sheet_source = source("datasheet", "TST sheet datasheet", artifact="manufacturing/sources/tst-sheet.pdf",
                          sha256=sha256(datasheet), conditions="TST-STD-1, 23 deg_C, dry")
    loads = [{"id": "head_load", "kind": "sustained", "magnitude": known(quantity(11, "N"), "user_declared"),
              "requirements": ["REQ-001"]}]
    use = use_case("draft", loads=loads)
    bracket = part("TST-0001", "Printed bracket", process="fdm", material="TST polymer", part_intent=intent(
        functions=[{"function": "structural", "requirements": ["REQ-001"]}],
        material_profile="tst_polymer_printed",
        service={"requirements": ["REQ-001"], "loads": ["head_load"]},
        critical_surfaces=[{"id": "pivot_bore", "feature": "pivot_bore", "role": "bore", "dimension_state": "after_finish",
                            "interfaces": ["IF-001"], "inspection": "bore_check"}],
        route=[operation("print", "fdm", "tst_printer", output_state="printed",
                         orientation={"status": "approved", "rotation": {"quaternion": [1, 0, 0, 0]}, "decision": "DR-001"},
                         supports={"strategy": "reachable_noncritical", "contact_regions": [{"feature": "underside"}],
                                   "removal": {"tools": ["flush cutters"], "access": "open underside"}}),
               operation("ream", "reaming", unknown("no reaming capability in this fixture"), targets=[{"feature": "pivot_bore"}],
                         allowances=[{"feature": "pivot_bore", "amount": quantity(0.1), "purpose": "finish", "source": source()}],
                         inspection=[{"id": "bore_check", "feature": "pivot_bore", "method": "plug gauge", "state": "after_finish"}])]))
    shaft = part("TST-0002", "Shaft", process="cnc", make_buy="buy", material="TST metal",
                 procurement={"mode": "made_to_drawing"}, part_intent=intent(
                     functions=[{"function": "rotating"}], material_profile="tst_metal_rod",
                     route=[operation("turn", "cnc", unknown("supplier capability not recorded in this fixture"), provider="outsourced")]))
    bearing = part("TST-0003", "Bearing", process="purchased", make_buy="buy", material="TST bearing steel",
                   procurement={"mode": "catalog"}, part_intent=intent(
                       functions=[{"function": "rotating"}], material_profile=not_applicable("catalogue item: its rating governs"),
                       critical_surfaces=not_applicable("catalogue item"), route=[operation("buy", "purchased")]))
    plate = part("TST-0004", "Clear cover plate", process="laser", material="TST transparent sheet", transparency="transparent",
                 part_intent=intent(
                     functions=[{"function": "optical"}, {"function": "cosmetic"}], material_profile="tst_clear_sheet",
                     critical_surfaces=not_applicable("no critical surface in this fixture"),
                     route=[operation("cut", "laser", "tst_laser", setup={"stock": {"material_profile": "tst_clear_sheet",
                                                                                     "thickness": known(quantity(3), "measured")}})]))
    return record(
        product_data=product(use),
        requirements_data=requirements([requirement("REQ-001", "Carry the head load", evidence_required=["simulation_supported"])]),
        parts=[bracket, shaft, bearing, plate],
        interfaces=[interface("IF-001", "TST-0001", "TST-0002", "motion", {
            "critical": known(True), "functions": [{"function": "rotating"}],
            "acceptance": [{"criterion": criterion("diametral_clearance", "between", [0.01, 0.05], "mm", id="running_clearance"),
                            "verification": {"method": "I", "evidence_required": ["physically_validated"], "stage": "release"}}]})],
        capabilities=[
            capability_profile("tst_printer", "fdm", parameters={"nozzle_diameter": known(quantity(0.4), "supplier_typical",
                                                                                            [source("supplier")])},
                               calibrations=[calibration("hole_coupon", "hole_diameter_deviation", "tst_polymer_printed",
                                                         quantity(lower=-0.2, upper=0.0), orientation="XY")]),
            capability_profile("tst_laser", "laser", materials=[{"material": "tst_clear_sheet", "status": "approved",
                                                                 "thickness": known(quantity(3), "user_declared"),
                                                                 "extraction": known("TST extraction"),
                                                                 "operations": ["cut"], "source": source("user")}]),
        ],
        materials=[
            material_profile("tst_polymer_printed", level="family", form="printed",
                             properties=[material_property("density", 1111, "kg_m3", orientation=known("XY"))]),
            material_profile("tst_metal_rod", form="rod", typical_not_allowable=False,
                             properties=[material_property("yield_strength", 111, "MPa", basis="allowable"),
                                         material_property("yield_strength", 99, "MPa", temperature=quantity(lower=20, upper=80, units="deg_C"))]),
            material_profile("tst_clear_sheet", form="sheet",
                             properties=[{"name": "light_transmission", "value": known(quantity(91, "percent"), "supplier_typical", [sheet_source]),
                                          "conditions": {"test_standard": known("TST-STD-2", "standard", [source("standard", "TST-STD-2")]),
                                                         "temperature": unknown("not stated by the source"),
                                                         "conditioning": "unknown"}}]),
        ],
        operations=[{"id": "fit_bearing", "kind": "press_fit", "part": "TST-0003", "into": "TST-0001", "interface": "IF-001",
                     "direction": [0, 0, -1], "acceptance": {"kind": "seated", "features": ["bearing_face", "seat_floor"]}}],
        sources={"manufacturing/sources/tst-sheet.pdf": datasheet},
    )


def migrated_copy(src_hw: Path, dst_hw: Path, today: date | None = None) -> Path:
    """Copy the record at `src_hw` (by default a version 1 example) to `dst_hw` and migrate it to record
    version 2 with migrate.py, as a test fixture for code that reads current records."""
    shutil.copytree(src_hw, dst_hw)
    migrate = migrate_module()
    migrate.apply(migrate.plan(dst_hw, today or date(2026, 1, 1)))
    return dst_hw
