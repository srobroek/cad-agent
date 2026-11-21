#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""The lamp-arm example's Bambu Lab H2D capability profile (`h2d`) and the two FDM print operations that name it.

Run: python3 -m unittest discover -s skills/cad-design/tests -p test_lamp_arm_h2d.py
Needs PyYAML and jsonschema (the CI uv command supplies them). Every test reads the real example record and
assesses it with process_checks, as form_check.py does, so a test fails when the record's data drifts. The
numbers are the ones Bambu Lab publishes on its H2D tech-specs page, accessed 2026-11-18. The geometry tests use a
synthetic bed-frame probe, because the example holds no built version, and a temporary copy of the record that
approves the print orientation, which the example itself leaves proposed.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent.parent
REPO = SKILLS.parent
HW = REPO / "examples" / "lamp-arm" / "hw"
TODAY = date(2026, 11, 18)
PRINTED = ("LMP-0001", "LMP-0004")
# The one finding each print obligation reports on the h2d profile, without geometry or a built version.
PRINT_REASONS = {"geometry": "process.fdm.geometry_unassessed", "thermal_distortion": "process.fdm.geometry_unassessed",
                 "orientation": "process.fdm.orientation_unapproved", "support": "process.fdm.support_unplanned"}
DEPS = importlib.util.find_spec("yaml") is not None and importlib.util.find_spec("jsonschema") is not None

for folder in (SKILLS / "cad-design" / "scripts", SKILLS / "cad-model" / "scripts"):
    sys.path.insert(0, str(folder))

if DEPS:
    import process_checks as pc
    import yaml

    MI = pc.intent()


def load(hw: Path) -> Any:
    record, problems = MI.check_record(hw)
    errors = [p.message for p in problems if p.severity == "error"]
    assert not errors, errors
    return record


def assess(record: Any, geometry: dict | None = None) -> Any:
    return pc.assess(record, geometry or {}, today=TODAY)


def codes(result: Any, obligation: str) -> list[str]:
    return sorted(f["code"] for f in result.findings if f.get("obligation") == obligation)


def measured(result: Any, obligation: str, code: str) -> str:
    return " ".join(str(f["measured"]) for f in result.findings if f.get("obligation") == obligation and f["code"] == code)


def probe(size: tuple[float, float, float]) -> dict[str, Any]:
    """The facts form_check measures for a part exported in its bed pose: a flat base of the given bounding box."""
    return {"format": 1, "frame": "bed", "build_axis_native": [0.0, 0.0, 1.0], "solids": 1, "shells": 1, "volume_mm3": 1000.0,
            "bbox_mm": {"min": [0, 0, 0], "max": list(size)}, "centre_mm": [5.0, 5.0, 10.0],
            "bed": {"z_mm": 0.0, "contact_area_mm2": 100.0, "contact_hull": [[0, 0], [10, 0], [10, 10], [0, 10]]},
            "faces": [], "holes": [], "corners": [], "pins": [], "bends": [], "walls": None, "features": {}, "unmapped": [],
            "prismatic_axis": None}


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class H2dProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.record = load(HW)
        cls.result = assess(cls.record)

    def profile(self) -> Any:
        profile = self.record.capabilities.get("h2d")
        self.assertIsNotNone(profile, "capabilities.yaml holds no h2d profile")
        return profile

    def test_the_record_validates_and_the_profile_is_a_local_fdm_machine(self) -> None:
        profile = self.profile()
        self.assertEqual((profile.process, profile.provider, profile.template), ("fdm", "local", False))
        self.assertEqual(profile.raw["machine"]["identity"]["value"], "Bambu Lab H2D")
        self.assertEqual(profile.calibrations, ())

    def test_the_envelope_is_the_published_single_nozzle_volume(self) -> None:
        envelope = self.profile().raw["envelope"]
        sizes = {axis: envelope[axis]["value"] for axis in "xyz"}
        self.assertEqual(sizes, {axis: {"value": value, "units": "mm"} for axis, value in zip("xyz", (325, 320, 325))})

    def test_published_parameters_are_known_and_layer_height_is_not(self) -> None:
        params = self.profile().raw["parameters"]
        known = {name: params[name]["value"] for name in ("nozzle_diameter", "hotend_max_temperature", "bed_max_temperature", "max_speed")}
        self.assertEqual(known, {"nozzle_diameter": {"value": 0.4, "units": "mm"},
                                 "hotend_max_temperature": {"value": 350, "units": "deg_C"},
                                 "bed_max_temperature": {"value": 120, "units": "deg_C"},
                                 "max_speed": {"value": 1000, "units": "mm_s"}})
        self.assertEqual(params["layer_height"]["status"], "unknown")

    def test_the_two_hotends_carry_their_published_max_flow(self) -> None:
        # The FDM parameter vocabulary has no flow parameter, so the published figure is in the tool description.
        tools = {tool["id"]: tool["description"] for tool in self.profile().raw["tools"]}
        self.assertEqual(set(tools), {"nozzle_04_standard", "nozzle_04_high_flow"})
        self.assertIn("40 mm3/s", tools["nozzle_04_standard"])
        self.assertIn("65 mm3/s", tools["nozzle_04_high_flow"])

    def test_petg_is_approved_by_a_decision_record_that_exists(self) -> None:
        approvals = {a["material"]: a for a in self.profile().raw["materials"]}
        self.assertEqual(approvals["petg"]["status"], "approved")
        decision = approvals["petg"]["source"]["decision"]
        self.assertEqual(decision, "DR-005")
        records = [path for path in (HW / "decisions").iterdir() if path.name.startswith(f"{decision}-")]
        self.assertEqual(len(records), 1, f"{decision} is one MADR record file")
        self.assertIn("petg", self.record.materials)

    def test_the_printed_parts_name_petg_as_their_material_profile(self) -> None:
        for pn in PRINTED:
            with self.subTest(part=pn):
                self.assertEqual(self.record.parts[pn].intent.material_profile, "petg")

    def test_both_print_operations_resolve_to_h2d_and_other_operations_stay_unknown(self) -> None:
        for pn in PRINTED:
            with self.subTest(part=pn):
                ops = {op.id: op for op in self.record.parts[pn].intent.route}
                self.assertEqual(ops["print"].capability, "h2d")
                self.assertTrue(MI.is_fact(ops["finish_holes"].capability))

    def test_the_print_operations_no_longer_report_an_unknown_capability(self) -> None:
        self.assertEqual(self.result.by_code("process.fdm.capability_unknown"), [])
        for pn in PRINTED:
            with self.subTest(part=pn):
                route = codes(self.result, f"route.consistency@{pn}")
                self.assertEqual(route.count("route.capability_unknown"), 1)  # the drilling operation's only
                self.assertIn("finish_holes", measured(self.result, f"route.consistency@{pn}", "route.capability_unknown"))
                self.assertNotIn("print", measured(self.result, f"route.consistency@{pn}", "route.capability_unknown"))
                self.assertNotIn("route.material_reference", route)
                self.assertNotIn("route.material_forbidden", route)

    def test_the_drilling_operations_are_unaffected(self) -> None:
        for pn in PRINTED:
            for family in ("access", "feature"):
                with self.subTest(part=pn, family=family):
                    self.assertEqual(codes(self.result, f"process.drilling.{family}@{pn}/finish_holes"),
                                     ["process.drilling.capability_unknown"])

    def test_each_print_obligation_stays_unknown_for_its_own_new_reason(self) -> None:
        for pn in PRINTED:
            for family, reason in PRINT_REASONS.items():
                obligation = f"process.fdm.{family}@{pn}/print"
                with self.subTest(obligation=obligation):
                    self.assertEqual(self.result.outcome(obligation)["outcome"], "unknown")
                    self.assertEqual(codes(self.result, obligation), [reason])


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class H2dEnvelopeTests(unittest.TestCase):
    """With the print orientation approved in a temporary copy, the checks compare the part with the published envelope."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="h2d-envelope-")
        self.addCleanup(tmp.cleanup)
        self.hw = Path(shutil.copytree(HW, Path(tmp.name) / "hw"))
        for pn in PRINTED:
            path = self.hw / "parts" / pn / "part.yaml"
            data = yaml.safe_load(path.read_text())
            for op in data["intent"]["route"]:
                if op["id"] == "print":
                    op["orientation"] = {"status": "approved", "rotation": {"quaternion": [1, 0, 0, 0]}, "decision": "DR-001"}
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.record = load(self.hw)

    def orient(self, pn: str, size: tuple[float, float, float]) -> Any:
        evidence = ({"path": f"hw/versions/v1/manufacturing/{pn}/part.step", "sha256": "0" * 64},)
        return assess(self.record, {(pn, "main"): pc.Geometry(pn, "main", probe(size), evidence)})

    def test_a_part_inside_325_320_325_passes_the_envelope(self) -> None:
        for pn in PRINTED:
            with self.subTest(part=pn):
                result = self.orient(pn, (325.0, 320.0, 325.0))
                obligation = f"process.fdm.orientation@{pn}/print"
                row = result.outcome(obligation)
                self.assertIn("inside the measured envelope", row["coverage"])
                self.assertNotIn("process.fdm.envelope", codes(result, obligation))
                self.assertNotIn("process.fdm.envelope_unknown", codes(result, obligation))

    def test_a_part_over_the_published_x_limit_fails_the_envelope(self) -> None:
        for pn in PRINTED:
            with self.subTest(part=pn):
                result = self.orient(pn, (325.5, 100.0, 100.0))
                obligation = f"process.fdm.orientation@{pn}/print"
                self.assertEqual(result.outcome(obligation)["outcome"], "fail")
                found = [f for f in result.by_code("process.fdm.envelope") if f.get("obligation") == obligation]
                self.assertEqual(found[0]["criterion"]["value"], 325.0)

    def test_the_unpublished_bed_contact_minimum_stays_unknown_as_uncalibrated(self) -> None:
        result = self.orient(PRINTED[0], (50.0, 50.0, 50.0))
        obligation = f"process.fdm.orientation@{PRINTED[0]}/print"
        self.assertEqual(result.outcome(obligation)["outcome"], "unknown")
        self.assertIn("process.fdm.contact_area_uncalibrated", codes(result, obligation))
        self.assertNotIn("process.fdm.capability_unknown", codes(result, obligation))


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class H2dCalibrationFlagTests(unittest.TestCase):
    """The lamp-arm print operations grade against the shipped guideline sets while printer calibration is off
    (the release behaviour), and keep today's UNKNOWN grades with CAD_CALIBRATION=1."""

    setUp = H2dEnvelopeTests.setUp  # a temporary copy of the record with the print orientation approved

    def judge(self, flag: bool, wall: float = 3.0) -> Any:
        facts = probe((50.0, 50.0, 50.0))
        facts["walls"] = {"thickness_mm": wall, "location_mm": [1.0, 1.0, 1.0], "features": [], "measured": 12}
        evidence = ({"path": f"hw/versions/v1/manufacturing/{PRINTED[0]}/part.step", "sha256": "0" * 64},)
        geometry = {(PRINTED[0], "main"): pc.Geometry(PRINTED[0], "main", facts, evidence)}
        return pc.assess(self.record, geometry, today=TODAY, catalog=pc.guidelines().discover(None), calibration=flag)

    def test_off_a_guideline_graded_wall_passes_without_a_guideline_only_or_uncalibrated_finding(self) -> None:
        result = self.judge(False)
        obligation = f"process.fdm.geometry@{PRINTED[0]}/print"
        self.assertEqual(result.outcome(obligation)["outcome"], "pass")
        self.assertEqual([c for c in codes(result, obligation) if c.endswith(("_guideline_only", "_uncalibrated"))], [])

    def test_off_a_guideline_graded_wall_beyond_the_limit_fails(self) -> None:
        result = self.judge(False, wall=0.3)
        self.assertEqual(result.outcome(f"process.fdm.geometry@{PRINTED[0]}/print")["outcome"], "fail")

    def test_on_the_same_wall_is_unknown_as_a_guideline_only_value(self) -> None:
        result = self.judge(True)
        obligation = f"process.fdm.geometry@{PRINTED[0]}/print"
        self.assertEqual(result.outcome(obligation)["outcome"], "unknown")
        self.assertIn("process.fdm.wall_guideline_only", codes(result, obligation))

    def test_off_the_unpublished_bed_contact_minimum_names_the_missing_guideline_and_asks_for_no_calibration(self) -> None:
        result = self.judge(False)
        found = [f for f in result.findings if f["code"] == "process.fdm.contact_area_uncalibrated"]
        self.assertTrue(found)
        self.assertIn("no guideline value covers min_bed_contact_area", found[0]["uncertainty"])
        self.assertEqual(found[0]["remedy"], "for a tighter limit, print a test coupon on the target printer")


if __name__ == "__main__":
    unittest.main()
