#!/usr/bin/env python3
"""assembly_operations: one normalizer of new and legacy operations, its dual-authority refusal, declared
insertion paths, and the pure joint-evidence checks with their `assembly.joint` outcomes. Kernel-free; the
order checks of plan section 11 row 15 are in skills/cad-motion/tests/test_access.py beside the geometric ones."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"


def load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ops = load("cad_record_assembly_operations", SCRIPTS / "assembly_operations.py")
fx = load("cad_record_mi_fixtures", HERE / "fixtures" / "manufacturing_intent.py")
intent = fx.intent_module()

PRESS = {"id": "fit-bearing", "kind": "press_fit", "part": "TST-0003", "into": "TST-0001", "interface": "IF-001",
         "direction": [0, 0, -2], "acceptance": {"kind": "seated", "features": ["bearing_face", "seat_floor"]},
         "tools": ["arbor press"]}


def validator(definition: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = HERE.parent / "schemas"
    resources = [(c["$id"], Resource.from_contents(c))
                 for c in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(schemas.glob("*.schema.json")))]
    return Draft202012Validator({"$ref": f"https://srobroek.com/cad/schemas/manufacturing.schema.json#/$defs/{definition}"},
                                registry=Registry().with_resources(resources))


class NormalizeTests(unittest.TestCase):
    def test_new_and_legacy_operations_normalize_to_the_same_semantics(self) -> None:
        current = ops.normalize({"operations": [PRESS]})
        legacy = ops.normalize({"drawing": {"number": "D-1", "operations": [PRESS]}})
        self.assertEqual((current.authority, legacy.authority), ("operations", "drawing.operations"))
        self.assertEqual(ops.summary(current), ops.summary(legacy))
        [op] = current.items
        self.assertEqual(op.direction, (0.0, 0.0, -1.0))
        self.assertTrue(op.installs and op.geometric_acceptance)
        self.assertEqual(op.tool_names, ("arbor press",))
        self.assertFalse(op.tools[0].declared or op.tools[0].reach_known)
        self.assertIsNone(op.tools[0].envelope_artifact)

    def test_both_authorities_are_refused(self) -> None:
        with self.assertRaises(ops.OperationsError) as caught:
            ops.normalize({"operations": [PRESS], "drawing": {"operations": [PRESS]}})
        self.assertEqual(caught.exception.code, "intent.dual_authority")

    def test_unknown_or_absent_operations_stay_unknown(self) -> None:
        unknown = ops.normalize({"operations": fx.unknown("not planned yet")})
        self.assertEqual((unknown.items, unknown.unknown), ((), "not planned yet"))
        absent = ops.normalize({"subassemblies": []})
        self.assertEqual((absent.authority, absent.unknown), ("none", "assembly.yaml records no operations"))

    def test_malformed_operations_are_refused(self) -> None:
        def changed(**fields):
            return {**copy.deepcopy(PRESS), **fields}

        cases = {
            "unknown kind": [changed(kind="glue")],
            "press fit without interface": [{k: v for k, v in PRESS.items() if k != "interface"}],
            "close without closes": [{"id": "shut", "kind": "close", "part": "TST-0002", "into": "TST-0001"}],
            "zero direction": [changed(direction=[0, 0, 0])],
            "seated with one feature": [changed(acceptance={"kind": "seated", "features": ["a"]})],
            "repeated id": [PRESS, PRESS],
            "tool without name": [changed(tool_access=[{"approach": [0, 0, -1]}])],
        }
        for name, items in cases.items():
            with self.subTest(name), self.assertRaises(ops.OperationsError):
                ops.normalize({"operations": items})

    def test_a_tool_envelope_names_its_solid_only_when_its_fact_cites_one(self) -> None:
        envelope = fx.known("TST socket", "supplier_typical", sources=[fx.source(
            "supplier", "TST socket CAD", artifact="manufacturing/sources/socket.step", sha256="a" * 64)])
        item = {**PRESS, "tool_access": [{"tool": "socket", "approach": [0, 0, -1], "envelope": envelope,
                                          "reach": fx.known(fx.quantity(80), "supplier_typical", sources=[fx.source("supplier")])},
                                         {"tool": "wrench", "envelope": fx.known("open-end wrench")}]}
        [op] = ops.normalize({"operations": [item]}).items
        socket, wrench = op.tools[1:]
        self.assertEqual(socket.envelope_artifact, ("manufacturing/sources/socket.step", "a" * 64))
        self.assertTrue(socket.reach_known)
        self.assertIsNone(wrench.envelope_artifact)

    def test_a_normalized_record_reads_the_same_operations(self) -> None:
        documents = fx.tst_record()
        with tempfile.TemporaryDirectory() as directory:
            hw = fx.write_record(Path(directory) / "hw", documents)
            record = intent.load_record(hw)
        from_record = ops.from_record(record)
        direct = ops.normalize(documents["assembly.yaml"])
        self.assertEqual(ops.summary(from_record), ops.summary(direct))
        self.assertEqual(from_record.ids, ("fit_bearing",))

    def test_a_declared_insertion_path_arrives_along_the_direction(self) -> None:
        item = {**PRESS, "path": {"start": [10, 0, 30], "via": [[0, 0, 30]]}}
        [op] = ops.normalize({"operations": [item]}).items
        self.assertEqual(op.path, ((10.0, 0.0, 30.0), (0.0, 0.0, 30.0)))
        self.assertEqual(ops.summary(ops.normalize({"operations": [item]}))[0]["path"], [[10.0, 0.0, 30.0], [0.0, 0.0, 30.0]])
        [request] = ops.access_plan(ops.normalize({"operations": [item]}))
        self.assertEqual(request.path, op.path)
        [default] = ops.access_plan(ops.normalize({"operations": [PRESS]}))
        self.assertIsNone(default.path, "without a path the straight line along direction is the default")
        refused = {
            "last leg across the direction": {**PRESS, "path": {"start": [10, 0, 30]}},
            "last leg against the direction": {**PRESS, "path": {"start": [0, 0, -5]}},
            "a leg of zero length": {**PRESS, "path": {"start": [0, 0, 30], "via": [[0, 0, 30]]}},
            "a path on an inspection": {"id": "look", "kind": "inspect", "part": "TST-0003", "direction": [0, 0, -1],
                                        "acceptance": {"kind": "inspected", "features": ["bore"]}, "path": {"start": [0, 0, 5]}},
            "edge distance on a closure": {"id": "shut", "kind": "close", "part": "TST-0002", "into": "TST-0001",
                                           "closes": ["TST-0003"], "edge_distance": {}},
        }
        for name, bad in refused.items():
            with self.subTest(name), self.assertRaises(ops.OperationsError):
                ops.normalize({"operations": [bad]})


class JointTests(unittest.TestCase):
    """Joint evidence, one assembly.joint outcome per joining operation: retention, thread engagement and
    torque, edge distance against its criterion, and the host's temperature limit."""

    def record(self, *, temperature=None, limit: float | None = 60.0, retention=None, sourced: bool = True) -> tuple:
        """The TST record and its operations; the host's limit is a datasheet value unless not `sourced` (an estimate)."""
        documents = fx.tst_record()
        operation = documents["assembly.yaml"]["operations"][0]
        if retention is not None:
            operation["retention"] = retention
        if temperature is not None:
            documents["product.yaml"]["use_case"]["environment"]["temperature"] = temperature
        if limit is not None:
            profile = next(p for p in documents["manufacturing/materials.yaml"]["profiles"]
                           if p["id"] == "tst_polymer_printed")
            datasheet = {"basis": "supplier_typical", "sources": [fx.source("datasheet", "TST polymer datasheet")]}
            profile["properties"].append(fx.material_property("max_service_temperature", limit, "deg_C",
                                                              orientation=fx.known("XY"), **(datasheet if sourced else {})))
        with tempfile.TemporaryDirectory() as directory:
            record = intent.load_record(fx.write_record(Path(directory) / "hw", documents))
        return record, ops.from_record(record)

    def codes(self, report) -> dict[str, str]:
        """Each finding's code and outcome; every finding and outcome is schema-valid and names its obligation."""
        finding, outcome = validator("finding"), validator("obligation_outcome")
        for item in report.findings:
            self.assertEqual(list(finding.iter_errors(item)), [], item["id"])
            self.assertEqual(item["obligation"], f"assembly.joint@op:{item['subject']['assembly_operation']}")
        for row in report.outcomes:
            self.assertEqual(list(outcome.iter_errors(row)), [], row["obligation"])
        return {item["code"]: item["outcome"] for item in report.findings}

    def fasten(self, **fields) -> object:
        sourced = {"sources": [fx.source("supplier", "TST insert datasheet")], "basis": "supplier_typical"}
        item = {"id": "screw", "kind": "fasten", "part": "TST-0002", "into": "TST-0001", "direction": [0, 0, -1],
                "acceptance": {"kind": "torqued", "features": ["head"]},
                "retention": fx.known("TST insert pull-out 400 N", **sourced),
                "engagement": fx.known(fx.quantity(6), **sourced), "torque": fx.known(fx.quantity(0.8, "N_m"), **sourced)}
        return ops.normalize({"operations": [{**item, **fields}]})

    def test_a_press_fit_without_retention_evidence_stays_unknown(self) -> None:
        record, operations = self.record(temperature=fx.known(fx.quantity(lower=10, upper=40, units="deg_C")))
        report = ops.check_joints(operations, record, intent.resolve_property)
        self.assertEqual(self.codes(report), {"assembly.retention_unknown": "unknown"})
        row = report.outcome("assembly.joint@op:fit_bearing")
        self.assertEqual((row["outcome"], row["level"], row["findings"]), ("unknown", "process_planned", ["fit_bearing_retention_unknown"]))
        self.assertIn("retention", row["uncertainty"])
        self.assertEqual(ops.JOINING, intent.JOINING_KINDS, "the record's joint obligations and this check cover one set")

    def test_a_host_softening_in_service_fails_the_joint(self) -> None:
        retention = fx.known("TST pull-out test at 80 deg_C", "measured", sources=[fx.source("test", "TST pull-out")])
        record, operations = self.record(temperature=fx.known(fx.quantity(lower=10, upper=80, units="deg_C")),
                                         retention=retention)
        report = ops.check_joints(operations, record, intent.resolve_property)
        self.assertEqual(self.codes(report), {"assembly.joint_temperature": "fail"})
        self.assertEqual(report.findings[0]["severity"], "critical")
        self.assertEqual(report.findings[0]["criterion"]["value"], 60.0)
        self.assertEqual(report.outcome("assembly.joint@op:fit_bearing")["outcome"], "fail")

    def test_an_unknown_service_temperature_or_limit_stays_unknown(self) -> None:
        record, operations = self.record(limit=None)
        found = self.codes(ops.check_joints(operations, record, intent.resolve_property))
        self.assertEqual(found, {"assembly.retention_unknown": "unknown", "assembly.joint_temperature_unknown": "unknown"})

    def test_a_host_that_resolves_to_no_part_keeps_the_joint_unknown(self) -> None:
        """Sourced retention alone never passes a press fit whose host material cannot be read: an occurrence
        reference with no part_of resolution, or a host without a material profile, stays unknown."""
        retention = fx.known("TST pull-out test", "measured", sources=[fx.source("test", "TST pull-out")])
        record, _ = self.record(temperature=fx.known(fx.quantity(lower=10, upper=40, units="deg_C")), retention=retention)
        press = {"id": "fit_bearing", "kind": "press_fit", "part": "TST-0003", "into": "@frame", "interface": "IF-001",
                 "direction": [0, 0, -1], "acceptance": {"kind": "seated", "features": ["bearing_face", "seat_floor"]},
                 "retention": retention}
        report = ops.check_joints(ops.normalize({"operations": [press]}), record, intent.resolve_property)
        self.assertEqual(self.codes(report), {"assembly.joint_temperature_unknown": "unknown"})
        self.assertIn("@frame resolves to no part", report.findings[0]["measured"]["reason"])
        resolved = ops.check_joints(ops.normalize({"operations": [press]}), record, intent.resolve_property,
                                    part_of=lambda ref: "TST-0001" if ref == "@frame" else ref)
        self.assertEqual(resolved.outcome("assembly.joint@op:fit_bearing")["outcome"], "pass")

    def test_an_estimated_host_limit_can_fail_a_joint_but_never_pass_it(self) -> None:
        """A limit that is an estimate cannot show the host holds: within it the joint stays unknown even with
        sourced retention; above it the joint still fails."""
        retention = fx.known("TST pull-out test", "measured", sources=[fx.source("test", "TST pull-out")])
        record, operations = self.record(temperature=fx.known(fx.quantity(lower=10, upper=40, units="deg_C")),
                                         retention=retention, sourced=False)
        within = ops.check_joints(operations, record, intent.resolve_property)
        self.assertEqual(self.codes(within), {"assembly.joint_temperature_unknown": "unknown"})
        self.assertIn("max_service_temperature 60 deg_C (estimate) is not a qualified limit",
                      within.findings[0]["measured"]["reason"])
        self.assertEqual(within.outcome("assembly.joint@op:fit_bearing")["outcome"], "unknown")
        record, operations = self.record(temperature=fx.known(fx.quantity(lower=10, upper=80, units="deg_C")),
                                         retention=retention, sourced=False)
        above = ops.check_joints(operations, record, intent.resolve_property)
        self.assertEqual(self.codes(above), {"assembly.joint_temperature": "fail"})
        self.assertEqual(above.outcome("assembly.joint@op:fit_bearing")["outcome"], "fail")

    def test_a_fastener_needs_sourced_engagement_torque_and_edge_distance(self) -> None:
        record, _ = self.record(limit=None)
        fasten = ops.normalize({"operations": [{"id": "screw", "kind": "fasten", "part": "TST-0002", "into": "TST-0001",
                                                "direction": [0, 0, -1],
                                                "acceptance": {"kind": "torqued", "features": ["head"]},
                                                "torque": fx.unknown("not specified")}]})
        found = self.codes(ops.check_joints(fasten, record, intent.resolve_property))
        self.assertEqual(found, {"assembly.retention_unknown": "unknown", "assembly.engagement_unknown": "unknown",
                                 "assembly.torque_unknown": "unknown", "assembly.edge_distance_unknown": "unknown"})

    def test_the_edge_distance_is_judged_against_its_criterion(self) -> None:
        record, _ = self.record(limit=None)
        criterion = fx.criterion("edge_distance", ">=", 4.5, "mm", source=fx.source("standard", "TST-STD-2 insert edge distance"))
        cases = {
            "meets its minimum": (fx.known(fx.quantity(lower=4.5, upper=5.0)), "pass", {}),
            "short of its minimum": (fx.known(fx.quantity(4200, "um")), "fail", {"assembly.edge_distance": "fail"}),
            "not measured": (fx.unknown("not measured on the model"), "unknown", {"assembly.edge_distance_unknown": "unknown"}),
        }
        for name, (distance, outcome, codes) in cases.items():
            with self.subTest(name):
                report = ops.check_joints(self.fasten(edge_distance={"criterion": criterion, "distance": distance}),
                                          record, intent.resolve_property)
                self.assertEqual(self.codes(report), codes)
                self.assertEqual(report.outcome("assembly.joint@op:screw")["outcome"], outcome)
        [failed] = ops.check_joints(self.fasten(edge_distance={"criterion": criterion, "distance": fx.known(fx.quantity(4))}),
                                    record, intent.resolve_property).findings
        self.assertEqual((failed["severity"], failed["criterion"]["source"]["reference"]), ("critical", "TST-STD-2 insert edge distance"))
        between = fx.criterion("edge_distance", "between", [3, 6], "mm")
        wide = ops.check_joints(self.fasten(edge_distance={"criterion": between, "distance": fx.known(fx.quantity(lower=5, upper=7))}),
                                record, intent.resolve_property)
        self.assertEqual(self.codes(wide), {"assembly.edge_distance": "fail"}, "a between criterion bounds the upper end too")


if __name__ == "__main__":
    unittest.main()
