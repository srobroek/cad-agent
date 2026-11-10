#!/usr/bin/env python3
"""Stack-up tests against an independent hand calculation, and the intent (version 2) stack rules."""

from __future__ import annotations

import contextlib
import copy
from datetime import date
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "scripts" / "stackup.py"
SPEC = importlib.util.spec_from_file_location("cad_stackup", SCRIPT)
assert SPEC and SPEC.loader
stackup = importlib.util.module_from_spec(SPEC)
sys.modules["cad_stackup"] = stackup
SPEC.loader.exec_module(stackup)

FIXTURES = HERE / "tests" / "fixtures"
_FX_SPEC = importlib.util.spec_from_file_location(
    "cad_record_mi_fixtures", HERE.parent / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
assert _FX_SPEC and _FX_SPEC.loader
fx = sys.modules.get("cad_record_mi_fixtures") or importlib.util.module_from_spec(_FX_SPEC)
if "cad_record_mi_fixtures" not in sys.modules:
    sys.modules["cad_record_mi_fixtures"] = fx
    _FX_SPEC.loader.exec_module(fx)
intent = fx.intent_module()
ON = date(2026, 6, 1)

# Bearing end float: housing width A closes against two bearings B, D and a spacer C.
#   A = 50.00 +0.10/-0.00 (+)   B = D = 20.00 +/-0.05 (-)   C = 9.90 +0.00/-0.06 (-)
# Hand calculation:
#   nominal gap      = 50.00 - 20.00 - 9.90 - 20.00                      = 0.10
#   worst-case max   = 50.10 - 19.95 - 9.84 - 19.95                      = 0.36
#   worst-case min   = 50.00 - 20.05 - 9.90 - 20.05                      = 0.00
#   mid-limit means  = 50.05 - 20.00 - 9.87 - 20.00                      = 0.18
#   RSS half-range   = sqrt(0.05^2 + 0.05^2 + 0.03^2 + 0.05^2) = sqrt(0.0084) = 0.0916515
#   RSS min / max    = 0.18 -/+ 0.0916515                                = 0.0883485 / 0.2716515
STACK = {
    "id": "STK-001",
    "title": "Bearing end float",
    "units": "mm",
    "requirement": {"min": 0.02, "max": 0.40, "method": "worst_case"},
    "contributors": [
        {"id": "A", "nominal": 50.00, "tol_plus": 0.10, "tol_minus": 0.00, "direction": 1},
        {"id": "B", "nominal": 20.00, "tol": 0.05, "direction": -1},
        {"id": "C", "nominal": 9.90, "tol_plus": 0.00, "tol_minus": 0.06, "direction": -1},
        {"id": "D", "nominal": 20.00, "tol": 0.05, "direction": -1},
    ],
}

STACK_YAML = """\
id: STK-001
title: Bearing end float
units: mm
requirement: {min: 0.02, max: 0.40, method: worst_case}
contributors:
  - {id: A, nominal: 50.00, tol_plus: 0.10, tol_minus: 0.00, direction: 1}
  - {id: B, nominal: 20.00, tol: 0.05, direction: -1}
  - {id: C, nominal: 9.90, tol_plus: 0.00, tol_minus: 0.06, direction: -1}
  - {id: D, nominal: 20.00, tol: 0.05, direction: -1}
"""


def fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))


def contributor(stack: dict, ident: str) -> dict:
    return next(c for c in stack["contributors"] if c["id"] == ident)


def codes(result: dict) -> list[str]:
    return sorted(f["code"] for f in result["findings"])


def closing_gap_documents(*, method: str = "worst_case", relevant: tuple[str, ...] = ("manufacture", "coating", "thermal")) -> dict:
    """tst_record with IF-001's stack declared as case closing-gap and a coated spacer shoulder."""
    documents = fx.tst_record()
    functional = documents["interfaces.yaml"]["interfaces"][0]["functional"]
    functional["variation"] = {"stack_case": "closing-gap", "contributors": list(relevant), "method": method}
    if method == "justified_rss":
        functional["variation"]["decision"] = "DR-001"
    shaft = documents["parts/TST-0002/part.yaml"]
    shaft["intent"]["route"].append(fx.operation("coat", "coating", targets=[{"feature": "shoulder_length"}]))
    return documents


def tab_slot_documents() -> dict:
    """Two laser-cut, powder-coated steel plates joined by a critical tab-and-slot interface IF-002."""
    def plate(pn: str, title: str, feature: str) -> dict:
        return fx.part(pn, title, process="laser", material="TST steel sheet", part_intent=fx.intent(
            functions=[{"function": "locating"}], material_profile="tst_steel_sheet",
            critical_surfaces=fx.not_applicable("the stack covers the fit"),
            route=[fx.operation("cut", "laser", "tst_laser"),
                   fx.operation("coat", "coating", targets=[{"feature": feature}])]))

    laser = fx.capability_profile(
        "tst_laser", "laser",
        materials=[{"material": "tst_steel_sheet", "status": "approved", "thickness": fx.known(fx.quantity(3)),
                    "extraction": fx.known("TST extraction"), "operations": ["cut"], "source": fx.source("user")}],
        calibrations=[fx.calibration("tst_slot_coupon", "slot_width_deviation", "tst_steel_sheet",
                                     fx.quantity(lower=-0.05, upper=0.05))])
    interface = fx.interface("IF-002", "TST-0011", "TST-0012", "mounting", functional={
        "critical": fx.known(True), "functions": [{"function": "locating"}],
        "variation": {"stack_case": "tab-slot", "contributors": ["manufacture", "coating"], "method": "worst_case"}})
    return fx.record(parts=[plate("TST-0011", "Slot plate", "slot"), plate("TST-0012", "Tab plate", "tab")],
                     interfaces=[interface], capabilities=[laser],
                     materials=[fx.material_profile("tst_steel_sheet", level="family", form="sheet")])


def write_case(hw: Path, case: str, stack: dict) -> Path:
    path = hw / "analysis" / "cases" / case / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(stack, sort_keys=False), encoding="utf-8")
    return path


def snapshot_version(hw: Path, label: str = "v1") -> str:
    """Seal hw's record documents and frozen sources as the record snapshot of built version `label`;
    the sha256 of its manifest."""
    version = hw / "versions" / label
    record = version / "record"
    for source in hw.rglob("*"):
        rel = source.relative_to(hw)
        if source.is_file() and rel.parts[0] not in ("versions", "analysis", "decisions") \
                and rel.suffix == ".yaml" or (source.is_file() and rel.parts[:2] == ("manufacturing", "sources")):
            target = record / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    files = {path.relative_to(version).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in version.rglob("*") if path.is_file()}
    manifest = version / "manifest.json"
    manifest.write_text(json.dumps({"label": label, "files": files}), encoding="utf-8")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def evidence_module() -> object:
    """cad-record evidence.py, the one reader of producer check packets."""
    name = "cad_record_evidence"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE.parent / "cad-record" / "scripts" / "evidence.py")
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def check_result_validator():
    """A validator of cad-record manufacturing-check-result.schema.json, the packet contract, offline."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((HERE.parent / "cad-record" / "schemas").glob("*.schema.json"))]
    registry = Registry().with_resources([(s["$id"], Resource.from_contents(s)) for s in schemas])
    return Draft202012Validator({"$ref": "https://srobroek.com/cad/schemas/manufacturing-check-result.schema.json"},
                                registry=registry)


class StackupHandCalculationTests(unittest.TestCase):
    def test_worst_case_and_rss_match_hand_calculation(self) -> None:
        result = stackup.compute(STACK)
        self.assertAlmostEqual(result["nominal"], 0.10, places=6)
        self.assertAlmostEqual(result["mean"], 0.18, places=6)
        self.assertAlmostEqual(result["worst_case"]["min"], 0.00, places=6)
        self.assertAlmostEqual(result["worst_case"]["max"], 0.36, places=6)
        rss = math.sqrt(0.0084)
        self.assertAlmostEqual(result["rss"]["half_range"], rss, places=6)
        self.assertAlmostEqual(result["rss"]["min"], 0.18 - rss, places=6)
        self.assertAlmostEqual(result["rss"]["max"], 0.18 + rss, places=6)

    def test_requirement_verdict_follows_governing_method(self) -> None:
        result = stackup.compute(STACK)
        self.assertEqual(result["requirement"]["pass_by_method"], {"worst_case": False, "rss": True})
        self.assertFalse(result["requirement"]["pass"])
        rss_governed = dict(STACK, requirement={"min": 0.02, "max": 0.40, "method": "rss"})
        self.assertTrue(stackup.compute(rss_governed)["requirement"]["pass"])

    def test_legacy_quadrature_stays_labelled_an_unqualified_heuristic(self) -> None:
        rss = stackup.compute(STACK)["rss"]
        self.assertIn("unqualified quadrature heuristic", rss["qualification"])
        self.assertIn("not a probabilistic range", rss["qualification"])

    def test_contribution_shares_sum_to_100(self) -> None:
        rows = stackup.compute(STACK)["contributors"]
        self.assertAlmostEqual(sum(r["wc_contribution_pct"] for r in rows), 100.0, places=4)
        self.assertAlmostEqual(sum(r["rss_contribution_pct"] for r in rows), 100.0, places=4)

    def test_invalid_contributors_are_rejected(self) -> None:
        bad_cases = [
            {"contributors": []},
            {"contributors": [{"nominal": 1.0, "tol": 0.1, "direction": 2}]},
            {"contributors": [{"nominal": 1.0, "tol": -0.1, "direction": 1}]},
            {"contributors": [{"nominal": 1.0, "tol_plus": 0.1, "direction": 1}]},
            {"contributors": [{"nominal": 1.0, "tol": 0.1, "tol_plus": 0.1, "tol_minus": 0.1, "direction": 1}]},
            {"contributors": [{"nominal": "1", "tol": 0.1, "direction": 1}]},
            {"contributors": [{"nominal": 1.0, "tol": 0.1, "direction": 1}], "requirement": {"method": "rss"}},
        ]
        for case in bad_cases:
            with self.subTest(case=case), self.assertRaises(stackup.StackError):
                stackup.compute(case)


class IntentStackContractTests(unittest.TestCase):
    """Intent stacks: worst case governs, unknown never passes, refused inputs (plan section 11 rows 10 and 14)."""

    def assert_schema_valid(self, result: dict) -> None:
        for finding in result["findings"]:
            errors = list(stackup._validator("finding").iter_errors(finding))
            self.assertEqual(errors, [], finding["id"])
        for row in result["obligations"]:
            self.assertEqual(list(stackup._validator("obligation_outcome").iter_errors(row)), [], row)

    def bound(self, documents: dict, stack: dict, case: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            hw = fx.write_record(Path(directory) / "project" / "hw", documents)
            write_case(hw, case, stack)
            record = intent.load_record(hw)
            result = stackup.compute(stack, record, case=case, on=ON, decisions=frozenset({"DR-001"}))
        self.assert_schema_valid(result)
        return result

    # ---- row 14: nominal clearance fails the stack

    def test_nominal_closing_gap_fails_the_worst_case_stack(self) -> None:
        result = self.bound(closing_gap_documents(), fixture("closing_gap.yaml"), "closing-gap")
        self.assertAlmostEqual(result["remaining"]["nominal"], 0.20, places=6)
        self.assertAlmostEqual(result["remaining"]["after_tolerances"]["min"], -0.10, places=6)
        self.assertAlmostEqual(result["worst_case"]["max"], 0.50, places=6)
        self.assertTrue(result["requirement"]["nominal_pass"])
        self.assertFalse(result["requirement"]["worst_case_pass"])
        self.assertEqual(result["requirement"]["governing"], "worst_case")
        self.assertEqual(result["outcome"], "fail")
        self.assertTrue(result["critical"])
        self.assertEqual(codes(result), ["stack.worst_case_violation"])
        violation = result["findings"][0]
        self.assertEqual(violation["severity"], "critical")
        self.assertEqual(violation["measured"], {"lower": -0.1, "upper": 0.5, "units": "mm"})
        self.assertIn("nominal 0.2 mm meets the requirement", violation["consequence"])
        self.assertEqual(result["obligations"][0]["obligation"], "interface.stack@IF-001")
        self.assertEqual(result["obligations"][0]["outcome"], "fail")
        qualifications = {row["id"]: row["qualification"] for row in result["contributors"]}
        self.assertEqual(qualifications, {"H": "inspection", "B": "source:datasheet", "S": "source:supplier",
                                          "C": "source:datasheet", "T": "source:calculation"})

    def test_unjustified_governing_rss_refuses_critical_acceptance(self) -> None:
        stack = fixture("closing_gap.yaml")
        stack["requirement"]["method"] = "justified_rss"
        result = self.bound(closing_gap_documents(method="justified_rss"), stack, "closing-gap")
        self.assertEqual(result["statistical"]["status"], "refused")
        self.assertEqual(result["requirement"]["governing"], "worst_case")
        self.assertEqual(result["outcome"], "fail")
        self.assertIn("stack.rss_refused", codes(result))
        refused = next(f for f in result["findings"] if f["code"] == "stack.rss_refused")
        self.assertEqual(refused["severity"], "critical")
        self.assertIn("no statistical acceptance", refused["consequence"])

    @staticmethod
    def justified(stack: dict) -> dict:
        stack["requirement"]["method"] = "justified_rss"
        stack["statistics"] = {"decision": "DR-001", "coverage_factor": 3, "probability": 0.9973,
                               "independence": fx.source("test", "TST independent process study")}
        for item in stack["contributors"]:
            item["distribution"] = {"tolerance_divisor": 3, "centred": True, "stable": True,
                                    "source": fx.source("test", f"TST {item['id']} capability study")}
        return stack

    def test_justified_rss_never_permits_a_forbidden_collision(self) -> None:
        stack = self.justified(fixture("closing_gap.yaml"))
        result = self.bound(closing_gap_documents(method="justified_rss"), stack, "closing-gap")
        sigma = math.sqrt(0.08 ** 2 + 0.06 ** 2 + 0.06 ** 2 + 0.02 ** 2 + 0.08 ** 2) / 3
        self.assertEqual(result["statistical"]["status"], "justified")
        self.assertAlmostEqual(result["statistical"]["sigma"], sigma, places=6)
        self.assertAlmostEqual(result["statistical"]["min"], 0.20 - 3 * sigma, places=6)
        self.assertTrue(result["requirement"]["statistical_pass"])
        self.assertEqual(result["requirement"]["governing"], "worst_case")
        self.assertEqual(result["outcome"], "fail")
        self.assertIn("stack.rss_refused", codes(result))

    def test_justified_rss_may_govern_a_performance_limit_with_the_worst_case_shown(self) -> None:
        stack = self.justified(fixture("closing_gap.yaml"))
        stack["consequence"] = "performance"
        result = stackup.compute(stack)
        self.assertEqual(result["requirement"]["governing"], "justified_rss")
        self.assertEqual(result["outcome"], "pass")
        self.assertAlmostEqual(result["worst_case"]["min"], -0.10, places=6)
        self.assertTrue(any("worst case -0.1..0.5 mm violates" in text for text in result["limits"]))
        self.assertTrue(any("not bound to a record" in text for text in result["limits"]))

    def test_correlations_widen_the_interval_and_an_inconsistent_set_is_refused(self) -> None:
        stack = self.justified(fixture("closing_gap.yaml"))
        stack["consequence"] = "performance"
        stack["statistics"]["correlations"] = [{"between": ["B", "S"], "rho": 0.5}]
        correlated = stackup.compute(stack)["statistical"]
        s_b = s_s = 0.06 / 3  # B and S both subtract: weights -s_b and -s_s, covariance term +2 rho s_b s_s
        base = (0.08 ** 2 + 0.06 ** 2 + 0.06 ** 2 + 0.02 ** 2 + 0.08 ** 2) / 9
        self.assertAlmostEqual(correlated["sigma"], math.sqrt(base + 2 * 0.5 * s_b * s_s), places=6)
        stack["statistics"]["correlations"] = [{"between": ["H", "B"], "rho": 0.9}, {"between": ["B", "S"], "rho": 0.9},
                                               {"between": ["H", "S"], "rho": -0.9}]
        inconsistent = stackup.compute(stack)
        self.assertEqual(inconsistent["statistical"]["status"], "refused")
        self.assertEqual(inconsistent["requirement"]["governing"], "worst_case")

    def test_unknown_contributor_or_missing_effect_is_never_a_pass(self) -> None:
        stack = fixture("closing_gap.yaml")
        stack["requirement"]["min"] = -1.0
        thermal = contributor(stack, "T")
        for key in ("direction", "nominal", "tol", "source"):
            thermal.pop(key)
        thermal.update(status="unknown", reason="no thermal expansion analysis yet")
        result = self.bound(closing_gap_documents(), stack, "closing-gap")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["worst_case"]["status"], "unknown")
        self.assertEqual(result["worst_case"]["unknown_contributors"], ["T"])
        self.assertEqual(codes(result), ["stack.contributor_unknown"])

        missing = fixture("closing_gap.yaml")
        missing["relevant"].append("moisture")
        result = self.bound(closing_gap_documents(relevant=("manufacture", "coating", "thermal", "moisture")),
                            missing, "closing-gap")
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("stack.effect_missing", codes(result))

    def test_an_effect_the_interface_declares_relevant_cannot_be_dropped(self) -> None:
        result = self.bound(closing_gap_documents(relevant=("manufacture", "coating", "thermal", "moisture")),
                            fixture("closing_gap.yaml"), "closing-gap")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.record_mismatch"])
        self.assertIn("moisture", result["findings"][0]["uncertainty"])

    def test_an_iso_designation_is_not_achievable_precision(self) -> None:
        stack = fixture("closing_gap.yaml")
        housing = contributor(stack, "H")
        housing.pop("inspection")
        housing["designation"] = "20 H7"
        housing["source"] = fx.source("standard", "ISO 286-1 H7 limits")
        result = self.bound(closing_gap_documents(), stack, "closing-gap")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.designation_not_capability"])

    def test_a_drawn_tolerance_alone_does_not_qualify_manufacturing_variation(self) -> None:
        stack = fixture("closing_gap.yaml")
        spacer = contributor(stack, "S")
        spacer["source"] = fx.source("user", "drawing tolerance")
        self.assertEqual(stackup.compute(stack)["outcome"], "unknown")

    def test_as_manufactured_size_of_a_reamed_feature_is_a_state_mismatch(self) -> None:
        stack = fixture("closing_gap.yaml")
        housing = contributor(stack, "H")
        housing.pop("inspection")
        housing["state"] = "as_manufactured"
        housing["operation"] = "print"
        result = self.bound(closing_gap_documents(), stack, "closing-gap")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.state_mismatch"])
        self.assertIn("ream", result["findings"][0]["uncertainty"])

    def test_perfect_dimensions_and_double_counting_are_refused(self) -> None:
        def zero_width(s: dict) -> None:
            contributor(s, "B")["tol"] = 0.0

        def twice(s: dict) -> None:
            extra = copy.deepcopy(contributor(s, "H"))
            extra.update(id="H2", state="as_manufactured")
            s["contributors"].append(extra)

        def coat_on_finished(s: dict) -> None:
            coat = copy.deepcopy(contributor(s, "C"))
            coat.update(id="C2", subject={"PN": "TST-0001", "feature": "pivot_bore"})
            s["contributors"].append(coat)

        def legacy_method(s: dict) -> None:
            s["requirement"]["method"] = "rss"

        def undeclared_effect(s: dict) -> None:
            s["relevant"].remove("thermal")

        def no_requirement_source(s: dict) -> None:
            s["requirement"].pop("source")

        for mutate in (zero_width, twice, coat_on_finished, legacy_method, undeclared_effect, no_requirement_source):
            stack = fixture("closing_gap.yaml")
            mutate(stack)
            with self.subTest(case=mutate.__name__), self.assertRaises(stackup.StackError):
                stackup.compute(stack)

    def test_a_legacy_stack_cannot_bind_a_record(self) -> None:
        with self.assertRaises(stackup.StackError):
            stackup.compute(copy.deepcopy(STACK), record=object())

    # ---- row 10: laser tab-and-slot

    def test_nominal_laser_tab_slot_fails_its_kerf_thickness_and_coating_interval(self) -> None:
        result = self.bound(tab_slot_documents(), fixture("laser_tab_slot.yaml"), "tab-slot")
        self.assertAlmostEqual(result["remaining"]["nominal"], 0.10, places=6)
        self.assertAlmostEqual(result["worst_case"]["min"], -0.15, places=6)
        self.assertAlmostEqual(result["worst_case"]["max"], 0.20, places=6)
        self.assertTrue(result["requirement"]["nominal_pass"])
        self.assertEqual(result["outcome"], "fail")
        self.assertEqual(codes(result), ["stack.worst_case_violation"])
        self.assertEqual(result["obligations"][0]["obligation"], "interface.stack@IF-002")
        slot = next(row for row in result["contributors"] if row["id"] == "W")
        self.assertEqual(slot["qualification"], "capability")
        self.assertEqual(slot["resolved"]["calibration"], "tst_slot_coupon")
        self.assertIn("process.laser.fit_coupon", {e.get("family") for e in result["required_evidence"]})

    def test_a_passing_tab_slot_interval_still_requires_its_fit_coupon(self) -> None:
        stack = fixture("laser_tab_slot.yaml")
        contributor(stack, "W")["nominal"] = 3.30
        result = self.bound(tab_slot_documents(), stack, "tab-slot")
        self.assertEqual(result["outcome"], "pass")
        self.assertAlmostEqual(result["worst_case"]["min"], 0.05, places=6)
        coupon = [e for e in result["required_evidence"] if e.get("family") == "process.laser.fit_coupon"]
        self.assertEqual(len(coupon), 1)
        self.assertEqual(coupon[0]["level"], "physically_validated")
        self.assertTrue(any("not physical validation" in text for text in result["limits"]))

    def test_slot_limits_tighter_than_the_calibration_stay_unknown(self) -> None:
        stack = fixture("laser_tab_slot.yaml")
        contributor(stack, "W")["tol"] = 0.02
        result = self.bound(tab_slot_documents(), stack, "tab-slot")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.limits_unqualified"])

    def test_an_uncounted_coating_on_the_slot_is_a_state_mismatch(self) -> None:
        stack = fixture("laser_tab_slot.yaml")
        stack["contributors"] = [c for c in stack["contributors"] if c["id"] != "CS"]
        result = self.bound(tab_slot_documents(), stack, "tab-slot")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.state_mismatch"])

    def test_an_uncalibrated_capability_stays_unknown(self) -> None:
        documents = tab_slot_documents()
        documents["manufacturing/capabilities.yaml"]["profiles"][0]["calibrations"] = []
        result = self.bound(documents, fixture("laser_tab_slot.yaml"), "tab-slot")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.capability_unresolved"])


class StackPacketTests(unittest.TestCase):
    """A record-bound intent stack at hw/analysis/cases/<case>/case.yaml publishes its analysis bundle as
    hw/analysis/<V>/<case>/ (the bundle release.py requires for the case) and the interface.stack / feature.fit
    outcome it judges as a required-check packet at hw/checks/<V>/stack-<case>/, beside it, where cad-record
    evidence.py reads producer packets; the governing results and unknowns are kept."""

    PACKET = "hw/checks/v1/stack-closing-gap"
    OBLIGATION = "interface.stack@IF-001"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hw = Path(directory.name) / "project" / "hw"

    def build(self, documents: dict, stack: dict) -> None:
        fx.write_record(self.hw, documents)
        self.case = write_case(self.hw, "closing-gap", stack)
        self.manifest_sha = snapshot_version(self.hw)

    def run_stack(self) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = stackup.main([str(self.case), "--hw", str(self.hw), "--version", "v1"])
        self.assertIn(status, (0, 1), err.getvalue())
        return status, json.loads(out.getvalue())

    def packet(self) -> tuple[dict, dict]:
        """(result.json, evidence/analysis.json) of the published packet, after validating the packet."""
        bundle = self.hw.parent / self.PACKET
        packet = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
        errors = [error.message for error in check_result_validator().iter_errors(packet)]
        self.assertEqual(errors, [])
        return packet, json.loads((bundle / "evidence" / "analysis.json").read_text(encoding="utf-8"))

    def assessed(self) -> object:
        """The evidence status of IF-001's stack obligation, from the packets evidence.py collects."""
        evidence = evidence_module()
        packets, findings = evidence.collect_packets(self.hw, "v1", self.manifest_sha)
        self.assertEqual((findings, [p.bundle for p in packets]), ([], [self.PACKET]))
        assessment = evidence.assess(intent.load_record(self.hw), version="v1", manifest_sha256=self.manifest_sha,
                                     packets=packets)
        self.assertNotIn("packet.invalid", [f.code for f in assessment.findings])
        return assessment.statuses[self.OBLIGATION]

    def test_a_failed_worst_case_is_the_stack_outcome_evidence_reads_and_a_rerun_reuses_it(self) -> None:
        self.build(closing_gap_documents(), fixture("closing_gap.yaml"))
        status, output = self.run_stack()
        self.assertEqual((status, output["check_bundle"]), (1, self.PACKET))
        packet, detail = self.packet()
        self.assertEqual([o["id"] for o in packet["expected"]], [self.OBLIGATION])
        self.assertEqual((packet["result"], packet["complete"], packet["record_version"]), ("fail", True, 2))
        row = packet["outcomes"][0]
        self.assertEqual((row["obligation"], row["outcome"], row["level"], row["method"]),
                         (self.OBLIGATION, "fail", "process_planned", "1D worst-case tolerance stack"))
        self.assertEqual([f["code"] for f in packet["findings"]], ["stack.worst_case_violation"])
        self.assertEqual(packet["dependencies"], {"stack_case": hashlib.sha256(self.case.read_bytes()).hexdigest()})
        analysis = self.hw / "analysis" / "v1" / "closing-gap"
        self.assertEqual(json.loads((analysis / "results.json").read_text(encoding="utf-8"))["id"], "STK-014")
        self.assertEqual(detail["analysis"], {"bundle": "hw/analysis/v1/closing-gap", "manifest_sha256": hashlib.sha256(
            (analysis / "manifest.json").read_bytes()).hexdigest()})
        self.assertEqual((detail["worst_case"]["min"], detail["worst_case"]["max"]), (-0.1, 0.5))
        self.assertEqual((detail["requirement"]["governing"], detail["requirement"]["nominal_pass"]), ("worst_case", True))
        status_of = self.assessed()
        self.assertEqual((status_of.result, status_of.sources), ("fail", (self.PACKET,)))
        sealed = (self.hw.parent / self.PACKET / "manifest.json").read_bytes()
        status, again = self.run_stack()
        self.assertEqual((status, again["reused"], again["check_bundle"]), (1, True, self.PACKET))
        self.assertEqual((self.hw.parent / self.PACKET / "manifest.json").read_bytes(), sealed)

    def test_an_unknown_contributor_is_published_unknown_never_pass(self) -> None:
        stack = fixture("closing_gap.yaml")
        stack["requirement"]["min"] = -1.0
        thermal = contributor(stack, "T")
        for key in ("direction", "nominal", "tol", "source"):
            thermal.pop(key)
        thermal.update(status="unknown", reason="no thermal expansion analysis yet")
        self.build(closing_gap_documents(), stack)
        status, output = self.run_stack()
        self.assertEqual((status, output["check_bundle"]), (1, self.PACKET))
        packet, detail = self.packet()
        self.assertEqual((packet["result"], packet["outcomes"][0]["outcome"]), ("unknown", "unknown"))
        self.assertIn("no thermal expansion analysis yet", packet["outcomes"][0]["uncertainty"])
        self.assertEqual([f["code"] for f in packet["findings"]], ["stack.contributor_unknown"])
        self.assertEqual((detail["worst_case"]["status"], detail["worst_case"]["unknown_contributors"]), ("unknown", ["T"]))
        self.assertEqual(self.assessed().result, "unknown")

    def test_a_justified_rss_publishes_its_statistical_outcome_and_approving_decision(self) -> None:
        """A performance limit an approved, sourced statistical interval meets passes by justified RSS; the
        worst case that violates it stays in the packet's evidence, never hidden."""
        stack = IntentStackContractTests.justified(fixture("closing_gap.yaml"))
        stack["consequence"] = "performance"
        self.build(closing_gap_documents(method="justified_rss"), stack)
        decision = self.hw / "decisions" / "DR-001.md"
        status, output = self.run_stack()
        self.assertEqual((status, output["check_bundle"]), (0, self.PACKET))
        packet, detail = self.packet()
        self.assertEqual((packet["result"], packet["outcomes"][0]["outcome"], packet["outcomes"][0]["method"]),
                         ("pass", "pass", "1D justified statistical tolerance stack"))
        self.assertEqual((detail["requirement"]["governing"], detail["statistical"]["status"]), ("justified_rss", "justified"))
        self.assertEqual((detail["worst_case"]["min"], detail["worst_case"]["max"]), (-0.1, 0.5))
        self.assertFalse(detail["requirement"]["worst_case_pass"])
        self.assertEqual(packet["dependencies"]["decision_dr_001"], hashlib.sha256(decision.read_bytes()).hexdigest())
        self.assertEqual(self.assessed().result, "pass")

    def test_a_stack_whose_obligation_the_record_does_not_expect_publishes_no_packet(self) -> None:
        documents = closing_gap_documents()
        documents["interfaces.yaml"]["interfaces"][0]["functional"]["critical"] = fx.known(False)
        self.build(documents, fixture("closing_gap.yaml"))
        status, output = self.run_stack()
        self.assertEqual((status, output["check_bundle"]), (1, None))
        self.assertFalse((self.hw / "checks").exists())


@unittest.skipUnless(shutil.which("uv"), "uv is required to run the PEP 723 script end to end")
class StackupCliTests(unittest.TestCase):
    def run_cli(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.yaml"
            path.write_text(text, encoding="utf-8")
            return subprocess.run(
                ["uv", "run", "--quiet", "--script", str(SCRIPT), str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )

    def test_failing_requirement_exits_1_with_json(self) -> None:
        proc = self.run_cli(STACK_YAML)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertAlmostEqual(result["worst_case"]["max"], 0.36, places=6)

    def test_passing_requirement_exits_0(self) -> None:
        proc = self.run_cli(STACK_YAML.replace("method: worst_case", "method: rss"))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_malformed_yaml_exits_2(self) -> None:
        proc = self.run_cli("contributors: [unclosed\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid YAML", proc.stderr)

    def test_stack_publishes_a_version_bound_analysis_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            hw = project / "hw"
            hw.mkdir(parents=True)
            version = hw / "versions" / "v1"
            version.mkdir(parents=True)
            record = version / "record"
            record.mkdir()
            (record / "product.yaml").write_text("{}\n", encoding="utf-8")
            files = {path.relative_to(version).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in version.rglob("*") if path.is_file()}
            (version / "manifest.json").write_text(json.dumps({"label": "v1", "files": files}), encoding="utf-8")
            stack = dict(STACK)
            stack["id"] = "STK-101"
            stack_dir = hw / "analysis" / "cases" / "stk-101"
            stack_dir.mkdir(parents=True)
            stack_path = stack_dir / "stack.yaml"
            stack_path.write_text(yaml.safe_dump(stack, sort_keys=False), encoding="utf-8")
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), str(stack_path),
                                   "--hw", str(hw), "--version", "v1"], capture_output=True, text=True,
                                  check=False, timeout=300)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            output = json.loads(proc.stdout)
            bundle = hw / "analysis" / "v1" / "STK-101"
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(output["bundle"], str(bundle))
            self.assertEqual(manifest["recipe"]["source_yaml_sha256"], hashlib.sha256(stack_path.read_bytes()).hexdigest())
            self.assertEqual(json.loads((bundle / "results.json").read_text(encoding="utf-8"))["nominal"], 0.1)

    def test_intent_stack_binds_the_version_record_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hw = fx.write_record(Path(directory) / "project" / "hw", closing_gap_documents())
            case = write_case(hw, "closing-gap", fixture("closing_gap.yaml"))
            snapshot_version(hw)
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), str(case), "--hw", str(hw),
                                   "--version", "v1"], capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("requirement not met (worst_case)", proc.stderr)
            output = json.loads(proc.stdout)
            self.assertTrue(output["record_bound"])
            self.assertEqual(output["case"], "closing-gap")
            self.assertEqual(output["outcome"], "fail")
            manifest = json.loads((hw / "analysis" / "v1" / "closing-gap" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "fail")
            self.assertEqual(manifest["recipe"]["options"]["intent_version"], 2)
            self.assertEqual(output["check_bundle"], "hw/checks/v1/stack-closing-gap")

    def test_rerun_reuses_bundle_and_recipe_change_requires_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            hw = project / "hw"
            version = hw / "versions" / "v1"
            record = version / "record"
            record.mkdir(parents=True)
            (record / "product.yaml").write_text("{}\n", encoding="utf-8")
            files = {path.relative_to(version).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in version.rglob("*") if path.is_file()}
            (version / "manifest.json").write_text(json.dumps({"label": "v1", "files": files}), encoding="utf-8")
            stack = dict(STACK, id="STK-102")
            stack_path = root / "stack.yaml"
            stack_path.write_text(yaml.safe_dump(stack, sort_keys=False), encoding="utf-8")
            args = ["uv", "run", "--quiet", "--script", str(SCRIPT), str(stack_path),
                    "--hw", str(hw), "--version", "v1"]
            first = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(first.returncode, 1, first.stderr)
            bundle = hw / "analysis" / "v1" / "STK-102"
            original = {path.relative_to(bundle).as_posix(): path.read_bytes()
                        for path in bundle.rglob("*") if path.is_file()}
            second = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertTrue(json.loads(second.stdout)["reused"])
            self.assertEqual(original, {path.relative_to(bundle).as_posix(): path.read_bytes()
                                        for path in bundle.rglob("*") if path.is_file()})

            stack_path.write_text(stack_path.read_text(encoding="utf-8").replace("nominal: 50", "nominal: 51"),
                                  encoding="utf-8")
            changed = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(changed.returncode, 2)
            self.assertIn("--replace", changed.stderr)
            self.assertEqual(original, {path.relative_to(bundle).as_posix(): path.read_bytes()
                                        for path in bundle.rglob("*") if path.is_file()})
            replaced = subprocess.run([*args, "--replace"], capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(replaced.returncode, 1, replaced.stderr)
            self.assertFalse(json.loads(replaced.stdout)["reused"])
            replaced_path = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["replaces"]["moved_to"]
            replaced_bundle = project / replaced_path
            self.assertEqual(original, {path.relative_to(replaced_bundle).as_posix(): path.read_bytes()
                                         for path in replaced_bundle.rglob("*") if path.is_file()})

    def test_standalone_output_override_outside_cad_work_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack_path = root / "stack.yaml"
            stack_path.write_text(STACK_YAML, encoding="utf-8")
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), str(stack_path),
                                  "--out", str(root / "outside.json")], capture_output=True, text=True,
                                 check=False, timeout=300)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--out must be under", proc.stderr)


if __name__ == "__main__":
    unittest.main()
