#!/usr/bin/env python3
"""Assembly order and access (plan section 11 row 15).

Pure order and closure checks run always (cad-record assembly_operations). The geometric part and tool
paths run access.py through `uv run --script` on access_fixture.py's built geometry, so they skip
without uv. Run: python3 -m unittest discover -s skills/cad-motion/tests -p test_access.py
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
SCRIPT = HERE.parent / "scripts" / "access.py"
FIXTURES = HERE / "fixtures"


def load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ops = load("cad_record_assembly_operations", SKILLS / "cad-record" / "scripts" / "assembly_operations.py")
assembly_data = load("cad_record_assembly_data", SKILLS / "cad-record" / "scripts" / "assembly_data.py")
fx = load("cad_record_mi_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
PNS = ("TST-0101", "TST-0102", "TST-0103", "TST-0104", "TST-0105")
TITLES = {"TST-0101": "Housing", "TST-0102": "Bearing", "TST-0103": "Cover", "TST-0104": "Cover screw",
          "TST-0105": "Bracket"}


def operations(name: str, socket_sha256: str = "0" * 64) -> list[dict]:
    text = (FIXTURES / name).read_text(encoding="utf-8").replace("{socket_sha256}", socket_sha256)
    return yaml.safe_load(text)["operations"]


def reordered(items: list[dict], *ids: str) -> list[dict]:
    by_id = {item["id"]: item for item in items}
    return [copy.deepcopy(by_id[ident]) for ident in ids]


def inventory():
    return assembly_data.normalize({"subassemblies": [{"id": "main", "title": "Main", "parts": list(PNS)}]})


def validator(definition: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = SKILLS / "cad-record" / "schemas"
    resources = [(c["$id"], Resource.from_contents(c))
                 for c in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(schemas.glob("*.schema.json")))]
    target = definition if definition.startswith("https:") else \
        f"https://srobroek.com/cad/schemas/manufacturing.schema.json#/$defs/{definition}"
    return Draft202012Validator({"$ref": target}, registry=Registry().with_resources(resources))


class OrderTests(unittest.TestCase):
    """The pure order and closure check: closure before install fails; a valid order passes, its scope stated."""

    def check(self, items: list[dict]):
        return ops.check_order(ops.normalize({"operations": items}), inventory())

    def assert_schema_valid(self, report) -> None:
        finding, outcome = validator("finding"), validator("obligation_outcome")
        for item in report.findings:
            self.assertEqual(list(finding.iter_errors(item)), [], item["id"])
        for row in report.outcomes:
            self.assertEqual(list(outcome.iter_errors(row)), [], row["obligation"])

    def test_a_bearing_cannot_be_installed_after_the_cover_closes_it(self) -> None:
        report = self.check(operations("closed_bearing_install.yaml"))
        self.assert_schema_valid(report)
        self.assertEqual(report.outcome("assembly.order@op:fit-cover")["outcome"], "pass")
        failed = report.outcome("assembly.order@op:seat-bearing")
        self.assertEqual(failed["outcome"], "fail")
        [finding] = report.findings
        self.assertEqual(finding["code"], "assembly.closed_before_install")
        self.assertEqual(finding["severity"], "critical")
        self.assertIn("fit-cover encloses", finding["measured"]["text"])
        self.assertEqual(failed["findings"], [finding["id"]])

    def test_a_valid_order_passes_with_its_limited_scope(self) -> None:
        report = self.check(operations("blocked_socket.yaml"))
        self.assert_schema_valid(report)
        self.assertEqual({row["obligation"]: row["outcome"] for row in report.outcomes},
                         {f"assembly.order@op:{ident}": "pass"
                          for ident in ("seat-bearing", "fit-cover", "mount-bracket", "fasten-cover")})
        self.assertEqual(report.findings, ())
        self.assertIn("part paths and tool reach are assembly.access", report.outcomes[0]["coverage"])

    def test_a_receiving_part_installed_later_and_a_late_predecessor_fail(self) -> None:
        items = operations("blocked_socket.yaml")
        late_into = reordered(items, "seat-bearing", "fasten-cover", "fit-cover", "mount-bracket")
        report = self.check(late_into)
        self.assertEqual(report.outcome("assembly.order@op:fasten-cover")["outcome"], "fail")
        self.assertIn("assembly.into_absent", {f["code"] for f in report.findings})
        constrained = copy.deepcopy(items)
        constrained[0]["predecessors"] = ["fit-cover"]
        report = self.check(constrained)
        self.assertEqual(report.outcome("assembly.order@op:seat-bearing")["outcome"], "fail")
        self.assertIn("assembly.order_impossible", {f["code"] for f in report.findings})

    def test_an_enclosed_part_cannot_be_disassembled_or_adjusted(self) -> None:
        items = operations("blocked_socket.yaml")[:2] + [
            {"id": "pull-bearing", "kind": "disassemble", "part": "TST-0102", "into": "TST-0101", "direction": [0, 0, 1]}]
        report = self.check(items)
        self.assertEqual(report.outcome("assembly.order@op:pull-bearing")["outcome"], "fail")
        self.assertEqual({f["code"] for f in report.findings}, {"assembly.closed_before_install"})

    def test_unknown_operations_stay_unknown(self) -> None:
        report = ops.check_order(ops.normalize({"operations": fx.unknown("not planned yet")}))
        self.assertEqual([(row["obligation"], row["outcome"]) for row in report.outcomes],
                         [("intent.assembly_operations@product", "unknown")])

    def test_the_access_plan_lists_obstacles_and_keeps_a_named_tool_unknown(self) -> None:
        items = operations("blocked_socket.yaml")
        items[3]["tools"] = ["torque driver"]
        plan = {request.operation.id: request for request in ops.access_plan(ops.normalize({"operations": items}),
                                                                           inventory())}
        self.assertEqual(plan["seat-bearing"].path_obstacles, frozenset())  # only the receiving housing is in place
        self.assertEqual(plan["fit-cover"].path_obstacles, frozenset({"TST-0102"}))
        self.assertEqual(plan["fasten-cover"].tool_obstacles, frozenset({"TST-0101", "TST-0102", "TST-0103", "TST-0105"}))
        self.assertEqual(plan["fasten-cover"].frame, "TST-0103")
        self.assertIn("tool 'torque driver' is a name only: its reach and envelope are unknown",
                      plan["fasten-cover"].unknown)


def access_record(items: list[dict]) -> dict:
    """A record version 2 project of the five fixture parts with `items` as its operations."""
    parts = [fx.part(pn, TITLES[pn], process="cnc") for pn in PNS]
    interface = fx.interface("IF-001", "TST-0101", "TST-0102", "mounting",
                             limits={"press_fit": {"fit": "22 H7/p6", "pair": ["TST-0102", "TST-0101"]}})
    return fx.record(parts=parts, interfaces=[interface], operations=items)


@unittest.skipUnless(shutil.which("uv"), "uv is required to build the access geometry and run access.py")
class GeometricAccessTests(unittest.TestCase):
    """access.py on built geometry: a blocked socket and a closed bearing fail, the valid order passes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name).resolve()
        build = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "access_fixture.py"),
                                str(cls.root / "geometry")], capture_output=True, text=True, timeout=900, check=False)
        if build.returncode != 0:
            raise RuntimeError(build.stdout + build.stderr)
        cls.socket_sha = hashlib.sha256((cls.root / "geometry" / "socket.step").read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.folder.cleanup()

    def project(self, name: str, items: list[dict]) -> Path:
        documents = access_record(items)
        hw = fx.write_record(self.root / name / "hw", documents)
        source = hw / "manufacturing" / "sources" / "socket.step"
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / "geometry" / "socket.step", source)
        return hw

    def run_access(self, hw: Path, *extra: str) -> tuple[int, dict, str]:
        attach = [arg for pn in PNS[1:] for arg in ("--attach", f"{pn}=TST-0101")]
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), "--hw", str(hw), *extra, *attach],
                              capture_output=True, text=True, timeout=1800, check=False)
        self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        return proc.returncode, json.loads(proc.stdout), proc.stderr

    def parts_args(self) -> tuple[str, ...]:
        return ("--parts-dir", str(self.root / "geometry" / "parts"))

    @staticmethod
    def outcomes(result: dict) -> dict[str, str]:
        return {row["obligation"]: row["outcome"] for row in result["outcomes"]}

    def assert_schema_valid(self, result: dict) -> None:
        finding, outcome = validator("finding"), validator("obligation_outcome")
        for item in result["findings"]:
            self.assertEqual(list(finding.iter_errors(item)), [], item["id"])
        for row in result["outcomes"]:
            self.assertEqual(list(outcome.iter_errors(row)), [], row["obligation"])

    def test_a_socket_the_bracket_blocks_fails_its_access(self) -> None:
        hw = self.project("blocked", operations("blocked_socket.yaml", self.socket_sha))
        code, result, _ = self.run_access(hw, *self.parts_args())
        self.assert_schema_valid(result)
        self.assertEqual(code, 1)
        outcomes = self.outcomes(result)
        self.assertEqual(outcomes["assembly.access@op:fasten-cover"], "fail")
        for ident in ("seat-bearing", "fit-cover", "mount-bracket"):
            self.assertEqual(outcomes[f"assembly.access@op:{ident}"], "pass", ident)
        for ident in ("seat-bearing", "fit-cover", "mount-bracket", "fasten-cover"):
            self.assertEqual(outcomes[f"assembly.order@op:{ident}"], "pass", ident)
        [blocked] = [f for f in result["findings"] if f["obligation"] == "assembly.access@op:fasten-cover"]
        self.assertEqual(blocked["code"], "assembly.tool_blocked")
        self.assertIn("TST-0105", blocked["consequence"])
        screw, socket = result["detail"]["paths"]["fasten-cover"]
        self.assertEqual((screw["status"], socket["status"]), ("clear", "collision"))
        self.assertEqual((screw["label"], screw["route"], socket["route"]), ("fasten-cover/part", "straight_default", "approach"),
                         "without a declared path the straight line along the direction is the reported default")
        self.assertTrue(result["complete"])
        self.assertEqual(result["result"], "fail")

    def test_the_valid_order_passes_with_its_coverage_stated_and_publishes_a_packet(self) -> None:
        items = reordered(operations("blocked_socket.yaml", self.socket_sha),
                          "seat-bearing", "fit-cover", "fasten-cover", "mount-bracket")
        hw = self.project("valid", items)
        version = hw / "versions" / "v1"
        (version / "parts").mkdir(parents=True)
        for step in (self.root / "geometry" / "parts").iterdir():
            shutil.copyfile(step, version / "parts" / step.name)
        for path in hw.rglob("*"):
            rel = path.relative_to(hw)
            if path.is_file() and rel.parts[0] not in ("versions", "decisions") and (
                    rel.suffix == ".yaml" or rel.parts[:2] == ("manufacturing", "sources")):
                target = version / "record" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        files = {p.relative_to(version).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(version.rglob("*")) if p.is_file()}
        (version / "manifest.json").write_text(json.dumps({"label": "v1", "files": files}), encoding="utf-8")
        code, result, stderr = self.run_access(hw, "--version", "v1")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(set(self.outcomes(result).values()), {"pass"})
        self.assertEqual(result["findings"], [])
        coverage = next(row["coverage"] for row in result["outcomes"]
                        if row["obligation"] == "assembly.access@op:fasten-cover")
        self.assertIn("curved paths, tool posture, hands, forces and other poses are not covered", coverage)
        bundle = hw / "checks" / "v1" / "assembly-access"
        packet = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(list(validator("https://srobroek.com/cad/schemas/manufacturing-check-result.schema.json")
                              .iter_errors(packet)), [])
        self.assertEqual((packet["result"], packet["complete"], packet["version"]), ("pass", True, "v1"))
        self.assertTrue((bundle / "evidence" / "access.json").is_file())
        again, rerun, _ = self.run_access(hw, "--version", "v1")
        self.assertEqual((again, rerun["reused"]), (0, True))

    def test_a_closed_cover_blocks_the_bearing_path_and_fails_the_order(self) -> None:
        hw = self.project("closed", operations("closed_bearing_install.yaml"))
        code, result, _ = self.run_access(hw, *self.parts_args())
        self.assertEqual(code, 1)
        outcomes = self.outcomes(result)
        self.assertEqual(outcomes["assembly.order@op:seat-bearing"], "fail")
        self.assertEqual(outcomes["assembly.access@op:seat-bearing"], "fail")
        codes = {f["code"] for f in result["findings"] if f["subject"] == {"assembly_operation": "seat-bearing"}}
        self.assertEqual(codes, {"assembly.closed_before_install", "assembly.part_path_blocked"})
        path = next(f for f in result["findings"] if f["code"] == "assembly.part_path_blocked")
        self.assertIn("TST-0103", path["consequence"])

    def test_a_tool_known_by_name_only_keeps_access_unknown(self) -> None:
        items = reordered(operations("blocked_socket.yaml", self.socket_sha),
                          "seat-bearing", "fit-cover", "fasten-cover", "mount-bracket")
        del items[2]["tool_access"]
        items[2]["tools"] = ["3 mm hex key"]
        hw = self.project("named", items)
        code, result, _ = self.run_access(hw, *self.parts_args())
        self.assertEqual(code, 1)
        self.assertEqual(self.outcomes(result)["assembly.access@op:fasten-cover"], "unknown")
        self.assertEqual(result["result"], "unknown")
        unknown = next(f for f in result["findings"] if f["code"] == "assembly.access_unknown")
        self.assertIn("is a name only", unknown["uncertainty"])

    def test_a_declared_insertion_path_is_travelled_leg_by_leg(self) -> None:
        """The screw slid in sideways above the cover, then down: the bracket blocks the first leg, though the
        default straight path down through the bracket hole is clear."""
        items = operations("blocked_socket.yaml", self.socket_sha)
        items[3]["path"] = {"start": [-25, 0, 22], "via": [[0, 0, 22]]}
        hw = self.project("declared", items)
        code, result, _ = self.run_access(hw, *self.parts_args())
        self.assertEqual(code, 1)
        self.assertEqual(self.outcomes(result)["assembly.access@op:fasten-cover"], "fail")
        legs = [p for p in result["detail"]["paths"]["fasten-cover"] if p["route"] == "declared_path"]
        self.assertEqual([(p["label"], p["travel_mm"]) for p in legs], [("fasten-cover/part/leg1", 25.0), ("fasten-cover/part/leg2", 22.0)])
        self.assertEqual(legs[0]["status"], "collision")
        self.assertEqual({c["obstacle"] for c in legs[0]["collisions"]}, {"TST-0105"})
        blocked = next(f for f in result["findings"] if f["code"] == "assembly.part_path_blocked")
        self.assertIn("the declared insertion path", blocked["uncertainty"])


if __name__ == "__main__":
    unittest.main()
