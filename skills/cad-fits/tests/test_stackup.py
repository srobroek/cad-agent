#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

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
import unittest.mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from decision_fixture import decision_path  # noqa: E402

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "stackup.py"
SPEC = importlib.util.spec_from_file_location("cad_stackup", SCRIPT)
assert SPEC and SPEC.loader
stackup = importlib.util.module_from_spec(SPEC)
sys.modules["cad_stackup"] = stackup
SPEC.loader.exec_module(stackup)

FIXTURES = HERE / "fixtures"
_FX_SPEC = importlib.util.spec_from_file_location(
    "cad_record_mi_fixtures", SKILL.parent / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
assert _FX_SPEC and _FX_SPEC.loader
fx = sys.modules.get("cad_record_mi_fixtures") or importlib.util.module_from_spec(_FX_SPEC)
if "cad_record_mi_fixtures" not in sys.modules:
    sys.modules["cad_record_mi_fixtures"] = fx
    _FX_SPEC.loader.exec_module(fx)
intent = fx.intent_module()
ON = date(2026, 6, 1)

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
        spec = importlib.util.spec_from_file_location(name, SKILL.parent / "cad-record" / "scripts" / "evidence.py")
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def check_result_validator():
    """A validator of cad-record manufacturing-check-result.schema.json, the packet contract, offline."""
    return fx.ref_validator("https://srobroek.com/cad/schemas/manufacturing-check-result.schema.json")


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

    def test_press_fit_pin_is_unknown_and_names_the_missing_source(self) -> None:
        stack = fixture("closing_gap.yaml")
        stack["requirement"]["min"] = -1.0
        documents = closing_gap_documents()
        documents["interfaces.yaml"]["interfaces"][0]["limits"] = {
            "press_fit": {"fit": "5 H7/p6", "hole": {"PN": "TST-0001", "feature": "cover_seat"},
                          "shaft": {"PN": "TST-0002", "feature": "shoulder_length"}}}
        result = self.bound(documents, stack, "closing-gap")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.press_fit_unknown"])
        finding = result["findings"][0]
        self.assertEqual(finding["outcome"], "unknown")
        self.assertIn("no qualifying press-fit source", finding["measured"]["reason"])
        self.assertIn("printed coupon on the target printer and material", finding["measured"]["reason"])

    def test_a_stack_without_a_press_fit_has_no_press_fit_finding(self) -> None:
        stack = fixture("closing_gap.yaml")
        stack["requirement"]["min"] = -1.0
        result = self.bound(closing_gap_documents(), stack, "closing-gap")
        self.assertNotIn("stack.press_fit_unknown", codes(result))

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

    def test_off_a_guideline_source_qualifies_manufacturing_variation(self) -> None:
        stack = fixture("closing_gap.yaml")
        contributor(stack, "S")["source"] = fx.source("guideline", "fdm.generic", revision="1", sha256="a" * 64)
        result = stackup.compute(stack, calibration=False)
        self.assertNotIn("stack.limits_unqualified", codes(result))

    def test_off_no_remedy_asks_for_a_calibrated_capability(self) -> None:
        stack = fixture("closing_gap.yaml")
        contributor(stack, "S")["source"] = fx.source("user", "drawing tolerance")
        result = stackup.compute(stack, calibration=False)
        remedies = [f["remedy"] for f in result["findings"] if f["code"] == "stack.limits_unqualified"]
        self.assertTrue(remedies)
        self.assertFalse([r for r in remedies if "calibrated" in r], remedies)

    def test_a_drawn_tolerance_alone_does_not_qualify_manufacturing_variation(self) -> None:
        stack = fixture("closing_gap.yaml")
        spacer = contributor(stack, "S")
        spacer["source"] = fx.source("user", "drawing tolerance")
        self.assertEqual(stackup.compute(stack)["outcome"], "unknown")

    def test_a_guideline_source_does_not_qualify_manufacturing_variation(self) -> None:
        stack = fixture("closing_gap.yaml")
        spacer = contributor(stack, "S")
        spacer["source"] = fx.source("guideline", "fdm.generic", revision="1", sha256="a" * 64)
        result = stackup.compute(stack)
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("stack.limits_unqualified", codes(result))

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

    def test_a_stack_without_intent_version_2_is_refused(self) -> None:
        stack = fixture("closing_gap.yaml")
        for version in (1, "2", True):
            with self.subTest(intent_version=version), self.assertRaisesRegex(stackup.StackError, "intent_version must be 2"):
                stackup.compute({**stack, "intent_version": version})
        del stack["intent_version"]
        with self.assertRaisesRegex(stackup.StackError, "intent_version must be 2"):
            stackup.compute(stack)

    def test_contribution_shares_sum_to_100(self) -> None:
        # Hand derivation from closing_gap.yaml: half-tolerances H 0.08, B 0.06, S 0.06, C 0.02, T 0.08 sum to
        # 0.30, so each share is 100 * half / 0.30:
        #   H = T = 26.666667 (80/3)   B = S = 20   C = 6.666667 (20/3)
        # The result rounds each share to 6 decimals, so the shares sum to 100 within 5 * 0.5e-6; allow 1e-5.
        # Dropped from the v1 test: the rss_contribution_pct half (that field is not in a v2 result) and the flat
        # row key (v2 keeps the share under row["resolved"]).
        rows = stackup.compute(fixture("closing_gap.yaml"))["contributors"]
        expected = {"H": 80 / 3, "B": 20.0, "S": 20.0, "C": 20 / 3, "T": 80 / 3}
        self.assertEqual(len(rows), 5)
        shares = {row["id"]: row["resolved"]["wc_contribution_pct"] for row in rows}
        self.assertEqual(shares.keys(), expected.keys())
        for ident, share in expected.items():
            with self.subTest(contributor=ident):
                self.assertAlmostEqual(shares[ident], share, delta=1e-6)
        self.assertAlmostEqual(sum(shares.values()), 100.0, delta=1e-5)

    def test_invalid_contributors_are_rejected(self) -> None:
        # Dropped from the v1 test: the legacy requirement method rss case, which
        # test_perfect_dimensions_and_double_counting_are_refused already covers. One bad change per case.
        baseline = fixture("closing_gap.yaml")
        self.assertEqual(stackup.compute(baseline)["outcome"], "fail")  # a valid v2 stack that computes

        def negative_tolerance(s: dict) -> None:
            contributor(s, "H")["tol"] = -0.08

        def both_tolerance_forms(s: dict) -> None:
            contributor(s, "H").update(tol_plus=0.08, tol_minus=0.08)

        def missing_tol_minus(s: dict) -> None:
            entry = contributor(s, "H")
            del entry["tol"]
            entry["tol_plus"] = 0.08

        def non_numeric_nominal(s: dict) -> None:
            contributor(s, "H")["nominal"] = "20.22"

        def bad_direction(s: dict) -> None:
            contributor(s, "H")["direction"] = 2

        def no_contributors(s: dict) -> None:
            s["contributors"] = []

        cases = [
            (negative_tolerance, "tolerances are magnitudes and must be >= 0"),
            (both_tolerance_forms, "give either tol or tol_plus/tol_minus, not both"),
            (missing_tol_minus, "needs tol, or both tol_plus and tol_minus"),
            (non_numeric_nominal, r"contributors\[0\]\.nominal must be a finite number, got '20\.22'"),
            (bad_direction, r"contributors\[0\]\.direction must be 1 or -1, got 2"),
            (no_contributors, "contributors must be a non-empty list"),
        ]
        for mutate, message in cases:
            stack = copy.deepcopy(baseline)
            mutate(stack)
            with self.subTest(case=mutate.__name__), self.assertRaisesRegex(stackup.StackError, message):
                stackup.compute(stack)

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


GL = stackup.GUIDELINES
GENERIC_SHA = "ab" * 32


def clearance_catalog(*limits: dict, revision: int = 2, family_limits: dict[str, dict] | None = None) -> object:
    """A hand-built catalog holding fdm.generic with `limits`, so these tests do not depend on the shipped file.
    `family_limits` adds one set per material family (family -> its sliding_clearance limit)."""
    generic = GL.Guideline("fdm.generic", revision, "TST generic", (), False, {"process": "fdm"}, tuple(limits),
                           "manufacturing/guidelines/fdm.generic.yaml", "shipped", GENERIC_SHA)
    sets = {"fdm.generic": generic}
    for family, family_limit in (family_limits or {}).items():
        ident = f"fdm.material.{family}"
        sets[ident] = GL.Guideline(ident, 1, f"TST {family}", (), False, {"process": "fdm", "material_family": (family,)},
                                   (family_limit,), f"manufacturing/guidelines/{ident}.yaml", "shipped", "cd" * 32)
    return GL.Catalog(sets)


def extending_catalog(parent: str) -> object:
    """A catalog whose fdm.child extends `parent`; `parent` is fdm.generic (revision 2) or not in the catalog."""
    child = GL.Guideline("fdm.child", 1, "TST child", (parent,), False, {"process": "fdm"}, (clearance_limit(),),
                         "manufacturing/guidelines/fdm.child.yaml", "shipped", GENERIC_SHA)
    sets = {"fdm.child": child}
    if parent == "fdm.generic":
        sets[parent] = GL.Guideline(parent, 2, "TST generic", (), False, {"process": "fdm"}, (clearance_limit(),),
                                    "manufacturing/guidelines/fdm.generic.yaml", "shipped", GENERIC_SHA)
    return GL.Catalog(sets)


def clearance_limit(value: float = 0.3, operator: str = ">=") -> dict:
    return {"characteristic": "sliding_clearance", "operator": operator, "quantity": {"value": value, "units": "mm"},
            "citations": [{"kind": "supplier", "reference": "TST clearance guide", "accessed": "2026-11-20"}]}


def clearance_documents(*, pn: str = "TST-0001", relevant: tuple[str, ...] = ("manufacture", "coating", "thermal"),
                        method: str = "worst_case") -> dict:
    """closing_gap_documents with IF-001's sliding surface on `pn`."""
    documents = closing_gap_documents(relevant=relevant, method=method)
    documents["interfaces.yaml"]["interfaces"][0]["functional"]["surfaces"] = [
        {"PN": pn, "feature": "pivot_bore", "role": "sliding", "dimension_state": "assembled"}]
    return documents


def two_sliding_documents() -> dict:
    """clearance_documents with a second printed sliding part, TST-0005, in the material family tst_b (TST-0001 is tst_a)."""
    documents = clearance_documents()
    documents["interfaces.yaml"]["interfaces"][0]["functional"]["surfaces"].append(
        {"PN": "TST-0005", "feature": "pivot_bore", "role": "sliding", "dimension_state": "assembled"})
    second = copy.deepcopy(documents["parts/TST-0001/part.yaml"])
    second["PN"] = "TST-0005"
    second["intent"]["material_profile"] = "tst_polymer_b"
    documents["parts/TST-0005/part.yaml"] = second
    documents["assembly.yaml"]["subassemblies"][0]["parts"].append("TST-0005")
    profiles = documents["manufacturing/materials.yaml"]["profiles"]
    profiles[0]["material_family"] = fx.known("tst_a")
    other = copy.deepcopy(profiles[0])
    other.update(id="tst_polymer_b", material_family=fx.known("tst_b"))
    profiles.append(other)
    return documents


def clearance_stack(per: str = "side", *, nominal_gap: float = 0.20, requirement_min: float = -1.0) -> dict:
    stack = fixture("closing_gap.yaml")
    stack["requirement"]["min"] = requirement_min
    contributor(stack, "H")["nominal"] = round(20.02 + nominal_gap, 6)
    stack["clearance"] = {"characteristic": "sliding_clearance", "per": per}
    return stack


def pinned(documents: dict) -> None:
    documents["manufacturing/capabilities.yaml"]["profiles"][0]["guidelines"] = ["fdm.generic@2", "fdm.material.nope@1"]

def no_material(documents: dict) -> None:
    documents["parts/TST-0001/part.yaml"]["intent"]["material_profile"] = "tst_unrecorded_material"

def no_profile(documents: dict) -> None:
    documents["parts/TST-0001/part.yaml"]["intent"]["route"][0]["capability"] = "tst_unrecorded_printer"

def unknown_material(documents: dict) -> None:
    documents["parts/TST-0001/part.yaml"]["intent"]["material_profile"] = fx.unknown("material not chosen yet")

def absent_part(documents: dict) -> None:
    documents["interfaces.yaml"]["interfaces"][0]["functional"]["surfaces"][0]["PN"] = "TST-0099"

def no_sliding_surface(documents: dict) -> None:
    documents["interfaces.yaml"]["interfaces"][0]["functional"]["surfaces"][0]["role"] = "fixed"

def unknown_family(documents: dict) -> None:
    documents["manufacturing/materials.yaml"]["profiles"][0]["material_family"] = fx.unknown("family not tested yet")

def family_tst_a(documents: dict) -> None:
    documents["manufacturing/materials.yaml"]["profiles"][0]["material_family"] = fx.known("tst_a")

def one_of(*sizes: float) -> dict:
    return {**clearance_limit(), "operator": "one_of", "quantities": [{"value": size, "units": "mm"} for size in sizes]}

def relative(to: str) -> dict:
    limit = {**clearance_limit(), "relative": {"relative_to": to, "factor": 1.0, "floor": {"value": 0.1, "units": "mm"}}}
    del limit["quantity"]
    return limit

def pin_generic_at_revision_1(documents: dict) -> None:
    documents["manufacturing/capabilities.yaml"]["profiles"][0]["guidelines"] = ["fdm.generic@1"]

def no_nozzle(documents: dict) -> None:
    documents["manufacturing/capabilities.yaml"]["profiles"][0]["parameters"].pop("nozzle_diameter")


def unknown_clearance_cases() -> dict:
    """label -> (documents or a function that edits them, catalog, bound, reason, remedy), one per way the clearance is unknown."""
    generic = clearance_catalog(clearance_limit())
    fallback = "resolve the guideline problem the reason names"
    cases = {
        "no bound record": (clearance_documents(), generic, False, "no bound record", "bind the stack to a hardware record"),
        "no sliding part printed by fdm": (clearance_documents(pn="TST-0002"), generic, True, "no sliding part is printed by fdm",
                                           "record an fdm route operation for a sliding part"),
        "no capability profile": (no_profile, generic, True, "capability profile", "name a capability profile"),
        "material missing from the record": (no_material, generic, True, "tst_unrecorded_material",
                                             "add the material profile tst_unrecorded_material"),
        "material profile unknown": (unknown_material, generic, True, "material profile is unknown",
                                     "record the material profile of TST-0001"),
        "sliding part absent from the record": (absent_part, generic, True, "TST-0099 is not a part of the record",
                                                "add TST-0099 to the record"),
        "no sliding surface": (no_sliding_surface, generic, True, "records no surface with role sliding",
                               "record the sliding surfaces"),
        "no catalog": (clearance_documents(), None, True, "no guideline catalog", "give the run a guideline catalog"),
        "the resolver finds no limit": (clearance_documents(), clearance_catalog(), True, "sliding_clearance",
                                        "record a guideline set that states a minimum sliding clearance"),
        "a scope fact is unknown": (unknown_family, clearance_catalog(family_limits={"tst_a": {**clearance_limit(), "operator": "equals", "value": True}}), True,
                                    "scope fact is unknown", "record the scope fact"),
        "equally specific limits conflict": (clearance_documents(), clearance_catalog(clearance_limit(0.3, "<"), clearance_limit(0.3, ">=")),
                                             True, "share no value", "change one of the named guideline limits"),
        "a set and a range share no size": (family_tst_a, clearance_catalog(clearance_limit(1.0), family_limits={"tst_a": one_of(0.2, 0.3)}),
                                            True, "share no size", "change the named discrete set or range"),
        "a relative limit without a nozzle diameter": (no_nozzle, clearance_catalog(relative("nozzle_diameter")), True,
                                                       "states no nozzle diameter", "record the nozzle diameter"),
        "a relative limit without a line width": (no_nozzle, clearance_catalog(relative("line_width")), True,
                                                  "neither a line width nor a nozzle diameter", "record the line width or the nozzle diameter"),
        "an unsupported operator": (clearance_documents(), clearance_catalog(clearance_limit(0.3, "~")), True,
                                    "operator ~ is not supported", fallback),
        "an extends parent that is not a known set": (clearance_documents(), extending_catalog("fdm.nope"), True,
                                                      "fdm.nope is not a known guideline set", fallback),
        "a pin at another revision": (pin_generic_at_revision_1, extending_catalog("fdm.generic"), True, "is pinned at revision 1", fallback),
        "no operator can state the limit": (clearance_documents(), clearance_catalog(clearance_limit(0.3, ">"), clearance_limit(0.5, "<=")),
                                            True, "cannot be stated", "state the sliding clearance with limits one operator"),
        "a maximum is no minimum": (clearance_documents(), clearance_catalog(clearance_limit(0.3, "<=")), True, "<=",
                                    "state the sliding clearance as a minimum"),
        "a pin that is not applied": (pinned, generic, True, "not applied", "correct the guideline pin"),
    }
    return cases



class ClearanceTests(unittest.TestCase):
    """The stack-level sliding-clearance check: the nominal gap per side against the guideline minimum."""

    def run_stack(self, documents: dict, stack: dict, catalog: object | None, *, bound: bool = True,
                  calibration: bool = True) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            hw = fx.write_record(Path(directory) / "project" / "hw", documents)
            write_case(hw, "closing-gap", stack)
            record = intent.load_record(hw) if bound else None
            result = stackup.compute(stack, record, case="closing-gap", on=ON, decisions=frozenset({"DR-001"}),
                                     catalog=catalog, calibration=calibration)
        for finding in result["findings"]:
            self.assertEqual(list(stackup._validator("finding").iter_errors(finding)), [], finding["id"])
        return result

    def test_a_nominal_gap_below_the_guideline_minimum_fails_and_cites_the_set(self) -> None:
        result = self.run_stack(clearance_documents(), clearance_stack("side"), clearance_catalog(clearance_limit()))
        self.assertEqual(result["outcome"], "fail")
        self.assertEqual(codes(result), ["stack.clearance_below_minimum"])
        row = result["clearance"]
        self.assertEqual((row["status"], row["met"], row["per"]), ("guideline", False, "side"))
        self.assertAlmostEqual(row["nominal_per_side_mm"], 0.2, places=6)
        self.assertEqual((row["minimum_mm"], row["operator"]), (0.3, ">="))
        winner = row["resolutions"][0]["provenance"][0]
        self.assertEqual(winner["guideline"], "fdm.generic@2")
        self.assertEqual(row["guidelines"], {"fdm.generic@2": GENERIC_SHA})
        self.assertNotIn("calibrate", result["findings"][0]["remedy"])

    def test_a_diametral_gap_is_halved_before_it_is_compared(self) -> None:
        stack = clearance_stack("diameter", nominal_gap=0.5)
        result = self.run_stack(clearance_documents(), stack, clearance_catalog(clearance_limit()))
        self.assertAlmostEqual(result["clearance"]["nominal_per_side_mm"], 0.25, places=6)
        self.assertEqual(codes(result), ["stack.clearance_below_minimum"])

    def test_a_met_guideline_clearance_never_raises_an_unknown_stack_to_pass(self) -> None:
        stack = clearance_stack("diameter", nominal_gap=0.8)
        stack["relevant"].append("moisture")  # a relevant effect with no contributor: the stack is unknown
        documents = clearance_documents(relevant=("manufacture", "coating", "thermal", "moisture"))
        result = self.run_stack(documents, stack, clearance_catalog(clearance_limit()))
        self.assertTrue(result["clearance"]["met"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(codes(result), ["stack.effect_missing"])

    def test_a_met_guideline_clearance_leaves_a_passing_stack_passing_and_adds_no_finding(self) -> None:
        stack = clearance_stack("side", nominal_gap=0.4)
        result = self.run_stack(clearance_documents(), stack, clearance_catalog(clearance_limit()))
        self.assertTrue(result["clearance"]["met"])
        self.assertEqual((result["outcome"], codes(result)), ("pass", []))

    def test_each_missing_input_makes_the_clearance_unknown_with_its_reason(self) -> None:
        cases = unknown_clearance_cases()
        for label, (documents, catalog, bound, reason, remedy) in cases.items():
            with self.subTest(label):
                if callable(documents):
                    base = clearance_documents()
                    documents(base)
                    documents = base
                result = self.run_stack(documents, clearance_stack("side"), catalog, bound=bound)
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(codes(result), ["stack.clearance_unknown"])
                self.assertEqual(result["clearance"]["status"], "unknown")
                self.assertIn(reason, result["findings"][0]["measured"]["reason"])
                self.assertIn(remedy, result["findings"][0]["remedy"])

    def test_the_largest_minimum_across_the_sliding_parts_governs(self) -> None:
        catalog = clearance_catalog(clearance_limit(0.3), family_limits={"tst_b": clearance_limit(0.5)})
        result = self.run_stack(two_sliding_documents(), clearance_stack("side", nominal_gap=0.4), catalog)
        row = result["clearance"]
        self.assertEqual((row["parts"], row["minimum_mm"], row["met"]), (["TST-0001", "TST-0005"], 0.5, False))
        self.assertEqual((result["outcome"], codes(result)), ("fail", ["stack.clearance_below_minimum"]))

    def test_a_strict_minimum_is_not_met_by_an_equal_gap(self) -> None:
        catalog = clearance_catalog(clearance_limit(0.3, ">"))
        equal = self.run_stack(clearance_documents(), clearance_stack("side", nominal_gap=0.3), catalog)
        self.assertEqual((equal["clearance"]["operator"], equal["clearance"]["met"]), (">", False))
        self.assertEqual(codes(equal), ["stack.clearance_below_minimum"])
        above = self.run_stack(clearance_documents(), clearance_stack("side", nominal_gap=0.31), catalog)
        self.assertTrue(above["clearance"]["met"])

    def test_equal_minima_keep_the_strict_one(self) -> None:
        catalog = clearance_catalog(clearance_limit(0.3, ">="), family_limits={"tst_b": clearance_limit(0.3, ">")})
        result = self.run_stack(two_sliding_documents(), clearance_stack("side", nominal_gap=0.3), catalog)
        self.assertEqual((result["clearance"]["operator"], result["clearance"]["met"]), (">", False))

    def test_an_unknown_nominal_gap_leaves_the_clearance_unknown(self) -> None:
        stack = clearance_stack("side")
        thermal = contributor(stack, "T")
        for key in ("direction", "nominal", "tol", "source"):
            thermal.pop(key)
        thermal.update(status="unknown", reason="no thermal expansion analysis yet")
        result = self.run_stack(clearance_documents(), stack, clearance_catalog(clearance_limit()))
        self.assertEqual(codes(result), ["stack.clearance_unknown", "stack.contributor_unknown"])
        self.assertEqual(result["clearance"]["status"], "unknown")
        self.assertIn("nominal gap is not established", result["clearance"]["reasons"][0])
        remedy = next(f for f in result["findings"] if f["code"] == "stack.clearance_unknown")["remedy"]
        self.assertIn("establish the unknown contributor", remedy)
        self.assertNotIn("guideline", remedy)

    def test_a_sliding_part_with_an_unknown_route_makes_the_clearance_unknown(self) -> None:
        documents = two_sliding_documents()
        documents["parts/TST-0005/part.yaml"]["intent"]["route"] = fx.unknown("route not recorded yet")
        catalog = clearance_catalog(clearance_limit(), family_limits={"tst_b": clearance_limit(0.5)})
        result = self.run_stack(documents, clearance_stack("side", nominal_gap=0.4), catalog)
        self.assertEqual(codes(result), ["stack.clearance_unknown"])
        self.assertIn("TST-0005's route is unknown", result["clearance"]["reasons"][0])
        self.assertIn("record the route of TST-0005", result["findings"][0]["remedy"])

    def test_a_calibrated_sliding_clearance_is_reported_as_not_judged_in_stackup(self) -> None:
        documents = clearance_documents()
        documents["manufacturing/capabilities.yaml"]["profiles"][0]["calibrations"].append(
            fx.calibration("slide_coupon", "sliding_clearance", "tst_polymer_printed", fx.quantity(lower=0.25, upper=0.35)))
        result = self.run_stack(documents, clearance_stack("side", nominal_gap=0.4), clearance_catalog(clearance_limit()))
        self.assertEqual(codes(result), ["stack.clearance_unknown"])
        self.assertEqual(result["clearance"]["status"], "unknown")
        self.assertIn("calibration not judged in stackup", result["clearance"]["reasons"][0])
        remedy = result["findings"][0]["remedy"]
        self.assertIn("by hand", remedy)
        self.assertNotIn("record a guideline set", remedy)

    def test_off_a_recorded_calibrated_sliding_clearance_is_not_used_and_the_guideline_governs(self) -> None:
        documents = clearance_documents()
        documents["manufacturing/capabilities.yaml"]["profiles"][0]["calibrations"].append(
            fx.calibration("slide_coupon", "sliding_clearance", "tst_polymer_printed", fx.quantity(lower=0.25, upper=0.35)))
        result = self.run_stack(documents, clearance_stack("side", nominal_gap=0.4), clearance_catalog(clearance_limit()),
                                calibration=False)
        self.assertNotIn("stack.clearance_unknown", codes(result))
        row = result["clearance"]
        self.assertEqual((row["status"], row["met"], row["minimum_mm"]), ("guideline", True, 0.3))

    def test_a_clearance_failure_keeps_the_statistical_governs_note_of_a_justified_stack(self) -> None:
        stack = IntentStackContractTests.justified(clearance_stack("side"))
        stack["consequence"] = "performance"
        stack["requirement"]["min"] = 0.0  # the worst case -0.1..0.5 misses it, the approved statistics meet it
        result = self.run_stack(clearance_documents(method="justified_rss"), stack, clearance_catalog(clearance_limit()))
        self.assertEqual((result["requirement"]["governing"], result["outcome"]), ("justified_rss", "fail"))
        self.assertEqual(codes(result), ["stack.clearance_below_minimum"])
        self.assertTrue(any("approved statistical interval governs" in text for text in result["limits"]))

    def test_a_justified_stack_whose_statistical_interval_also_misses_gains_no_statistical_note(self) -> None:
        stack = IntentStackContractTests.justified(clearance_stack("side"))
        stack["consequence"] = "performance"
        stack["requirement"]["min"] = 0.15  # the statistics 0.1..0.3 miss it as well, so they accept nothing
        result = self.run_stack(clearance_documents(method="justified_rss"), stack, clearance_catalog(clearance_limit()))
        self.assertEqual((result["requirement"]["governing"], result["requirement"]["statistical_pass"]), ("justified_rss", False))
        self.assertEqual(result["outcome"], "fail")
        self.assertFalse(any("statistical interval governs" in text for text in result["limits"]))

    def test_the_shipped_catalog_sliding_clearance_resolves_in_a_stack(self) -> None:
        shipped = GL.discover(None)
        result = self.run_stack(clearance_documents(), clearance_stack("side"), shipped)
        row = result["clearance"]
        self.assertEqual((row["status"], row["minimum_mm"], row["operator"], row["met"]), ("guideline", 0.3, ">=", False))
        generic = shipped.guidelines["fdm.generic"]
        self.assertEqual(row["guidelines"]["fdm.generic@2"], generic.sha256)
        self.assertEqual(codes(result), ["stack.clearance_below_minimum"])

    def test_the_packet_depends_on_every_guideline_set_the_clearance_resolved_through(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stack_path = Path(directory) / "case.yaml"
            stack_path.write_text("id: STK\n", encoding="utf-8")
            found = stackup.packet_dependencies(Path(directory), stack_path, {}, {"fdm.generic@2": GENERIC_SHA})
        self.assertEqual(found["guideline_fdm_generic_r2"], GENERIC_SHA)
        self.assertIn("stack_case", found)

    def test_a_stack_without_a_clearance_block_has_no_clearance_row(self) -> None:
        stack = clearance_stack()
        del stack["clearance"]
        result = self.run_stack(clearance_documents(), stack, clearance_catalog(clearance_limit()))
        self.assertNotIn("clearance", result)

    def test_the_clearance_block_names_its_characteristic_and_basis(self) -> None:
        for block in ({"characteristic": "sliding_clearance"}, {"characteristic": "min_wall", "per": "side"},
                      {"characteristic": "sliding_clearance", "per": "radius"}, {"per": "side"}):
            with self.subTest(block=block):
                stack = clearance_stack()
                stack["clearance"] = block
                with self.assertRaises(stackup.StackError):
                    self.run_stack(clearance_documents(), stack, clearance_catalog(clearance_limit()))


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

    def test_the_stack_up_reads_the_frozen_guideline_copies_not_the_live_install(self) -> None:
        guidelines = intent._sibling("cad_record_guidelines", "guidelines.py")
        shipped = self.hw.parent / "shipped"

        def shipped_set(minimum: float) -> str:
            return yaml.safe_dump({
                "schema_version": 1, "id": "fdm.material.pa", "revision": 1, "title": "TST", "scope": {"process": "fdm"},
                "limits": [{"characteristic": "min_wall", "operator": ">=", "quantity": fx.quantity(minimum),
                            "citations": [fx.source("datasheet", "TST guide")]}]}, sort_keys=False)

        self.build(closing_gap_documents(), fixture("closing_gap.yaml"))
        shipped.mkdir()
        with unittest.mock.patch.object(guidelines, "SHIPPED", shipped):
            (shipped / "pa.yaml").write_text(shipped_set(1.5), encoding="utf-8")
            for name, data in guidelines.snapshot_files(self.hw).items():  # what a build copies into the version
                target = self.hw / "versions" / "v1" / "record" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            frozen = dict(intent.load_record(self.hw).guideline_digests["tst_printer"]["tst_polymer_printed"])
            (self.hw / "versions" / "v1" / "manifest.json").unlink()
            snapshot_version(self.hw)  # seal the manifest over the copies
            (shipped / "pa.yaml").write_text(shipped_set(2.5), encoding="utf-8")  # the live install changes
            self.assertNotEqual(dict(intent.load_record(self.hw).guideline_digests["tst_printer"]["tst_polymer_printed"]), frozen)
            seen: list = []
            real = intent.load_record

            def spy(hw, **kwargs):
                seen.append(real(hw, **kwargs))
                return seen[-1]

            with unittest.mock.patch.object(intent, "load_record", spy):
                self.run_stack()
        self.assertEqual([p.message for p in seen[0].problems if p.code == "intent.guideline"], [], "a shipped copy is accepted")
        self.assertEqual(dict(seen[0].guideline_digests.get("tst_printer", {}).get("tst_polymer_printed", {})), frozen, "the digests are those of the snapshot")

    def build_with_snapshot_catalog(self) -> None:
        self.build(clearance_documents(), clearance_stack("side", nominal_gap=0.4))
        for name, data in GL.snapshot_files(self.hw).items():  # what a build copies into the version
            target = self.hw / "versions" / "v1" / "record" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.hw / "versions" / "v1" / "manifest.json").unlink()
        snapshot_version(self.hw)

    def test_main_reads_the_clearance_through_the_snapshot_catalog(self) -> None:
        self.build_with_snapshot_catalog()
        status, output = self.run_stack()
        row = output["clearance"]
        self.assertEqual((status, row["status"], row["minimum_mm"], row["met"]), (0, "guideline", 0.3, True))

    def test_the_packet_depends_on_the_guideline_set_the_clearance_read(self) -> None:
        self.build_with_snapshot_catalog()
        self.run_stack()
        packet, _ = self.packet()
        shipped = GL.discover(None).guidelines["fdm.generic"].sha256
        self.assertEqual(packet["dependencies"]["guideline_fdm_generic_r2"], shipped)

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
        decision = decision_path(self.hw, "DR-001")
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

    def test_malformed_yaml_exits_2(self) -> None:
        proc = self.run_cli("contributors: [unclosed\n")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid YAML", proc.stderr)

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

    def assembly_stack(self) -> dict:
        """A PN-subject stack of two sourced assembly contributors that passes: 0.2 mm +/- 0.1 against 0.0 to 0.4."""
        source = {"kind": "user", "reference": "TST stack input", "origin": "TST fixture user"}
        return {"intent_version": 2, "id": "STK-021", "title": "PN stack", "units": "mm",
                "subject": {"PN": "TST-0001", "feature": "pivot_bore"}, "dimension_state": "assembled",
                "consequence": "performance", "relevant": ["assembly"],
                "requirement": {"min": 0.0, "max": 0.4, "method": "worst_case", "source": source},
                "contributors": [{"id": "A", "category": "assembly", "nominal": 50.2, "tol": 0.05, "direction": 1, "source": source},
                                 {"id": "B", "category": "assembly", "nominal": 50.0, "tol": 0.05, "direction": -1, "source": source}]}

    def test_a_snapshot_that_is_not_record_version_2_is_refused_and_nothing_is_published(self) -> None:
        for label, change in (("absent", lambda text: text.replace("record_version: 2\n", "")),
                              ("unknown", lambda text: text.replace("record_version: 2", "record_version: 3")),
                              ("non-mapping", lambda text: "- not\n- a mapping\n")):
            with self.subTest(snapshot=label), tempfile.TemporaryDirectory() as directory:
                hw = fx.write_record(Path(directory) / "project" / "hw", closing_gap_documents())
                case = write_case(hw, "pn-stack", self.assembly_stack())
                snapshot_version(hw)
                product = hw / "versions" / "v1" / "record" / "product.yaml"
                product.write_text(change(product.read_text(encoding="utf-8")), encoding="utf-8")
                version = hw / "versions" / "v1"  # reseal the manifest over the edited snapshot, so only the version is wrong
                files = {path.relative_to(version).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in version.rglob("*") if path.is_file() and path.name != "manifest.json"}
                (version / "manifest.json").write_text(json.dumps({"label": "v1", "files": files}), encoding="utf-8")
                args = ["uv", "run", "--quiet", "--script", str(SCRIPT), str(case), "--hw", str(hw), "--version", "v1"]
                proc = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("record_version", proc.stderr)
                self.assertFalse((hw / "analysis" / "v1").exists(), "no analysis bundle")
                self.assertFalse((hw / "checks").exists(), "no check packet")

    def test_the_same_pn_stack_passes_against_a_record_version_2_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hw = fx.write_record(Path(directory) / "project" / "hw", closing_gap_documents())
            case = write_case(hw, "pn-stack", self.assembly_stack())
            snapshot_version(hw)
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), str(case), "--hw", str(hw),
                                   "--version", "v1"], capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((hw / "analysis" / "v1" / "pn-stack").is_dir())

    def test_rerun_reuses_bundle_and_recipe_change_requires_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            hw = fx.write_record(project / "hw", closing_gap_documents())
            case = write_case(hw, "closing-gap", fixture("closing_gap.yaml"))
            snapshot_version(hw)
            args = ["uv", "run", "--quiet", "--script", str(SCRIPT), str(case), "--hw", str(hw), "--version", "v1"]
            first = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(first.returncode, 1, first.stderr)
            bundle = hw / "analysis" / "v1" / "closing-gap"
            original = {path.relative_to(bundle).as_posix(): path.read_bytes()
                        for path in bundle.rglob("*") if path.is_file()}
            second = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertTrue(json.loads(second.stdout)["reused"])
            self.assertEqual(original, {path.relative_to(bundle).as_posix(): path.read_bytes()
                                        for path in bundle.rglob("*") if path.is_file()})

            case.write_text(case.read_text(encoding="utf-8").replace("Cover closing gap", "Cover closing gap, rechecked"),
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
            stack_path.write_text((FIXTURES / "closing_gap.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), str(stack_path),
                                  "--out", str(root / "outside.json")], capture_output=True, text=True,
                                 check=False, timeout=300)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--out must be under", proc.stderr)


if __name__ == "__main__":
    unittest.main()
