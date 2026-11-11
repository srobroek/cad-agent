#!/usr/bin/env python3
"""impact.py: dependency-domain fingerprints, what a change makes stale, the decisions it reopens, the
earliest stage it reopens without raising the recheck threshold, explicit carry-forward candidates, and
the selection compared by selection.py's own input digest.

Regression matrix row 13: sealed grade, route and orientation variants of a synthetic TST record make the
dependent FEA, fit, support, mass and test obligations and evidence stale, and never the unaffected ones.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


acceptance = load("cad_record_acceptance_fixtures", HERE / "fixtures" / "acceptance.py")
fx, mi, impact = acceptance.fx, acceptance.mi, acceptance.impact
GEOMETRY = {"TST-0001": "1" * 64, "TST-0002": "2" * 64, "TST-0004": "4" * 64}


def documents() -> dict[str, Any]:
    return {rel: data for rel, data in fx.tst_record().items() if not rel.startswith("_")}


def normalized(docs: dict[str, Any]) -> Any:
    return mi.normalize_record(copy.deepcopy(docs))


def bracket(docs: dict[str, Any]) -> dict[str, Any]:
    return docs["parts/TST-0001/part.yaml"]["intent"]["route"][0]


class Fingerprints(unittest.TestCase):
    def test_each_record_domain_is_fingerprinted_and_stable(self) -> None:
        record = normalized(documents())
        prints = impact.fingerprints(record, {"PN": "TST-0001"}, GEOMETRY)
        self.assertEqual(set(prints), set(impact.DOMAINS) - {"lot"}, "the lot belongs to physical evidence, not the record")
        self.assertEqual(prints, impact.fingerprints(normalized(documents()), {"PN": "TST-0001"}, GEOMETRY))
        self.assertEqual(impact.fingerprints(record, {"PN": "TST-0001", "operation": "print", "feature": "pivot_bore"},
                                             GEOMETRY), prints, "operations and features share their part's fingerprints")

    def test_the_evidence_ledger_is_never_a_dependency(self) -> None:
        docs = documents()
        before = impact.subject_fingerprints(normalized(docs), GEOMETRY)
        docs["manufacturing/evidence.yaml"] = {"entries": [acceptance.entry(normalized(docs), "EV-001", {"PN": "TST-0001"},
                                                                            "cad_valid", "planned", obligation="cad.readback@TST-0001")]}
        self.assertEqual(impact.subject_fingerprints(normalized(docs), GEOMETRY), before, "no ledger-rebuild recursion")


class Variants(unittest.TestCase):
    """A sealed grade, route or orientation variant of the bracket against the same record before it."""

    def setUp(self) -> None:
        self.docs = documents()
        self.before = normalized(self.docs)

    def compare(self, change: Any, geometry: dict[str, str] | None = None) -> Any:
        after = copy.deepcopy(self.docs)
        change(after)
        return impact.compare(self.before, normalized(after), before_geometry=GEOMETRY,
                              after_geometry=geometry or GEOMETRY, since="v1", version="v2")

    def assertStale(self, result: Any, stale: tuple[str, ...], kept: tuple[str, ...]) -> None:
        for ident in stale:
            self.assertIn(ident, result.stale_obligations, ident)
        for ident in kept:
            self.assertNotIn(ident, result.stale_obligations, ident)

    def test_a_grade_change_stales_engineering_fit_support_and_tests_not_geometry(self) -> None:
        def grade(docs: dict[str, Any]) -> None:
            docs["manufacturing/materials.yaml"]["profiles"][0]["grade"] = fx.known("TST other grade")
        result = self.compare(grade)
        self.assertEqual(result.changed["TST-0001"], ("material",))
        self.assertIn("material_condition", result.groups()["TST-0001"])
        self.assertStale(result, ("engineering.strength@TST-0001", "engineering.creep@TST-0001", "feature.fit@TST-0001#pivot_bore",
                                  "process.fdm.support@TST-0001/print", "process.fdm.trial@TST-0001/print"),
                         ("cad.readback@TST-0001", "cad.readback@TST-0002", "process.cnc.stock@TST-0002/turn"))
        self.assertEqual(result.earliest_stage, "parts")

    def test_a_route_change_stales_the_process_plan_and_its_consumers(self) -> None:
        def route(docs: dict[str, Any]) -> None:
            docs["manufacturing/capabilities.yaml"]["profiles"].append(fx.capability_profile("tst_printer_2", "fdm"))
            bracket(docs)["capability"] = "tst_printer_2"
        result = self.compare(route)
        self.assertTrue({"capability", "route"} <= set(result.changed["TST-0001"]))
        self.assertStale(result, ("process.fdm.geometry@TST-0001/print", "route.operation_plan@TST-0001/print",
                                  "feature.fit@TST-0001#pivot_bore", "process.reaming.allowance@TST-0001/ream"),
                         ("cad.readback@TST-0001", "process.laser.kerf@TST-0004/cut"))

    def test_an_orientation_change_stales_support_anisotropy_and_fit_and_reopens_its_approval(self) -> None:
        def orientation(docs: dict[str, Any]) -> None:
            bracket(docs)["orientation"]["rotation"] = {"axis": [1, 0, 0], "angle_deg": 90}
        result = self.compare(orientation)
        self.assertEqual(result.changed["TST-0001"], ("orientation",))
        self.assertStale(result, ("process.fdm.support@TST-0001/print", "engineering.anisotropy@TST-0001",
                                  "feature.fit@TST-0001#pivot_bore"),
                         ("cad.readback@TST-0001", "assembly.order@op:fit_bearing"))
        self.assertIn("DR-001", result.reopened_decisions)
        self.assertEqual(result.earliest_stage, "detail")

    def test_a_geometry_change_stales_the_readback(self) -> None:
        result = self.compare(lambda docs: None, geometry={**GEOMETRY, "TST-0001": "9" * 64})
        self.assertStale(result, ("cad.readback@TST-0001", "interface.fit@IF-001"), ("cad.readback@TST-0002",))
        self.assertEqual(result.earliest_stage, "layout")

    def test_record_derived_obligations_are_recomputed_never_stale(self) -> None:
        def use_case(docs: dict[str, Any]) -> None:
            docs["product.yaml"]["use_case"]["purpose"] = fx.known("TST another purpose")
        result = self.compare(use_case)
        self.assertFalse([ident for ident in result.stale_obligations if ident.startswith("intent.")])
        self.assertEqual(result.earliest_stage, "intent")

    def test_nothing_changed_is_no_impact(self) -> None:
        result = self.compare(lambda docs: None)
        self.assertFalse(result.any)
        self.assertEqual((result.changed, result.earliest_stage, result.proposed_flow), ({}, None, None))


class EvidenceAndFlow(unittest.TestCase):
    def test_changed_evidence_is_stale_and_unchanged_evidence_is_only_a_carry_forward_candidate(self) -> None:
        docs = documents()
        before = normalized(docs)
        entries = [acceptance.entry(before, "EV-001", {"PN": "TST-0001"}, "simulation_supported",
                                    obligation="engineering.strength@TST-0001", version="v1", manifest_sha256="a" * 64,
                                    geometry=GEOMETRY),
                   acceptance.entry(before, "EV-002", {"PN": "TST-0004"}, "geometrically_manufacturable",
                                    obligation="process.laser.kerf@TST-0004/cut", version="v1", manifest_sha256="a" * 64,
                                    geometry=GEOMETRY),
                   {"id": "EV-003", "subject": {"PN": "TST-0002"}, "level": "cad_valid", "result": "planned",
                    "method": "TST", "obligation": "cad.readback@TST-0002"}]
        after_docs = copy.deepcopy(docs)
        after_docs["manufacturing/materials.yaml"]["profiles"][0]["grade"] = fx.known("TST other grade")
        after_docs["manufacturing/evidence.yaml"] = {"entries": entries}
        result = impact.compare(before, normalized(after_docs), before_geometry=GEOMETRY, after_geometry=GEOMETRY,
                                since="v1", version="v2")
        self.assertEqual(result.stale_evidence["EV-001"], ("material",))
        self.assertEqual(result.stale_evidence["EV-003"], ("unrecorded",), "absent dependency metadata is never current")
        self.assertEqual([item["evidence"] for item in result.carry_forward], ["EV-002"])
        self.assertIn("candidate only", result.carry_forward[0]["note"])

    def test_a_reopened_stage_never_raises_the_recheck_threshold(self) -> None:
        docs = documents()
        docs["product.yaml"]["flow"] = {"stage": "analysis", "recheck_from": "layout"}
        before = normalized(docs)
        after = copy.deepcopy(docs)
        bracket(after)["orientation"]["rotation"] = {"axis": [0, 1, 0], "angle_deg": 90}
        result = impact.compare(before, normalized(after), before_geometry=GEOMETRY, after_geometry=GEOMETRY)
        self.assertEqual(result.proposed_flow, {"stage": "detail", "recheck_from": "layout"})
        after["product.yaml"]["use_case"]["purpose"] = fx.known("TST changed purpose")
        result = impact.compare(before, normalized(after), before_geometry=GEOMETRY, after_geometry=GEOMETRY)
        self.assertEqual(result.proposed_flow, {"stage": "intent", "recheck_from": "intent"})


class Selection(unittest.TestCase):
    """A confirmed selection (U2's qualified polymer cover, confirmed by DR-001) against changed records."""

    def setUp(self) -> None:
        self.docs, self.digest = acceptance.recorded_selection(acceptance.selection_scenarios().qualified_polymer_cover(),
                                                               primary="printed_cover")

    def compare(self, after: dict[str, Any]) -> Any:
        return impact.compare(normalized(self.docs), normalized(after), before_selection_sha256=self.digest,
                              after_selection_sha256=acceptance.evidence.selection.input_digest(after))

    def test_a_changed_selection_input_reopens_the_confirmed_choice(self) -> None:
        after = copy.deepcopy(self.docs)
        after["manufacturing/materials.yaml"]["profiles"][0]["revision"] = 2
        result = self.compare(after)
        self.assertEqual((result.selection["changed"], result.selection["stale"], result.selection["recorded"]),
                         (True, True, self.digest))
        self.assertIn("the selection inputs changed (selection.input_digest)", result.reopened_decisions["DR-001"])
        self.assertTrue(result.any)

    def test_confirming_the_choice_is_no_selection_input(self) -> None:
        before, _ = acceptance.recorded_selection(acceptance.selection_scenarios().qualified_polymer_cover())
        result = impact.compare(normalized(before), normalized(self.docs),
                                before_selection_sha256=acceptance.evidence.selection.input_digest(before),
                                after_selection_sha256=self.digest)
        self.assertEqual((result.selection["changed"], result.selection["stale"]), (False, False))
        self.assertEqual((result.reopened_decisions, result.any), ({}, False))

    def test_an_assessment_of_other_inputs_is_reopened_without_a_change(self) -> None:
        stale, digest = acceptance.recorded_selection(self.docs, primary="printed_cover", digest="0" * 64)
        result = impact.compare(normalized(stale), normalized(stale), before_selection_sha256=digest,
                                after_selection_sha256=digest)
        self.assertEqual((result.selection["changed"], result.selection["stale"], result.any), (False, True, False))
        self.assertEqual(result.reopened_decisions["DR-001"], ("the recorded selection assessment is not of the current inputs",))

    def test_without_digests_the_selection_is_not_compared(self) -> None:
        after = copy.deepcopy(self.docs)
        after["manufacturing/materials.yaml"]["profiles"][0]["revision"] = 2
        result = impact.compare(normalized(self.docs), normalized(after))
        self.assertIsNone(result.selection)
        self.assertNotIn("DR-001", result.reopened_decisions)


class Cli(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-impact-test-")
        self.addCleanup(temporary.cleanup)
        self.hw = fx.write_record(Path(temporary.name) / "hw", fx.tst_record())
        for pn in GEOMETRY:
            (self.hw / "parts" / pn / "model.py").write_text(f"# {pn}\n", encoding="utf-8")

    def build(self, label: str) -> None:
        folder = self.hw / "versions" / label
        pns = sorted(p.parent.name for p in (self.hw / "parts").glob("*/part.yaml"))
        for name in acceptance.layout.snapshot_names(self.hw, pns):
            target = folder / "record" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.hw / name).read_bytes())
        files = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(folder.rglob("*")) if p.is_file()}
        parts = {path.parent.name: {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(self.hw.glob("parts/*/model.py"))}
        (folder / "manifest.json").write_text(json.dumps({"label": label, "parts": parts, "files": files}), encoding="utf-8")

    def run_cli(self, *args: str) -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = impact.main(["--hw", str(self.hw), *args])
        return code, json.loads(out.getvalue())

    def test_a_material_change_since_a_built_version_is_reported_and_nothing_is_written(self) -> None:
        self.build("v1")
        code, report = self.run_cli("--since-version", "v1")
        self.assertEqual((code, report["status"]), (0, "unchanged"))
        self.assertEqual((report["selection"]["changed"], report["selection"]["recorded"]), (False, None))
        materials = self.hw / "manufacturing" / "materials.yaml"
        data = yaml.safe_load(materials.read_text(encoding="utf-8"))
        data["profiles"][0]["revision"] = 2
        materials.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        before = {p: p.read_bytes() for p in self.hw.rglob("*") if p.is_file()}
        code, report = self.run_cli("--since-version", "v1", "--out", str(self.hw.parent / ".cad-work" / "impact.json"))
        self.assertEqual((code, report["status"]), (1, "impact"))
        self.assertIn("material", report["changed"]["TST-0001"])
        self.assertIn("engineering.creep@TST-0001", report["stale_obligations"])
        self.assertTrue(report["selection"]["changed"], "materials.yaml is a selection input")
        self.assertEqual({p: p.read_bytes() for p in self.hw.rglob("*") if p.is_file()}, before, "report only")
        self.assertTrue((self.hw.parent / ".cad-work" / "impact.json").is_file())

    def test_a_version_1_record_refuses_with_the_migration_command(self) -> None:
        product = self.hw / "product.yaml"
        data = yaml.safe_load(product.read_text(encoding="utf-8"))
        del data["record_version"]
        product.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.build("v1")
        code, report = self.run_cli("--since-version", "v1")
        self.assertEqual((code, report["code"]), (2, "record.migration_required"))
        self.assertIn("migrate.py", report["action"])


if __name__ == "__main__":
    unittest.main()
