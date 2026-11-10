#!/usr/bin/env python3
"""cad-design process_checks: process-dispatched manufacturability checks per made component and route
operation (plan section 11 rows 6, 7, 9, 10, 11, 12, 17 and 18, every disconnected component, the optical
route of decision 2, and the resin, casting, sheet-forming, waterjet and laser-flatness handlers).

Run: python3 -m unittest discover -s skills/cad-design/tests -p test_process_checks.py
The pure suites need PyYAML and jsonschema (the CI uv command supplies them). The geometry suites run the
OCP probe fixture (fixtures/process_probe.py) and a cad-model build through `uv`, and skip without it.
Every number is a synthetic TST value with a synthetic source.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import itertools
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SKILLS = SKILL.parent
SCRIPTS = SKILL / "scripts"
PROBE = HERE / "fixtures" / "process_probe.py"
FORM_CHECK = SCRIPTS / "form_check.py"
CAD_MODEL = SKILLS / "cad-model" / "scripts" / "cad_model.py"
TODAY = date(2026, 6, 1)
UV = shutil.which("uv") is not None
DEPS = importlib.util.find_spec("yaml") is not None and importlib.util.find_spec("jsonschema") is not None


def _module(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


sys.path.insert(0, str(SCRIPTS))
import process_checks as pc  # noqa: E402

FX = _module("cad_record_mi_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py") if DEPS else None
MI = pc.intent()


# --- record builders ----------------------------------------------------------------------------------------------

def known(value: Any, basis: str = "user_declared", sources: list | None = None, **extra: Any) -> dict[str, Any]:
    return FX.known(value, basis, sources, **extra)


def mm(value: float | None = None, *, lower: float | None = None, upper: float | None = None, units: str = "mm") -> dict:
    return FX.quantity(value, units, lower=lower, upper=upper)


def datasheet(reference: str) -> dict[str, Any]:
    return FX.source("datasheet", reference)


def calibration(ident: str, characteristic: str, material: str, lower: float, upper: float, units: str = "mm",
                **scope: Any) -> dict[str, Any]:
    return FX.calibration(ident, characteristic, material, mm(lower=lower, upper=upper, units=units), **scope)


def approval(material: str, **extra: Any) -> dict[str, Any]:
    return {"material": material, "status": "approved", "source": FX.source("user"), **extra}


APPROVED = {"status": "approved", "rotation": {"quaternion": [1, 0, 0, 0]}, "decision": "DR-001"}
ENVELOPE = {axis: known(mm(200), "measured", [FX.source("calibration", "TST envelope")]) for axis in "xyz"}
POLYMER = "tst_polymer"
ALU = "tst_alu"
ACRYLIC = "tst_acrylic"


def part(pn: str, route: list[dict[str, Any]], *, process: str, material: str | None = POLYMER, critical: Any = None,
         components: list[dict[str, Any]] | None = None, functions: Any = None, **extra: Any) -> dict[str, Any]:
    intent = FX.intent(functions=functions or [{"function": "cosmetic"}],
                       material_profile=material if material is not None else FX.unknown("stock not identified"),
                       route=route, critical_surfaces=critical if critical is not None else FX.not_applicable("none here"))
    if components is not None:
        intent["components"] = components
    return FX.part(pn, process=process, part_intent=intent, **extra)


def surface(feature: str, role: str, state: str = "as_manufactured", **extra: Any) -> dict[str, Any]:
    return {"id": feature, "feature": feature, "role": role, "dimension_state": state, **extra}


def materials(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    return [FX.material_profile(POLYMER, form="printed"), FX.material_profile(ALU, form="wrought", family="TST aluminium"),
            FX.material_profile(ACRYLIC, form="sheet", family="TST cast acrylic"), *extra]


def load(test: unittest.TestCase, documents: dict[str, Any]) -> Any:
    """The normalized Record of `documents`, which must be schema-valid and error-free."""
    with tempfile.TemporaryDirectory() as tmp:
        hw = FX.write_record(Path(tmp) / "project" / "hw", documents)
        record, problems = MI.check_record(hw)
    errors = [f"{p.code} {p.path}#{p.pointer}: {p.message}" for p in problems if p.severity == "error"]
    test.assertEqual(errors, [], "the fixture record must be valid")
    return record


# --- geometry facts builders (the probe format of process_checks.core.Geometry) -----------------------------------

def down(angle: float = 90.0, area: float = 200.0, kind: str = "overhang", *, span: float | None = None,
         start_part: float = 0.0, start_features: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"area_mm2": area, "max_angle_deg": angle, "angles": [[angle, area]], "kind": kind, "span_mm": span,
            "start": {"bed_mm2": area - start_part, "part_mm2": start_part, "features": list(start_features)}}


def face(ident: str, *, features: tuple[str, ...] = (), down_: dict | None = None, visible: dict | None = None,
         on_bbox: str | None = None, bed: bool = False, normals: tuple = ((0.0, 0.0, 1.0),)) -> dict[str, Any]:
    data: dict[str, Any] = {"id": ident, "type": "plane", "area_mm2": 100.0, "z_mm": [0.0, 10.0], "features": list(features),
                            "normals": [list(n) for n in normals], "visible": visible or {}}
    if down_ is not None:
        data["down"] = down_
    if on_bbox:
        data["on_bbox"] = on_bbox
    if bed:
        data["bed"] = True
    return data


def facts(*, frame: str = "bed", faces: tuple = (), holes: tuple = (), corners: tuple = (), features: dict | None = None,
          walls: dict | None = None, shells: int = 1, contact: float = 100.0) -> dict[str, Any]:
    mapped = {}
    for item in faces:
        for name in item["features"]:
            mapped.setdefault(name, {"kind": "face", "faces": [], "mapped": True})["faces"].append(item["id"])
    for item in holes:
        for name in item["features"]:
            mapped.setdefault(name, {"kind": "bore", "faces": ["hole"], "mapped": True})
    mapped.update(features or {})
    return {"format": 1, "frame": frame, "build_axis_native": [0.0, 0.0, 1.0] if frame == "bed" else None,
            "solids": 1, "shells": shells, "volume_mm3": 1000.0, "bbox_mm": {"min": [0, 0, 0], "max": [30, 20, 30]},
            "centre_mm": [5.0, 5.0, 10.0], "bed": {"z_mm": 0.0, "contact_area_mm2": contact, "contact_min": [0, 0],
                                                   "contact_max": [10, 10]},
            "faces": list(faces), "holes": list(holes), "corners": list(corners), "pins": [], "bends": [], "walls": walls,
            "features": mapped, "unmapped": [], "prismatic_axis": None}


def geometry(pn: str, data: dict[str, Any], component: str = "main") -> dict[tuple[str, str], Any]:
    evidence = ({"path": f"hw/versions/v1/manufacturing/{pn}/part.step", "sha256": "0" * 64},)
    return {(pn, component): pc.Geometry(pn, component, data, evidence)}


def hole(feature: str | None, diameter: float = 10.0, axis: tuple = (0.0, 1.0, 0.0), *, through: bool = True,
         depth: float = 20.0) -> dict[str, Any]:
    return {"id": "H1", "diameter_mm": diameter, "axis": list(axis), "position": [15.0, 0.0, 10.0], "depth_mm": depth,
            "through": through, "features": [feature] if feature else [], "floor_mm": None}


# --- packet schema ------------------------------------------------------------------------------------------------

_VALIDATOR: Any = None


def packet_errors(packet: dict[str, Any]) -> list[str]:
    global _VALIDATOR
    if _VALIDATOR is None:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        schemas = {}
        for path in (SKILLS / "cad-record" / "schemas").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            schemas[data["$id"]] = data
        registry = Registry().with_resources([(key, Resource.from_contents(value)) for key, value in schemas.items()])
        schema = schemas["https://srobroek.com/cad/schemas/manufacturing-check-result.schema.json"]
        _VALIDATOR = Draft202012Validator(schema, registry=registry)
    return [f"{list(e.absolute_path)}: {e.message[:300]}" for e in _VALIDATOR.iter_errors(packet)]


class Checks(unittest.TestCase):
    """Shared assertions: every assessment is a schema-valid packet, complete and never a false pass."""

    def assess(self, documents: dict[str, Any], geometry_: dict | None = None) -> Any:
        record = load(self, documents)
        result = pc.assess(record, geometry_ or {}, today=TODAY)
        packet = result.packet("v1", {"version_manifest": "0" * 64})
        self.assertEqual(packet_errors(packet), [])
        self.assertTrue(result.complete)
        self.assertEqual(len(result.outcomes), len(result.expected))
        return result

    def outcome(self, result: Any, obligation: str) -> str:
        found = result.outcome(obligation)
        self.assertIsNotNone(found, f"{obligation} is not expected: {[o.id for o in result.expected]}")
        return found["outcome"]

    def codes(self, result: Any, obligation: str | None = None) -> list[str]:
        return sorted(f["code"] for f in result.findings if obligation is None or f.get("obligation") == obligation)

    def finding(self, result: Any, code: str) -> dict[str, Any]:
        found = result.by_code(code)
        self.assertTrue(found, f"no {code} finding; codes: {self.codes(result)}")
        return found[0]


# --- row 6 and 7: supports ------------------------------------------------------------------------------------------

OVERHANG = calibration("overhang_coupon", "overhang_angle", POLYMER, 45.0, 50.0, "deg")
BRIDGE = calibration("bridge_coupon", "bridge_span", POLYMER, 5.0, 6.0)
REMOVAL = {"tools": ["flush cutters"], "access": "open from the front"}


def printed(pn: str, supports: Any, *, calibrations: list | None = None, critical: Any = None,
            extra_ops: list | None = None) -> dict[str, Any]:
    route = [FX.operation("print", "fdm", "tst_printer", output_state="printed", orientation=copy.deepcopy(APPROVED),
                          supports=supports), *(extra_ops or [])]
    profile = FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE,
                                    calibrations=calibrations if calibrations is not None else [OVERHANG, BRIDGE])
    return FX.record(parts=[part(pn, route, process="fdm", critical=critical)], capabilities=[profile], materials=materials())


SHELF = facts(faces=(face("f1", bed=True, normals=((0, 0, -1),)),
                     face("f10", features=("shelf_underside",), down_=down(90.0, 200.0)),
                     face("f12", features=("shelf_top",))))


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class SupportPolicyTests(Checks):
    """Row 6: supports on reachable non-critical surfaces are accepted; an overhang beyond the calibrated
    unsupported limit is a planning finding when a credible support plan covers it, not a failure."""

    def test_a_credible_reachable_noncritical_plan_satisfies_the_overhang_obligation(self) -> None:
        supports = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "shelf_underside"}], "removal": REMOVAL}
        result = self.assess(printed("TST-0101", supports), geometry("TST-0101", SHELF))
        obligation = "process.fdm.support@TST-0101/print"
        self.assertEqual(self.outcome(result, obligation), "pass")
        planned = self.finding(result, "process.fdm.overhang_planned")
        self.assertEqual((planned["severity"], planned["outcome"], planned["obligation"]), ("info", "pass", obligation))
        self.assertEqual(planned["criterion"]["source"]["kind"], "calibration")  # the calibrated limit, not a universal one
        evidence = self.finding(result, "process.fdm.evidence_required")
        self.assertNotIn("obligation", evidence)  # print trials are physical evidence the ledger holds
        self.assertEqual(evidence["recheck"]["families"], ["process.fdm.trial"])
        self.assertNotIn("process.fdm.trial@TST-0101/print", {o.id for o in result.expected})

    def test_the_same_overhang_without_a_plan_fails_and_an_unlisted_region_stays_unknown(self) -> None:
        none = self.assess(printed("TST-0101", {"strategy": "none"}), geometry("TST-0101", SHELF))
        self.assertEqual(self.outcome(none, "process.fdm.support@TST-0101/print"), "fail")
        self.assertEqual(self.finding(none, "process.fdm.overhang")["severity"], "major")
        elsewhere = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "shelf_top"}], "removal": REMOVAL}
        partial = self.assess(printed("TST-0101", elsewhere), geometry("TST-0101", SHELF))
        self.assertEqual(self.outcome(partial, "process.fdm.support@TST-0101/print"), "unknown")
        self.assertIn("process.fdm.support_region_unplanned", self.codes(partial))

    def test_without_a_calibrated_limit_the_need_for_support_is_unknown_never_a_universal_angle(self) -> None:
        result = self.assess(printed("TST-0101", {"strategy": "none"}, calibrations=[]), geometry("TST-0101", SHELF))
        self.assertEqual(self.outcome(result, "process.fdm.support@TST-0101/print"), "unknown")
        found = self.finding(result, "process.fdm.overhang_uncalibrated")
        self.assertEqual(found["criterion"]["status"], "unknown")

    def test_an_absent_support_strategy_is_unknown_never_none(self) -> None:
        documents = printed("TST-0101", None)
        del documents["parts/TST-0101/part.yaml"]["intent"]["route"][0]["supports"]
        result = self.assess(documents, geometry("TST-0101", SHELF))
        self.assertEqual(self.outcome(result, "process.fdm.support@TST-0101/print"), "unknown")
        self.assertIn("process.fdm.support_unplanned", self.codes(result))


SEAT = facts(faces=(face("f1", bed=True, normals=((0, 0, -1),)),
                    face("f7", features=("bearing_seat",), down_=down(68.0, 314.0, start_part=314.0,
                                                                        start_features=("bearing_seat",)))),
             holes=(hole("bearing_seat"),))
SEAT_SURFACE = [surface("bearing_seat", "seat")]


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class PrecisionSupportTests(Checks):
    """Row 7: support contact on a precision seat is a MAJOR challenge unless planned; soluble supports alone
    establish nothing; only a finished_critical exception with finishing, allowance, access and inspection is
    accepted, and then only as scoped evidence still to validate."""

    def obligation(self) -> str:
        return "process.fdm.support@TST-0102/print"

    def test_unplanned_support_contact_on_the_seat_is_a_major_failure(self) -> None:
        supports = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "underside"}], "removal": REMOVAL}
        result = self.assess(printed("TST-0102", supports, critical=SEAT_SURFACE), geometry("TST-0102", SEAT))
        self.assertEqual(self.outcome(result, self.obligation()), "fail")
        found = self.finding(result, "process.fdm.support_on_critical")
        self.assertEqual((found["severity"], found["subject"].get("feature")), ("major", "bearing_seat"))
        self.assertIn("finishing", found["criterion"]["note"])

    def test_soluble_supports_alone_do_not_establish_precision(self) -> None:
        supports = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "bearing_seat"}],
                    "material": "PVA soluble interface", "removal": {"tools": ["water bath"], "access": "dissolved"}}
        result = self.assess(printed("TST-0102", supports, critical=SEAT_SURFACE), geometry("TST-0102", SEAT))
        self.assertEqual(self.outcome(result, self.obligation()), "fail")
        found = self.finding(result, "process.fdm.support_on_critical")
        self.assertIn("soluble or interface supports alone do not establish precision", found["consequence"])

    def finished(self, *, ream: bool) -> dict[str, Any]:
        supports = {"strategy": "finished_critical", "contact_regions": [{"feature": "bearing_seat"}], "removal": REMOVAL,
                    "allowance": mm(0.2), "finish": "reamed to size", "inspection": ["seat_check"]}
        ops = [FX.operation("ream", "reaming", "tst_reamer", targets=[{"feature": "bearing_seat"}],
                            inspection=[{"id": "seat_check", "feature": "bearing_seat", "method": "plug gauge",
                                         "state": "after_finish"}])]
        if not ream:
            ops = [FX.operation("check", "inspection", "tst_gauge",
                                inspection=[{"id": "seat_check", "feature": "bearing_seat", "method": "plug gauge",
                                             "state": "after_finish"}])]
        documents = printed("TST-0102", supports, critical=[surface("bearing_seat", "seat", "after_finish", inspection="seat_check")],
                            extra_ops=ops)
        profiles = documents["manufacturing/capabilities.yaml"]["profiles"]
        profiles += [FX.capability_profile("tst_reamer", "reaming"), FX.capability_profile("tst_gauge", "inspection")]
        return documents

    def test_a_finished_critical_exception_is_accepted_only_with_its_finishing_operation(self) -> None:
        result = self.assess(self.finished(ream=True), geometry("TST-0102", SEAT))
        self.assertEqual(self.outcome(result, self.obligation()), "pass")
        exception = self.finding(result, "process.fdm.support_exception")
        self.assertEqual((exception["severity"], exception["outcome"]), ("info", "pass"))
        self.assertIn("scoped surface and dimensional validation", exception["consequence"])
        self.assertEqual(self.finding(result, "process.fdm.evidence_required")["outcome"], "unknown")
        missing = self.assess(self.finished(ream=False), geometry("TST-0102", SEAT))
        self.assertEqual(self.outcome(missing, self.obligation()), "fail")
        self.assertIn("a later finishing operation", self.finding(missing, "process.fdm.support_exception")["measured"]["text"])


# --- row 9: CNC access and workholding ------------------------------------------------------------------------------

def end_mill(diameter: float, reach: float) -> dict[str, Any]:
    return {"description": f"{diameter:g} mm end mill", "diameter": known(mm(diameter), "supplier_typical", [datasheet(f"TST {diameter:g} mm cutter")]),
            "reach": known(mm(reach), "supplier_typical", [datasheet(f"TST {diameter:g} mm cutter")])}


def milled(pn: str, tools: list[dict[str, Any]], *, workholding: bool = False, provider: str | None = None,
           capability: Any = "tst_mill", make_buy: str = "make", **extra: Any) -> dict[str, Any]:
    setup: dict[str, Any] = {"stock": {"material_profile": ALU, "grade": known("TST 6061-T6"), "dimensions": known("45 x 35 x 30")},
                             "datums": ["datum_a"], "access": {"sides": ["+Z"], "tools": tools}}
    if workholding:
        setup["workholding"] = {"method": known("vice on the datum faces")}
    op = FX.operation("mill", "cnc", capability, setup=setup, **({"provider": provider} if provider else {}))
    return part(pn, [op], process="cnc", material=ALU, make_buy=make_buy, **extra)


def mill_profile() -> dict[str, Any]:
    return FX.capability_profile("tst_mill", "cnc", parameters={"axes": known(mm(3, units="count"))},
                                 materials=[approval(ALU, operations=["machine"])])


POCKET_FACES = (face("f1", on_bbox="-z", bed=True, normals=((0, 0, -1),), visible={"+z": 0.0}),
                face("f15", features=("pocket_floor",), visible={"+z": 1.0}),
                face("f20", visible={"+z": 1.0}, normals=((1, 0, 0),)))
POCKET = facts(frame="model", faces=POCKET_FACES,
               corners=tuple({"radius_mm": 2.0, "axis": [0.0, 0.0, 1.0], "length_mm": 20.0, "features": []} for _ in range(4)))


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class CncAccessTests(Checks):
    """Row 9: a deep blind pocket with R2 corners: a short cutter's holder collides, a large cutter cannot make
    the radius, both sourced from the tools' datasheets; a missing clamp plan stays UNKNOWN."""

    def run_cnc(self, tools: list[dict[str, Any]], data: dict[str, Any] = POCKET) -> Any:
        documents = FX.record(parts=[milled("TST-0201", tools)], capabilities=[mill_profile()], materials=materials())
        return self.assess(documents, geometry("TST-0201", data))

    def test_a_short_cutter_that_fits_the_corner_collides_with_its_holder(self) -> None:
        result = self.run_cnc([end_mill(4.0, 8.0), end_mill(6.0, 30.0)])
        self.assertEqual(self.outcome(result, "process.cnc.access@TST-0201/mill"), "fail")
        reach = self.finding(result, "process.cnc.reach")
        self.assertEqual((reach["measured"]["value"], reach["criterion"]["value"]), (20.0, 8.0))
        self.assertEqual(reach["criterion"]["source"]["kind"], "datasheet")
        self.assertIn("holder would collide", reach["consequence"])
        self.assertNotIn("process.cnc.internal_radius", self.codes(result))

    def test_a_cutter_larger_than_the_internal_radius_cannot_make_the_corner(self) -> None:
        result = self.run_cnc([end_mill(6.0, 30.0)])
        radius = self.finding(result, "process.cnc.internal_radius")
        self.assertEqual((radius["measured"]["value"], radius["criterion"]["value"]), (2.0, 3.0))
        self.assertEqual(self.outcome(result, "process.cnc.access@TST-0201/mill"), "fail")

    def test_a_missing_clamp_plan_is_unknown_never_pass(self) -> None:
        result = self.run_cnc([end_mill(4.0, 25.0)])
        self.assertEqual(self.outcome(result, "process.cnc.access@TST-0201/mill"), "pass")
        self.assertEqual(self.outcome(result, "process.cnc.setup@TST-0201/mill"), "unknown")
        self.assertEqual(self.finding(result, "process.cnc.workholding_unknown")["severity"], "major")

    def test_an_undercut_hidden_from_every_access_side_fails(self) -> None:
        hidden = facts(frame="model", faces=POCKET_FACES + (face("f30", features=("t_slot",), visible={"+z": 0.0}),))
        result = self.run_cnc([end_mill(4.0, 25.0)], hidden)
        self.assertEqual(self.finding(result, "process.cnc.undercut")["subject"].get("feature"), "t_slot")


# --- rows 10 and 11: laser ----------------------------------------------------------------------------------------

def laser_profile(*entries: dict[str, Any], calibrations: list | None = None, parameters: dict | None = None) -> dict:
    return FX.capability_profile("tst_laser", "laser", parameters=parameters or {},
                                 materials=list(entries) if entries else FX.unknown("no approvals recorded"),
                                 calibrations=calibrations or [])


ACRYLIC_APPROVAL = approval(ACRYLIC, thickness=known(mm(lower=2.5, upper=3.5)), coating=known("none: masking film removed"),
                            extraction=known("TST filtered extraction"), operations=["cut", "engrave"])
KERF = calibration("kerf_coupon", "kerf_deviation", ACRYLIC, -0.05, 0.03, thickness=mm(3))


def cut(pn: str, *, stock: Any = ACRYLIC, thickness: dict[str, Any] | None = None, coat: str | None = None,
        coating: dict[str, Any] | None = None, material: str | None = ACRYLIC) -> dict[str, Any]:
    stock_block: dict[str, Any] = {"material_profile": stock,
                                   "thickness": known(thickness or mm(3.0), "supplier_typical", [datasheet("TST sheet tolerance")]),
                                   "dimensions": known("600 x 400")}
    route = [FX.operation("cut", "laser", "tst_laser", setup={"stock": stock_block}, output_state="cut",
                          post_processing=["peel film", "deburr"])]
    if coat:
        route.append(FX.operation("coat", "coating", "tst_coater", targets=[{"feature": coat}],
                                  allowances=[{"feature": coat, "amount": coating, "purpose": "coating", "source": datasheet("TST coating")}]))
    return part(pn, route, process="laser", material=material)


def tab_slot(*, tab_thickness: dict[str, Any], coating: dict[str, Any] | None, calibrations: list) -> dict[str, Any]:
    interface = FX.interface("IF-002", "TST-0301", "TST-0302", "mounting", {
        "critical": known(True), "functions": [{"function": "locating"}],
        "surfaces": [{"PN": "TST-0301", "feature": "slot", "role": "locating", "dimension_state": "assembled"},
                     {"PN": "TST-0302", "feature": "tab", "role": "locating", "dimension_state": "assembled"}],
        "acceptance": [{"criterion": FX.criterion("clearance", "between", [0.0, 0.3], "mm", id="tab_fit"),
                        "verification": {"method": "T", "evidence_required": ["physically_validated"], "stage": "release"}}]})
    coater = FX.capability_profile("tst_coater", "coating", parameters={"coating_type": known("TST paint"), "method": known("spray")})
    return FX.record(parts=[cut("TST-0301", coat="slot" if coating else None, coating=coating),
                            cut("TST-0302", thickness=tab_thickness, coat="tab" if coating else None, coating=coating)],
                     interfaces=[interface], capabilities=[laser_profile(ACRYLIC_APPROVAL, calibrations=calibrations), coater],
                     materials=materials())


SLOT = facts(frame="model", features={"slot": {"kind": "slot", "faces": ["f2", "f3"], "mapped": True, "width_mm": 3.10}})


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class LaserTabSlotTests(Checks):
    """Row 10 (laser side): a nominal +0.10 mm tab-and-slot clearance fails once the tab sheet's thickness,
    the calibrated kerf and the coating vary (worst case -0.15 mm); a fit coupon is always required."""

    obligation = "process.laser.kerf@TST-0301/cut"

    def test_thickness_kerf_and_coating_variation_invalidate_the_nominal_fit(self) -> None:
        documents = tab_slot(tab_thickness=mm(lower=2.9, upper=3.1), coating=mm(lower=0.0, upper=0.025), calibrations=[KERF])
        result = self.assess(documents, geometry("TST-0301", SLOT))
        self.assertEqual(self.outcome(result, self.obligation), "fail")
        fit = self.finding(result, "process.laser.tab_slot_fit")
        self.assertAlmostEqual(fit["measured"]["lower"], -0.15, places=9)
        self.assertAlmostEqual(fit["measured"]["upper"], 0.23, places=9)
        self.assertIn("nominal clearance +0.100 mm", fit["uncertainty"])
        self.assertEqual(fit["criterion"]["value"], [0.0, 0.3])
        coupon = self.finding(result, "process.laser.evidence_required")
        self.assertEqual(coupon["recheck"]["families"], ["process.laser.fit_coupon"])
        self.assertNotIn("obligation", coupon)

    def test_the_fit_passes_geometrically_when_its_worst_case_holds_and_still_needs_a_coupon(self) -> None:
        documents = tab_slot(tab_thickness=mm(lower=2.95, upper=3.05), coating=None, calibrations=[KERF])
        result = self.assess(documents, geometry("TST-0301", SLOT))
        self.assertEqual(self.outcome(result, self.obligation), "pass")
        self.assertIn("process.laser.evidence_required", self.codes(result))

    def test_an_uncalibrated_kerf_leaves_the_fit_unknown(self) -> None:
        documents = tab_slot(tab_thickness=mm(lower=2.9, upper=3.1), coating=None, calibrations=[])
        result = self.assess(documents, geometry("TST-0301", SLOT))
        self.assertEqual(self.outcome(result, self.obligation), "unknown")
        self.assertIn("process.laser.kerf_uncalibrated", self.codes(result))


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class LaserMaterialSafetyTests(Checks):
    """Row 11: PVC is rejected whatever a profile approves; an unidentified plastic or coating, or a material
    without machine and extraction approval, blocks pending identification; only exact approved stock passes."""

    obligation = "process.laser.material@TST-0401/cut"

    def material(self, stock_profile: dict[str, Any] | None, entry: dict[str, Any] | None, *,
                 parameters: dict | None = None) -> Any:
        ident = stock_profile["id"] if stock_profile else None
        extra = [stock_profile] if stock_profile and stock_profile["id"] != ACRYLIC else []
        documents = FX.record(parts=[cut("TST-0401", stock=ident or FX.unknown("unidentified clear plastic offcut"),
                                         material=ident)],
                              capabilities=[laser_profile(*([entry] if entry else []), parameters=parameters)],
                              materials=materials(*extra))
        if entry is None:
            documents["manufacturing/capabilities.yaml"]["profiles"][0]["materials"] = [approval(ALU, thickness=known(mm(3)))]
        return self.assess(documents)

    def test_pvc_is_rejected_even_when_a_profile_approves_it(self) -> None:
        pvc = FX.material_profile("tst_pvc_sheet", form="sheet", family="PVC")
        result = self.material(pvc, {**ACRYLIC_APPROVAL, "material": "tst_pvc_sheet"})
        self.assertEqual(self.outcome(result, self.obligation), "fail")
        found = self.finding(result, "process.laser.material_unsafe")
        self.assertEqual((found["severity"], found["criterion"]["source"]["kind"]), ("critical", "policy"))

    def test_unidentified_plastics_and_unapproved_stock_are_blocked_pending_identification(self) -> None:
        plastic = FX.material_profile("tst_clear_plastic", level="family", form="sheet", family="clear plastic")
        cases = {
            "unknown stock": (None, None, "process.laser.material_unidentified"),
            "family only": (plastic, {**ACRYLIC_APPROVAL, "material": "tst_clear_plastic"}, "process.laser.material_unidentified"),
            "no approval": (FX.material_profile(ACRYLIC, form="sheet"), None, "process.laser.material_approval"),
            "coating unidentified": (FX.material_profile(ACRYLIC, form="sheet"),
                                     {key: value for key, value in ACRYLIC_APPROVAL.items() if key != "coating"},
                                     "process.laser.coating_unidentified"),
            "no extraction": (FX.material_profile(ACRYLIC, form="sheet"),
                              {key: value for key, value in ACRYLIC_APPROVAL.items() if key != "extraction"},
                              "process.laser.extraction"),
        }
        for name, (profile, entry, code) in cases.items():
            with self.subTest(case=name):
                result = self.material(profile, entry)
                self.assertEqual(self.outcome(result, self.obligation), "unknown")
                self.assertEqual(self.finding(result, code)["severity"], "critical")

    def test_a_forbidden_material_fails_and_exact_approved_stock_passes(self) -> None:
        forbidden = {"material": ACRYLIC, "status": "forbidden", "reason": "TST: melts on this tube", "source": FX.source("user")}
        result = self.material(FX.material_profile(ACRYLIC, form="sheet"), forbidden)
        self.assertEqual(self.outcome(result, self.obligation), "fail")
        approved = self.material(FX.material_profile(ACRYLIC, form="sheet"), ACRYLIC_APPROVAL)
        self.assertEqual(self.outcome(approved, self.obligation), "pass")
        self.assertEqual(self.codes(approved, self.obligation), [])


# --- row 12: a complete multi-process route ---------------------------------------------------------------------------

PLATE = facts(frame="model", faces=(face("f1", on_bbox="-z", visible={"+z": 0.0, "-x": 0.0}),
                                    face("f2", features=("datum_a",), visible={"+z": 1.0, "-x": 0.0})),
              holes=(hole("ream_bore", 6.0, (0.0, 0.0, -1.0), depth=3.0),))


def multiprocess(*, complete: bool) -> dict[str, Any]:
    """laser cut, ream, coat and inspect; `complete` adds the finishing and coating allowances and the
    reamer's access side, which the broken route lacks."""
    stock = {"material_profile": ACRYLIC, "thickness": known(mm(3.0), "measured"), "dimensions": known("600 x 400")}
    cut_op = FX.operation("cut", "laser", "tst_laser", setup={"stock": stock}, output_state="cut",
                          post_processing=["peel film", "deburr"],
                          **({"allowances": [{"feature": "ream_bore", "amount": mm(0.1), "purpose": "finish",
                                              "source": FX.source("user")}]} if complete else {}))
    ream = FX.operation("ream", "reaming", "tst_reamer", targets=[{"feature": "ream_bore"}],
                        setup={"datums": ["datum_a"], "access": {"sides": ["+Z" if complete else "-X"], "tools": [
                            {"description": "6 mm reamer", "diameter": known(mm(6.0), "supplier_typical", [datasheet("TST reamer")]),
                             "reach": known(mm(12.0), "supplier_typical", [datasheet("TST reamer")])}]}})
    coat = FX.operation("coat", "coating", "tst_coater",
                        **({"allowances": [{"feature": "ream_bore", "amount": mm(lower=0.0, upper=0.02), "purpose": "coating",
                                            "source": datasheet("TST coating")}]} if complete else {}))
    inspect = FX.operation("inspect", "inspection", "tst_gauge", setup={"datums": ["datum_a"]},
                           inspection=[{"id": "bore_check", "feature": "ream_bore", "method": "plug gauge", "state": "after_finish",
                                        "capability": "tst_gauge"}])
    plate = part("TST-0501", [cut_op, ream, coat, inspect], process="laser", material=ACRYLIC,
                 critical=[surface("ream_bore", "bore", "after_finish", inspection="bore_check"), surface("datum_a", "datum")])
    profiles = [laser_profile(ACRYLIC_APPROVAL, calibrations=[KERF]), FX.capability_profile("tst_reamer", "reaming"),
                FX.capability_profile("tst_coater", "coating", parameters={"coating_type": known("TST paint"), "method": known("spray")}),
                FX.capability_profile("tst_gauge", "inspection",
                                      calibrations=[calibration("gauge_study", "measurement_uncertainty", ACRYLIC, 0.0, 0.004)])]
    return FX.record(parts=[plate], capabilities=profiles, materials=materials())


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class MultiprocessRouteTests(Checks):
    """Row 12: laser cut, ream, coat and inspect get every operation's checks, datum transfer, allowances and
    order; a missing ream allowance or access and an uncompensated coating on the fit are invalid; nothing is
    checked as FDM."""

    def test_every_operation_of_the_route_has_its_own_checks_and_no_fdm_substitute(self) -> None:
        result = self.assess(multiprocess(complete=False), geometry("TST-0501", PLATE))
        families = {o.family for o in result.expected}
        for kind in ("laser", "reaming", "coating", "inspection"):
            self.assertTrue(any(f.startswith(f"process.{kind}.") for f in families), kind)
        self.assertFalse(any(f.startswith("process.fdm.") for f in families))
        plans = sorted(o.id for o in result.expected if o.family == "route.operation_plan")
        self.assertEqual(plans, [f"route.operation_plan@TST-0501/{op}" for op in ("coat", "cut", "inspect", "ream")])

    def test_a_missing_allowance_blocked_reamer_and_uncompensated_coating_are_invalid(self) -> None:
        result = self.assess(multiprocess(complete=False), geometry("TST-0501", PLATE))
        self.assertEqual(self.outcome(result, "process.reaming.allowance@TST-0501/ream"), "fail")
        self.assertIn("process.reaming.allowance_missing", self.codes(result))
        self.assertEqual(self.outcome(result, "process.reaming.access@TST-0501/ream"), "fail")
        self.assertIn("process.reaming.target_access", self.codes(result))
        self.assertEqual(self.outcome(result, "process.coating.allowance@TST-0501/coat"), "fail")
        self.assertEqual(self.finding(result, "process.coating.fit_allowance")["subject"]["feature"], "ream_bore")

    def test_the_complete_route_passes_those_checks(self) -> None:
        result = self.assess(multiprocess(complete=True), geometry("TST-0501", PLATE))
        for obligation in ("process.reaming.allowance@TST-0501/ream", "process.reaming.access@TST-0501/ream",
                           "process.coating.allowance@TST-0501/coat", "process.inspection.capability@TST-0501/inspect",
                           "route.operation_plan@TST-0501/ream"):
            with self.subTest(obligation=obligation):
                self.assertEqual(self.outcome(result, obligation), "pass", self.codes(result, obligation))


# --- row 17: dispatch by process -----------------------------------------------------------------------------------

@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class DispatchByProcessTests(Checks):
    """Row 17: one shape made by FDM, CNC, bought from a catalogue, or made to drawing by an outsourced CNC
    supplier: FDM checks only on the FDM part, ratings only on the bought one, and the outsourced route is
    assessed against its (unknown) supplier capability rather than exempted."""

    def documents(self) -> dict[str, Any]:
        printed_part = part("TST-0601", [FX.operation("print", "fdm", "tst_printer", orientation=copy.deepcopy(APPROVED),
                                                      supports={"strategy": "none"})], process="fdm")
        cnc_part = milled("TST-0602", [end_mill(4.0, 25.0)], workholding=True)
        bought = part("TST-0603", [FX.operation("buy", "purchased", purchase={
            "identity": known("TST-BRG 608"), "specification": known("TST ISO 15 608", "standard", [FX.source("standard", "TST ISO 15")]),
            "ratings": known("TST C 3.3 kN", "supplier_typical", [datasheet("TST bearing")]),
            "availability": known("TST stock item")})], process="purchased", material=None, make_buy="buy",
            procurement={"mode": "catalog"})
        bought["intent"]["material_profile"] = FX.not_applicable("catalogue item")
        outsourced = milled("TST-0604", [end_mill(4.0, 25.0)], workholding=True, provider="outsourced",
                            capability=FX.unknown("supplier capability not recorded"), make_buy="buy",
                            procurement={"mode": "made_to_drawing"})
        return FX.record(parts=[printed_part, cnc_part, bought, outsourced],
                         capabilities=[FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE), mill_profile()],
                         materials=materials())

    def test_each_part_gets_only_its_own_process_checks(self) -> None:
        shape = {**geometry("TST-0601", POCKET), **geometry("TST-0602", POCKET), **geometry("TST-0604", POCKET)}
        result = self.assess(self.documents(), shape)
        by_pn: dict[str, set[str]] = {}
        for obligation in result.expected:
            by_pn.setdefault(obligation.subject["PN"], set()).add(obligation.family)
        self.assertTrue({"process.fdm.support", "process.fdm.geometry"} <= by_pn["TST-0601"])
        for pn in ("TST-0602", "TST-0603", "TST-0604"):
            with self.subTest(part=pn):
                self.assertFalse([f for f in by_pn[pn] if f.startswith("process.fdm.")])
                self.assertFalse([f["code"] for f in result.findings
                                  if f["subject"]["PN"] == pn and f["code"].startswith("process.fdm.")])
        self.assertEqual(by_pn["TST-0603"], {"purchase.identity", "purchase.rating", "purchase.dimensions",
                                              "purchase.mounting", "purchase.availability"})
        self.assertEqual(self.outcome(result, "purchase.rating@TST-0603/buy"), "pass")
        self.assertEqual(self.outcome(result, "purchase.dimensions@TST-0603/buy"), "pass")
        for family in ("stock", "setup", "access", "features", "precision"):
            self.assertEqual(self.outcome(result, f"process.cnc.{family}@TST-0604/mill"), "unknown")
        outsourced = [f for f in result.findings if f["code"] == "process.cnc.capability_unknown"
                      and f["subject"]["PN"] == "TST-0604"]
        self.assertTrue(outsourced and all("outsourcing is no exemption" in f["consequence"] for f in outsourced))
        self.assertEqual(self.outcome(result, "process.cnc.access@TST-0602/mill"), "pass")


# --- row 18: unsupported process ------------------------------------------------------------------------------------

@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class CoverageGapTests(Checks):
    """Row 18: a process with no check contract stays a readable record whose coverage is UNKNOWN: no FDM or
    other substitute, no default output files, no PASS, so freeze and release stay refused."""

    def test_an_unsupported_process_is_an_unknown_coverage_gap(self) -> None:
        sinter = FX.operation("sinter", "sintering", FX.unknown("no sintering capability"))
        documents = FX.record(parts=[part("TST-0701", [sinter], process="other")], materials=materials())
        result = self.assess(documents)
        process = sorted(o.id for o in result.expected if o.family.startswith("process."))
        self.assertEqual(process, ["process.sintering.coverage@TST-0701/sinter"])
        self.assertEqual(self.outcome(result, process[0]), "unknown")
        gap = self.finding(result, "process.coverage_gap")
        self.assertIn("no FDM or other process's checks stand in", gap["consequence"])
        self.assertIn("freeze and release stay blocked", gap["consequence"])
        self.assertEqual(result.result, "unknown")
        self.assertNotIn("pass", {o["outcome"] for o in result.outcomes if o["obligation"].startswith("process.")})
        layout = _module("cad_record_layout", SKILLS / "cad-record" / "scripts" / "layout.py")
        with self.assertRaises(layout.UnsupportedProcess):
            layout.manufacture_policy(documents["parts/TST-0701/part.yaml"])


# --- every disconnected component -----------------------------------------------------------------------------------

@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class EveryComponentTests(Checks):
    """Every disconnected made component is assessed: an unplanned loose solid gets its own route outcome
    (UNKNOWN, major), never folded into the planned one; process checks run per component and operation."""

    def test_each_component_is_judged_on_its_own(self) -> None:
        components = [{"id": "body", "witness": [20.0, 10.0, 5.0]}, {"id": "tab", "witness": [65.0, 2.5, 1.0]}]
        route = [FX.operation("print", "fdm", "tst_printer", orientation=copy.deepcopy(APPROVED), supports={"strategy": "none"},
                              components=["body"])]
        documents = FX.record(parts=[part("TST-0001", route, process="fdm", components=components)],
                              capabilities=[FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE)], materials=materials())
        shape = {**geometry("TST-0001", SHELF, "body"), **geometry("TST-0001", SHELF, "tab")}
        result = self.assess(documents, shape)
        ids = {o.id for o in result.expected}
        self.assertTrue({"route.consistency@TST-0001.body", "route.consistency@TST-0001.tab",
                         "process.fdm.support@TST-0001.body/print"} <= ids)
        self.assertFalse([i for i in ids if i.startswith("process.") and ".tab/" in i])
        self.assertEqual(self.outcome(result, "route.consistency@TST-0001.tab"), "unknown")
        unplanned = self.finding(result, "route.component_unplanned")
        self.assertEqual((unplanned["severity"], unplanned["subject"]), ("major", {"PN": "TST-0001", "component": "tab"}))


# --- every handler of the table ----------------------------------------------------------------------------------------

CALIBRATION_FREE = frozenset({"process.resin.drainage"})  # an enclosed void is a machine-independent B-rep fact


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class HandlerSweepTests(Checks):
    """Every route kind of the table runs on a bare operation, with and without its capability profile and
    geometry: each packet is schema-valid and complete, and no process check passes on a profile without
    calibrations (only the calibration-free enclosed-void count may)."""

    def test_no_kind_passes_a_process_check_without_calibration(self) -> None:
        for kind in sorted(set(pc.TABLE) - {"purchased"}):
            for with_profile, measured in itertools.product((False, True), repeat=2):
                with self.subTest(kind=kind, profile=with_profile, geometry=measured):
                    op = FX.operation("op", kind, f"tst_{kind}" if with_profile else None)
                    primary = kind if kind in MI.PRIMARY_PROCESSES else "other"
                    documents = FX.record(parts=[part("TST-0901", [op], process=primary)], materials=materials(),
                                          capabilities=[FX.capability_profile(f"tst_{kind}", kind)] if with_profile else [])
                    frame = "bed" if kind in ("fdm", "resin") else "model"
                    result = self.assess(documents, geometry("TST-0901", facts(frame=frame)) if measured else None)
                    families = {o.id: o.family for o in result.expected}
                    self.assertTrue(any(f.startswith(f"process.{kind}.") for f in families.values()))
                    passed = [o["obligation"] for o in result.outcomes if o["outcome"] == "pass"
                              and families[o["obligation"]].startswith("process.")
                              and families[o["obligation"]] not in CALIBRATION_FREE]
                    self.assertEqual(passed, [])
                    self.assertNotEqual(result.result, "pass")


# --- decision 2: optical parts ---------------------------------------------------------------------------------------

CLEAR = "tst_clear_sheet"


def clear_sheet() -> dict[str, Any]:
    transmission = {"name": "light_transmission",
                    "value": known(FX.quantity(92, "percent"), "supplier_typical", [datasheet("TST clear sheet")]),
                    "conditions": {"test_standard": known("TST-STD-2", "standard", [FX.source("standard", "TST-STD-2")]),
                                   "temperature": FX.unknown("not stated by the source"), "conditioning": "unknown"}}
    return FX.material_profile(CLEAR, form="sheet", family="TST cast acrylic", properties=[transmission])


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class OpticalRouteTests(Checks):
    """Decision 2: an optical requirement eliminates FDM and resin (see-through FDM is not a thing); a purchased
    transparent part needs traceable optical criteria, and cut stock must be an identified grade with a sourced
    light transmission."""

    obligation = "optical.route@TST-0801"

    def documents(self, made: dict[str, Any], *profiles: dict[str, Any]) -> dict[str, Any]:
        made["intent"]["functions"] = [{"function": "optical"}]
        return FX.record(parts=[made], capabilities=list(profiles), materials=materials(clear_sheet()))

    def optical(self, made: dict[str, Any], *profiles: dict[str, Any]) -> Any:
        return self.assess(self.documents(made, *profiles))

    def test_fdm_and_resin_are_eliminated_with_a_traceable_remedy(self) -> None:
        for kind in ("fdm", "resin"):
            with self.subTest(kind=kind):
                route = [FX.operation("print", kind, f"tst_{kind}", orientation=copy.deepcopy(APPROVED), supports={"strategy": "none"})]
                documents = self.documents(part("TST-0801", route, process=kind), FX.capability_profile(f"tst_{kind}", kind))
                result = self.assess(documents)
                self.assertEqual(self.outcome(result, self.obligation), "fail")
                found = self.finding(result, "optical.opaque_process")
                self.assertIn("cast acrylic", found["remedy"])
                # transmission is physical evidence: the record requires it, the ledger (not this producer) answers it
                self.assertIn("optical.transmission@TST-0801", {o.id for o in MI.required_checks(load(self, documents))})
                self.assertNotIn("optical.transmission@TST-0801", {o.id for o in result.expected})

    def test_a_purchased_part_passes_only_with_its_optical_criteria(self) -> None:
        purchase = {"identity": known("TST window W-30"), "specification": known("TST cast acrylic, 3 mm"),
                    "ratings": known("TST service temperature 80 deg C", "supplier_typical", [datasheet("TST window")]),
                    "optical": known("light transmission 92 percent", "supplier_typical", [datasheet("TST window")])}
        for name, data, outcome in (("traceable", purchase, "pass"),
                                    ("no optical grade", {k: v for k, v in purchase.items() if k != "optical"}, "unknown")):
            with self.subTest(case=name):
                bought = part("TST-0801", [FX.operation("buy", "purchased", purchase=data)], process="purchased",
                              material=None, make_buy="buy", procurement={"mode": "catalog"})
                bought["intent"]["material_profile"] = FX.not_applicable("catalogue item")
                result = self.optical(bought)
                self.assertEqual(self.outcome(result, self.obligation), outcome)
                self.assertEqual(outcome == "unknown", "optical.purchase_criteria" in self.codes(result, self.obligation))

    def test_cut_stock_must_be_an_identified_transparent_grade(self) -> None:
        unidentified = self.optical(cut("TST-0801"), laser_profile(ACRYLIC_APPROVAL))
        self.assertEqual(self.outcome(unidentified, self.obligation), "unknown")
        self.assertIn("optical.stock_unidentified", self.codes(unidentified, self.obligation))
        identified = self.optical(cut("TST-0801", stock=CLEAR, material=CLEAR), laser_profile({**ACRYLIC_APPROVAL, "material": CLEAR}))
        self.assertEqual(self.outcome(identified, self.obligation), "pass")


# --- the other route kinds: resin, casting, sheet forming, waterjet, laser flatness ----------------------------------

def shaped(kind: str, calibrations: list, *, critical: Any = None, material: str = ALU, **op: Any) -> dict[str, Any]:
    """One `kind` operation in an approved orientation on a TST-0902 of `material`, against a profile that
    approves the material and holds `calibrations`."""
    route = [FX.operation("form", kind, f"tst_{kind}", orientation=copy.deepcopy(APPROVED), **op)]
    profile = FX.capability_profile(f"tst_{kind}", kind, calibrations=calibrations, materials=[approval(material)])
    return FX.record(parts=[part("TST-0902", route, process=kind, material=material, critical=critical)],
                     capabilities=[profile], materials=materials())


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class OtherRouteKindTests(Checks):
    """Each remaining handler judges its own calibrated characteristic: enclosed resin voids, casting draft
    along the approved pull, sheet bend radius, and as-cut edge finish and datum flatness against the critical
    surface's own limit, never a calibration that merely exists."""

    def test_an_enclosed_void_traps_resin_and_an_open_part_still_needs_its_slicer_review(self) -> None:
        ops = {"supports": {"strategy": "none"}}
        trapped = self.assess(shaped("resin", [], material=POLYMER, **ops), geometry("TST-0902", facts(shells=2)))
        self.assertEqual(self.outcome(trapped, "process.resin.drainage@TST-0902/form"), "fail")
        self.assertEqual(self.finding(trapped, "process.resin.trapped_volume")["measured"], {"value": 1, "units": "count"})
        open_ = self.assess(shaped("resin", [], material=POLYMER, **ops), geometry("TST-0902", facts()))
        self.assertEqual(self.outcome(open_, "process.resin.drainage@TST-0902/form"), "pass")
        review = self.finding(open_, "process.resin.evidence_required")
        self.assertEqual((review["recheck"]["families"], "obligation" in review), (["process.resin.trial"], False))

    def test_casting_draft_is_measured_along_the_approved_pull(self) -> None:
        draft = calibration("draft_study", "min_draft", ALU, 1.0, 1.5, "deg")
        visible = {"+pull": 1.0, "-pull": 0.0}
        tilt = math.sin(math.radians(2.0))
        for name, normal, outcome in (("vertical wall", (1.0, 0.0, 0.0), "fail"),
                                      ("2 deg draft", (math.cos(math.radians(2.0)), 0.0, tilt), "pass")):
            with self.subTest(case=name):
                shape = facts(frame="model", faces=(face("f1", visible=visible, normals=(normal,)),
                                                    face("f2", visible=visible, normals=((0.0, 0.0, 1.0),))))
                result = self.assess(shaped("casting", [draft]), geometry("TST-0902", shape))
                self.assertEqual(self.outcome(result, "process.casting.draft@TST-0902/form"), outcome)
                self.assertEqual(outcome == "fail", "process.casting.draft" in self.codes(result))
        self.assertIn("+pull", pc.probe_plan(load(self, shaped("casting", [draft])), "TST-0902", "main")["directions"])

    def test_a_bend_tighter_than_the_calibrated_radius_cracks(self) -> None:
        radius = calibration("bend_study", "min_bend_radius", ALU, 1.0, 1.5)
        for inner, outcome in ((0.8, "fail"), (2.0, "pass")):
            with self.subTest(inner=inner):
                shape = facts(frame="model")
                shape["bends"] = [{"inner_radius_mm": inner, "thickness_mm": 1.5, "axis": [0.0, 1.0, 0.0],
                                   "axis_point": [0.0, 0.0, 0.0], "features": []}]
                result = self.assess(shaped("sheet_forming", [radius]), geometry("TST-0902", shape))
                self.assertEqual(self.outcome(result, "process.sheet_forming.bend@TST-0902/form"), outcome)
                self.assertEqual(outcome == "fail", "process.sheet_forming.bend_radius" in self.codes(result))

    def test_a_calibrated_waterjet_edge_must_meet_the_edge_roughness_limit(self) -> None:
        limit = FX.criterion("surface_roughness", "<=", 3.2, "um")
        edge = [surface("cut_edge", "seal", criteria=[limit])]
        taper = calibration("taper_study", "taper", ALU, 0.0, 0.05)
        for upper, outcome in ((6.3, "fail"), (3.0, "pass")):
            with self.subTest(edge_finish=upper):
                finish = calibration("finish_study", "edge_finish", ALU, 1.6, upper, "um")
                result = self.assess(shaped("waterjet", [taper, finish], critical=edge))
                self.assertEqual(self.outcome(result, "process.waterjet.edge@TST-0902/form"), outcome)
                self.assertEqual(outcome == "fail", "process.waterjet.edge_finish" in self.codes(result))

    def test_calibrated_laser_flatness_must_meet_the_datum_flatness_limit(self) -> None:
        thermal = [calibration("flat_study", "flatness", ACRYLIC, 0.0, 0.3),
                   calibration("haz_study", "heat_affected_zone", ACRYLIC, 0.0, 0.2)]
        documents = FX.record(parts=[cut("TST-0902")], materials=materials(),
                              capabilities=[laser_profile(ACRYLIC_APPROVAL, calibrations=[KERF, *thermal])])
        made = documents["parts/TST-0902/part.yaml"]["intent"]
        for limit, outcome in ((0.1, "fail"), (0.5, "pass")):
            with self.subTest(limit=limit):
                made["critical_surfaces"] = [surface("datum_a", "datum", criteria=[FX.criterion("flatness", "<=", limit, "mm")])]
                result = self.assess(documents)
                self.assertEqual(self.outcome(result, "process.laser.thermal@TST-0902/cut"), outcome)
                self.assertEqual(outcome == "fail", "process.laser.flatness" in self.codes(result))


# --- contract -------------------------------------------------------------------------------------------------------

@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class DispatchContractTests(Checks):
    """The closed table answers every route kind's families except physical evidence; a version 1 record is
    not assessable; a handler cannot link a failing finding to an obligation it did not fail."""

    def test_the_table_covers_every_check_family_but_physical_evidence(self) -> None:
        for kind, checks in MI.PROCESS_CHECKS.items():
            with self.subTest(kind=kind):
                wanted = {family for family, level, _ in checks if level != "physically_validated"}
                self.assertEqual(set(pc.TABLE[kind]), wanted)
        self.assertEqual(set(pc.TABLE), set(MI.PROCESS_CHECKS))

    def test_a_version_1_record_is_refused_with_its_migration(self) -> None:
        record = MI.load_record(FX.LAMP)
        self.assertEqual(record.version, 1)
        with self.assertRaisesRegex(pc.NotAssessable, "migrat"):
            pc.expected(record)

    def test_the_fixture_record_assesses_every_owned_obligation_without_geometry(self) -> None:
        result = self.assess(FX.tst_record())
        self.assertTrue(result.expected)
        geometric = [o for o in result.outcomes if o["level"] == "geometrically_manufacturable"]
        self.assertTrue(geometric and all(o["outcome"] != "pass" or "purchase" in o["obligation"] for o in geometric))
        self.assertNotIn("pass", {o["outcome"] for o in result.outcomes if o["obligation"].startswith("process.fdm.")})


# --- geometry: the OCP probe ----------------------------------------------------------------------------------------

@unittest.skipUnless(UV and DEPS, "uv, pyyaml and jsonschema are required for the OCP probe")
class ProbeGeometryTests(Checks):
    """The probe measures the facts the handlers read on real B-rep (fixtures/process_probe.py), and the
    policy rows hold on measured geometry, not only on hand-written facts (rows 6, 7, 9, 12, 17)."""

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(PROBE)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AssertionError(f"process_probe.py failed: {proc.stderr[-3000:]}")
        cls.probed = json.loads(proc.stdout)

    def face_of(self, case: str, feature: str) -> dict[str, Any]:
        return next(f for f in self.probed[case]["faces"] if feature in f["features"])

    def test_the_shelf_underside_is_a_supported_overhang_and_the_bore_top_starts_supports_on_itself(self) -> None:
        shelf = self.face_of("shelf", "shelf_underside")["down"]
        self.assertEqual((shelf["kind"], shelf["max_angle_deg"], shelf["start"]["part_mm2"]), ("overhang", 90.0, 0.0))
        self.assertAlmostEqual(shelf["area_mm2"], 200.0, places=6)
        self.assertEqual(self.probed["shelf"]["bed"]["contact_area_mm2"], 100.0)
        seat = self.face_of("seat", "bearing_seat")["down"]
        self.assertGreater(seat["max_angle_deg"], 45.0)
        self.assertEqual(seat["start"]["features"], ["bearing_seat"])
        self.assertEqual([h["features"] for h in self.probed["seat"]["holes"]], [["bearing_seat"]])

    def test_rows_6_and_7_hold_on_the_probed_shapes(self) -> None:
        supports = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "shelf_underside"}], "removal": REMOVAL}
        planned = self.assess(printed("TST-0101", supports), geometry("TST-0101", self.probed["shelf"]))
        self.assertEqual(self.outcome(planned, "process.fdm.support@TST-0101/print"), "pass")
        unplanned = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "shelf_top"}], "removal": REMOVAL}
        seat = self.assess(printed("TST-0102", unplanned, critical=SEAT_SURFACE), geometry("TST-0102", self.probed["seat"]))
        self.assertEqual(self.outcome(seat, "process.fdm.support@TST-0102/print"), "fail")
        self.assertIn("process.fdm.support_on_critical", self.codes(seat))

    def test_the_pocket_corners_reach_and_visibility_are_measured(self) -> None:
        pocket = self.probed["pocket"]
        self.assertEqual([(c["radius_mm"], c["length_mm"]) for c in pocket["corners"]], [(2.0, 20.0)] * 4)
        self.assertEqual(self.face_of("pocket", "pocket_floor")["visible"], {"+z": 1.0})
        documents = FX.record(parts=[milled("TST-0201", [end_mill(4.0, 8.0)])], capabilities=[mill_profile()], materials=materials())
        result = self.assess(documents, geometry("TST-0201", pocket))
        self.assertEqual(self.outcome(result, "process.cnc.access@TST-0201/mill"), "fail")
        self.assertIn("process.cnc.reach", self.codes(result))
        self.assertNotIn("process.cnc.undercut", self.codes(result))

    def test_the_multiprocess_plate_bore_is_located_and_reached_only_from_its_axis(self) -> None:
        plate = self.probed["plate"]
        bore = self.face_of("plate", "ream_bore")
        self.assertEqual(bore["visible"], {"+z": 1.0, "-x": 0.0})
        self.assertEqual([(h["diameter_mm"], h["through"], h["features"]) for h in plate["holes"]], [(6.0, True, ["ream_bore"])])
        data = copy.deepcopy(plate)
        data["features"]["datum_a"] = {"kind": "face", "faces": [data["faces"][0]["id"]], "mapped": True}
        broken = self.assess(multiprocess(complete=False), geometry("TST-0501", data))
        self.assertEqual(self.outcome(broken, "process.reaming.access@TST-0501/ream"), "fail")
        fixed = self.assess(multiprocess(complete=True), geometry("TST-0501", data))
        self.assertEqual(self.outcome(fixed, "process.reaming.access@TST-0501/ream"), "pass")

    def test_equivalent_members_are_probed_one_by_one(self) -> None:
        pair = self.probed["pair"]
        self.assertEqual((pair["solids"], pair["shells"]), (2, 2))
        self.assertEqual(len({f["id"][:2] for f in pair["faces"]}), 2)


# --- every component end to end: U3's loose project, built and checked -------------------------------------------------

def _projects() -> Any:
    return _module("cad_model_manufacturing_projects", SKILLS / "cad-model" / "tests" / "fixtures" / "manufacturing_projects.py")


@unittest.skipUnless(UV and DEPS, "uv, pyyaml and jsonschema are required for the kernel build")
class LooseComponentCoverageTests(Checks):
    """cad-model builds U3's two loose solids (the smaller unplanned); form_check --version publishes one
    packet in which both components are assessed on their own exported files, the unplanned one UNKNOWN."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        mp = _projects()
        cls.hw = mp.loose_project(Path(cls.tmp.name) / "project" / "hw", "cadquery", [[60.0, 10.0, 5.0, 2.0]],
                                  parts_of=mp.components(), print_components=["body"])
        built = subprocess.run(["uv", "run", "--quiet", "--script", str(CAD_MODEL), "run", str(cls.hw), "--label", "v1"],
                               capture_output=True, text=True, check=False)
        if built.returncode != 0:
            raise AssertionError(f"cad_model run failed: {built.stdout[-2000:]}{built.stderr[-2000:]}")
        cls.runs = [cls.form_check() for _ in range(2)]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    @classmethod
    def form_check(cls) -> tuple[int, dict[str, Any]]:
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FORM_CHECK), "--hw", str(cls.hw), "--version", "v1",
                               "--fidelity", "F2", "--on", TODAY.isoformat()], capture_output=True, text=True, check=False)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError as error:
            raise AssertionError(f"form_check printed no JSON (exit {proc.returncode}): {proc.stderr[-3000:]}") from error

    def test_both_loose_components_are_assessed_and_published(self) -> None:
        code, report = self.runs[0]
        self.assertEqual(code, 1, report)  # uncalibrated and unplanned: unknown, never a pass
        made = report["manufacturing"]
        self.assertEqual((made["status"], made["complete"], made["reused"]), ("unknown", True, False))
        bundle = Path(made["bundle"])
        self.assertEqual(bundle, self.hw / "checks" / "v1" / "process")
        layout = _module("cad_record_layout", SKILLS / "cad-record" / "scripts" / "layout.py")
        self.assertEqual(layout.verify(bundle), [])
        packet = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(packet_errors(packet), [])
        ids = {o["id"] for o in packet["expected"]}
        self.assertTrue({"route.consistency@TST-0001.body", "route.consistency@TST-0001.tab",
                         "process.fdm.orientation@TST-0001.body/print"} <= ids)
        self.assertFalse([i for i in ids if i.startswith("process.") and ".tab/" in i])
        outcomes = {o["obligation"]: o["outcome"] for o in packet["outcomes"]}
        self.assertEqual(outcomes["route.consistency@TST-0001.tab"], "unknown")
        self.assertIn("route.component_unplanned", {f["code"] for f in packet["findings"]})
        measured = [f for f in packet["findings"] if f["subject"].get("component") == "body" and "evidence" in f["method"]]
        self.assertTrue(all(e["path"].startswith("hw/versions/v1/manufacturing/TST-0001/components/body/")
                            for f in measured for e in f["method"]["evidence"]))
        manifest = self.hw / "versions" / "v1" / "manifest.json"
        self.assertEqual(packet["dependencies"]["version_manifest"], hashlib.sha256(manifest.read_bytes()).hexdigest())

    def test_a_second_run_reuses_the_sealed_bundle(self) -> None:
        code, report = self.runs[1]
        self.assertEqual((code, report["manufacturing"]["reused"]), (1, True))


if __name__ == "__main__":
    unittest.main()
