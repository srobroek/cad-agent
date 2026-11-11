#!/usr/bin/env python3
"""manufacturing_intent.py: record versions, units, the normalized record and its problems, and the
policy every skill reads it through (use-case readiness, part routes, condition-qualified material
properties, calibrated capability limits and required checks)."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent


def load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


fx = load("cad_record_mi_fixtures", HERE / "fixtures" / "manufacturing_intent.py")
mi = fx.intent_module()


def temp_hw(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="cad-intent-test-")
    test.addCleanup(temporary.cleanup)
    return Path(temporary.name) / "project" / "hw"


def purchase_criteria() -> dict[str, Any]:
    return {"identity": fx.known("TST pin 6x20"), "specification": fx.known("TST hardened steel pin"),
            "ratings": fx.known("TST shear rating"), "supplier_evidence": [fx.source("supplier")]}


def proposing_selection(pn: str = "TST-0901") -> dict[str, Any]:
    """An assessed selection whose one candidate keeps TST-0001 printed, buys TST-0002 (made to drawing in the
    record) as a catalogue item with its purchase criteria, and proposes a new catalogue pin `pn`."""
    bought = {"kind": "purchased", "capability": fx.not_applicable("a purchase has no capability"), "purchase": purchase_criteria()}
    return {"status": "assessed", "assessment": {"input_sha256": "a" * 64, "method": "TST", "date": "2026-01-01"},
            "candidates": [{
                "id": "pinned", "title": "Printed bracket with a bought shaft and pin", "architecture": "printed bracket, bought shaft, new pin",
                "parts": [{"PN": "TST-0001", "material_profile": "tst_polymer_printed", "route": [{"kind": "fdm", "capability": "tst_printer"}]},
                          {"PN": "TST-0002", "material_profile": fx.not_applicable("catalogue item"), "route": [copy.deepcopy(bought)]},
                          {"PN": pn, "material_profile": fx.not_applicable("catalogue item"), "route": [copy.deepcopy(bought)],
                           "proposed": {"title": "TST pin", "sourcing": "catalogue", "reason": "the pin carries the bracket pivot",
                                        "functions": [{"function": "rotating", "requirements": ["REQ-001"]}]}}],
                "evaluations": [], "pros": ["TST pro"], "cons": ["TST con"], "tradeoffs": ["TST tradeoff"]}]}


class Versions(unittest.TestCase):
    def test_absent_version_is_legacy_and_only_version_2_is_current(self) -> None:
        self.assertEqual(mi.record_version({}), 1)
        self.assertEqual(mi.record_version({"record_version": 2}), 2)
        for value in (1, 3, True, "2", None):
            with self.subTest(value=value), self.assertRaises(mi.UnsupportedRecordVersion):
                mi.record_version({"record_version": value})

    def test_migration_problem_names_the_command_and_blocks(self) -> None:
        problem = mi.migration_problem({"id": "x"})
        self.assertEqual((problem.code, problem.severity, problem.path), ("record.migration_required", "error", "hw/product.yaml"))
        self.assertIn(f"{mi.MIGRATE_COMMAND} --dry-run", problem.action)
        self.assertIn("--apply", problem.action)
        self.assertEqual(problem.finding(), {"path": "hw/product.yaml", "message": problem.message, "severity": "error",
                                             "code": "record.migration_required", "canonical_location": None, "action": problem.action})
        self.assertIsNone(mi.migration_problem({"record_version": 2}))
        self.assertEqual(mi.migration_problem({"record_version": 7}).code, "record.version_unsupported")
        self.assertIsNone(mi.migration_problem(None))
        self.assertLessEqual({"record.migration_required", "record.version_unsupported", "record.incomplete"}, set(mi.CODES))


class Quantities(unittest.TestCase):
    def test_conversion_stays_within_a_dimension(self) -> None:
        self.assertAlmostEqual(mi.convert(1500, "mm", "m"), 1.5)
        self.assertAlmostEqual(mi.convert(23, "deg_C", "K"), 296.15)
        self.assertAlmostEqual(mi.convert(180, "deg", "rad"), math.pi)
        self.assertAlmostEqual(mi.convert(1.24, "g_cm3", "kg_m3"), 1240)
        self.assertAlmostEqual(mi.convert(50, "percent", "dimensionless"), 0.5)
        with self.assertRaisesRegex(mi.IntentError, "cannot convert"):
            mi.convert(1, "MPa", "kg_m3")
        with self.assertRaisesRegex(mi.IntentError, "unknown unit"):
            mi.convert(1, "inch", "mm")

    def test_quantities_refuse_non_finite_boolean_and_reversed_values(self) -> None:
        for data in ({"value": float("nan"), "units": "mm"}, {"value": float("inf"), "units": "mm"}, {"value": True, "units": "mm"},
                     {"lower": 2, "upper": 1, "units": "mm"}, {"value": 1, "units": "furlong"}, {"units": "mm"}):
            with self.subTest(data=data), self.assertRaises(mi.IntentError):
                mi.quantity(data)
        tested = mi.quantity({"lower": 20, "upper": 80, "units": "deg_C"})
        self.assertTrue(tested.contains(mi.quantity({"value": 60, "units": "deg_C"})))
        self.assertTrue(tested.contains(mi.quantity({"value": 300, "units": "K"})))
        self.assertFalse(tested.contains(mi.quantity({"lower": 60, "upper": 90, "units": "deg_C"})))
        self.assertFalse(tested.contains(mi.quantity({"value": 60, "units": "mm"})))


class Normalization(unittest.TestCase):
    def test_the_tst_record_normalizes_without_problems(self) -> None:
        hw = fx.write_record(temp_hw(self), fx.tst_record())
        record, problems = mi.check_record(hw)
        self.assertEqual([p.finding() for p in problems], [])
        self.assertTrue(record.migrated)
        self.assertEqual({pn: p.sourcing for pn, p in record.parts.items()},
                         {"TST-0001": "make", "TST-0002": "made_to_drawing", "TST-0003": "catalogue", "TST-0004": "make"})
        bracket = record.parts["TST-0001"]
        self.assertEqual([op.id for op in bracket.intent.route], ["print", "ream"])
        self.assertEqual(bracket.intent.material_profile, "tst_polymer_printed")
        self.assertEqual(bracket.material, "TST polymer", "the legacy declaration stays a string")
        load_fact = record.use_case.loads[0].magnitude
        self.assertEqual((load_fact.status, load_fact.value), ("known", mi.Quantity("N", value=11.0)))
        self.assertTrue(record.use_case.service_class.unknown)
        self.assertEqual(record.use_case.service_class.question, "Q-001")
        self.assertEqual(dict(mi.source_artifacts(record)), {"manufacturing/sources/tst-sheet.pdf": fx.sha256(b"%PDF-1.4 TST fixture datasheet\n")})
        self.assertEqual(record.materials["tst_metal_rod"].properties[0].value.basis, "allowable")
        self.assertTrue(record.capabilities["tst_printer"].parameters["nozzle_diameter"].known)
        self.assertEqual(len(record.assembly_operations), 1)

    def test_a_legacy_record_reads_with_explicit_unknowns_and_one_migration_problem(self) -> None:
        record = mi.load_record(fx.LAMP)
        self.assertEqual(record.version, 1)
        self.assertEqual([p.code for p in record.problems], ["record.migration_required"])
        self.assertEqual(len(record.parts), 11)
        for pn, part in record.parts.items():
            with self.subTest(pn):
                for name in ("functions", "material_profile", "service", "critical_surfaces", "route"):
                    value = getattr(part.intent, name)
                    self.assertTrue(value.unknown and value.reason == mi.LEGACY_REASON, (name, value))
        self.assertEqual(record.parts["LMP-0001"].material, "PETG")
        self.assertEqual(record.use_case.status, "unknown")
        self.assertEqual(len(record.assembly_operations), 9, "legacy drawing.operations stay the operations of a version 1 record")
        self.assertTrue(all(i.functional.critical.unknown for i in record.interfaces.values()))
        self.assertEqual((dict(record.capabilities), dict(record.materials)), ({}, {}))

    def test_a_candidate_proposes_a_new_part_that_is_never_a_record(self) -> None:
        documents = fx.tst_record()
        documents["manufacturing/selection.yaml"] = proposing_selection()
        record, problems = mi.check_record(fx.write_record(temp_hw(self), documents))
        self.assertEqual([p.finding() for p in problems], [])
        self.assertEqual(set(record.selection.proposed), {"pinned"})
        self.assertEqual(set(record.selection.proposed["pinned"]), {"TST-0901"}, "declared parts are not proposals")
        proposal = record.selection.proposed["pinned"]["TST-0901"]
        self.assertEqual((proposal.pn, proposal.candidate, proposal.title, proposal.sourcing, proposal.transparency),
                         ("TST-0901", "pinned", "TST pin", "catalogue", None))
        self.assertEqual([(f.function, f.requirements) for f in proposal.functions], [("rotating", ("REQ-001",))])
        self.assertNotIn("TST-0901", record.parts)
        self.assertEqual([o.id for o in mi.required_checks(record) if o.subject.get("PN") == "TST-0901"], [], "a proposal has no obligations")
        step = record.selection.candidates["pinned"]["parts"][1]["route"][0]
        self.assertEqual((step["kind"], step["purchase"]["identity"]["value"]), ("purchased", "TST pin 6x20"),
                         "a purchased alternative to a made part carries its purchase criteria")

    def test_each_contradiction_is_reported_with_its_code(self) -> None:
        def part(d: dict[str, Any], pn: str = "TST-0001") -> dict[str, Any]:
            return d[f"parts/{pn}/part.yaml"]

        def route(d: dict[str, Any], pn: str = "TST-0001") -> list[dict[str, Any]]:
            return part(d, pn)["intent"]["route"]

        def material(d: dict[str, Any], index: int) -> dict[str, Any]:
            return d["manufacturing/materials.yaml"]["profiles"][index]

        def capability(d: dict[str, Any], index: int) -> dict[str, Any]:
            return d["manufacturing/capabilities.yaml"]["profiles"][index]

        def drawing_operations(d: dict[str, Any]) -> None:
            d["assembly.yaml"]["drawing"] = {"number": "TST", "rev": "X1", "title": "TST", "views": [], "items": [],
                                             "operations": copy.deepcopy(d["assembly.yaml"]["operations"])}

        def propose(d: dict[str, Any], pn: str = "TST-0901") -> None:
            d["manufacturing/selection.yaml"] = proposing_selection(pn)

        def candidate_parts(d: dict[str, Any]) -> list[dict[str, Any]]:
            propose(d)
            return d["manufacturing/selection.yaml"]["candidates"][0]["parts"]

        cases: dict[str, tuple[Callable[[dict[str, Any]], Any], str]] = {
            "unanswered question id": (lambda d: part(d)["intent"].update(functions=fx.unknown("x", "Q-099")), "intent.reference"),
            "undeclared requirement": (lambda d: part(d)["intent"]["service"].update(requirements=["REQ-009"]), "intent.reference"),
            "undeclared load": (lambda d: part(d)["intent"]["service"].update(loads=["wind"]), "intent.reference"),
            "undeclared material profile": (lambda d: part(d)["intent"].update(material_profile="unobtainium"), "intent.reference"),
            "undeclared capability": (lambda d: route(d)[0].update(capability="ghost_printer"), "intent.reference"),
            "undeclared inspection": (lambda d: part(d)["intent"]["critical_surfaces"][0].update(inspection="nothing"), "intent.reference"),
            "missing decision record": (lambda d: route(d)[0]["orientation"].update(decision="DR-009"), "intent.reference"),
            "missing stack case": (lambda d: d["interfaces.yaml"]["interfaces"][0]["functional"].update(
                variation={"stack_case": "stk-9", "contributors": ["manufacture"], "method": "worst_case"}), "intent.reference"),
            "duplicate question": (lambda d: d["requirements.yaml"]["open_questions"].append(fx.question("Q-001")), "intent.duplicate"),
            "duplicate operation": (lambda d: route(d).append(fx.operation("print", "finishing")), "intent.duplicate"),
            "force given in mm": (lambda d: d["product.yaml"]["use_case"]["loads"][0].update(magnitude=fx.known(fx.quantity(11, "mm"))), "intent.units"),
            "temperature given in MPa": (lambda d: d["product.yaml"]["use_case"]["environment"].update(
                temperature=fx.known(fx.quantity(40, "MPa"))), "intent.units"),
            "not a number": (lambda d: d["product.yaml"]["use_case"]["loads"][0].update(magnitude=fx.known(fx.quantity(float("nan"), "N"))),
                             "intent.number"),
            "reversed interval": (lambda d: d["product.yaml"]["use_case"]["environment"].update(
                temperature=fx.known(fx.quantity(lower=60, upper=-20, units="deg_C"))), "intent.number"),
            "not a unit quaternion": (lambda d: route(d)[0]["orientation"]["rotation"].update(quaternion=[1, 1, 0, 0]), "intent.number"),
            "dependency on no operation": (lambda d: route(d)[1].update(depends_on=["mill"]), "intent.route"),
            "cyclic route": (lambda d: (route(d)[0].update(depends_on=["ream"]), route(d)[1].update(depends_on=["print"])), "intent.route"),
            "catalogue buy that is machined": (lambda d: route(d, "TST-0003").append(fx.operation("drill", "drilling")), "intent.route"),
            "catalogue buy without its purchase": (lambda d: part(d, "TST-0003")["intent"].update(route=[fx.operation("check", "inspection")]),
                                                   "intent.route"),
            "made part bought as an operation": (lambda d: route(d).insert(0, fx.operation("buy", "purchased")), "intent.route"),
            "two operation authorities": (drawing_operations, "intent.dual_authority"),
            "allowable in a typical profile": (lambda d: material(d, 2)["properties"].append(
                fx.material_property("yield_strength", 1, "MPa", basis="allowable")), "intent.allowable"),
            "allowable in family guidance": (lambda d: material(d, 0)["properties"].append(
                fx.material_property("yield_strength", 1, "MPa", basis="allowable", orientation=fx.known("XY"))), "intent.allowable"),
            "printed property without orientation": (lambda d: material(d, 0)["properties"].append(fx.material_property("youngs_modulus", 1, "MPa")),
                                                     "intent.profile"),
            "unknown property name": (lambda d: material(d, 1)["properties"].append(fx.material_property("strength_from_density", 1, "MPa")),
                                      "intent.profile"),
            "property of the wrong dimension": (lambda d: material(d, 1)["properties"].append(fx.material_property("density", 1, "MPa")),
                                                "intent.units"),
            "accuracy as a parameter": (lambda d: capability(d, 0)["parameters"].update(hole_accuracy=fx.known(fx.quantity(0.1))), "intent.profile"),
            "text parameter as a quantity": (lambda d: capability(d, 0)["parameters"].update(enclosure=fx.known(fx.quantity(1))), "intent.units"),
            "template used as a capability": (lambda d: capability(d, 0).update(template=True, guidance=["x"], calibrations=[]), "intent.profile"),
            "laser profile on a print": (lambda d: route(d)[0].update(capability="tst_laser"), "intent.profile"),
            "calibration from an estimate": (lambda d: capability(d, 0)["calibrations"][0].update(source=fx.source("estimate")), "intent.profile"),
            "unknown route kind": (lambda d: route(d).append(fx.operation("sinter", "sintering")), "intent.coverage"),
            "part without intent": (lambda d: part(d).pop("intent"), "record.incomplete"),
            "interface without functional": (lambda d: d["interfaces.yaml"]["interfaces"][0].pop("functional"), "record.incomplete"),
            "assembly without operations": (lambda d: d["assembly.yaml"].pop("operations"), "record.incomplete"),
            "requirements without questions": (lambda d: d["requirements.yaml"].pop("open_questions"), "record.incomplete"),
            "no selection record": (lambda d: d.pop("manufacturing/selection.yaml"), "record.incomplete"),
            "source bytes changed": (lambda d: d["_sources"].update({"manufacturing/sources/tst-sheet.pdf": b"edited"}), "intent.source_artifact"),
            "source missing": (lambda d: d["_sources"].clear(), "intent.source_artifact"),
            "freeze without an evidence anchor": (lambda d: d["product.yaml"]["flow"].update(
                frozen={"version": "v1", "manifest_sha256": "a" * 64, "decision": "DR-001"}), "record.legacy_freeze"),
            "proposed part that is already declared": (lambda d: propose(d, "TST-0003"), "intent.duplicate"),
            "candidate part listed twice": (lambda d: (lambda parts: parts.append(copy.deepcopy(parts[0])))(candidate_parts(d)), "intent.duplicate"),
            "candidate part neither declared nor proposed": (lambda d: candidate_parts(d)[2].pop("proposed"), "intent.reference"),
            "unanswered question in a candidate purchase": (lambda d: candidate_parts(d)[1]["route"][0]["purchase"].update(
                ratings=fx.unknown("x", "Q-099")), "intent.reference"),
            "undeclared requirement of a proposed part": (lambda d: candidate_parts(d)[2]["proposed"]["functions"][0].update(
                requirements=["REQ-009"]), "intent.reference"),
            "undeclared policy decision": (lambda d: d["requirements.yaml"]["requirements"][0].setdefault("acceptance", []).append(
                fx.criterion("load", ">=", 1, "N", source=fx.source("policy", decision="DR-009"))), "intent.reference"),
            "edge distance criterion in degrees": (lambda d: d["assembly.yaml"]["operations"][0].update(edge_distance={
                "criterion": fx.criterion("edge_distance", ">=", 3, "deg"), "distance": fx.known(fx.quantity(4))}), "intent.units"),
            "edge distance given as an angle": (lambda d: d["assembly.yaml"]["operations"][0].update(edge_distance={
                "criterion": fx.criterion("edge_distance", ">=", 3, "mm"), "distance": fx.known(fx.quantity(4, "deg"))}), "intent.units"),
            "angular compliance in mm": (lambda d: d["interfaces.yaml"]["interfaces"][0]["functional"].update(play={
                "compliance": {"angular": fx.known(fx.quantity(0.1))}}), "intent.units"),
        }
        for label, (edit, code) in cases.items():
            with self.subTest(label):
                documents = fx.tst_record()
                edit(documents)
                hw = fx.write_record(temp_hw(self), documents)
                record = mi.load_record(hw)
                codes = [p.code for p in record.problems]
                self.assertIn(code, codes, [p.message for p in record.problems])
                severity = {p.code: p.severity for p in record.problems}[code]
                self.assertEqual(severity, "warning" if code in ("intent.coverage", "record.legacy_freeze") else "error")

    def test_normalization_needs_no_files(self) -> None:
        documents = fx.tst_record()
        documents.pop("_decisions")
        documents.pop("_sources")
        record = mi.normalize_record(documents)
        self.assertEqual([p.finding() for p in record.problems], [], "without hw, decision and artifact files are not checked")


class UseCaseReadiness(unittest.TestCase):
    def confirmed(self) -> Any:
        """The TST record with its use case confirmed by DR-001: every question resolved and every use-case fact
        answered (here not applicable), as a resolved question requires."""
        def answered(value: Any) -> Any:
            if isinstance(value, dict):
                return fx.not_applicable("TST answer, DR-001") if value.get("status") == "unknown" else \
                    {key: answered(item) for key, item in value.items()}
            return [answered(item) for item in value] if isinstance(value, list) else value

        documents = fx.tst_record()
        use = documents["product.yaml"]["use_case"] = answered(documents["product.yaml"]["use_case"])
        use.update(status="confirmed", decision="DR-001", service_class=fx.known("functional_demo"))
        for item in documents["requirements.yaml"]["open_questions"]:
            item.update(status="resolved", resolution={"answer": "TST answer", "decision": "DR-001"})
        return documents

    def test_a_draft_use_case_supports_only_conditional_recommendations(self) -> None:
        assessment = mi.assess_use_case(mi.normalize_record(fx.tst_record()))
        self.assertFalse(assessment.final_ready)
        self.assertIn("the use case is not confirmed by a decision", assessment.blocking)
        self.assertIn("the service class is unknown", assessment.blocking)
        self.assertIn("Q-001", assessment.open_questions)
        self.assertIn("environment.temperature", assessment.unknown)

    def test_a_confirmed_use_case_with_answered_questions_is_ready(self) -> None:
        assessment = mi.assess_use_case(mi.normalize_record(self.confirmed()))
        self.assertEqual((assessment.final_ready, assessment.blocking, assessment.service_class), (True, (), "functional_demo"))

    def test_untracked_unknowns_and_safety_policy_block(self) -> None:
        documents = self.confirmed()
        documents["product.yaml"]["use_case"]["environment"]["uv"] = fx.unknown("never asked")
        assessment = mi.assess_use_case(mi.normalize_record(documents))
        self.assertFalse(assessment.final_ready)
        self.assertIn("use_case.environment.uv is unknown and no open question tracks it", assessment.blocking)
        documents = self.confirmed()
        documents["product.yaml"]["use_case"]["service_class"] = fx.known("safety_critical")
        assessment = mi.assess_use_case(mi.normalize_record(documents))
        self.assertIn("a safety-critical use case needs use_case.consequences.standards", assessment.blocking)
        documents = self.confirmed()
        documents["requirements.yaml"]["open_questions"].append(fx.question("Q-050", material=True))
        self.assertEqual(mi.assess_use_case(mi.normalize_record(documents)).open_questions, ("Q-050",))
        self.assertFalse(mi.assess_use_case(mi.load_record(fx.LAMP)).final_ready)

    def test_a_resolved_question_does_not_track_an_unknown(self) -> None:
        documents = self.confirmed()
        question = mi.normalize_record(fx.tst_record()).use_case.facts["environment.uv"].question
        documents["product.yaml"]["use_case"]["environment"]["uv"] = fx.unknown("never measured", question)
        contradiction = (f"use_case.environment.uv is unknown but its question {question} is resolved; "
                         "record the answer in the fact or reopen the question")
        assessment = mi.assess_use_case(mi.normalize_record(documents))
        self.assertFalse(assessment.final_ready)
        self.assertEqual(assessment.blocking, (contradiction,))
        entry = next(q for q in documents["requirements.yaml"]["open_questions"] if q["id"] == question)
        entry.update(status="open")
        entry.pop("resolution")
        assessment = mi.assess_use_case(mi.normalize_record(documents))
        self.assertNotIn(contradiction, assessment.blocking, "an open question tracks the unknown")
        self.assertFalse(any("environment.uv" in reason for reason in assessment.blocking))


class Routes(unittest.TestCase):
    def test_route_order_and_predecessors(self) -> None:
        documents = fx.tst_record()
        route = documents["parts/TST-0001/part.yaml"]["intent"]["route"]
        route.append(fx.operation("coat", "coating", depends_on=["print"]))
        record = mi.normalize_record(documents)
        planned = mi.part_route(record, "TST-0001")
        self.assertEqual([op.id for op in planned.operations], ["print", "ream", "coat"])
        self.assertEqual(dict(planned.predecessors), {"print": (), "ream": ("print",), "coat": ("print",)})
        self.assertEqual((planned.sourcing, planned.components, planned.unsupported, planned.unknown), ("make", ("main",), (), None))
        with self.assertRaisesRegex(mi.IntentError, "no part"):
            mi.part_route(record, "TST-0999")

    def test_unknown_and_unsupported_routes_stay_visible(self) -> None:
        documents = fx.tst_record()
        documents["parts/TST-0001/part.yaml"]["intent"]["route"] = fx.unknown("not planned", "Q-001")
        documents["parts/TST-0002/part.yaml"]["intent"]["route"].append(fx.operation("sinter", "sintering"))
        record = mi.normalize_record(documents)
        self.assertEqual((mi.part_route(record, "TST-0001").operations, mi.part_route(record, "TST-0001").unknown.reason), ((), "not planned"))
        self.assertEqual(mi.part_route(record, "TST-0002").unsupported, ("sinter",))
        documents = fx.tst_record()
        route = documents["parts/TST-0001/part.yaml"]["intent"]["route"]
        route[0]["depends_on"], route[1]["depends_on"] = ["ream"], ["print"]
        with self.assertRaisesRegex(mi.IntentError, "cycle"):
            mi.part_route(mi.normalize_record(documents), "TST-0001")


class MaterialProperties(unittest.TestCase):
    def setUp(self) -> None:
        self.materials = mi.normalize_record(fx.tst_record()).materials

    def test_an_allowable_resolves_only_under_its_conditions(self) -> None:
        rod = self.materials["tst_metal_rod"]
        room = mi.Quantity("deg_C", value=23.0)
        found = mi.resolve_property(rod, "yield_strength", temperature=room, conditioning="dry", allowable=True)
        self.assertEqual((found.status, found.value, found.basis), ("resolved", mi.Quantity("MPa", value=111.0), "allowable"))
        self.assertEqual(found.conditions["conditioning"], "dry")
        hot = mi.resolve_property(rod, "yield_strength", temperature=mi.Quantity("deg_C", value=60.0))
        self.assertEqual((hot.status, hot.value, hot.basis), ("resolved", mi.Quantity("MPa", value=99.0), "estimate"),
                         "a room-temperature value never answers 60 deg_C; the 20..80 value does")
        self.assertEqual(mi.resolve_property(rod, "yield_strength", temperature=mi.Quantity("deg_C", value=60.0), allowable=True).status,
                         "refused", "typical or estimated values are never relabelled allowable")
        hotter = mi.resolve_property(rod, "yield_strength", temperature=mi.Quantity("deg_C", value=120.0))
        self.assertEqual(hotter.status, "refused")
        self.assertTrue(any("120 deg_C requested" in reason for reason in hotter.reasons), hotter.reasons)
        self.assertEqual(mi.resolve_property(rod, "yield_strength", conditioning="conditioned").status, "refused")
        self.assertEqual(mi.resolve_property(rod, "yield_strength", form="printed").status, "refused")

    def test_family_guidance_orientation_and_density_never_stand_in(self) -> None:
        printed = self.materials["tst_polymer_printed"]
        self.assertEqual(mi.resolve_property(printed, "density", orientation="XY").status, "resolved")
        self.assertEqual(mi.resolve_property(printed, "density", orientation="ZX").status, "refused")
        self.assertEqual(mi.resolve_property(printed, "yield_strength").status, "unknown", "density is not strength")
        self.assertEqual(mi.resolve_property(printed, "density", allowable=True).status, "refused")
        with self.assertRaises(mi.IntentError):
            mi.resolve_property(printed, "strength_from_density")

    def test_two_matching_values_that_differ_are_ambiguous(self) -> None:
        documents = fx.tst_record()
        documents["manufacturing/materials.yaml"]["profiles"][1]["properties"].append(
            fx.material_property("yield_strength", 105, "MPa", basis="allowable"))
        rod = mi.normalize_record(documents).materials["tst_metal_rod"]
        self.assertEqual(mi.resolve_property(rod, "yield_strength", allowable=True).status, "refused")


class Capabilities(unittest.TestCase):
    def test_only_a_scoped_calibration_answers_achievable_accuracy(self) -> None:
        documents = fx.tst_record()
        documents["manufacturing/capabilities.yaml"]["profiles"][0]["calibrations"].append(
            {**fx.calibration("old", "min_wall", "tst_polymer_printed", fx.quantity(lower=0.8, upper=1.0)), "valid_until": "2026-02-01"})
        printer = mi.normalize_record(documents).capabilities["tst_printer"]
        found = mi.capability_limit(printer, "hole_diameter_deviation", material="tst_polymer_printed", orientation="XY")
        self.assertEqual((found.status, found.calibration.limits), ("calibrated", mi.Quantity("mm", lower=-0.2, upper=0.0)))
        self.assertEqual(mi.capability_limit(printer, "hole_diameter_deviation", material="tst_metal_rod").status, "refused")
        self.assertEqual(mi.capability_limit(printer, "hole_diameter_deviation", material="tst_polymer_printed", orientation="ZX").status,
                         "refused")
        self.assertEqual(mi.capability_limit(printer, "nozzle_diameter", material="tst_polymer_printed").status, "unknown",
                         "a nominal parameter is never a calibration")
        self.assertEqual(mi.capability_limit(printer, "min_wall", material="tst_polymer_printed", on=date(2026, 3, 1)).status, "refused")
        documents["manufacturing/capabilities.yaml"]["profiles"][0]["template"] = True
        template = mi.normalize_record(documents).capabilities["tst_printer"]
        self.assertEqual(mi.capability_limit(template, "hole_diameter_deviation", material="tst_polymer_printed").status, "refused")


class RequiredChecks(unittest.TestCase):
    def obligations(self, documents: dict[str, Any]) -> dict[str, Any]:
        return {o.id: o for o in mi.required_checks(mi.normalize_record(documents))}

    def test_every_obligation_validates_and_is_enumerated_once(self) -> None:
        checks = mi.required_checks(mi.normalize_record(fx.tst_record()))
        registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in (
            json.loads(p.read_text(encoding="utf-8")) for p in (SKILL / "schemas").glob("*.schema.json")))
        schema = Draft202012Validator({"$ref": "https://srobroek.com/cad/schemas/manufacturing.schema.json#/$defs/obligation"}, registry=registry)
        self.assertEqual([o.id for o in checks], sorted({o.id for o in checks}))
        for obligation in checks:
            with self.subTest(obligation.id):
                self.assertEqual([e.message for e in schema.iter_errors(obligation.as_dict())], [])

    def test_route_function_interface_and_requirement_obligations(self) -> None:
        found = self.obligations(fx.tst_record())
        for ident in ("process.fdm.orientation@TST-0001/print", "process.fdm.support@TST-0001/print", "process.fdm.trial@TST-0001/print",
                      "process.reaming.allowance@TST-0001/ream", "route.operation_plan@TST-0001/ream", "cad.readback@TST-0001",
                      "engineering.strength@TST-0001", "engineering.creep@TST-0001", "engineering.anisotropy@TST-0001",
                      "feature.fit@TST-0001#pivot_bore", "process.cnc.access@TST-0002/turn", "contact.wear@TST-0002",
                      "process.laser.material@TST-0004/cut", "optical.route@TST-0004", "interface.stack@IF-001",
                      "interface.acceptance.physically_validated@IF-001#running_clearance", "assembly.access@op:fit_bearing",
                      "requirement.simulation_supported@REQ-001", "intent.use_case@product", "intent.critical_surfaces@TST-0002"):
            self.assertIn(ident, found)
        self.assertEqual((found["process.fdm.trial@TST-0001/print"].level, found["process.fdm.trial@TST-0001/print"].stage),
                         ("physically_validated", "release"))
        self.assertEqual(found["engineering.creep@TST-0001"].level, "physically_validated", "static analysis cannot pass creep")

    def test_purchased_items_get_purchase_checks_only(self) -> None:
        found = self.obligations(fx.tst_record())
        bearing = sorted(i for i in found if "TST-0003" in i)
        self.assertEqual(bearing, ["purchase.availability@TST-0003/buy", "purchase.dimensions@TST-0003/buy", "purchase.identity@TST-0003/buy",
                                   "purchase.mounting@TST-0003/buy", "purchase.rating@TST-0003/buy"])
        self.assertFalse(any(i.startswith("process.") for i in found if "TST-0002" in i and "cnc" not in i),
                         "a made-to-drawing buy gets its actual route's checks, never FDM ones")

    def test_unknowns_and_unsupported_kinds_are_explicit_gaps(self) -> None:
        documents = fx.tst_record()
        documents["parts/TST-0002/part.yaml"]["intent"]["route"] = [fx.operation("sinter", "sintering")]
        documents["parts/TST-0004/part.yaml"]["intent"].update(route=fx.unknown("not planned"), functions=fx.unknown("not asked"))
        documents["product.yaml"]["use_case"]["loads"] = fx.unknown("not measured")
        found = self.obligations(documents)
        self.assertEqual(sorted(i for i in found if i.startswith("process.") and "TST-0002" in i), ["process.sintering.coverage@TST-0002/sinter"])
        self.assertIn("intent.route@TST-0004", found)
        self.assertIn("intent.functions@TST-0004", found)
        self.assertIn("optical.route@TST-0004", found, "transparency still demands an optical route")
        self.assertIn("engineering.load_cases@TST-0001", found)
        self.assertNotIn("engineering.creep@TST-0001", found)
        legacy = {o.id for o in mi.required_checks(mi.load_record(fx.LAMP))}
        self.assertIn("intent.route@LMP-0001", legacy)
        self.assertIn("intent.use_case@product", legacy)
        self.assertFalse(any(i.startswith("process.fdm") for i in legacy), "a legacy fdm label dispatches no process checks")

    def test_every_component_gets_its_own_obligations(self) -> None:
        documents = fx.tst_record()
        documents["parts/TST-0001/part.yaml"]["intent"]["components"] = [{"id": "left", "witness": [0, 0, 1]},
                                                                          {"id": "right", "witness": [50, 0, 1]}]
        found = self.obligations(documents)
        for component in ("left", "right"):
            self.assertIn(f"process.fdm.support@TST-0001.{component}/print", found)
            self.assertIn(f"cad.readback@TST-0001.{component}", found)
        self.assertNotIn("process.fdm.support@TST-0001/print", found)

    def test_joining_operations_get_joint_evidence_and_moving_interfaces_get_play(self) -> None:
        """assembly.joint for every joining operation, never a closure or service step; motion.play for a
        critical rotating or sliding interface before it declares any play, and for any declared play."""
        documents = fx.tst_record()
        documents["assembly.yaml"]["operations"] += [
            {"id": "fasten_plate", "kind": "fasten", "part": "TST-0004", "into": "TST-0001", "direction": [0, 0, -1],
             "acceptance": {"kind": "torqued", "features": ["head"]}},
            {"id": "close_cover", "kind": "close", "part": "TST-0004", "into": "TST-0001", "closes": ["TST-0003"]},
            {"id": "check_fit", "kind": "inspect", "part": "TST-0003", "acceptance": {"kind": "inspected", "features": ["bore"]}}]
        found = self.obligations(documents)
        self.assertEqual(sorted(i for i in found if i.startswith("assembly.joint")),
                         ["assembly.joint@op:fasten_plate", "assembly.joint@op:fit_bearing"])
        joint = found["assembly.joint@op:fit_bearing"]
        self.assertEqual((joint.level, joint.stage, dict(joint.subject)), ("process_planned", "detail", {"assembly_operation": "fit_bearing"}))
        self.assertIn("edge distance", joint.basis)
        self.assertIn("motion.play@IF-001", found, "a critical rotating interface needs its play checked though it declares none")
        functional = documents["interfaces.yaml"]["interfaces"][0]["functional"]
        functional["functions"] = [{"function": "locating"}]
        self.assertNotIn("motion.play@IF-001", self.obligations(documents))
        functional["play"] = {"radial": fx.known(fx.quantity(0.02))}
        self.assertIn("motion.play@IF-001", self.obligations(documents), "declared play is checked whatever the function")


class ProfileDocuments(unittest.TestCase):
    def test_standalone_profiles_are_checked_without_a_project(self) -> None:
        documents = fx.tst_record()
        materials = copy.deepcopy(documents["manufacturing/materials.yaml"])
        profiles, problems = mi.profile_document(materials, "materials", "library.yaml")
        self.assertEqual(([p.id for p in profiles], [p.finding() for p in problems]),
                         (["tst_polymer_printed", "tst_metal_rod", "tst_clear_sheet"], []))
        materials["profiles"][2]["properties"].append(fx.material_property("yield_strength", 1, "MPa", basis="allowable"))
        materials["profiles"].append(copy.deepcopy(materials["profiles"][0]))
        _profiles, problems = mi.profile_document(materials, "materials", "library.yaml")
        self.assertEqual(sorted(p.code for p in problems), ["intent.allowable", "intent.duplicate"])
        with self.assertRaises(mi.IntentError):
            mi.profile_document(materials, "selection", "x.yaml")


if __name__ == "__main__":
    unittest.main()
