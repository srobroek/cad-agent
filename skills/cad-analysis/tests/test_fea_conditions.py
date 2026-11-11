#!/usr/bin/env python3
"""FEA material sourcing and evidence: properties come only from the analysed part's material profile under the
case's stated conditions, refusals happen before any solve, and a result is simulation-supported evidence at
most. The FreeCAD job is replaced by a stand-in that records the material card it was given."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

SKILLS = Path(__file__).resolve().parents[2]
SCRIPTS = SKILLS / "cad-analysis" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fea_static  # noqa: E402

_spec = importlib.util.spec_from_file_location("cad_record_mi_fixtures",
                                               SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
assert _spec and _spec.loader
fx = sys.modules.get(_spec.name)
if fx is None:
    fx = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = fx
    _spec.loader.exec_module(fx)


def evidence_module() -> Any:
    """cad-record evidence.py, the reader of every producer's required-check packet."""
    name = "cad_record_evidence"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SKILLS / "cad-record" / "scripts" / "evidence.py")
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


PN = "TST-0101"
DATASHEET = fx.source("datasheet", "TST alloy datasheet")
TOOLS = {"freecad": {"path": "/opt/freecad/freecadcmd", "version": "1.1.4"},
         "gmsh": {"path": "/opt/freecad/gmsh", "version": "4.15.0"},
         "calculix": {"path": "/opt/freecad/ccx", "version": "2.23"}}
DRY_23C = {"conditioning": "dry", "temperature": {"value": 23, "units": "deg_C"}}
SERVICE = {"conditioning": "dry", "temperature": {"lower": 15, "upper": 30, "units": "deg_C"}}  # the use case's range


def prop(name: str, value: float, units: str, *, basis: str = "supplier_typical", **extra: Any) -> dict[str, Any]:
    """A profile property with a synthetic datasheet source (allowables keep the fixture's derivation)."""
    data = fx.material_property(name, value, units, basis=basis, **extra)
    if basis != "allowable":
        data["value"]["sources"] = [DATASHEET]
    return data


def alloy(*properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A synthetic wrought rod grade profile; with no properties, a complete typical set tested over 10..40 deg_C."""
    tested = fx.quantity(lower=10, upper=40, units="deg_C")
    return fx.material_profile("tst_alloy_rod", form="rod", properties=list(properties) or [
        prop("youngs_modulus", 70000, "MPa", temperature=tested), prop("poisson_ratio", 0.33, "dimensionless", temperature=tested),
        prop("density", 2700, "kg_m3", temperature=tested), prop("yield_strength", 250, "MPa", temperature=tested)], **extra)


def printed(**extra: Any) -> dict[str, Any]:
    """A synthetic printed grade profile with XY and Z values: orientation-dependent material."""
    properties = []
    for orientation, modulus, strength in (("XY", 2000, 40), ("Z", 1500, 20)):
        properties += [prop("youngs_modulus", modulus, "MPa", orientation=fx.known(orientation)),
                       prop("yield_strength", strength, "MPa", orientation=fx.known(orientation))]
    properties += [prop("poisson_ratio", 0.35, "dimensionless", orientation=fx.known("XY")),
                   prop("poisson_ratio", 0.35, "dimensionless", orientation=fx.known("Z")),
                   prop("density", 1200, "kg_m3", orientation=fx.known("XY")),
                   prop("density", 1200, "kg_m3", orientation=fx.known("Z"))]
    data = fx.material_profile("tst_polymer_fdm", form="printed", properties=properties, **extra)
    data["process_state"] = {"process": "fdm", "conditioning": "dry"}
    return data


def bracket(profile: Any = "tst_alloy_rod", route: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return fx.part(PN, "Bracket", process="cnc", material="TST alloy", part_intent=fx.intent(
        functions=[{"function": "structural", "requirements": ["REQ-001"]}], material_profile=profile,
        service={"requirements": ["REQ-001"], "loads": ["head_load"]},
        critical_surfaces=fx.not_applicable("no critical surface in this fixture"),
        route=route if route is not None else [fx.operation("turn", "cnc", fx.unknown("no capability in this fixture"))]))


def documents(*, profiles: list[dict[str, Any]] | None = None, part: dict[str, Any] | None = None,
              service_class: str = "working_prototype") -> dict[str, Any]:
    use = fx.use_case("draft", service_class=fx.known(service_class),
                      loads=[{"id": "head_load", "kind": "static", "magnitude": fx.known(fx.quantity(50, "N")),
                              "requirements": ["REQ-001"]}])
    use["environment"]["temperature"] = fx.known(fx.quantity(lower=15, upper=30, units="deg_C"))
    requirements = fx.requirements([
        fx.requirement("REQ-001", "Carry the head load",
                       acceptance=[fx.criterion("yield_safety_factor", ">=", 2, "dimensionless")]),
        fx.requirement("REQ-002", "Stay stiff", acceptance=[fx.criterion("max_displacement", "<=", 0.5, "mm")])])
    return fx.record(product_data=fx.product(use, stage="analysis"), requirements_data=requirements,
                     parts=[part or bracket()], materials=profiles if profiles is not None else [alloy()],
                     decisions=("DR-001", "DR-002", "DR-003"))


class SourcedFEACase(unittest.TestCase):
    """A versioned case in a record version 2 project, solved by a stand-in FreeCAD job."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.hw = self.project / "hw"
        self.solved: list[dict[str, Any]] = []
        self.solution = {"safety_factor_yield": 3.0, "max_displacement_mm": 0.2, "max_von_mises_mpa": 83.3,
                         "node_count": 1200, "element_count": 600}

    def build(self, docs: dict[str, Any], version: str = "v1") -> None:
        fx.write_record(self.hw, docs)
        folder = self.hw / "versions" / version
        record = folder / "record"
        for rel in docs:
            if not rel.startswith("_") and (self.hw / rel).is_file():
                (record / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.hw / rel, record / rel)
        files = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in folder.rglob("*") if p.is_file()}
        (folder / "manifest.json").write_text(json.dumps({"label": version, "files": files}), encoding="utf-8")

    def case(self, material: dict[str, Any], **extra: Any) -> Path:
        folder = self.hw / "analysis" / "cases" / "bracket"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bracket.step").write_bytes(b"ISO-10303-21; TST bracket\n")
        data = {"step": "bracket.step", "part": PN, "material": material,
                "fixed_faces": {"indices": [1]}, "loads": [{"selector": {"indices": [2]}, "force": [0, 0, -50],
                                                             "load": "head_load"}],
                "mesh_size": 2}
        data.update(extra)
        path = folder / "case.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    def run_job(self, case_path: Path, case: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], int]:
        self.solved.append(copy.deepcopy(case["material"]))
        run_dir = Path(kwargs["scratch_root"]) / "jobs" / "fea_static" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "solver.dat").write_text("solver output\n", encoding="utf-8")
        result = fea_static.apply_required_safety_factor(
            {"run_directory": str(run_dir), "retained_files": ["solver.dat"], "solver_versions": TOOLS, **self.solution},
            case.get("required_safety_factor"))
        kwargs["result_path"].write_text(json.dumps(result), encoding="utf-8")
        return result, fea_static.result_exit_code(result)

    def call(self, case_path: Path, *extra: str) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        with patch("fea_static.find_freecad", return_value=Path("/fake/freecadcmd")), \
             patch("fea_static.solver_versions", return_value=TOOLS), \
             patch("fea_static.run_job", side_effect=self.run_job), patch("sys.stdout", output):
            status = fea_static.main([str(case_path), "--hw", str(self.hw), "--version", "v1", *extra])
        return status, json.loads(output.getvalue())

    def refused(self, case_path: Path) -> list[str]:
        status, report = self.call(case_path)
        self.assertEqual(status, 2, report)
        self.assertEqual(report["error"]["code"], "analysis_refused", report)
        self.assertEqual(self.solved, [], "a refused case must never reach the solver")
        return [problem["code"] for problem in report["error"]["details"]["problems"]]

    def outcomes(self, report: dict[str, Any]) -> dict[str, str]:
        return {row["obligation"]: row["outcome"] for row in report["engineering"]["outcomes"]}


class Refusals(SourcedFEACase):
    def test_a_density_only_profile_is_refused_before_the_solve(self) -> None:
        self.build(documents(profiles=[alloy(prop("density", 2700, "kg_m3"))]))
        codes = self.refused(self.case({"conditions": DRY_23C}))
        self.assertIn("material.density_only", codes)
        self.assertIn("material.property", codes)  # E, nu and yield are unknown, never derived from density

    def test_a_property_tested_under_another_condition_is_refused(self) -> None:
        self.build(documents())
        self.assertIn("material.condition", self.refused(self.case({"conditions": {**DRY_23C, "conditioning": "conditioned"}})))
        self.solved.clear()
        hot = {"conditioning": "dry", "temperature": {"value": 80, "units": "deg_C"}}
        self.assertIn("material.condition", self.refused(self.case({"conditions": hot})))

    def test_numbers_inherited_into_a_versioned_case_are_refused(self) -> None:
        self.build(documents())
        explicit = {"name": "Aluminium 6061", "E": 69000, "nu": 0.33, "density": 2700, "yield": 276}
        self.assertEqual(self.refused(self.case(explicit)), ["material.inherited"])

    def test_a_profile_copied_from_another_part_is_refused(self) -> None:
        self.build(documents(profiles=[alloy(), printed()]))
        codes = self.refused(self.case({"profile": "tst_polymer_fdm", "conditions": DRY_23C}))
        self.assertIn("material.inherited", codes)

    def test_a_material_name_without_a_profile_is_refused(self) -> None:
        self.build(documents(part=bracket(profile=fx.unknown("material not chosen yet"))))
        self.assertEqual(self.refused(self.case({"conditions": DRY_23C})), ["material.name_only"])

    def test_family_guidance_is_refused(self) -> None:
        self.build(documents(profiles=[alloy(level="family")]))
        self.assertIn("material.grade", self.refused(self.case({"conditions": DRY_23C})))

    def test_orientation_dependent_material_without_an_effective_model_is_refused(self) -> None:
        part = bracket("tst_polymer_fdm", [fx.operation("print", "fdm", fx.unknown("no capability in this fixture"))])
        self.build(documents(profiles=[printed()], part=part))
        self.assertIn("material.condition", self.refused(self.case({"conditions": DRY_23C})))  # orientation unstated
        self.solved.clear()
        self.assertEqual(self.refused(self.case({"conditions": {**DRY_23C, "orientation": "Z"}})), ["material.anisotropy"])

    def test_bulk_properties_never_stand_in_for_a_printed_part(self) -> None:
        part = bracket("tst_alloy_rod", [fx.operation("print", "fdm", fx.unknown("no capability in this fixture"))])
        self.build(documents(part=part))
        self.assertIn("material.form_process", self.refused(self.case({"conditions": DRY_23C})))

    def test_production_service_needs_a_yield_allowable(self) -> None:
        self.build(documents(service_class="production"))
        self.assertIn("material.allowable", self.refused(self.case({"conditions": DRY_23C})))

    def test_a_standalone_case_cannot_name_profile_conditions(self) -> None:
        self.build(documents())
        output = io.StringIO()
        with patch("fea_static.find_freecad", return_value=Path("/fake/freecadcmd")), patch("sys.stdout", output):
            status = fea_static.main([str(self.case({"conditions": DRY_23C}))])
        self.assertEqual(status, 2)
        self.assertIn("--hw and --version", json.loads(output.getvalue())["error"]["message"])


class QualifiedProfile(SourcedFEACase):
    def test_the_solver_gets_the_profile_values_and_the_result_is_simulation_evidence_only(self) -> None:
        self.build(documents())
        status, report = self.call(self.case({"conditions": DRY_23C}))
        self.assertEqual(status, 0, report)
        self.assertEqual(self.solved, [{"name": "tst_alloy_rod (TST fixture)", "E": 70000.0, "nu": 0.33,
                                        "density": 2700.0, "yield": 250.0}])
        self.assertEqual(report["evidence"]["level"], "simulation_supported")
        self.assertFalse(report["evidence"]["physical_validation"])
        self.assertFalse(report["evidence"]["exploratory"])
        self.assertEqual(report["material"]["source"], "profile")
        rows = {row["input"]: row for row in report["material"]["properties"]}
        self.assertEqual((rows["E"]["basis"], rows["E"]["sources"][0]["kind"]), ("supplier_typical", "datasheet"))
        self.assertEqual(report["mesh"]["convergence"]["status"], "not_demonstrated")
        binding = report["binding"]
        self.assertEqual((binding["PN"], binding["part_revision"], binding["route"]), (PN, "X1", ["cnc"]))
        self.assertEqual(binding["material_profile"]["id"], "tst_alloy_rod")
        manifest = json.loads((self.hw / "analysis" / "v1" / "bracket" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["recipe"]["conditions"]["binding"], binding)
        # No criterion is named and no convergence is shown: an expected check stays unknown, never pass.
        self.assertEqual(set(self.outcomes(report).values()), {"unknown"})
        self.assertEqual(report["engineering"]["result"], "unknown")
        self.validate_packet(report["engineering"])

    def test_a_criterion_service_loads_and_convergence_make_strength_and_stiffness_pass(self) -> None:
        self.build(documents())
        status, report = self.call(self.case({"conditions": SERVICE}, acceptance={"strength": "REQ-001", "stiffness": "REQ-002"},
                                             mesh_convergence={"decision": "DR-002"}))
        self.assertEqual(status, 0, report)
        self.assertEqual(report["required_safety_factor"], 2.0)  # from REQ-001, not a bare case number
        self.assertTrue(report["evidence"]["service_evidence"], report["evidence"]["gaps"])
        self.assertEqual(self.outcomes(report), {f"engineering.stiffness@{PN}": "pass", f"engineering.strength@{PN}": "pass"})
        self.assertEqual({row["level"] for row in report["engineering"]["outcomes"]}, {"simulation_supported"})
        self.assertEqual(report["engineering"]["dependencies"]["decision_dr_002"],
                         hashlib.sha256((self.hw / "decisions" / "DR-002.md").read_bytes()).hexdigest())
        self.validate_packet(report["engineering"])

    def test_the_engineering_packet_is_sealed_where_evidence_reads_producer_packets(self) -> None:
        """The record version 2 packet is published beside its analysis bundle as hw/checks/<V>/fea-<case>/,
        bound to the version and citing the analysis bundle; cad-record evidence.py collects it (its one
        reader of producer packets) and a rerun of the same case reuses both bundles."""
        self.build(documents())
        path = self.case({"conditions": SERVICE}, acceptance={"strength": "REQ-001", "stiffness": "REQ-002"},
                         mesh_convergence={"decision": "DR-002"})
        status, report = self.call(path)
        self.assertEqual((status, report["check_bundle"]), (0, "hw/checks/v1/fea-bracket"), report)
        bundle = self.hw / "checks" / "v1" / "fea-bracket"
        self.assertEqual(json.loads((bundle / "result.json").read_text(encoding="utf-8")), report["engineering"])
        analysis = json.loads((bundle / "evidence" / "analysis.json").read_text(encoding="utf-8"))["analysis"]
        self.assertEqual(analysis, {"bundle": "hw/analysis/v1/bracket", "manifest_sha256": hashlib.sha256(
            (self.hw / "analysis" / "v1" / "bracket" / "manifest.json").read_bytes()).hexdigest()})
        manifest_sha = hashlib.sha256((self.hw / "versions" / "v1" / "manifest.json").read_bytes()).hexdigest()
        packets, findings = evidence_module().collect_packets(self.hw, "v1", manifest_sha)
        self.assertEqual((findings, [p.bundle for p in packets]), ([], ["hw/checks/v1/fea-bracket"]))
        status, again = self.call(path)
        self.assertEqual((status, again["reused"], again["check_bundle"]), (0, True, "hw/checks/v1/fea-bracket"))
        self.assertEqual(self.solved[1:], [], "a reused case is not solved again")

    def test_a_missed_criterion_fails_with_a_critical_finding(self) -> None:
        self.build(documents())
        self.solution["safety_factor_yield"] = 1.5
        status, report = self.call(self.case({"conditions": DRY_23C}, acceptance={"strength": "REQ-001"}))
        self.assertEqual(status, 1)
        self.assertEqual(self.outcomes(report)[f"engineering.strength@{PN}"], "fail")
        finding = next(f for f in report["engineering"]["findings"] if f["code"] == "fea.strength.margin")
        self.assertEqual((finding["severity"], finding["measured"]), ("critical", {"value": 1.5, "units": "dimensionless"}))
        self.validate_packet(report["engineering"])

    def test_an_undercovered_service_load_keeps_strength_unknown(self) -> None:
        self.build(documents())
        path = self.case({"conditions": SERVICE}, acceptance={"strength": "REQ-001"}, mesh_convergence={"decision": "DR-002"},
                         loads=[{"selector": {"indices": [2]}, "force": [0, 0, -20], "load": "head_load"}])
        status, report = self.call(path)
        self.assertEqual(status, 0)
        self.assertEqual(self.outcomes(report)[f"engineering.strength@{PN}"], "unknown")
        self.assertIn("reaches 50 N", report["engineering"]["outcomes"][1]["uncertainty"])

    def test_an_approved_effective_isotropic_model_analyses_printed_material_at_its_orientation(self) -> None:
        part = bracket("tst_polymer_fdm", [fx.operation("print", "fdm", fx.unknown("no capability in this fixture"))])
        self.build(documents(profiles=[printed()], part=part))
        model = {"decision": "DR-003", "justification": "TST: Z-direction values bound the layer-normal load path"}
        status, report = self.call(self.case({"conditions": {**DRY_23C, "orientation": "Z"}, "effective_isotropic": model}))
        self.assertEqual(status, 0, report)
        self.assertEqual((self.solved[0]["E"], self.solved[0]["yield"]), (1500.0, 20.0))
        self.assertEqual(report["material"]["isotropy"]["model"], "effective_isotropic")
        self.assertIn(f"engineering.anisotropy@{PN}", self.outcomes(report))
        self.validate_packet(report["engineering"])

    def test_production_service_resolves_the_yield_allowable(self) -> None:
        profile = alloy(prop("youngs_modulus", 70000, "MPa"), prop("poisson_ratio", 0.33, "dimensionless"),
                        prop("density", 2700, "kg_m3"), prop("yield_strength", 250, "MPa"),
                        prop("yield_strength", 200, "MPa", basis="allowable"), typical_not_allowable=False)
        self.build(documents(profiles=[profile], service_class="production"))
        status, report = self.call(self.case({"conditions": DRY_23C}))
        self.assertEqual(status, 0, report)
        self.assertEqual(self.solved[0]["yield"], 200.0)
        rows = {row["input"]: row for row in report["material"]["properties"]}
        self.assertEqual(rows["yield"]["basis"], "allowable")

    def test_a_changed_decision_invalidates_the_published_bundle(self) -> None:
        self.build(documents())
        path = self.case({"conditions": DRY_23C}, mesh_convergence={"decision": "DR-002"})
        self.assertEqual(self.call(path)[0], 0)
        (self.hw / "decisions" / "DR-002.md").write_text("# DR-002\n\nRevised mesh study.\n", encoding="utf-8")
        status, report = self.call(path)
        self.assertEqual(status, 2)
        self.assertIn("--replace", report["error"]["message"])

    def validate_packet(self, packet: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        schemas = SKILLS / "cad-record" / "schemas"
        contents = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(schemas.glob("*.schema.json"))]
        registry = Registry().with_resources((c["$id"], Resource.from_contents(c)) for c in contents)
        schema = json.loads((schemas / "manufacturing-check-result.schema.json").read_text(encoding="utf-8"))
        errors = [f"{list(e.absolute_path)}: {e.message}" for e in Draft202012Validator(schema, registry=registry).iter_errors(packet)]
        self.assertEqual(errors, [])


class LegacyExplicitValues(SourcedFEACase):
    EXPLICIT = {"E": 69000, "nu": 0.33, "density": 2700, "yield": 276}

    def test_a_record_version_1_case_keeps_its_numbers_labelled_unsourced(self) -> None:
        docs = documents()
        docs["product.yaml"] = {key: value for key, value in docs["product.yaml"].items()
                                if key not in ("record_version", "use_case")}
        self.build(docs)
        status, report = self.call(self.case(dict(self.EXPLICIT)))
        self.assertEqual(status, 0, report)
        self.assertEqual(report["material"]["source"], "unsourced_explicit")
        self.assertEqual((report["evidence"]["level"], report["evidence"]["exploratory"], report["evidence"]["service_evidence"]),
                         (None, True, False))
        self.assertNotIn("engineering", report)

    def test_a_standalone_case_keeps_its_numbers_labelled_unsourced(self) -> None:
        path = self.case(dict(self.EXPLICIT))
        output = io.StringIO()

        def run_job(case_path: Path, case: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], int]:
            result = {"status": "pass", **self.solution}
            fea_static.default_result_path(case_path).write_text(json.dumps(result), encoding="utf-8")
            return result, 0

        with patch("fea_static.find_freecad", return_value=Path("/fake/freecadcmd")), \
             patch("fea_static.run_job", side_effect=run_job), patch("sys.stdout", output):
            status = fea_static.main([str(path)])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0, report)
        self.assertEqual((report["material"]["source"], report["evidence"]["level"]), ("unsourced_explicit", None))
        written = json.loads(fea_static.default_result_path(path).read_text(encoding="utf-8"))
        self.assertEqual(written["evidence"], report["evidence"])


if __name__ == "__main__":
    unittest.main()
