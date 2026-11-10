#!/usr/bin/env python3
"""selection.py: the use case gates a final recommendation; hard constraints (mandatory requirements, capability
and material approvals, the optical route) eliminate before preferences rank; the comparison, the proposal and
its findings; and the read-only CLI. Plan section 11 rows 1-5 and the transparency case, on synthetic TST
records from tests/fixtures/selection/scenarios.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPT = SKILL / "scripts" / "selection.py"


def load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


scenarios = load("cad_record_selection_scenarios", HERE / "fixtures" / "selection" / "scenarios.py")
fx = scenarios.fx
mi = fx.intent_module()
selection = load("cad_record_selection", SCRIPT)


def _validator(ref: str) -> Draft202012Validator:
    contents = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((SKILL / "schemas").glob("*.schema.json"))]
    registry = Registry().with_resources([(item["$id"], Resource.from_contents(item)) for item in contents])
    return Draft202012Validator({"$ref": ref}, registry=registry)


FINDING = _validator("https://srobroek.com/cad/schemas/manufacturing.schema.json#/$defs/finding")
RECOMMENDATION = _validator("https://srobroek.com/cad/schemas/manufacturing-selection.schema.json#/$defs/recommendation")


def write_at(hw: Path, documents: dict[str, Any], *, current_digest: bool = True) -> Path:
    """Write `documents` as a project at `hw`; record the current input digest in selection.yaml unless the test
    wants the placeholder (a stale assessment)."""
    hw = fx.write_record(hw, documents)
    if current_digest:
        path = hw / "manufacturing" / "selection.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["assessment"]["input_sha256"] = selection.input_digest(mi.read_documents(hw)[0])
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return hw


def write(test: unittest.TestCase, documents: dict[str, Any], *, current_digest: bool = True) -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="cad-selection-test-")
    test.addCleanup(temporary.cleanup)
    return write_at(Path(temporary.name) / "project" / "hw", documents, current_digest=current_digest)


def assess(test: unittest.TestCase, documents: dict[str, Any], **options: Any) -> dict[str, Any]:
    report = selection.assess_hw(write(test, documents, **options))
    test.assertEqual(report["problems"], [], "the scenario record itself must be valid")
    return report


_REPORTS: dict[str, dict[str, Any]] = {}
_SCRATCH: list[tempfile.TemporaryDirectory[str]] = []


def setUpModule() -> None:
    _SCRATCH.append(tempfile.TemporaryDirectory(prefix="cad-selection-reports-"))


def tearDownModule() -> None:
    _SCRATCH.pop().cleanup()
    _REPORTS.clear()


def scenario_report(test: unittest.TestCase, build: Any) -> dict[str, Any]:
    """The report of an unmodified scenario, assessed once per module run; tests only read it."""
    name = build.__name__
    if name not in _REPORTS:
        report = selection.assess_hw(write_at(Path(_SCRATCH[0].name) / name / "hw", build()))
        test.assertEqual(report["problems"], [], "the scenario record itself must be valid")
        _REPORTS[name] = report
    return _REPORTS[name]


def only_decision(report: dict[str, Any]) -> dict[str, Any]:
    assert len(report["decisions"]) == 1, report["decisions"]
    return report["decisions"][0]


def candidate(report: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(c for d in report["decisions"] for c in d["candidates"] if c["id"] == ident)


def evaluations(entry: dict[str, Any], constraint: str) -> list[dict[str, Any]]:
    return [e for e in entry["evaluations"] if e["constraint"] == constraint]


def findings(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [f for f in report["findings"] if f["code"] == code]


def tree_digest(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def profile(documents: dict[str, Any], kind: str, ident: str) -> dict[str, Any]:
    return next(p for p in documents[f"manufacturing/{kind}.yaml"]["profiles"] if p["id"] == ident)


def selection_candidate(documents: dict[str, Any], ident: str) -> dict[str, Any]:
    return next(c for c in documents["manufacturing/selection.yaml"]["candidates"] if c["id"] == ident)


class AssessmentBeforeFinalRecommendation(unittest.TestCase):
    """Row 1: missing material-to-choice and safety facts refuse a final recommendation, never a report."""

    def test_an_unassessed_use_case_allows_only_a_conditional_report(self) -> None:
        report = scenario_report(self, scenarios.unassessed_case)
        decision = only_decision(report)
        self.assertEqual((report["status"], selection.exit_status(report)), ("conditional", 1))
        self.assertEqual(candidate(report, "convenient_print")["status"], "feasible", "the print itself meets REQ-001")
        self.assertFalse(decision["final_allowed"])
        recommendation = decision["recommendation"]
        self.assertEqual((recommendation["status"], recommendation["primary"]), ("conditional", "convenient_print"))
        self.assertNotIn("end_use", recommendation, "no end-use route without the use case it serves")
        refused = " | ".join(decision["refused_because"])
        self.assertIn("the use case is not confirmed by a decision", refused)
        self.assertIn("the service class is unknown", refused)
        self.assertIn("Q-001", refused)
        self.assertTrue(set(decision["refused_because"]) <= set(recommendation["limits"]), "the limits state why it is conditional")
        missing = {m["field"]: m for m in report["use_case"]["missing"]}
        self.assertEqual((missing["use_case.service_class"]["question"], missing["use_case.service_class"]["safety_relevant"]),
                         ("Q-001", True))
        self.assertTrue(missing["use_case.environment.temperature"]["could_change_choice_or_safety"])
        self.assertEqual([q["id"] for q in report["use_case"]["open_questions"]][:2], ["Q-001", "Q-002"])
        (finding,) = findings(report, "selection.use_case_incomplete")
        self.assertEqual((finding["severity"], finding["outcome"]), ("major", "unknown"))
        self.assertIn("Q-001", finding["remedy"])
        computed = evaluations(candidate(report, "convenient_print"), "REQ-001")
        self.assertTrue(all("the operating temperature is unknown" in e["uncertainty"] for e in computed))

    def test_the_report_is_read_only(self) -> None:
        hw = write(self, scenarios.unassessed_case())
        before = tree_digest(hw.parent)
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), str(hw.parent)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Missing facts that could change the material or process choice", result.stdout)
        self.assertIn("### Recommendation (conditional)", result.stdout)
        self.assertEqual(tree_digest(hw.parent), before, "selection.py never writes the design record")


class PrintableButUnsuitable(unittest.TestCase):
    """Row 2: a part that prints (envelope, calibrated printer, approved material) still fails its service."""

    def test_a_sourced_heat_criterion_eliminates_the_printable_route(self) -> None:
        report = scenario_report(self, scenarios.hot_loaded_bracket)
        printed = candidate(report, "printed_bracket")
        self.assertEqual((printed["status"], printed["eliminated_by"], printed["rank"]), ("eliminated", ["REQ-002"], None))
        for constraint in ("material_form", "capability", "material_approval", "REQ-001"):
            self.assertTrue(all(e["outcome"] == "pass" for e in evaluations(printed, constraint)), constraint)
        (heat,) = evaluations(printed, "REQ-002")
        self.assertEqual((heat["outcome"], heat["measured"], heat["basis"]), ("fail", {"value": 65.0, "units": "deg_C"}, "supplier_typical"))
        self.assertEqual(heat["sources"][0]["kind"], "datasheet")
        recommendation = only_decision(report)["recommendation"]
        self.assertEqual((recommendation["status"], recommendation["primary"], recommendation["end_use"]),
                         ("final", "machined_bracket", "machined_bracket"))
        self.assertEqual(recommendation["governing_requirements"], ["REQ-002"])
        self.assertTrue(any("printed_bracket is eliminated: REQ-002" in reason for reason in recommendation["reasons"]))
        (finding,) = findings(report, "selection.recorded_route_eliminated")
        self.assertEqual((finding["severity"], finding["subject"]["PN"], finding["requirements"]), ("major", "TST-0201", ["REQ-002"]))
        self.assertEqual(finding["criterion"]["property"], "max_service_temperature")
        self.assertIn("unsuitable for its intended service", finding["consequence"])
        self.assertEqual((report["status"], selection.exit_status(report)), ("final_ready", 0))

    def test_the_eliminated_print_may_only_be_a_limited_prototype(self) -> None:
        report = scenario_report(self, scenarios.hot_loaded_bracket)
        decision = only_decision(report)
        self.assertEqual(decision["recommendation"]["prototype"], "printed_bracket")
        self.assertNotEqual(decision["recommendation"].get("fallback"), "printed_bracket")
        prototype = decision["prototype"]
        self.assertIn("never be the primary, end-use or fallback", prototype["role"])
        cannot = " | ".join(prototype["cannot_validate"])
        self.assertIn("REQ-002: fail on the prototype route", cannot)
        self.assertIn("strength, stiffness, fatigue, creep, heat, wear, service life", cannot)
        self.assertTrue(any("geometry, fit, assembly order" in text for text in prototype["can_validate"]))


class PolymerAcceptedWithEvidence(unittest.TestCase):
    """Row 3: a polymer that demonstrably meets its criteria is recommended; functional is not a reason for metal."""

    def test_a_qualified_polymer_is_the_final_primary(self) -> None:
        report = scenario_report(self, scenarios.qualified_polymer_cover)
        decision = only_decision(report)
        self.assertEqual([(c["id"], c["status"], c["rank"]) for c in decision["candidates"]],
                         [("printed_cover", "feasible", 1), ("machined_cover", "feasible", 2)])
        recommendation = decision["recommendation"]
        self.assertEqual((recommendation["status"], recommendation["primary"], recommendation["fallback"]),
                         ("final", "printed_cover", "machined_cover"))
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["recorded"]["assessment"]["current"])
        self.assertEqual((report["status"], selection.exit_status(report)), ("final_ready", 0))
        accuracy = decision["comparison"][0]["accuracy_finish_stability"]
        self.assertIn("hole_diameter_deviation -0.15..0.05 mm", accuracy)
        self.assertIn("no calibration for tst_metal_d_bar", decision["comparison"][1]["accuracy_finish_stability"])

    def test_a_room_temperature_value_does_not_pass_at_the_operating_temperature(self) -> None:
        documents = scenarios.qualified_polymer_cover()
        polymer = profile(documents, "materials", "tst_polymer_c_printed")
        polymer["properties"][:2] = scenarios.printed("yield_strength", 40, "MPa", temperature=(23, 23))
        report = assess(self, documents)
        printed = candidate(report, "printed_cover")
        self.assertEqual((printed["status"], printed["unknown"]), ("conditional", ["REQ-001"]))
        (strength,) = evaluations(printed, "REQ-001")
        self.assertIn("15..35 deg_C requested", " ".join(strength["reasons"]))
        self.assertEqual(only_decision(report)["recommendation"]["primary"], "machined_cover",
                         "a feasible candidate outranks a conditional one whatever the scores")

    def test_a_candidate_step_without_a_capability_is_conditional_never_a_crash(self) -> None:
        """A schema-invalid step that records no capability (evidence.py assesses records without the schema
        check) reads as capability unknown: the candidate stays conditional and the report still renders."""
        documents = scenarios.qualified_polymer_cover()
        del selection_candidate(documents, "printed_cover")["parts"][0]["route"][0]["capability"]
        report = selection.assess(mi.normalize_record(documents))
        printed = candidate(report, "printed_cover")
        self.assertEqual((printed["status"], printed["unknown"]), ("conditional", ["capability"]))
        (capability,) = evaluations(printed, "capability")
        self.assertIn("the candidate records no capability", capability["reasons"][0])
        self.assertEqual(printed["parts"][0]["route"][0]["capability"], "unknown")
        self.assertEqual(only_decision(report)["recommendation"]["primary"], "machined_cover")
        self.assertIn("printed_cover", selection.render_markdown(report))

    def test_family_guidance_never_passes_a_requirement(self) -> None:
        documents = scenarios.qualified_polymer_cover()
        polymer = profile(documents, "materials", "tst_polymer_c_printed")
        polymer.update(level="family", grade=fx.not_applicable("family guidance only"))
        report = assess(self, documents)
        reasons = " ".join(r for e in evaluations(candidate(report, "printed_cover"), "REQ-001") for r in e["reasons"])
        self.assertEqual(candidate(report, "printed_cover")["status"], "conditional")
        self.assertIn("family guidance", reasons)

    def test_a_recorded_assessment_goes_stale_when_its_inputs_change(self) -> None:
        documents = scenarios.qualified_polymer_cover()
        hw = write(self, documents)
        requirements = hw / "requirements.yaml"
        data = yaml.safe_load(requirements.read_text(encoding="utf-8"))
        data["requirements"][1]["acceptance"][0]["value"] = 55
        requirements.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        report = selection.assess_hw(hw)
        self.assertFalse(report["recorded"]["assessment"]["current"])
        (finding,) = findings(report, "selection.assessment_stale")
        self.assertEqual(finding["severity"], "major")


class JustifiedAlternatives(unittest.TestCase):
    """Row 4: metal, purchased, sheet and hybrid alternatives win when the print cannot meet the service."""

    def test_machined_purchased_and_hybrid_alternatives_replace_a_printed_shaft(self) -> None:
        report = scenario_report(self, scenarios.wear_shaft_options)
        decision = only_decision(report)
        self.assertEqual([(c["id"], c["status"], c["rank"]) for c in decision["candidates"]],
                         [("purchased_shaft", "feasible", 1), ("hybrid_pin", "feasible", 2), ("machined_shaft", "feasible", 3),
                          ("printed_shaft", "eliminated", None)])
        self.assertEqual(candidate(report, "printed_shaft")["eliminated_by"], ["REQ-001", "REQ-002"])
        self.assertEqual(candidate(report, "machined_shaft")["families"], ["additive", "hybrid", "machined"])
        self.assertIn("purchased", candidate(report, "hybrid_pin")["families"])
        recommendation = decision["recommendation"]
        self.assertEqual({key: recommendation.get(key) for key in ("primary", "end_use", "fallback", "prototype")},
                         {"primary": "purchased_shaft", "end_use": "purchased_shaft", "fallback": "hybrid_pin",
                          "prototype": "printed_shaft"})
        self.assertEqual(recommendation["governing_requirements"], ["REQ-001", "REQ-002"])
        kinds = {trigger["kind"] for trigger in decision["change_triggers"]}
        self.assertLessEqual({"loads", "temperature", "life", "tolerances", "equipment", "volume"}, kinds)
        self.assertTrue(any("radial_load above 50 N" in text for text in recommendation["what_would_change"]))
        self.assertTrue(any("TST-0401" in change and "make_buy becomes buy" in change for change in recommendation["proposed_changes"]))
        self.assertTrue(any(text.startswith("cannot validate: REQ-001") for text in recommendation["prototype_validates"]))
        pairs = candidate(report, "machined_shaft")["pairs"]
        self.assertEqual([(p["interface"], p["materials"], p["critical"]) for p in pairs],
                         [("IF-001", ["tst_metal_o_bar", "tst_polymer_m_printed"], True)])
        self.assertIn("IF-001 wear and friction evidence", decision["comparison"][2]["evidence"])
        wear = [f for f in findings(report, "selection.recorded_route_eliminated") if f["requirements"] == ["REQ-002"]]
        self.assertEqual(wear[0]["subject"]["interface"], "IF-001")

    def test_a_missing_evaluation_keeps_an_alternative_conditional(self) -> None:
        documents = scenarios.wear_shaft_options()
        selection_candidate(documents, "purchased_shaft")["evaluations"] = []
        report = assess(self, documents)
        purchased = candidate(report, "purchased_shaft")
        self.assertEqual((purchased["status"], purchased["unknown"]), ("conditional", ["REQ-002"]))
        (unknown,) = evaluations(purchased, "REQ-002")
        self.assertIn("no evaluation states wear_depth <= 0.1 mm", unknown["reasons"][0])
        self.assertEqual(only_decision(report)["recommendation"]["primary"], "hybrid_pin")

    def test_a_sheet_frame_replaces_a_printed_frame_that_is_not_stiff_enough(self) -> None:
        report = scenario_report(self, scenarios.plate_frame_options)
        decision = only_decision(report)
        self.assertEqual(candidate(report, "printed_frame")["eliminated_by"], ["REQ-001"])
        self.assertEqual((decision["recommendation"]["primary"], decision["recommendation"]["fallback"]),
                         ("sheet_frame", "machined_frame"))
        sheet = candidate(report, "sheet_frame")
        self.assertEqual(sheet["families"], ["sheet"])
        self.assertEqual([e["outcome"] for e in evaluations(sheet, "material_approval")], ["pass", "pass"])

    def test_a_laser_approval_without_extraction_is_not_assumed(self) -> None:
        documents = scenarios.plate_frame_options()
        profile(documents, "capabilities", "tst_laser")["materials"][0].pop("extraction")
        report = assess(self, documents)
        sheet = candidate(report, "sheet_frame")
        self.assertEqual((sheet["status"], sheet["unknown"]), ("conditional", ["material_approval"]))
        self.assertEqual(only_decision(report)["recommendation"]["primary"], "machined_frame")

    def test_no_feasible_candidate_names_the_route_families_not_yet_considered(self) -> None:
        documents = scenarios.plate_frame_options()
        documents["manufacturing/selection.yaml"]["candidates"] = documents["manufacturing/selection.yaml"]["candidates"][:1]
        report = assess(self, documents)
        decision = only_decision(report)
        self.assertIsNone(decision["recommendation"])
        self.assertEqual((report["status"], selection.exit_status(report)), ("blocked", 1))
        (finding,) = findings(report, "selection.no_feasible_candidate")
        alternatives = " | ".join(a["text"] for a in finding["alternatives"])
        for family in ("machined", "purchased", "sheet", "hybrid"):
            self.assertIn(family, alternatives)
        self.assertIn("never relax a mandatory requirement", finding["remedy"])


class MandatoryConstraintsBeforeScores(unittest.TestCase):
    """Row 5: hard constraints eliminate before any score ranks; an unknown mandatory result never wins."""

    def test_a_score_never_compensates_a_mandatory_failure(self) -> None:
        report = scenario_report(self, scenarios.mandatory_vs_score)
        decision = only_decision(report)
        self.assertEqual([(c["id"], c["status"], c["rank"], c["preference"]["score"]) for c in decision["candidates"]],
                         [("compliant", "feasible", 1, 10.0), ("unproven", "conditional", 2, 1000.0),
                          ("cheap_unsafe", "eliminated", None, None)])
        cheap = candidate(report, "cheap_unsafe")
        computed = [e for e in evaluations(cheap, "REQ-001") if not e["authored"]]
        self.assertTrue(computed and all(e["outcome"] == "fail" for e in computed))
        self.assertIn("even the typical value fails", " ".join(computed[0]["reasons"]))
        self.assertFalse(cheap["prototype_eligible"], "a critical-risk failure rules it out even as a prototype")
        recommendation = decision["recommendation"]
        self.assertEqual((recommendation["primary"], recommendation["fallback"]), ("compliant", "unproven"))
        self.assertNotIn("prototype", recommendation)
        self.assertIn("not ranked", decision["comparison"][2]["preference"])

    def test_typical_values_cannot_pass_production_service(self) -> None:
        report = scenario_report(self, scenarios.mandatory_vs_score)
        unproven = candidate(report, "unproven")
        self.assertEqual(unproven["unknown"], ["REQ-001"])
        reasons = " ".join(r for e in evaluations(unproven, "REQ-001") for r in e["reasons"])
        self.assertIn("needs a justified design allowable", reasons)
        compliant = evaluations(candidate(report, "compliant"), "REQ-001")
        self.assertEqual([(e["outcome"], e["basis"]) for e in compliant], [("pass", "allowable")])

    def test_authored_passes_do_not_override_computed_failures_or_weaker_criteria(self) -> None:
        report = scenario_report(self, scenarios.mandatory_vs_score)
        (conflict,) = findings(report, "selection.evaluation_conflict")
        self.assertEqual((conflict["severity"], conflict["requirements"]), ("major", ["REQ-001"]))
        (mismatch,) = findings(report, "selection.evaluation_mismatch")
        self.assertEqual(mismatch["criterion"]["value"], 100)
        self.assertIn("yield_strength >= 30 MPa", mismatch["measured"]["text"])
        self.assertEqual(candidate(report, "unproven")["status"], "conditional")

    def test_a_recorded_final_choice_of_an_eliminated_candidate_blocks(self) -> None:
        report = scenario_report(self, scenarios.mandatory_vs_score)
        self.assertEqual({f["severity"] for f in findings(report, "selection.recorded_recommendation_eliminated")}, {"critical"})
        self.assertEqual(len(findings(report, "selection.final_unsupported")), 1)
        self.assertEqual(findings(report, "selection.recorded_route_eliminated")[0]["severity"], "critical")
        self.assertEqual((report["status"], selection.exit_status(report)), ("blocked", 1))

    def test_a_conditional_candidate_is_never_a_final_primary(self) -> None:
        documents = scenarios.mandatory_vs_score()
        candidates = documents["manufacturing/selection.yaml"]["candidates"]
        documents["manufacturing/selection.yaml"]["candidates"] = [c for c in candidates if c["id"] != "compliant"]
        report = assess(self, documents)
        decision = only_decision(report)
        self.assertEqual((decision["recommendation"]["primary"], decision["recommendation"]["status"]), ("unproven", "conditional"))
        self.assertFalse(decision["final_allowed"])
        self.assertTrue(any("conditional" in reason for reason in decision["refused_because"]))
        self.assertEqual(len(findings(report, "selection.primary_unknown")), 1)


class TransparencyIsAHardConstraint(unittest.TestCase):
    """Decision 2: an optical part is a purchased transparent part or identified transparent stock, never a print."""

    def test_opaque_processes_are_eliminated_and_identified_stock_is_recommended(self) -> None:
        report = scenario_report(self, scenarios.transparent_cover)
        decision = only_decision(report)
        self.assertEqual({c["id"]: c["status"] for c in decision["candidates"]},
                         {"laser_window": "feasible", "purchased_window": "conditional", "fdm_window": "eliminated",
                          "resin_window": "eliminated"})
        resin = candidate(report, "resin_window")
        self.assertEqual(resin["eliminated_by"], ["optical_route"])
        self.assertEqual([e["outcome"] for e in evaluations(resin, "REQ-001")], ["pass"],
                         "a passing transmission figure does not make an opaque process a transparent route")
        self.assertFalse(candidate(report, "fdm_window")["prototype_eligible"])
        recommendation = decision["recommendation"]
        self.assertEqual((recommendation["primary"], recommendation["fallback"]), ("laser_window", "purchased_window"))
        self.assertNotIn("prototype", recommendation, "see-through FDM is not offered even as a prototype")
        (optical,) = decision["optical"]
        criteria = {c["name"]: c for c in optical["purchase_criteria"]}
        self.assertEqual(list(criteria), ["material_identity_grade", "optical_grade", "thickness", "supplier_evidence"])
        self.assertEqual(criteria["optical_grade"]["value"], "light_transmission 92 percent (supplier_typical)")
        self.assertEqual(criteria["supplier_evidence"]["sources"][0]["kind"], "datasheet")
        self.assertEqual(criteria["thickness"]["status"], "unknown")
        self.assertTrue(any("thickness" in change for change in recommendation["proposed_changes"]))
        critical = [f for f in findings(report, "selection.recorded_route_eliminated") if f["severity"] == "critical"]
        self.assertEqual(critical[0]["criterion"]["property"], "optical_route")
        self.assertIn("purchase criteria", critical[0]["remedy"])
        purchased = evaluations(candidate(report, "purchased_window"), "optical_route")[0]
        self.assertIn("not recorded", purchased["reasons"][0])

    def test_recorded_purchase_criteria_make_a_purchased_window_feasible(self) -> None:
        documents = scenarios.transparent_cover()
        part = documents["parts/TST-0601/part.yaml"]
        part.update(make_buy="buy", procurement={"mode": "catalog"}, process="purchased")
        part["intent"]["route"] = [fx.operation("buy", "purchased", purchase={
            "identity": fx.known("TST window 1"), "specification": fx.known("TST clear grade"),
            "ratings": fx.not_applicable("no rating applies"), "optical": fx.known("TST optical grade A"),
            "thickness": fx.known(fx.quantity(3)), "supplier_evidence": [fx.source("supplier", "TST window datasheet")]})]
        report = assess(self, documents)
        purchased = candidate(report, "purchased_window")
        self.assertEqual(purchased["status"], "feasible")
        self.assertEqual([e["outcome"] for e in evaluations(purchased, "optical_route")], ["pass"])


class ComparisonOutput(unittest.TestCase):
    def test_findings_and_recommendations_follow_the_record_schemas(self) -> None:
        for name in ("unassessed_case", "hot_loaded_bracket", "qualified_polymer_cover", "wear_shaft_options",
                     "plate_frame_options", "mandatory_vs_score", "transparent_cover"):
            with self.subTest(scenario=name):
                report = scenario_report(self, getattr(scenarios, name))
                for finding in report["findings"]:
                    self.assertEqual([e.message for e in FINDING.iter_errors(finding)], [], finding["id"])
                ids = [finding["id"] for finding in report["findings"]]
                self.assertEqual(len(ids), len(set(ids)))
                for decision in report["decisions"]:
                    if decision["recommendation"] is not None:
                        self.assertEqual([e.message for e in RECOMMENDATION.iter_errors(decision["recommendation"])], [])

    def test_the_report_is_deterministic_and_renders_every_column(self) -> None:
        hw = write(self, scenarios.wear_shaft_options())
        first, second = selection.assess_hw(hw), selection.assess_hw(hw)
        self.assertEqual(json.dumps(first, sort_keys=False), json.dumps(second, sort_keys=False))
        decision = only_decision(first)
        for row in decision["comparison"]:
            self.assertEqual(list(row), [key for key, _ in selection.COLUMNS])
        markdown = selection.render_markdown(first)
        header = "| " + " | ".join(title for _, title in selection.COLUMNS) + " |"
        self.assertIn(header, markdown)
        for ident in ("purchased_shaft", "hybrid_pin", "machined_shaft", "printed_shaft"):
            self.assertIn(f"| {ident}: ", markdown)
        self.assertIn("cost 8 EUR (estimate", decision["comparison"][0]["cost_lead_time"])
        self.assertIn("at 10 count", decision["comparison"][0]["cost_lead_time"])

    def test_candidates_for_different_parts_are_separate_decisions(self) -> None:
        documents = scenarios.hot_loaded_bracket()
        documents["parts/TST-0202/part.yaml"] = scenarios.made_part("TST-0202", "Cover", "fdm", "tst_polymer_a_printed", "tst_printer")
        documents["manufacturing/selection.yaml"]["candidates"].append(scenarios.candidate(
            "printed_cover", "Printed cover", "one printed part",
            [scenarios.cpart("TST-0202", "tst_polymer_a_printed", scenarios.step("fdm", "tst_printer", "local"))], cost=2))
        report = assess(self, documents)
        self.assertEqual([d["parts"] for d in report["decisions"]], [["TST-0201"], ["TST-0202"]])
        self.assertEqual(report["decisions"][1]["recommendation"]["primary"], "printed_cover")

    def test_every_material_property_has_one_comparison_group(self) -> None:
        grouped = [name for _, names in selection.PROPERTY_GROUPS for name in names]
        self.assertEqual(sorted(grouped), sorted(mi.MATERIAL_PROPERTIES))


class CommandLine(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True, text=True, check=False)

    def test_exit_status_follows_the_report(self) -> None:
        hw = write(self, scenarios.qualified_polymer_cover())
        result = self.run_cli(str(hw), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "final_ready")
        out = hw.parent / ".cad-work" / "selection" / "report.json"
        result = self.run_cli(str(hw.parent), "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["decisions"][0]["recommendation"]["primary"], "printed_cover")
        refused = self.run_cli(str(hw), "--out", str(hw / "report.json"))
        self.assertEqual(refused.returncode, 2)
        self.assertFalse((hw / "report.json").exists())

    def test_an_invalid_or_missing_record_exits_2(self) -> None:
        documents = scenarios.qualified_polymer_cover()
        selection_candidate(documents, "printed_cover")["parts"][0]["material_profile"] = "tst_undeclared"
        hw = write(self, documents)
        result = self.run_cli(str(hw), "--json")
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["decisions"]), ("invalid", []))
        self.assertIn("intent.reference", {p["code"] for p in report["problems"]})
        self.assertEqual(self.run_cli(str(hw.parent / "missing")).returncode, 2)

    def test_a_legacy_record_is_readable_and_not_assessed(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-selection-legacy-")
        self.addCleanup(temporary.cleanup)
        hw = Path(temporary.name) / "hw"
        shutil.copytree(fx.LAMP, hw)
        before = tree_digest(hw)
        result = self.run_cli(str(hw), "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["record_version"]), ("not_assessed", 1))
        self.assertIn("record.migration_required", {p["code"] for p in report["problems"]})
        self.assertFalse(report["use_case"]["final_ready"])
        self.assertEqual(tree_digest(hw), before)


if __name__ == "__main__":
    unittest.main()
