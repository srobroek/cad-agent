#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

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
from types import SimpleNamespace
from typing import Any
from unittest import mock

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
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from modload import result_payload  # noqa: E402

FX = _module("cad_record_mi_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py") if DEPS else None
MI = pc.intent()
GL = _module("cad_record_guidelines", SKILLS / "cad-record" / "scripts" / "guidelines.py")
SHIPPED = GL.discover(None) if DEPS else None  # the shipped guideline sets: fdm.generic and the nozzle, material sets


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
            "centre_mm": [5.0, 5.0, 10.0], "bed": {"z_mm": 0.0, "contact_area_mm2": contact, "contact_hull": [[0, 0], [10, 0], [10, 10], [0, 10]]},
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
        _VALIDATOR = FX.schema_registry_module().document_validator(
            SKILLS / "cad-record" / "schemas", "manufacturing-check-result.schema.json")
    return [f"{list(e.absolute_path)}: {e.message[:300]}" for e in _VALIDATOR.iter_errors(packet)]


class Checks(unittest.TestCase):
    """Shared assertions: every assessment is a schema-valid packet, complete and never a false pass."""

    def assess(self, documents: dict[str, Any], geometry_: dict | None = None, *, catalog: Any = None) -> Any:
        record = load(self, documents)
        result = pc.assess(record, geometry_ or {}, today=TODAY, **({"catalog": catalog} if catalog is not None else {}))
        packet = result.packet({"version_manifest": "0" * 64})
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


CONTACT = calibration("bed_contact_coupon", "min_bed_contact_area", POLYMER, 20.0, 25.0, "mm2")
TRIANGLE = [[0, 0], [10, 0], [0, 10]]  # its bounding box is the 10 mm square, twice its area


def stable(centre: list[float], hull: list, contact: float = 100.0) -> dict[str, Any]:
    data = facts(contact=contact)
    data["centre_mm"] = [*centre, 10.0]
    data["bed"]["contact_hull"] = hull
    data["bed"]["contact_extent"] = {"min": [min(p[0] for p in hull), min(p[1] for p in hull)],
                                     "max": [max(p[0] for p in hull), max(p[1] for p in hull)]}
    return data


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class BedStabilityTests(Checks):
    """The center of volume against the convex hull of the bed contact, and the contact area against its calibrated minimum."""

    OBLIGATION = "process.fdm.orientation@TST-0101/print"

    def orient(self, data: dict[str, Any], calibrations: list | None = None) -> Any:
        documents = printed("TST-0101", {"strategy": "none"}, calibrations=[CONTACT] if calibrations is None else calibrations)
        return self.assess(documents, geometry("TST-0101", data))

    def test_a_centre_over_the_hull_passes(self) -> None:
        result = self.orient(stable([2.0, 2.0], TRIANGLE))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "pass")

    def test_a_centre_in_the_bounding_box_but_off_the_hull_fails(self) -> None:
        result = self.orient(stable([8.0, 8.0], TRIANGLE))  # x + y = 16 > 10: beyond the hypotenuse, inside the 10 mm box
        self.assertEqual(self.outcome(result, self.OBLIGATION), "fail")
        self.assertEqual(self.finding(result, "process.fdm.stability")["severity"], "major")

    def test_a_centre_on_the_hull_boundary_passes(self) -> None:
        result = self.orient(stable([5.0, 5.0], TRIANGLE))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "pass")

    def test_a_contact_below_the_calibrated_minimum_fails(self) -> None:
        result = self.orient(stable([2.0, 2.0], TRIANGLE, contact=10.0))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "fail")
        found = self.finding(result, "process.fdm.contact_area")
        self.assertEqual((found["measured"]["value"], found["criterion"]["value"], found["criterion"]["units"]), (10.0, 25.0, "mm2"))

    def test_a_contact_at_the_calibrated_minimum_passes(self) -> None:
        result = self.orient(stable([2.0, 2.0], TRIANGLE, contact=25.0))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "pass")

    def test_a_centre_on_a_slanted_edge_after_the_probe_rounds_its_coordinates_is_stable(self) -> None:
        """Boundary on an edge is stable. The producer rounds coordinates to 1e-6 mm, so a center exactly on the edge
        from (0, 0) to (10/3, 1) at x = 2995/900 arrives as (3.327778, 0.998333), 4.8e-7 mm outside it."""
        form_check = load_module("tst_form_check_boundary", FORM_CHECK)
        hull = form_check._convex_hull([(0.0, 0.0), (10 / 3, 1.0), (0.0, 1.0)])
        data = stable([0.0, 0.0], hull)
        data["centre_mm"] = form_check.rounded_xyz_tuple((2995 / 900, 0.3 * 2995 / 900, 10.0))
        self.assertEqual(self.outcome(self.orient(data), self.OBLIGATION), "pass")

    def test_a_centre_one_micrometre_beyond_the_edge_still_fails(self) -> None:
        form_check = load_module("tst_form_check_boundary", FORM_CHECK)
        hull = form_check._convex_hull([(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)])
        data = stable([0.0, 0.0], hull)
        data["centre_mm"] = [5.0 + 0.0007, 5.0 + 0.0007, 10.0]  # 1 micrometre beyond x + y = 10
        self.assertEqual(self.outcome(self.orient(data), self.OBLIGATION), "fail")

    def test_an_uncalibrated_contact_area_is_unknown_never_pass(self) -> None:
        result = self.orient(stable([2.0, 2.0], TRIANGLE), calibrations=[])
        self.assertEqual(self.outcome(result, self.OBLIGATION), "unknown")
        self.assertEqual(self.finding(result, "process.fdm.contact_area_uncalibrated")["criterion"]["status"], "unknown")

    def test_the_warp_footprint_is_the_larger_extent_of_the_contact_hull(self) -> None:
        warp = calibration("warp_coupon", "warp", POLYMER, 0.1, 0.2, size=mm(15))
        small = self.orient(stable([2.0, 2.0], [[0, 0], [10, 0], [10, 12], [0, 12]]), calibrations=[CONTACT, warp])
        self.assertEqual(small.by_code("process.fdm.warp_scope"), [])
        large = self.orient(stable([2.0, 2.0], [[0, 0], [10, 0], [10, 18], [0, 18]]), calibrations=[CONTACT, warp])
        self.assertEqual(self.finding(large, "process.fdm.warp_scope")["measured"]["value"], 18.0)


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

    def test_a_flat_downward_face_with_no_measured_span_is_judged_by_its_angle(self) -> None:
        """A ceiling that is not a strip (a ring around a pillar, a notched outline) has bridge facts with no span."""
        unmeasured = facts(faces=(face("f1", bed=True, normals=((0, 0, -1),)),
                                  face("f10", features=("ceiling",), down_=down(90.0, 200.0, "bridge"))))
        result = self.assess(printed("TST-0101", {"strategy": "none"}), geometry("TST-0101", unmeasured))
        self.assertEqual(self.outcome(result, "process.fdm.support@TST-0101/print"), "fail")
        self.assertEqual(self.finding(result, "process.fdm.overhang")["severity"], "major")
        self.assertNotIn("process.fdm.overhang_uncalibrated", self.codes(result))
        steep = facts(faces=(face("f1", bed=True, normals=((0, 0, -1),)),
                             face("f10", features=("ceiling",), down_=down(45.0, 200.0, "bridge"))))
        within = self.assess(printed("TST-0101", {"strategy": "none"}), geometry("TST-0101", steep))
        self.assertEqual(self.outcome(within, "process.fdm.support@TST-0101/print"), "pass")

    def test_without_a_calibrated_limit_the_shipped_overhang_guideline_decides(self) -> None:
        """The user's rule replaces "never a universal angle": with no calibration, fdm.generic's 45 deg decides, and a
        90 deg shelf with support strategy none fails, citing the guideline set."""
        result = self.assess(printed("TST-0101", {"strategy": "none"}, calibrations=[]), geometry("TST-0101", SHELF),
                             catalog=SHIPPED)
        self.assertEqual(self.outcome(result, "process.fdm.support@TST-0101/print"), "fail")
        found = self.finding(result, "process.fdm.overhang")
        self.assertEqual((found["criterion"]["operator"], found["criterion"]["value"], found["criterion"]["units"]), ("<=", 45.0, "deg"))
        self.assertEqual((found["criterion"]["source"]["kind"], found["criterion"]["source"]["reference"],
                          found["criterion"]["source"]["revision"]), ("guideline", "fdm.generic", "2"))

    def test_without_a_calibration_or_a_catalog_the_need_for_support_is_unknown_never_a_universal_angle(self) -> None:
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
        self.assertEqual(found["criterion"]["source"]["origin"], "benchhand plugin")
        self.assertEqual(found["criterion"]["source"]["reference"],
                         "benchhand manufacturing-intent dispatch policy (cad-design process_checks)")

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


def tab_slot(*, tab_thickness: dict[str, Any], coating: dict[str, Any] | None, calibrations: list,
             clearances: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    interface = FX.interface("IF-002", "TST-0301", "TST-0302", "mounting", {
        "critical": known(True), "functions": [{"function": "locating"}],
        "surfaces": [{"PN": "TST-0301", "feature": "slot", "role": "locating", "dimension_state": "assembled"},
                     {"PN": "TST-0302", "feature": "tab", "role": "locating", "dimension_state": "assembled"}],
        "acceptance": [{"criterion": criterion,
                        "verification": {"method": "T", "evidence_required": ["physically_validated"], "stage": "release"}}
                       for criterion in clearances or [FX.criterion("clearance", "between", [0.0, 0.3], "mm", id="tab_fit")]]})
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
        self.assertEqual(found["criterion"]["source"]["origin"], "benchhand plugin")

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

    def test_a_guideline_source_does_not_trace_a_purchased_specification(self) -> None:
        documents = self.documents()
        route = documents["parts/TST-0603/part.yaml"]["intent"]["route"][0]
        route["purchase"]["specification"]["sources"] = [FX.source("guideline", "fdm.generic", revision="1", sha256="a" * 64)]
        result = self.assess(documents, {**geometry("TST-0601", POCKET), **geometry("TST-0602", POCKET), **geometry("TST-0604", POCKET)})
        self.assertEqual(self.outcome(result, "purchase.dimensions@TST-0603/buy"), "unknown")
        self.assertIn("purchase.dimensions_untraceable", self.codes(result, "purchase.dimensions@TST-0603/buy"))

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


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class PurchasedMountingOccurrenceTests(Checks):
    """A bought part placed three times appears by occurrence (`@id`) in assembly operations. Its mounting check
    needs an operation that names each occurrence as part or into."""

    OBLIGATION = "purchase.mounting@TST-0603/buy"

    def documents(self, operations: list[dict[str, Any]], rings: tuple[str, ...] = ("ring-a", "ring-b", "ring-c"),
                  interfaces: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
        ring = part("TST-0603", [FX.operation("buy", "purchased", purchase={
            "identity": known("TST-RING 15"), "specification": known("TST ISO 15", "standard", [FX.source("standard", "TST ISO 15")]),
            "ratings": known("TST 1 kN", "supplier_typical", [datasheet("TST ring")]),
            "availability": known("TST stock item")})], process="purchased", material=None, make_buy="buy",
            procurement={"mode": "catalog"}, model_frame="part_local")
        ring["intent"]["material_profile"] = FX.not_applicable("catalogue item")
        housing = part("TST-0604", [FX.operation("print", "fdm", "tst_printer", orientation=copy.deepcopy(APPROVED),
                                                 supports={"strategy": "none"})], process="fdm", model_frame="part_local")
        documents = FX.record(parts=[ring, housing], operations=operations, interfaces=list(interfaces),
                              capabilities=[FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE)],
                              materials=materials())
        documents["assembly.yaml"]["subassemblies"] = [{"id": "main", "title": "Main", "occurrences": [
            "@housing", *(f"@{ident}" for ident in rings)]}]
        documents["assembly.yaml"]["occurrences"] = [{"id": "housing", "PN": "TST-0604"}] + [
            {"id": ident, "PN": "TST-0603"} for ident in rings]
        return documents

    @staticmethod
    def install(ident: str, part_ref: str, into: str = "@housing") -> dict[str, Any]:
        return {"id": ident, "kind": "join", "part": part_ref, "into": into, "direction": [0, 0, -1],
                "acceptance": {"kind": "standoff", "features": ["foot", "top"]}}

    def rings(self, *names: str) -> list[dict[str, Any]]:
        return [self.install(f"mount-{name}", f"@ring-{name}") for name in names]

    def test_every_occurrence_with_an_installing_operation_is_mounted(self) -> None:
        result = self.assess(self.documents(self.rings("a", "b", "c")))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "pass")

    def test_an_occurrence_without_an_operation_fails_and_is_named(self) -> None:
        result = self.assess(self.documents(self.rings("a", "b")))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "fail")
        finding = self.finding(result, "purchase.mounting_unmounted")
        self.assertIn("@ring-c", finding["measured"]["text"])
        self.assertNotIn("@ring-a", finding["measured"]["text"])

    def test_an_occurrence_named_only_as_into_counts_as_mounted(self) -> None:
        operations = [*self.rings("a", "b"), self.install("seat-c", "@housing", into="@ring-c")]
        result = self.assess(self.documents(operations))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "pass")

    def test_a_part_placed_once_and_named_by_id_is_mounted(self) -> None:
        result = self.assess(self.documents(self.rings("a"), rings=("ring-a",)))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "pass")

    def test_an_interface_does_not_stand_in_for_an_unmounted_occurrence(self) -> None:
        """purchased.py:98-108: a part placed twice or more fails for any occurrence no operation names, even if
        an interface names the part number."""
        interface = FX.interface("IF-001", "TST-0603", "TST-0604")
        result = self.assess(self.documents(self.rings("a", "b"), interfaces=(interface,)))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "fail")
        self.assertIn("@ring-c", self.finding(result, "purchase.mounting_unmounted")["measured"]["text"])

    def test_an_operation_naming_a_part_the_inventory_lacks_mounts_nothing(self) -> None:
        """The part is in no subassembly, so `TST-0603` resolves to no occurrence and the operation mounts nothing.
        The old code compared the unresolved reference as written, and it equalled the part number."""
        operations = [self.install("mount-x", "TST-0603")]
        result = self.assess(self.documents(operations, rings=()))
        self.assertEqual(self.outcome(result, self.OBLIGATION), "unknown")
        self.assertIn("purchase.mounting_unknown", self.codes(result, self.OBLIGATION))


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
class PurchasedIdentityTests(Checks):
    """A standard designation with a size (DIN 471 - 5x0.6) or a manufacturer part number identifies a bought item. A
    supplier order code is not required. The designation field is canonical, and a BOM title that parses as a standard
    designation stands in for an empty field and draws an advisory."""

    pn = "TST-0901"
    obligation = "purchase.identity@TST-0901/buy"

    def bought(self, title: str = "Bushing", *, identity: dict[str, Any] | None = None, **procurement: Any) -> Any:
        facts = {"identity": identity or known("TST item"), "specification": known("TST spec"),
                 "ratings": known("TST rating", "supplier_typical", [datasheet("TST item")]),
                 "availability": known("TST stock item")}
        item = part(self.pn, [FX.operation("buy", "purchased", purchase=facts)], process="purchased", material=None,
                    make_buy="buy", title=title, procurement={"mode": "catalog", **procurement})
        item["intent"]["material_profile"] = FX.not_applicable("catalogue item")
        return self.assess(FX.record(parts=[item], capabilities=[], materials=materials()))

    def test_a_standard_designation_with_a_size_identifies_the_part_without_an_order_code(self) -> None:
        for designation in ("DIN 471 - 5x0.6", "DIN 471 – 8×0.8", "ISO 4762 M5x20", "EN ISO 4017 - M8 x 30",
                            "ISO/EN/DIN 898-1 M6x25", "ISO 2338 - 5 m6 x 20", "ISO 7089 - 5 - 200 HV", "ISO 7089 5", "DIN 125 - 8.4", "ASME B18.2.1 1/4-20 x 1", "ANSI B18.3 #10-24 x 1",
                            "Retaining ring DIN 471 - 5x0.6, spring steel C60S"):
            with self.subTest(designation=designation):
                result = self.bought(designation=designation)
                self.assertEqual(self.outcome(result, self.obligation), "pass")
                self.assertEqual(self.codes(result, self.obligation), [])

    def test_a_part_standard_still_counts_beside_a_feature_standard_or_with_the_number_of_one(self) -> None:
        for designation in ("DIN 912 M5x20, thread ISO 965-1 6g", "DIN 965 M5x12"):
            with self.subTest(designation=designation):
                self.assertEqual(self.outcome(self.bought(designation=designation), self.obligation), "pass")

    def test_a_feature_standard_alone_is_not_a_part_designation(self) -> None:
        for designation in ("Heat-set insert, thread ISO 965-1 M10x1-6H", "ISO 286 - 5 H7 x 20", "ASME B1.1 1/4-20 x 1",
                            "ISO/EN/DIN 965-1 M10x1-6H", "DIN ISO 965-1 M10x1-6H", "DIN EN ISO 965-1 M10x1-6H",
                            "General tolerance ISO 2768-m"):
            with self.subTest(designation=designation):
                result = self.bought(designation=designation)
                self.assertEqual(self.outcome(result, self.obligation), "fail")
                self.assertEqual(self.codes(result, self.obligation), ["purchase.designation_missing"])

    def test_a_feature_standard_designation_with_a_manufacturer_part_number_passes_or_stays_open(self) -> None:
        spec = "Heat-set insert, thread ISO 965-1 M10x1-6H"
        self.assertEqual(self.outcome(self.bought(designation=spec, manufacturer_part_number="QT-M10"), self.obligation), "pass")
        opened = self.bought(designation=spec, open={"manufacturer_part_number": "supplier to confirm"})
        self.assertEqual(self.outcome(opened, self.obligation), "unknown")

    def test_a_made_to_drawing_cnc_part_gets_no_purchase_check(self) -> None:
        made = milled("TST-0902", [end_mill(4.0, 25.0)], workholding=True, make_buy="buy",
                      procurement={"mode": "made_to_drawing", "designation": "Shoulder pin to drawing"})
        result = self.assess(FX.record(parts=[made], capabilities=[mill_profile()], materials=materials()))
        self.assertEqual([o.id for o in result.expected if o.family.startswith("purchase.")], [])
        self.assertEqual([o["obligation"] for o in result.outcomes if o["obligation"].startswith("purchase.")], [])
        self.assertEqual([f["code"] for f in result.findings if f["code"].startswith("purchase.")], [])

    def test_a_standard_number_without_a_size_does_not_identify_a_part(self) -> None:
        for designation in ("DIN 471", "ISO 4762", "EN 10025-2", "DIN 471 retaining ring", "Standard fastener",
                            "Washer ISO 7089 200HV"):
            with self.subTest(designation=designation):
                result = self.bought(designation=designation)
                self.assertEqual(self.outcome(result, self.obligation), "fail")
                self.assertEqual(self.codes(result, self.obligation), ["purchase.designation_missing"])

    def test_a_fractional_or_unified_size_without_a_length_identifies_a_part(self) -> None:
        for size in ("1/4-20", "1/4", "3/8-16", "#10-24", "5/16-18 UNC", "1/2\"", "1/4-20UNC", "1/4-20 UNC-2B", "1/4-20 x 1"):
            designation = f"ASME B18.2.2 {size}"
            with self.subTest(size=size, where="field"):
                self.assertEqual(self.outcome(self.bought(designation=designation), self.obligation), "pass")
            with self.subTest(size=size, where="title"):
                self.assertEqual(self.outcome(self.bought(f"Hex nut {designation}"), self.obligation), "pass")

    def test_a_size_token_that_continues_into_letters_is_not_a_size(self) -> None:
        for size in ("1/4-20HV", "1/4abc", "1/4-20UNCX"):
            designation = f"ASME B18.2.2 {size}"
            with self.subTest(size=size, where="field"):
                self.assertEqual(self.outcome(self.bought(designation=designation), self.obligation), "fail")
            with self.subTest(size=size, where="title"):
                self.assertEqual(self.outcome(self.bought(f"Hex nut {designation}"), self.obligation), "fail")

    def test_a_grade_is_not_a_size_but_a_real_size_beside_a_grade_is(self) -> None:
        """A token is a grade only with a grade marker: a hardness unit (HV, HB, HRC) after a number, or a stainless class
        (A2-70). A property class (8.8) beside a thread size does not change the size. A decimal such as 8.4 is a washer size."""
        cases = (
            ("Washer ISO 7089 - 200 HV", "fail"), ("Washer ISO 7089 200 HV", "fail"), ("Washer ISO 7089 - 40 HRC", "fail"),
            ("ISO 4762 - A2-70", "fail"), ("A2-70", "fail"),
            ("ISO 7089 - 5 - 200 HV", "pass"), ("Washer ISO 7089 - 8 - 200 HV", "pass"), ("DIN 125 - 8.4", "pass"),
            ("Washer ISO 7089 - 8.4 - 200 HV", "pass"), ("ISO 4762 M6x20 - 8.8", "pass"), ("ISO 4762 M6 - 8.8", "pass"),
            ("ISO 4762 M3x10 10.9", "pass"),
        )
        self.assert_designations(cases)

    def test_a_threaded_fastener_standard_needs_a_thread_designation_as_its_size(self) -> None:
        """For ISO 4762 and the other listed screw, bolt and nut standards a bare number is not a size. An unlisted standard
        (a washer, a ring, a pin) keeps a bare decimal as a size."""
        cases = [("ISO 4762 - 8.8", "fail"), ("ISO 4762 10.9", "fail"), ("ISO 4017 - 10.9", "fail"), ("ISO 4762 5", "fail"),
                 ("ISO 4032 - 8", "fail"), ("ISO 4762 5x20", "fail"), ("ISO 4762 1/4", "fail"), ("ISO 4762 #10", "fail"),
                 ("ISO 4762 M6", "pass"), ("ISO 4762 #10-24", "pass"), ("ISO 4762 1/4-20", "pass"), ("ISO 4017 M8x30", "pass"),
                 ("ISO 7089 - 8.8", "pass"), ("DIN 125 - 8.4", "pass"), ("DIN 471 - 5x0.6", "pass")]
        # The table holds ISO 4762 itself. The loop covers the other eight listed standards.
        for standard in ("ISO 4014", "ISO 4017", "ISO 7380", "ISO 10642", "ISO 4032", "DIN 912", "DIN 933", "DIN 934"):
            cases += [(f"{standard} - 8.8", "fail"), (f"{standard} M6", "pass")]
        self.assert_designations(cases)

    def assert_designations(self, cases: Any) -> None:
        for designation, expected in cases:
            with self.subTest(designation=designation, where="field"):
                result = self.bought(designation=designation)
                self.assertEqual(self.outcome(result, self.obligation), expected)
                if expected == "fail":
                    self.assertEqual(self.codes(result, self.obligation), ["purchase.designation_missing"])
            with self.subTest(designation=designation, where="title"):
                self.assertEqual(self.outcome(self.bought(designation), self.obligation), expected)

    def test_a_hardness_grade_in_the_title_is_not_a_size(self) -> None:
        result = self.bought("Washer ISO 7089 200HV")
        self.assertEqual(self.outcome(result, self.obligation), "fail")
        self.assertEqual(self.codes(result, self.obligation), ["purchase.designation_missing"])

    def test_a_manufacturer_part_number_identifies_a_non_standard_part(self) -> None:
        result = self.bought(designation="Compression limiter CL8000, M4 x 8", manufacturer_part_number="CMPL 4 X 8 EK CL8000")
        self.assertEqual(self.outcome(result, self.obligation), "pass")
        self.assertEqual(self.codes(result, self.obligation), [])

    def test_a_non_standard_part_without_a_manufacturer_part_number_fails(self) -> None:
        for name, procurement in (("designation only", {"designation": "Heat-set insert QT-M10x12.7"}),
                                  ("neither", {}),
                                  ("the field wins over a standard title", {"designation": "Bushing xyz"})):
            with self.subTest(case=name):
                title = "Retaining ring DIN 471 - 5x0.6" if "wins" in name else "Heat-set insert"
                result = self.bought(title, **procurement)
                self.assertEqual(self.outcome(result, self.obligation), "fail")
                found = self.finding(result, "purchase.designation_missing")
                self.assertEqual((found["severity"], found["outcome"]), ("major", "fail"))

    def test_a_standard_title_stands_in_for_an_empty_field_with_an_advisory(self) -> None:
        result = self.bought("Retaining ring DIN 471 - 5x0.6")
        self.assertEqual(self.outcome(result, self.obligation), "pass")
        found = self.finding(result, "purchase.designation_in_title")
        self.assertEqual((found["severity"], found["outcome"]), ("info", "pass"))
        self.assertIn("procurement.designation", found["remedy"])

    def test_an_unparseable_title_with_an_empty_field_fails(self) -> None:
        for title in ("Bushing", "Retaining ring DIN 471"):
            with self.subTest(title=title):
                result = self.bought(title)
                self.assertEqual(self.outcome(result, self.obligation), "fail")
                self.assertEqual(self.codes(result, self.obligation), ["purchase.designation_missing"])

    def test_an_open_manufacturer_part_number_is_unknown_not_fail(self) -> None:
        result = self.bought(open={"manufacturer_part_number": "supplier not chosen yet"})
        self.assertEqual(self.outcome(result, self.obligation), "unknown")
        self.assertEqual(self.codes(result, self.obligation), ["purchase.designation_open"])

    def test_an_open_field_does_not_excuse_a_part_that_is_identified_anyway(self) -> None:
        result = self.bought(designation="DIN 471 - 5x0.6", open={"supplier_part_number": "pick at order time"})
        self.assertEqual(self.outcome(result, self.obligation), "pass")

    def test_an_identified_part_still_needs_a_known_identity_fact(self) -> None:
        result = self.bought(designation="DIN 471 - 5x0.6", identity=FX.unknown("not recorded in this fixture"))
        self.assertEqual(self.outcome(result, self.obligation), "unknown")
        self.assertEqual(self.codes(result, self.obligation), ["purchase.identity_unknown"])


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


# --- strict criteria: `<` and `>` beside `<=` and `>=` ----------------------------------------------------------------

@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class StrictCriterionTests(Checks):
    """A strict criterion is evaluated strictly or refused as UNKNOWN; it is never read as its non-strict neighbour."""

    def test_a_calibration_states_its_criterion_with_the_operator_it_was_asked_for(self) -> None:
        calibrated = pc.core.Calibrated("calibrated", "wall", MI.quantity(mm(lower=2, upper=3)), (), SimpleNamespace(
            id="cal_1", scope={}, samples=3, date="2026-06-01", source=FX.source("calibration")))
        for operator, value in ((">", 3), (">=", 3), ("<", 2), ("<=", 2), ("between", [2, 3])):
            with self.subTest(operator=operator):
                found = calibrated.criterion("wall", operator)
                self.assertEqual((found["operator"], found["value"]), (operator, value))
        with self.assertRaises(ValueError):
            calibrated.criterion("wall", "equals")

    def test_a_calibrated_waterjet_edge_at_a_strict_roughness_limit_fails_it(self) -> None:
        taper = calibration("taper_study", "taper", ALU, 0.0, 0.05)
        finish = calibration("finish_study", "edge_finish", ALU, 1.6, 3.2, "um")
        for operator, limit, outcome in (("<=", 3.2, "pass"), ("<", 3.2, "fail"), ("<", 3.3, "pass"), ("<=", 3.1, "fail")):
            with self.subTest(operator=operator, limit=limit):
                edge = [surface("cut_edge", "seal", criteria=[FX.criterion("surface_roughness", operator, limit, "um")])]
                result = self.assess(shaped("waterjet", [taper, finish], critical=edge))
                self.assertEqual(self.outcome(result, "process.waterjet.edge@TST-0902/form"), outcome)
                self.assertEqual(outcome == "fail", "process.waterjet.edge_finish" in self.codes(result))

    def test_calibrated_laser_flatness_at_a_strict_limit_fails_it(self) -> None:
        thermal = [calibration("flat_study", "flatness", ACRYLIC, 0.0, 0.3),
                   calibration("haz_study", "heat_affected_zone", ACRYLIC, 0.0, 0.2)]
        documents = FX.record(parts=[cut("TST-0902")], materials=materials(),
                              capabilities=[laser_profile(ACRYLIC_APPROVAL, calibrations=[KERF, *thermal])])
        made = documents["parts/TST-0902/part.yaml"]["intent"]
        for operator, limit, outcome in (("<=", 0.3, "pass"), ("<", 0.3, "fail"), ("<", 0.31, "pass")):
            with self.subTest(operator=operator, limit=limit):
                made["critical_surfaces"] = [surface("datum_a", "datum", criteria=[FX.criterion("flatness", operator, limit, "mm")])]
                result = self.assess(documents)
                self.assertEqual(self.outcome(result, "process.laser.thermal@TST-0902/cut"), outcome)
                self.assertEqual(outcome == "fail", "process.laser.flatness" in self.codes(result))

    def test_calibrated_print_warp_at_a_strict_flatness_limit_fails_it(self) -> None:
        warp = calibration("warp_coupon", "warp", POLYMER, 0.1, 0.3, size=mm(15))
        for operator, limit, expected in (("<=", 0.3, []), ("<", 0.3, ["process.fdm.warp"]), ("<", 0.31, [])):
            with self.subTest(operator=operator, limit=limit):
                datum = [surface("datum_a", "datum", criteria=[FX.criterion("flatness", operator, limit, "mm")])]
                documents = printed("TST-0101", {"strategy": "none"}, calibrations=[CONTACT, warp], critical=datum)
                result = self.assess(documents, geometry("TST-0101", stable([2.0, 2.0], [[0, 0], [10, 0], [10, 12], [0, 12]])))
                self.assertEqual([c for c in self.codes(result) if c == "process.fdm.warp"], expected)

    def test_a_calibrated_milled_finish_at_a_strict_roughness_limit_fails_it_and_a_strict_lower_bound_is_refused(self) -> None:
        profile = FX.capability_profile("tst_mill", "cnc", parameters={"axes": known(mm(3, units="count"))},
                                        materials=[approval(ALU, operations=["machine"])],
                                        calibrations=[calibration("finish_study", "surface_roughness", ALU, 0.8, 3.2, "um")])
        obligation = "process.cnc.precision@TST-0201/mill"
        for operator, limit, outcome, finding in (("<=", 3.2, "pass", False), ("<", 3.2, "fail", True), ("<", 3.3, "pass", False),
                                                  (">", 1.0, "unknown", False), (">=", 1.0, "unknown", False)):
            with self.subTest(operator=operator, limit=limit):
                critical = [surface("pocket_floor", "seal", criteria=[FX.criterion("surface_roughness", operator, limit, "um")])]
                documents = FX.record(parts=[milled("TST-0201", [end_mill(4.0, 25.0)], critical=critical)],
                                      capabilities=[profile], materials=materials())
                result = self.assess(documents, geometry("TST-0201", POCKET))
                self.assertEqual(self.outcome(result, obligation), outcome)
                self.assertEqual("process.cnc.finish" in self.codes(result), finding)

    def test_a_strict_tab_slot_clearance_is_refused_never_read_as_a_minimum(self) -> None:
        # the worst case is 0.00..0.18 mm; each list is refused whole, so a supported criterion never hides a refused one
        for criteria in (["> -1"], ["< 1"], ["between -1 1", "> -1"], ["> -1", "between -1 1"], [">= -1", "< 1"]):
            with self.subTest(criteria=criteria):
                clearances = [_criterion("clearance", c, "mm") for c in criteria]
                documents = tab_slot(tab_thickness=mm(lower=2.95, upper=3.05), coating=None, calibrations=[KERF], clearances=clearances)
                result = self.assess(documents, geometry("TST-0301", SLOT))
                self.assertEqual(self.outcome(result, "process.laser.kerf@TST-0301/cut"), "unknown")
                self.assertNotIn("process.laser.tab_slot_fit", self.codes(result))

    def test_a_tab_slot_miss_on_a_supported_clearance_fails_beside_a_refused_one(self) -> None:
        clearances = [_criterion("clearance", "between 0 0.3", "mm"), _criterion("clearance", "> -1", "mm")]
        documents = tab_slot(tab_thickness=mm(lower=2.9, upper=3.1), coating=None, calibrations=[KERF], clearances=clearances)
        result = self.assess(documents, geometry("TST-0301", SLOT))
        self.assertEqual(self.outcome(result, "process.laser.kerf@TST-0301/cut"), "fail")

    def test_a_criterion_this_check_cannot_judge_never_lets_a_surface_pass(self) -> None:
        """Waterjet roughness and laser flatness: a calibration whose upper end is 3.2 um (0.3 mm) against `>` alone, a
        supported `<=` beside it, or `equals` is UNKNOWN, never PASS; a miss on a supported criterion still FAILs."""
        taper = calibration("taper_study", "taper", ALU, 0.0, 0.05)
        finish = calibration("finish_study", "edge_finish", ALU, 1.6, 3.2, "um")
        cases = {
            "only a strict lower bound": (["> 1.0"], "unknown"),
            "a lower bound": ([">= 1.0"], "unknown"),
            "met upper bound beside a strict lower bound": (["<= 5.0", "> 1.0"], "unknown"),
            "strict lower bound before a met upper bound": (["> 1.0", "<= 5.0"], "unknown"),
            "met upper bound beside equals": (["<= 5.0", "equals 3.2"], "unknown"),
            "missed strict upper bound beside a strict lower bound": (["> 1.0", "< 3.2"], "fail"),
            "met upper bound only": (["<= 5.0"], "pass"),
        }
        for name, (criteria, outcome) in cases.items():
            with self.subTest(name):
                edge = [surface("cut_edge", "seal", criteria=[_roughness(c) for c in criteria])]
                result = self.assess(shaped("waterjet", [taper, finish], critical=edge))
                self.assertEqual(self.outcome(result, "process.waterjet.edge@TST-0902/form"), outcome)
                self.assertEqual(outcome == "fail", "process.waterjet.edge_finish" in self.codes(result))
        flat = [calibration("flat_study", "flatness", ACRYLIC, 0.0, 0.3), calibration("haz_study", "heat_affected_zone", ACRYLIC, 0.0, 0.2)]
        documents = FX.record(parts=[cut("TST-0902")], materials=materials(),
                              capabilities=[laser_profile(ACRYLIC_APPROVAL, calibrations=[KERF, *flat])])
        made = documents["parts/TST-0902/part.yaml"]["intent"]
        for name, (criteria, outcome) in {"strict lower bound": (["> 0.1"], "unknown"), "met upper beside strict lower": (["<= 0.5", "> 0.1"], "unknown"),
                                          "missed strict upper beside strict lower": (["> 0.1", "< 0.3"], "fail")}.items():
            with self.subTest(f"laser {name}"):
                made["critical_surfaces"] = [surface("datum_a", "datum", criteria=[_flatness_criterion(c) for c in criteria])]
                result = self.assess(documents)
                self.assertEqual(self.outcome(result, "process.laser.thermal@TST-0902/cut"), outcome)
                self.assertEqual(outcome == "fail", "process.laser.flatness" in self.codes(result))

    def test_a_flatness_criterion_this_check_cannot_judge_never_lets_a_print_pass(self) -> None:
        warp = calibration("warp_coupon", "warp", POLYMER, 0.1, 0.3, size=mm(15))
        hull = [[0, 0], [10, 0], [10, 12], [0, 12]]
        cases = {"strict lower bound": (["> 0.1"], "unknown"), "met upper beside strict lower": (["<= 0.5", "> 0.1"], "unknown"),
                 "strict lower beside met upper": (["> 0.1", "<= 0.5"], "unknown"),
                 "missed strict upper beside strict lower": (["> 0.1", "< 0.3"], "fail"), "met upper only": (["<= 0.5"], "pass")}
        for name, (criteria, outcome) in cases.items():
            with self.subTest(name):
                datum = [surface("datum_a", "datum", criteria=[_flatness_criterion(c) for c in criteria])]
                documents = printed("TST-0101", {"strategy": "none"}, calibrations=[CONTACT, warp], critical=datum)
                result = self.assess(documents, geometry("TST-0101", stable([2.0, 2.0], hull)))
                self.assertEqual(self.outcome(result, "process.fdm.thermal_distortion@TST-0101/print"), outcome)

    def test_a_bore_is_judged_against_every_diameter_criterion(self) -> None:
        """A 6 mm bore, calibrated to print +0.0..+0.0 mm, against a diameter band and a strict bound in one list."""
        deviation = calibration("hole_study", "hole_diameter_deviation", POLYMER, 0.0, 0.0, orientation="XY")
        wall = calibration("wall_study", "min_wall", POLYMER, 0.8, 1.2)  # with a 3 mm wall, so only the bore can open the result
        small_hole = calibration("small_hole_study", "min_hole", POLYMER, 1.5, 2.0)  # the 6 mm bore clears it
        cases = {
            "strict lower bound at the diameter": (["> 6"], "fail"),
            "non-strict lower bound at the diameter": ([">= 6"], "pass"),
            "band that holds beside a strict lower bound at the diameter": (["between 5.9 6.1", "> 6"], "fail"),
            "strict lower bound at the diameter before a band that holds": (["> 6", "between 5.9 6.1"], "fail"),
            "strict upper bound at the diameter": (["< 6"], "fail"),
            "non-strict upper bound at the diameter": (["<= 6"], "pass"),
            "band that holds only": (["between 5.9 6.1"], "pass"),
            "band that holds beside equals": (["between 5.9 6.1", "equals 6"], "unknown"),
            "equals only": (["equals 6"], "unknown"),
        }
        for name, (criteria, outcome) in cases.items():
            with self.subTest(name):
                bore = [surface("bore", "locating", criteria=[_diameter_criterion(c) for c in criteria])]
                documents = printed("TST-0101", {"strategy": "none"}, calibrations=[CONTACT, deviation, wall, small_hole], critical=bore)
                result = self.assess(documents, geometry("TST-0101", facts(holes=(hole("bore", 6.0),), walls={"thickness_mm": 3.0})))
                codes = self.codes(result)
                self.assertEqual("process.fdm.hole_fit" in codes, outcome == "fail", codes)
                self.assertEqual(self.outcome(result, "process.fdm.geometry@TST-0101/print"), outcome)

    def test_a_tapped_bore_is_judged_against_every_engagement_criterion(self) -> None:
        """An 8 mm deep thread against an engagement minimum: `>` is strict, and every criterion of the list counts."""
        cases = {
            "met minimum": ([">= 8"], "pass"), "strict minimum at the depth": (["> 8"], "fail"), "strict minimum below": (["> 7"], "pass"),
            "missed minimum": ([">= 9"], "fail"), "met beside a missed strict one": ([">= 5", "> 8"], "fail"),
            "a missed strict one before a met one": (["> 8", ">= 5"], "fail"),
            "met beside a maximum": ([">= 5", "< 20"], "unknown"), "only a maximum": (["<= 20"], "unknown"),
            "missed beside a maximum": ([">= 9", "< 20"], "fail"),
        }
        for name, (criteria, outcome) in cases.items():
            with self.subTest(name):
                tap = FX.operation("tap", "tapping", "tst_tap", targets=[{"feature": "thread"}],
                                   criteria=[_criterion("thread_engagement", c, "mm") for c in criteria])
                documents = FX.record(parts=[part("TST-0902", [tap], process="cnc", material=ALU)],
                                      capabilities=[FX.capability_profile("tst_tap", "tapping")], materials=materials())
                result = self.assess(documents, geometry("TST-0902", facts(holes=(hole("thread", 6.0, depth=8.0),))))
                self.assertEqual(self.outcome(result, "process.tapping.engagement@TST-0902/tap"), outcome)
                self.assertEqual(outcome == "fail", "process.tapping.engagement" in self.codes(result))


def _criterion(prop: str, text: str, units: str) -> dict[str, Any]:
    """`"> 6"`, `"between 5.9 6.1"` or `"equals 6"` as a criterion on `prop`."""
    operator, *numbers = text.split()
    values = [float(n) for n in numbers]
    return FX.criterion(prop, operator, values if operator == "between" else values[0], units)


def _roughness(text: str) -> dict[str, Any]:
    return _criterion("surface_roughness", text, "um")


def _flatness_criterion(text: str) -> dict[str, Any]:
    return _criterion("flatness", text, "mm")


def _diameter_criterion(text: str) -> dict[str, Any]:
    return _criterion("diameter", text, "mm")


# --- contract -------------------------------------------------------------------------------------------------------

LENIENT = {"appearance_concept", "fit_assembly", "functional_demo"}  # the user's rule, written out apart from the code
NOZZLE_04 = {"nozzle_diameter": known(mm(0.4), "supplier_typical", [datasheet("TST printer datasheet")])} if DEPS else {}
GEOMETRY = "process.fdm.geometry@TST-0101/print"
SUPPORT = "process.fdm.support@TST-0101/print"


def guided(*, service: str | None = None, calibrations: tuple = (), parameters: dict | None = None,
           dispositions: tuple = (), supports: Any = None, pin: tuple = ()) -> dict[str, Any]:
    """A printed part on a 0.4 mm nozzle profile (unless `parameters` say otherwise) whose limits come from the shipped
    guideline sets unless `calibrations` hold them, with support strategy none and the use case's `service` class."""
    route = [FX.operation("print", "fdm", "tst_printer", output_state="printed", orientation=copy.deepcopy(APPROVED),
                          supports=supports or {"strategy": "none"})]
    profile = FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE, calibrations=list(calibrations),
                                    parameters=copy.deepcopy(NOZZLE_04 if parameters is None else parameters),
                                    **({"guidelines": list(pin)} if pin else {}))
    use = FX.use_case(**({"service_class": known(service)} if service else {}))
    documents = FX.record(parts=[part("TST-0101", route, process="fdm")], capabilities=[profile], materials=materials(),
                          product_data=FX.product(use), decisions=("DR-001", "DR-002"))
    if dispositions:
        documents["manufacturing/findings.yaml"] = {"dispositions": list(dispositions)}
    return documents


def walled(thickness: float, *, pins: tuple = (), holes: tuple = ()) -> dict[str, Any]:
    data = facts(holes=tuple(hole(None, d) for d in holes),
                 walls={"thickness_mm": thickness, "location_mm": [1.0, 1.0, 1.0], "features": [], "measured": 12})
    data["pins"] = [{"diameter_mm": d, "features": []} for d in pins]
    return data


def bed_face() -> dict[str, Any]:
    return face("f1", bed=True, normals=((0, 0, -1),))


def underside(angle: float, kind: str = "overhang", span: float | None = None) -> dict[str, Any]:
    """A part whose one down-facing face, 200 mm2, is `angle` deg from vertical (a bridge of `span` mm when given)."""
    return facts(faces=(bed_face(), face("f10", features=("underside",), down_=down(angle, 200.0, kind, span=span))),
                 walls={"thickness_mm": 3.0, "location_mm": [1.0, 1.0, 1.0], "features": [], "measured": 12})


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class GuidelineLimitTests(Checks):
    """Without a calibration the FDM checks judge against the shipped guideline sets, and a guideline pass counts only
    for the appearance_concept, fit_assembly and functional_demo service classes."""

    def judged(self, data: dict[str, Any], **options: Any) -> Any:
        return self.assess(guided(**options), geometry("TST-0101", data), catalog=SHIPPED)

    def test_a_guideline_limit_fails_a_part_beyond_it_and_cites_the_set(self) -> None:
        cases = {  # name: (facts, obligation, code, operator, limit, units, guideline set)
            "wall 1.0 mm against 3 x 0.4 mm": (walled(1.0), GEOMETRY, "process.fdm.wall", ">=", 1.2, "mm", "fdm.nozzle.d0_4"),
            "pin 1.5 mm": (walled(3.0, pins=(1.5,)), GEOMETRY, "process.fdm.small_feature", ">=", 2.0, "mm", "fdm.generic"),
            "hole 1.5 mm": (walled(3.0, holes=(1.5,)), GEOMETRY, "process.fdm.small_hole", ">=", 2.0, "mm", "fdm.generic"),
            "overhang 60 deg": (underside(60.0), SUPPORT, "process.fdm.overhang", "<=", 45.0, "deg", "fdm.generic"),
            "bridge 6 mm": (underside(90.0, "bridge", 6.0), SUPPORT, "process.fdm.overhang", "<", 5.0, "mm", "fdm.generic"),
            "bridge at the strict 5 mm": (underside(90.0, "bridge", 5.0), SUPPORT, "process.fdm.overhang", "<", 5.0, "mm",
                                          "fdm.generic"),
        }
        for name, (data, obligation, code, operator, value, units, ref) in cases.items():
            with self.subTest(name):
                result = self.judged(data)
                self.assertEqual(self.outcome(result, obligation), "fail")
                criterion = self.finding(result, code)["criterion"]
                self.assertEqual((criterion["operator"], criterion["value"], criterion["units"]), (operator, value, units))
                self.assertEqual((criterion["source"]["kind"], criterion["source"]["reference"], criterion["source"]["revision"],
                                  criterion["source"]["sha256"]), ("guideline", ref, str(SHIPPED.guidelines[ref].revision), SHIPPED.guidelines[ref].sha256))

    def test_a_guideline_pass_counts_only_for_three_service_classes(self) -> None:
        cases = {"wall": (walled(3.0), GEOMETRY), "small_feature": (walled(3.0, pins=(3.0,)), GEOMETRY),
                 "small_hole": (walled(3.0, holes=(3.0,)), GEOMETRY), "support": (underside(30.0), SUPPORT)}
        for service in MI.SERVICE_CLASSES:
            for check, (data, obligation) in cases.items():
                with self.subTest(service=service, check=check):
                    result = self.judged(data, service=service)
                    only = f"process.fdm.{check}_guideline_only"
                    if service in LENIENT:
                        self.assertEqual(self.outcome(result, obligation), "pass")
                        self.assertNotIn(only, self.codes(result))
                    else:
                        self.assertEqual(self.outcome(result, obligation), "unknown")
                        self.assertEqual(self.finding(result, only)["criterion"]["source"]["kind"], "guideline")

    def test_an_unknown_service_class_takes_no_guideline_pass(self) -> None:
        result = self.judged(walled(3.0))
        self.assertEqual(self.outcome(result, GEOMETRY), "unknown")
        self.assertIn("process.fdm.wall_guideline_only", self.codes(result))

    def test_a_guideline_fail_fails_in_every_service_class(self) -> None:
        for service in (*MI.SERVICE_CLASSES, None):
            with self.subTest(service=service):
                self.assertEqual(self.outcome(self.judged(walled(1.0), service=service), GEOMETRY), "fail")

    def test_a_calibration_beats_the_guideline(self) -> None:
        """1.1 mm misses the guideline's 1.2 mm but meets the 0.8..1.0 mm calibration: the calibration decides."""
        wall = calibration("wall_study", "min_wall", POLYMER, 0.8, 1.0)
        result = self.judged(walled(1.1), calibrations=(wall,))
        self.assertEqual(self.outcome(result, GEOMETRY), "pass")
        self.assertNotIn("process.fdm.wall_guideline_only", self.codes(result))

    def test_a_refused_calibration_lets_the_guideline_apply_and_keeps_its_reasons(self) -> None:
        other = calibration("wall_study", "min_wall", ALU, 0.4, 0.5)
        result = self.judged(walled(1.0), calibrations=(other,))
        self.assertEqual(self.outcome(result, GEOMETRY), "fail")
        criterion = self.finding(result, "process.fdm.wall")["criterion"]
        self.assertEqual((criterion["value"], criterion["source"]["kind"]), (1.2, "guideline"))
        self.assertIn("wall_study is for tst_alu", criterion["conditions"])

    def test_a_profile_without_nozzle_or_line_width_judges_the_wall_by_the_floor_and_says_so(self) -> None:
        passed = self.judged(walled(1.0), parameters={}, service="functional_demo")
        self.assertEqual(self.outcome(passed, GEOMETRY), "pass")
        self.assertIn("the profile states no nozzle or line width", passed.outcome(GEOMETRY)["coverage"])
        failed = self.judged(walled(0.7), parameters={})
        self.assertEqual(self.finding(failed, "process.fdm.wall")["criterion"]["value"], 0.8)

    def test_without_a_catalog_only_calibrations_count(self) -> None:
        result = self.assess(guided(service="functional_demo"), geometry("TST-0101", walled(3.0)))
        self.assertEqual(self.outcome(result, GEOMETRY), "unknown")
        self.assertEqual(self.finding(result, "process.fdm.wall_uncalibrated")["criterion"]["status"], "unknown")

    def test_the_packet_depends_on_every_guideline_set_its_limits_can_come_from(self) -> None:
        packet = self.judged(walled(3.0)).packet({"version_manifest": "0" * 64})
        self.assertEqual(packet_errors(packet), [])
        for ref, key in (("fdm.generic", "guideline_fdm_generic_r2"), ("fdm.nozzle.d0_4", "guideline_fdm_nozzle_d0_4_r1")):
            self.assertEqual(packet["dependencies"][key], SHIPPED.guidelines[ref].sha256)
        self.assertNotIn("guideline_fdm_nozzle_d0_6_r1", packet["dependencies"], "a 0.6 mm set cannot apply to a 0.4 mm nozzle")

    def test_the_guideline_only_note_is_stated_once_when_an_overhang_and_a_bridge_are_both_judged(self) -> None:
        strict = sorted(set(MI.SERVICE_CLASSES) - LENIENT)[0]
        data = facts(faces=(bed_face(), face("f10", features=("underside",), down_=down(60.0, 200.0)),
                            face("f11", features=("bridge",), down_=down(90.0, 200.0, "bridge", span=4.0))),
                     walls={"thickness_mm": 3.0, "location_mm": [1.0, 1.0, 1.0], "features": [], "measured": 12})
        planned = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "underside"}], "removal": REMOVAL}
        coverage = self.judged(data, service=strict, supports=planned).outcome(SUPPORT)["coverage"]
        self.assertEqual(coverage.count("(guideline value only)"), 1, coverage)

    def test_the_probe_plan_scans_walls_at_the_guideline_minimum(self) -> None:
        record = load(self, guided())
        self.assertEqual(pc.probe_plan(record, "TST-0101", "main", today=TODAY, catalog=SHIPPED)["wall_mm"], 1.2)
        self.assertIsNone(pc.probe_plan(record, "TST-0101", "main", today=TODAY)["wall_mm"])

    def test_a_value_equal_to_a_non_strict_limit_passes_and_a_bridge_inside_the_strict_one_passes(self) -> None:
        cases = {"wall at 1.2 mm": (walled(1.2), GEOMETRY), "overhang at 45 deg": (underside(45.0), SUPPORT),
                 "pin at 2.0 mm": (walled(3.0, pins=(2.0,)), GEOMETRY), "hole at 2.0 mm": (walled(3.0, holes=(2.0,)), GEOMETRY),
                 "bridge of 4.9 mm under the strict 5 mm": (underside(90.0, "bridge", 4.9), SUPPORT)}
        for name, (data, obligation) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.outcome(self.judged(data, service="functional_demo"), obligation), "pass")

    def test_planned_supports_keep_the_guideline_grade(self) -> None:
        planned = {"strategy": "reachable_noncritical", "contact_regions": [{"feature": "underside"}], "removal": REMOVAL}
        strict = sorted(set(MI.SERVICE_CLASSES) - LENIENT)[0]
        for service, outcome in (("functional_demo", "pass"), (strict, "unknown")):
            with self.subTest(service=service):
                result = self.judged(underside(60.0), service=service, supports=planned)
                self.assertEqual(self.outcome(result, SUPPORT), outcome)
                self.assertEqual("process.fdm.support_guideline_only" in self.codes(result), outcome == "unknown")

    def test_an_unknown_recorded_limit_grades_unknown_once_without_a_guideline_only_finding(self) -> None:
        """No catalog and no calibration: the overhang limit is unknown, and a flat down-facing face (nothing beyond
        any limit) still reaches the support grade, which says so once and files no guideline-only finding."""
        result = self.assess(guided(service="functional_demo"), geometry("TST-0101", underside(0.0)))
        self.assertEqual(self.outcome(result, SUPPORT), "unknown")
        self.assertEqual(result.outcome(SUPPORT)["coverage"].count("(limit unknown)"), 1, result.outcome(SUPPORT)["coverage"])
        self.assertEqual([c for c in self.codes(result) if c.endswith("_guideline_only")], [])

    def test_a_resolver_warning_reaches_the_finding_that_cites_the_limit(self) -> None:
        result = self.judged(walled(1.0), pin=("fdm.generic@7",))
        conditions = self.finding(result, "process.fdm.wall")["criterion"]["conditions"]
        self.assertIn("pinned fdm.generic@7, but the catalog has revision 2: not applied", conditions)

    def test_a_refused_guideline_file_surfaces_as_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "manufacturing" / "guidelines"
            folder.mkdir(parents=True)
            (folder / "garbled.yaml").write_text("id: [unclosed\n", encoding="utf-8")
            catalog = GL.discover(Path(tmp))
        self.assertEqual([r.path for r in catalog.refused], ["manufacturing/guidelines/garbled.yaml"])
        result = self.assess(guided(service="functional_demo"), geometry("TST-0101", walled(3.0)), catalog=catalog)
        found = self.finding(result, "process.guideline_refused")
        self.assertIn("manufacturing/guidelines/garbled.yaml is refused", found["measured"]["text"])
        self.assertEqual(self.outcome(result, GEOMETRY), "pass")
        clean = self.judged(walled(3.0), service="functional_demo")
        self.assertNotIn("process.guideline_refused", self.codes(clean))

    def test_the_packet_cites_the_resolution_trace_on_findings_that_used_a_guideline(self) -> None:
        result = pc.assess(load(self, guided()), geometry("TST-0101", walled(1.0)), today=TODAY, catalog=SHIPPED)
        row = {"path": "hw/checks/v1/process/guideline-trace-x.json", "sha256": "c" * 64}
        packet = result.packet({"version_manifest": "0" * 64}, trace=row)
        self.assertEqual(packet_errors(packet), [])
        wall = next(f for f in packet["findings"] if f["code"] == "process.fdm.wall")
        self.assertIn(row, wall["method"]["evidence"])
        self.assertLessEqual({"min_wall"}, {r.characteristic for r in result.resolutions})

    def test_a_resin_part_with_a_hole_is_judged_as_before(self) -> None:
        """Resin shares the geometry handler but has no guideline sets: a hole adds no min_hole finding, as on main."""
        wall = calibration("wall_study", "min_wall", POLYMER, 0.8, 1.2)
        outcomes = []
        for holes in ((), (1.0,)):
            result = self.assess(shaped("resin", [wall], material=POLYMER), geometry("TST-0902", walled(3.0, holes=holes)),
                                 catalog=SHIPPED)
            outcomes.append((self.outcome(result, "process.resin.geometry@TST-0902/form"), self.codes(result)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual([c for c in outcomes[1][1] if "small_hole" in c], [])

    def test_the_guideline_pass_classes_are_known_service_classes(self) -> None:
        self.assertLessEqual(set(pc.GUIDELINE_PASS_CLASSES), set(MI.SERVICE_CLASSES) - {"unknown"})

    def test_an_approved_waiver_of_the_guideline_only_finding_waives_the_obligation(self) -> None:
        evidence = _module("cad_record_evidence", SKILLS / "cad-record" / "scripts" / "evidence.py")
        acceptance = _module("cad_record_acceptance_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "acceptance.py")
        found = self.finding(self.judged(walled(3.0)), "process.fdm.wall_guideline_only")
        waiver = acceptance.waiver(found["subject"], obligation=GEOMETRY)
        disposition = {"finding": found["id"], "producer": {"bundle": "hw/checks/v1/process", "manifest_sha256": "b" * 64},
                       "status": "accepted_waiver", "reason": "TST", "decision": "DR-002", "waiver": waiver}
        documents = guided(dispositions=(disposition,))
        record = load(self, documents)
        packet = pc.assess(record, geometry("TST-0101", walled(3.0)), today=TODAY, catalog=SHIPPED).packet(
            {"version_manifest": "0" * 64})
        result = evidence.assess(record, version="v1", manifest_sha256="0" * 64, today=TODAY,
                                 packets=[evidence.ProducerPacket("hw/checks/v1/process", "b" * 64, packet)],
                                 approved=lambda decision: decision in ("DR-001", "DR-002"))
        self.assertEqual(result.statuses[GEOMETRY].result, "waived")


TPU_WALL_SET = """\
schema_version: 1
id: fdm.test.tpu_wall
revision: 1
title: A project wall for tpu
scope:
  process: fdm
  material_family: [tpu]
limits:
  - characteristic: min_wall
    operator: ">="
    quantity: {value: 2, units: mm}
    citations:
      - {kind: datasheet, reference: Synthetic fixture, url: "https://example.invalid/guide", accessed: "2026-01-01"}
"""


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class LegacyWallLimitTests(Checks):
    """form_check's legacy min_wall diagnostic reads the resolved guideline value, and a product.yaml override is a
    project policy with its own source."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.form_check = load_module("tst_form_check_walls", FORM_CHECK)

    def limit(self, documents: dict[str, Any], process: str | None = "fdm", overrides: dict | None = None,
              catalog: Any = SHIPPED) -> Any:
        record = load(self, documents) if isinstance(documents, dict) else documents  # a Record: built unchecked
        return self.form_check.wall_limit(pc, record, "TST-0101", process, catalog, overrides or {})

    def test_an_fdm_part_without_a_catalog_has_an_unknown_limit_not_a_skipped_scan(self) -> None:
        limit = self.limit(guided(), catalog=None)
        self.assertEqual((limit["required_mm"], limit["status"]), (None, "unknown"))
        self.assertTrue(limit["reasons"])

    def test_an_fdm_part_on_a_0_4_mm_nozzle_takes_three_line_widths(self) -> None:
        limit = self.limit(guided())
        self.assertEqual((limit["required_mm"], limit["status"], limit["guideline"]), (1.2, "guideline", "fdm.nozzle.d0_4@1"))
        self.assertEqual((limit["source"]["kind"], limit["source"]["sha256"]), ("guideline", SHIPPED.guidelines["fdm.nozzle.d0_4"].sha256))
        self.assertNotIn("note", limit)

    def test_without_a_nozzle_or_line_width_the_floor_applies_and_says_so(self) -> None:
        limit = self.limit(guided(parameters={}))
        self.assertEqual((limit["required_mm"], limit["guideline"]), (0.8, "fdm.generic@2"))
        self.assertIn("no nozzle or line width", limit["note"])

    def test_the_wall_diagnostic_and_the_process_checks_agree_on_every_limit_they_cannot_state(self) -> None:
        """A part with no capability profile (so no material scope either) has no minimum wall in the process checks'
        probe plan, so the diagnostic must not invent the 0.8 mm floor: an unknown is never a default."""
        bare = FX.record(parts=[part("TST-0101", [FX.operation("print", "fdm")], process="fdm", material=None)],
                         materials=materials())
        for name, documents in (("no capability profile, material unknown", bare), ("0.4 mm nozzle", guided()),
                                ("profile without nozzle", guided(parameters={}))):
            with self.subTest(name):
                planned = pc.probe_plan(load(self, documents), "TST-0101", "main", today=TODAY, catalog=SHIPPED)["wall_mm"]
                self.assertEqual(self.limit(documents)["required_mm"], planned)

    def test_a_later_operation_with_a_valid_profile_gives_the_wall_limit_the_probe_plan_scans_at(self) -> None:
        """The first fdm operation names a capability the record lacks, or a profile of another process; a later one
        names the 0.4 mm printer. The scan runs at 1.2 mm, and so must the diagnostic."""
        for name, first in (("a missing profile", "tst_missing"), ("a milling profile", "tst_mill")):
            with self.subTest(name):
                record = MI.normalize_record(self.after_a_broken_operation(first))  # the schema refuses this first operation
                planned = pc.probe_plan(record, "TST-0101", "main", today=TODAY, catalog=SHIPPED)["wall_mm"]
                self.assertEqual((planned, self.limit(record)["required_mm"]), (1.2, 1.2))

    def test_the_unknown_wall_limit_says_whether_the_operation_or_its_profile_is_missing(self) -> None:
        unnamed = FX.record(parts=[part("TST-0101", [FX.operation("print", "fdm")], process="fdm")], materials=materials())
        cases = {"no capability named": (MI.normalize_record(unnamed), "names no capability"),
                 "a missing capability": (MI.normalize_record(self.after_a_broken_operation("tst_missing", valid=False)),
                                          "tst_missing, which the record does not hold"),
                 "a capability of another process": (MI.normalize_record(self.after_a_broken_operation("tst_mill", valid=False)),
                                                     "tst_mill is a cnc capability")}
        for name, (record, reason) in cases.items():
            with self.subTest(name):
                limit = self.limit(record)
                self.assertEqual((limit["required_mm"], limit["status"]), (None, "unknown"))
                self.assertIn(reason, " ".join(limit["reasons"]))

    def test_the_stock_material_of_the_setup_scopes_the_wall_limit_in_both_paths(self) -> None:
        """A project set asks 2 mm of tpu. The part's own material is not tpu, but the print setup names tpu stock,
        which the process checks read, so the diagnostic must too."""
        stock = FX.material_profile("tst_tpu_stock", form="printed", material_family=known("tpu"))
        route = [FX.operation("print", "fdm", "tst_printer", output_state="printed", orientation=copy.deepcopy(APPROVED),
                              supports={"strategy": "none"}, setup={"stock": {"material_profile": "tst_tpu_stock"}})]
        profile = FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE, parameters=copy.deepcopy(NOZZLE_04))
        documents = FX.record(parts=[part("TST-0101", route, process="fdm")], capabilities=[profile],
                              materials=materials(stock), product_data=FX.product(FX.use_case()), decisions=("DR-001", "DR-002"))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "manufacturing" / "guidelines"
            folder.mkdir(parents=True)
            (folder / "tpu_wall.yaml").write_text(TPU_WALL_SET, encoding="utf-8")
            catalog = GL.discover(Path(tmp))
        self.assertEqual(list(catalog.refused), [])
        planned = pc.probe_plan(load(self, documents), "TST-0101", "main", today=TODAY, catalog=catalog)["wall_mm"]
        self.assertEqual((planned, self.limit(documents, catalog=catalog)["required_mm"]), (2.0, 2.0))

    def test_a_part_with_declared_components_takes_the_limit_of_the_operation_that_names_its_component(self) -> None:
        components = [{"id": "body", "witness": [20.0, 10.0, 5.0]}, {"id": "tab", "witness": [65.0, 2.5, 1.0]}]
        route = [FX.operation("print", "fdm", "tst_printer", output_state="printed", orientation=copy.deepcopy(APPROVED),
                              supports={"strategy": "none"}, components=["body"])]
        profile = FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE, parameters=copy.deepcopy(NOZZLE_04))
        documents = FX.record(parts=[part("TST-0101", route, process="fdm", components=components)], capabilities=[profile],
                              materials=materials(), product_data=FX.product(FX.use_case()), decisions=("DR-001", "DR-002"))
        planned = pc.probe_plan(load(self, documents), "TST-0101", "body", today=TODAY, catalog=SHIPPED)["wall_mm"]
        limit = self.limit(documents)
        self.assertEqual((planned, limit["required_mm"], limit["status"]), (1.2, 1.2, "guideline"))

    def test_an_operation_that_acts_on_no_component_of_the_part_is_named_as_such(self) -> None:
        components = [{"id": "body", "witness": [20.0, 10.0, 5.0]}]
        route = [FX.operation("print", "fdm", "tst_printer", orientation=copy.deepcopy(APPROVED),
                              supports={"strategy": "none"}, components=["ghost"])]
        profile = FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE, parameters=copy.deepcopy(NOZZLE_04))
        record = MI.normalize_record(FX.record(parts=[part("TST-0101", route, process="fdm", components=components)],
                                               capabilities=[profile], materials=materials()))  # the schema refuses "ghost"
        limit = self.limit(record)
        self.assertEqual((limit["required_mm"], limit["status"]), (None, "unknown"))
        self.assertIn("none of its 1 route operations acts on a component of TST-0101 (body)", " ".join(limit["reasons"]))

    def test_the_largest_guideline_wall_of_the_scoped_operations_applies_whatever_their_order(self) -> None:
        """A 0.4 mm nozzle asks 1.2 mm and a 0.6 mm nozzle 1.8 mm. The probe plan scans at the largest minimum of any
        operation, so the diagnostic does too. (It stays a guideline diagnostic: it reads neither calibrations, which the
        process checks' wall obligation judges, nor min_web.)"""
        nozzle_06 = {"nozzle_diameter": known(mm(0.6), "supplier_typical", [datasheet("TST printer datasheet")])}
        for name, order in (("0.4 mm first", ("tst_fine", "tst_coarse")), ("0.6 mm first", ("tst_coarse", "tst_fine"))):
            with self.subTest(name):
                route = [FX.operation(f"print_{i}", "fdm", cap, output_state="printed", orientation=copy.deepcopy(APPROVED),
                                      supports={"strategy": "none"}) for i, cap in enumerate(order)]
                profiles = [FX.capability_profile("tst_fine", "fdm", envelope=ENVELOPE, parameters=copy.deepcopy(NOZZLE_04)),
                            FX.capability_profile("tst_coarse", "fdm", envelope=ENVELOPE, parameters=nozzle_06)]
                documents = FX.record(parts=[part("TST-0101", route, process="fdm")], capabilities=profiles,
                                      materials=materials(), product_data=FX.product(FX.use_case()),
                                      decisions=("DR-001", "DR-002"))
                planned = pc.probe_plan(load(self, documents), "TST-0101", "main", today=TODAY, catalog=SHIPPED)["wall_mm"]
                self.assertEqual((planned, self.limit(documents)["required_mm"]), (1.8, 1.8))

    def test_a_part_name_the_record_does_not_hold_has_an_unknown_wall_limit_not_a_traceback(self) -> None:
        limit = self.form_check.wall_limit(pc, load(self, guided()), "TST-9999", "fdm", SHIPPED, {})
        self.assertEqual((limit["required_mm"], limit["status"]), (None, "unknown"))
        self.assertIn("TST-9999 is not a part of the record", " ".join(limit["reasons"]))

    @staticmethod
    def after_a_broken_operation(first: str, *, valid: bool = True) -> dict[str, Any]:
        """A print route whose first fdm operation names capability `first` (absent from the record, or a cnc profile),
        followed, when `valid`, by one that names the 0.4 mm printer."""
        route = [FX.operation("pre", "fdm", first, output_state="printed", orientation=copy.deepcopy(APPROVED),
                              supports={"strategy": "none"})]
        if valid:
            route.append(FX.operation("print", "fdm", "tst_printer", output_state="printed",
                                      orientation=copy.deepcopy(APPROVED), supports={"strategy": "none"}))
        capabilities = [FX.capability_profile("tst_mill", "cnc")]
        if valid:
            capabilities.append(FX.capability_profile("tst_printer", "fdm", envelope=ENVELOPE, parameters=copy.deepcopy(NOZZLE_04)))
        return FX.record(parts=[part("TST-0101", route, process="fdm")], capabilities=capabilities, materials=materials(),
                         product_data=FX.product(FX.use_case()), decisions=("DR-001", "DR-002"))

    def test_an_override_is_a_project_policy_that_warns_when_looser(self) -> None:
        looser = self.limit(guided(), overrides={"fdm": 1.0})
        self.assertEqual((looser["required_mm"], looser["status"], looser["source"]["kind"]), (1.0, "override", "policy"))
        self.assertEqual(looser["source"]["document"], "hw/product.yaml#/manufacturing/min_wall_mm/fdm")
        self.assertEqual(looser["resolved"]["required_mm"], 1.2)
        self.assertEqual(looser["warnings"],
                         ["manufacturing.min_wall_mm.fdm 1 mm is looser than the resolved 1.2 mm (fdm.nozzle.d0_4@1)"])
        self.assertNotIn("warnings", self.limit(guided(), overrides={"fdm": 1.5}))

    def test_a_process_without_a_guideline_minimum_has_none_unless_overridden(self) -> None:
        self.assertIsNone(self.limit(guided(), process="purchased"))
        self.assertIsNone(self.limit(guided(), process="cnc"))
        self.assertEqual(self.limit(guided(), process="cnc", overrides={"cnc": 1.0})["required_mm"], 1.0)

    def test_the_producer_code_changes_with_the_guideline_resolver(self) -> None:
        stub = SimpleNamespace(code_sha256=lambda: "0" * 64)
        before = self.form_check.producer_code(stub)
        with tempfile.TemporaryDirectory() as tmp:
            changed = Path(tmp) / "guidelines.py"
            changed.write_bytes(self.form_check.GUIDELINES_PATH.read_bytes() + b"\n# changed\n")
            with mock.patch.object(self.form_check, "GUIDELINES_PATH", changed):
                self.assertNotEqual(self.form_check.producer_code(stub), before)


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class GuidelineTraceTests(Checks):
    """form_check writes the guideline resolution trace into the bundle's evidence folder."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.form_check = load_module("tst_form_check_trace", FORM_CHECK)

    def test_the_trace_is_written_under_evidence_and_named_by_its_hash(self) -> None:
        result = pc.assess(load(self, guided()), geometry("TST-0101", walled(1.0)), today=TODAY, catalog=SHIPPED)
        with tempfile.TemporaryDirectory() as tmp:
            row = self.form_check.trace_row(pc, result, Path(tmp), "hw/checks/v1/process/evidence")
            written = Path(tmp) / Path(row["path"]).name
            self.assertEqual(row["sha256"], hashlib.sha256(written.read_bytes()).hexdigest())
            self.assertTrue(row["path"].startswith("hw/checks/v1/process/evidence/guideline-trace-"))
            self.assertIn("min_wall", [r["characteristic"] for r in json.loads(written.read_text())["resolutions"]])
        bare = pc.assess(load(self, guided()), {}, today=TODAY)
        self.assertIsNone(self.form_check.trace_row(pc, bare, Path(tmp), "x"))


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class DispatchContractTests(Checks):
    """The closed table answers every route kind's families except physical evidence; a record of an unsupported version is
    not assessable; a handler cannot link a failing finding to an obligation it did not fail."""

    def test_the_table_covers_every_check_family_but_physical_evidence(self) -> None:
        for kind, checks in MI.PROCESS_CHECKS.items():
            with self.subTest(kind=kind):
                wanted = {family for family, level, _ in checks if level != "physically_validated"}
                self.assertEqual(set(pc.TABLE[kind]), wanted)
        self.assertEqual(set(pc.TABLE), set(MI.PROCESS_CHECKS))

    def test_a_record_of_an_unsupported_version_is_not_assessable(self) -> None:
        documents = FX.tst_record()
        del documents["product.yaml"]["record_version"]
        record = MI.normalize_record(documents)
        self.assertEqual(record.version, 0)
        with self.assertRaisesRegex(pc.NotAssessable, "not supported"):
            pc.expected(record)

    def test_the_fixture_record_assesses_every_owned_obligation_without_geometry(self) -> None:
        result = self.assess(FX.tst_record())
        self.assertTrue(result.expected)
        geometric = [o for o in result.outcomes if o["level"] == "geometrically_manufacturable"]
        self.assertTrue(geometric and all(o["outcome"] != "pass" or "purchase" in o["obligation"] for o in geometric))
        self.assertNotIn("pass", {o["outcome"] for o in result.outcomes if o["obligation"].startswith("process.fdm.")})


class OutcomeOrderTests(unittest.TestCase):
    """D37: fail > unknown > not_assessed > pass at check, part and packet level; not_applicable drops out; a stray
    outcome is refused, never counted as a pass."""

    @staticmethod
    def combined(*outcomes: str) -> str:
        return pc.core.combine("m", [(o, "c") for o in outcomes], []).outcome

    def test_combine_takes_the_worst_outcome_of_each_pair_in_the_documented_order(self) -> None:
        order = ["fail", "unknown", "not_assessed", "pass"]
        for worse, better in itertools.combinations(order, 2):
            for parts in ((worse, better), (better, worse)):
                with self.subTest(parts=parts):
                    self.assertEqual(self.combined(*parts), worse)

    def test_combine_lets_not_applicable_drop_out_beside_not_assessed(self) -> None:
        self.assertEqual(self.combined("not_applicable", "not_assessed"), "not_assessed")
        self.assertEqual(self.combined("not_applicable", "not_applicable"), "not_applicable")

    def test_combine_refuses_an_outcome_it_does_not_know(self) -> None:
        for stray in ("no_limit", "Pass", "waived", ""):
            with self.subTest(stray=stray), self.assertRaisesRegex(ValueError, "not a check outcome"):
                self.combined("pass", stray)

    def test_combine_keeps_the_reason_only_on_a_not_assessed_result(self) -> None:
        kept = pc.core.combine("m", [("pass", ""), ("not_assessed", "")], [], reason="calibration_off")
        dropped = pc.core.combine("m", [("pass", ""), ("unknown", "")], [], reason="calibration_off")
        self.assertEqual((kept.outcome, kept.reason, dropped.outcome, dropped.reason),
                         ("not_assessed", "calibration_off", "unknown", None))

    @staticmethod
    def assessment(outcomes: list[str], expected: int | None = None) -> Any:
        rows = tuple({"obligation": f"o{i}", "outcome": o} for i, o in enumerate(outcomes))
        wanted = tuple(SimpleNamespace(id=f"o{i}") for i in range(len(outcomes) if expected is None else expected))
        return pc.Assessment(wanted, rows, ())

    def test_the_packet_result_takes_the_worst_outcome_in_the_documented_order(self) -> None:
        for outcomes, result in ((["pass", "not_assessed"], "not_assessed"), (["not_assessed", "unknown"], "unknown"),
                                 (["fail", "not_assessed", "unknown"], "fail"), (["pass", "not_applicable"], "pass"),
                                 (["pass", "not_assessed", "not_applicable"], "not_assessed")):
            with self.subTest(outcomes=outcomes):
                self.assertEqual(self.assessment(outcomes).result, result)

    def test_an_incomplete_packet_stays_unknown_beside_not_assessed(self) -> None:
        incomplete = self.assessment(["not_assessed", "pass"], expected=3)
        self.assertFalse(incomplete.complete)
        self.assertEqual(incomplete.result, "unknown")


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class NotAssessedSerializationTests(Checks):
    """A not_assessed result carries its reason onto the serialized outcome and finding, where the schema requires it."""

    def assess_with(self, result: Any) -> Any:
        with mock.patch.object(pc.dispatch, "_result", return_value=result):
            return pc.assess(load(self, FX.tst_record()), {}, today=TODAY)

    def not_assessed(self, reason: str | None) -> Any:
        core = pc.core
        draft = core.Draft("process.fdm.overhang", "info", "not_assessed", core.text("not measured"),
                           core.policy("overhang_angle", "equals", "a calibrated limit"), "calibration check",
                           "calibration is off", "the overhang is not assessed", reason=reason)
        return core.Result("not_assessed", "calibration check", "nothing assessed", [draft], reason=reason)

    def test_the_reason_reaches_the_outcome_the_finding_and_a_valid_packet(self) -> None:
        assessed = self.assess_with(self.not_assessed("calibration_off"))
        self.assertEqual({o["outcome"] for o in assessed.outcomes}, {"not_assessed"})
        self.assertEqual({o["reason"] for o in assessed.outcomes}, {"calibration_off"})
        self.assertTrue(assessed.findings)
        self.assertEqual({(f["outcome"], f["reason"]) for f in assessed.findings}, {("not_assessed", "calibration_off")})
        packet = assessed.packet({"version_manifest": "0" * 64})
        self.assertEqual((packet["result"], packet_errors(packet)), ("not_assessed", []))

    def test_a_not_assessed_result_without_a_reason_is_a_handler_error(self) -> None:
        with self.assertRaisesRegex(pc.HandlerError, "needs a reason"):
            self.assess_with(self.not_assessed(None))

    def test_a_reason_on_another_outcome_is_a_handler_error(self) -> None:
        result = pc.core.Result("unknown", "m", "c", reason="calibration_off")
        with self.assertRaisesRegex(pc.HandlerError, "belongs to a not_assessed outcome"):
            self.assess_with(result)


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

    def test_a_flat_roof_is_judged_by_its_shape_on_real_geometry(self) -> None:
        for case, expected in (
            ("slot", ("bridge", 4.0)),  # 4 x 30, held on all four sides
            ("beam_long", ("bridge", 4.0)),  # 4 x 30, held on the two long sides
            ("beam_three", ("bridge", 4.0)),  # 4 x 28, held on both long sides and one end
            ("beam_ends", ("bridge", 30.0)),  # 4 x 30, held on the two short ends only: the bridge runs along it
            ("beam_corner", ("overhang", None)),  # 18 x 18, held on two adjacent sides only
            ("beam_split_long", ("bridge", 4.0)),  # 4 x 30, long sides each two contiguous edges, ends free
            ("beam_split_four", ("bridge", 4.0)),  # 4 x 26, long sides each two contiguous edges, ends held
            ("pocket_notch", ("bridge", None)),  # 4 x 30 with a 3 x 2 notch in a short end: 95% of the strip
            ("pocket_stadium", ("bridge", 4.0)),  # 4 x 30 with round ends
            ("pocket_hole", ("bridge", None)),  # 4 x 30 with a vertical hole through the roof
            ("pocket_rounded", ("bridge", 4.0)),  # 4 x 30 with R1 corners: straight ends with a tangent corner arc each side
            ("pocket_flared", ("bridge", None)),  # one end flares out in two concave R1 arcs
            ("pocket_scooped", ("bridge", None)),  # R1 corner arcs that are not tangent to the sides
            ("pocket_reversed", ("bridge", 4.0)),  # a plain 4 x 30 roof whose long edges run in opposite directions
            ("pocket_bowtie", ("bridge", None)),  # the two ends cross: low end of one run to high end of the other
            ("pocket_elliptic", ("bridge", None)),  # a half-ellipse end is not a circle
            ("pocket_elliptic_thin", ("bridge", None)),  # the same at 0.0015 mm wide: radius 0 is within tolerance of gap / 2
            ("pocket_oversize", ("bridge", None)),  # corner arcs of radius 2.5 and 0.5 on a 4 mm gap: 2.5 is above gap / 2
            ("pocket_stadium_hole", ("bridge", None)),  # the stadium with a 1 mm hole through a round end
            ("pocket_stadium_notch", ("bridge", None)),  # the stadium with a notch confined to a round end
            ("pocket_l", ("bridge", None)),  # two 30 x 4 arms
            ("pocket_trapezoid", ("bridge", None)),  # parallel sides 10 and 6
            ("pocket_ring", ("bridge", None)),  # an annulus around a pillar
            ("pocket_boss", ("bridge", None)),  # 30 x 6 around a 7.5 x 4 boss: never the 5 mm gap
        ):
            with self.subTest(case):
                # the locator can also match a wall closer than the tolerance (the 0.0015 mm strip), so take the face with a down fact
                roof = next(f["down"] for f in self.probed[case]["faces"] if "roof" in f["features"] and "down" in f)
                self.assertEqual((roof["kind"], roof["span_mm"]), expected)

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

    def test_the_warp_footprint_of_a_circular_contact_is_its_exact_diameter_whatever_the_seam(self) -> None:
        disc = self.probed["disc"]
        self.assertAlmostEqual(disc["bed"]["contact_area_mm2"], math.pi * 50.0**2, places=3)
        warp = calibration("warp_coupon", "warp", POLYMER, 0.1, 0.2, size=mm(99.8))
        documents = printed("TST-0101", {"strategy": "none"}, calibrations=[CONTACT, warp])
        found = self.finding(self.assess(documents, geometry("TST-0101", disc)), "process.fdm.warp_scope")
        self.assertAlmostEqual(found["measured"]["value"], 100.0, places=4)

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
            return proc.returncode, result_payload(proc.stdout)
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
        rows = [e for f in measured for e in f["method"]["evidence"]]
        trace = [e for e in rows if e["path"].startswith("hw/checks/v1/process/evidence/guideline-trace-")]
        self.assertTrue(trace, "findings of a guideline-consulting obligation cite the resolution trace")
        self.assertTrue(all(e["path"].startswith("hw/versions/v1/manufacturing/TST-0001/components/body/")
                            for e in rows if e not in trace))
        for row in trace:  # the cited file is in the sealed bundle, and its hash is the row's
            written = bundle / "evidence" / Path(row["path"]).name
            self.assertEqual(row["sha256"], hashlib.sha256(written.read_bytes()).hexdigest())
        manifest = self.hw / "versions" / "v1" / "manifest.json"
        self.assertEqual(packet["dependencies"]["version_manifest"], hashlib.sha256(manifest.read_bytes()).hexdigest())

    def test_a_second_run_reuses_the_sealed_bundle(self) -> None:
        code, report = self.runs[1]
        self.assertEqual((code, report["manufacturing"]["reused"]), (1, True))


if __name__ == "__main__":
    unittest.main()
