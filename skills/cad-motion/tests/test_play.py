#!/usr/bin/env python3
"""Mechanism play (motion.play): declared play and loads become extreme-pose scenario cases run through the
sweep pair evaluator. Play extremes turn a nominal pass into a collision, play within the clearance passes,
and undeclared relevant play is unknown, never zero.

PlayBoundsTests are kernel-free; PlayGeometryTests build the play_fixture.py geometry and run play.py.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
SCRIPT = HERE.parent / "scripts" / "play.py"
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


fx = load("cad_record_mi_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
play = load("cad_motion_play", SCRIPT)
intent = fx.intent_module()
PNS = ("TST-0201", "TST-0202", "TST-0203")
TITLES = {"TST-0201": "Base", "TST-0202": "Arm", "TST-0203": "Bridge"}
JOINTS = [{"id": "J-001", "type": "revolute", "parent": "TST-0201", "child": "TST-0202", "axis": [0, 0, 1],
           "origin": [0, 0, 0], "min": -20, "max": 20, "nominal": 0},
          {"id": "J-002", "type": "fixed", "parent": "TST-0201", "child": "TST-0203", "axis": [0, 0, 1],
           "origin": [0, 0, 0], "min": 0, "max": 0, "nominal": 0}]


def mm(value: float, units: str = "mm") -> dict:
    return fx.known(fx.quantity(value, units), "calculated", sources=[fx.source("calculation", "TST fit stack")])


def documents(play_block: dict | None, *, rotating: tuple[str, ...] = (), **functional) -> dict:
    """A record version 2 project of the fixture parts: IF-001 a critical rotating pivot with `play_block`;
    the `rotating` parts declare that function themselves."""
    block = {"critical": fx.known(True), "functions": [{"function": "rotating"}], **functional}
    if play_block is not None:
        block["play"] = play_block
    parts = [fx.part(pn, TITLES[pn], process="cnc", part_intent=fx.intent(functions=[{"function": "rotating"}]))
             if pn in rotating else fx.part(pn, TITLES[pn], process="cnc") for pn in PNS]
    docs = fx.record(parts=parts, interfaces=[fx.interface("IF-001", "TST-0201", "TST-0202", "motion", block)])
    docs["assembly.yaml"]["joints"] = [dict(joint) for joint in JOINTS]
    return docs


def interface(play_block: dict | None, **functional):
    return intent.normalize_record(documents(play_block, **functional)).interfaces["IF-001"]


def joint(kind: str = "revolute"):
    return play.pose.Joint("J-001", kind, "TST-0201", "TST-0202", (0.0, 0.0, 1.0), (0.0, 0.0, 0.0), -20.0, 20.0, 0.0)


class PlayBoundsTests(unittest.TestCase):
    """The declared play and loads as extreme offsets, kernel-free."""

    def test_undeclared_relevant_play_is_unknown_never_zero(self) -> None:
        bounds = play.play_bounds("IF-001", interface({"radial": mm(0.2)}), joint(), coupled=False)
        self.assertEqual(bounds.halves, {"radial": 0.1})
        self.assertEqual((bounds.notes["axial"], bounds.notes["angular"]), ("undeclared", "undeclared"))
        self.assertEqual(len(bounds.unknown), 2)
        for name, reason in zip(("axial", "angular"), bounds.unknown):
            self.assertIn(f"{name} play is undeclared; undeclared play is unknown, never zero", reason)
        self.assertEqual(len(play.corner_cases(joint(), bounds.halves)), 4, "the declared radial play is still evaluated")

    def test_not_applicable_is_a_declaration_and_an_interval_takes_its_upper_bound(self) -> None:
        declared = {"axial": fx.not_applicable("preloaded angular-contact pair"),
                    "radial": fx.known(fx.quantity(lower=0.05, upper=0.3), "calculated", sources=[fx.source("calculation")]),
                    "angular": mm(0.2, "deg")}
        bounds = play.play_bounds("IF-001", interface(declared), joint(), coupled=False)
        self.assertEqual((bounds.unknown, bounds.halves), ([], {"radial": 0.15, "angular": 0.1}))
        self.assertTrue(bounds.notes["axial"].startswith("not applicable"))
        self.assertEqual(bounds.units, {"axial": "mm", "radial": "mm", "angular": "deg", "backlash": "deg"})

    def test_loads_need_their_compliance(self) -> None:
        declared = {"axial": mm(0.4), "radial": mm(0.2), "angular": mm(0.2, "deg")}
        spring = [{"function": "rotating"}, {"function": "spring"}]
        loaded = play.play_bounds("IF-001", interface(declared, functions=spring), joint(), coupled=False)
        self.assertEqual(len(loaded.unknown), 3)
        self.assertTrue(all("carries its spring function" in reason and "loaded extremes are unknown" in reason
                            for reason in loaded.unknown))
        compliant = dict(declared, compliance={"axial": mm(0.05), "radial": mm(0.01), "angular": mm(0.02, "deg")})
        bounds = play.play_bounds("IF-001", interface(compliant, functions=spring), joint(), coupled=False)
        self.assertEqual(bounds.unknown, [])
        self.assertAlmostEqual(bounds.halves["axial"], 0.25)
        self.assertAlmostEqual(bounds.halves["radial"], 0.11)
        self.assertAlmostEqual(bounds.halves["angular"], 0.12)

    def test_a_driven_joint_needs_its_backlash_as_a_joint_coordinate(self) -> None:
        declared = {"axial": mm(0.1), "radial": mm(0.1), "angular": mm(0.1, "deg"), "backlash": mm(0.2)}
        revolute = play.play_bounds("IF-001", interface(declared), joint(), coupled=True)
        self.assertEqual(len(revolute.unknown), 1)
        self.assertIn("backlash play is not a deg quantity", revolute.unknown[0])
        prismatic = play.play_bounds("IF-001", interface(declared), joint("prismatic"), coupled=True)
        self.assertEqual((prismatic.unknown, prismatic.halves["backlash"]), ([], 0.1))
        undeclared = play.play_bounds("IF-001", interface({k: v for k, v in declared.items() if k != "backlash"}),
                                      joint(), coupled=True)
        self.assertIn("backlash play is undeclared", undeclared.unknown[0])

    def test_the_corners_of_the_play_box(self) -> None:
        halves = {"axial": 0.5, "radial": 0.1, "angular": 0.05, "backlash": 1.0}
        cases = play.corner_cases(joint(), halves)
        self.assertEqual(len(cases), 64)
        self.assertEqual(len({case.label for case in cases}), 64)
        [up] = [c for c in play.corner_cases(joint(), {"axial": 0.5}) if c.label == "axial+"]
        self.assertEqual(up.offset.apply((10.0, 0.0, 0.0)), (10.0, 0.0, 0.5))
        moved = {round(abs(c.offset.apply((0.0, 0.0, 0.0))[i]), 9) for c in play.corner_cases(joint(), {"radial": 0.1})
                 for i in range(3)}
        self.assertEqual(moved, {0.0, 0.1}, "radial extremes move across the axis by half the play")
        chain = play.pose.build_chain([joint()], ["TST-0201", "TST-0202"])
        shifted = play.offset_chain(chain, chain.joint("J-001"), up.offset)
        self.assertEqual(shifted.transforms(shifted.complete({"J-001": 90.0}))["TST-0202"].apply((10.0, 0.0, 0.0)),
                         chain.transforms(chain.complete({"J-001": 90.0}))["TST-0202"].apply((10.0, 0.0, 0.5)),
                         "the offset sits in the parent's frame: the child keeps its motion")


@unittest.skipUnless(shutil.which("uv"), "uv is required to build the play geometry and run play.py")
class PlayGeometryTests(unittest.TestCase):
    """play.py on built geometry: the arm passes 0.5 mm under the bridge at its nominal pose."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls.folder.name).resolve()
        build = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "play_fixture.py"),
                                str(cls.root / "geometry")], capture_output=True, text=True, timeout=900, check=False)
        if build.returncode != 0:
            raise RuntimeError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.folder.cleanup()

    def run_play(self, name: str, play_block: dict, rotating: tuple[str, ...] = ()) -> tuple[int, dict]:
        hw = fx.write_record(self.root / name / "hw", documents(play_block, rotating=rotating))
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(SCRIPT), "--hw", str(hw), "--parts-dir",
                               str(self.root / "geometry" / "parts"), "--steps", "5"],
                              capture_output=True, text=True, timeout=1800, check=False)
        self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        finding, outcome = validator("finding"), validator("obligation_outcome")
        for item in result["findings"]:
            self.assertEqual(list(finding.iter_errors(item)), [], item["id"])
        for row in result["outcomes"]:
            self.assertEqual(list(outcome.iter_errors(row)), [], row["obligation"])
        self.assertEqual([o["id"] for o in result["expected"]],
                         ["motion.play@IF-001", *(f"motion.play@{pn}" for pn in rotating)])
        return proc.returncode, result

    def test_play_extremes_turn_a_nominal_pass_into_a_collision(self) -> None:
        code, result = self.run_play("loose", {"axial": mm(2.0), "radial": mm(0.2), "angular": mm(0.1, "deg")})
        self.assertEqual((code, result["result"]), (1, "fail"))
        detail = result["detail"]["interfaces"]["IF-001"]
        self.assertEqual((detail["joint"], detail["moving"]), ("J-001", ["TST-0202"]))
        self.assertEqual(detail["nominal"]["status"], "clear", "the nominal, centred pose passes")
        self.assertEqual(len(detail["cases"]), 32)
        statuses = {case["label"]: case["status"] for case in detail["cases"]}
        self.assertTrue(all(status == "fail" for label, status in statuses.items() if label.startswith("axial+")))
        self.assertTrue(all(status == "clear" for label, status in statuses.items() if label.startswith("axial-")))
        [collision] = [f for f in result["findings"] if f["code"] == "motion.play_collision"]
        self.assertEqual((collision["obligation"], collision["severity"]), ("motion.play@IF-001", "critical"))
        self.assertIn("TST-0203", collision["consequence"])
        self.assertIn("play extreme axial+", collision["consequence"])

    def test_declared_play_within_the_clearance_passes(self) -> None:
        code, result = self.run_play("snug", {"axial": mm(0.4), "radial": mm(0.2), "angular": mm(0.2, "deg")})
        self.assertEqual((code, result["result"], result["findings"]), (0, "pass", []))
        [row] = result["outcomes"]
        self.assertEqual((row["outcome"], row["level"]), ("pass", "simulation_supported"))
        self.assertIn("motion between samples", row["coverage"])
        detail = result["detail"]["interfaces"]["IF-001"]
        self.assertEqual({case["status"] for case in detail["cases"]}, {"clear"})
        closest = min(case["closest"]["distance_mm"] for case in detail["cases"])
        self.assertTrue(0.2 < closest < 0.31, closest)
        self.assertEqual(detail["samples"], {"source": "per_joint_sweep", "steps": 5, "count": 5})

    def test_undeclared_relevant_play_keeps_the_obligation_unknown(self) -> None:
        code, result = self.run_play("radial-only", {"radial": mm(0.2)})
        self.assertEqual((code, result["result"]), (1, "unknown"))
        [row] = result["outcomes"]
        self.assertEqual(row["outcome"], "unknown")
        self.assertIn("undeclared play is unknown, never zero", row["uncertainty"])
        detail = result["detail"]["interfaces"]["IF-001"]
        self.assertEqual(len(detail["cases"]), 4, "the declared radial play is evaluated though the rest is unknown")
        self.assertEqual({f["code"] for f in result["findings"]}, {"motion.play_unknown"})

    def test_a_rotating_part_is_judged_through_its_interfaces(self) -> None:
        code, result = self.run_play("parts", {"axial": mm(0.4), "radial": mm(0.2), "angular": mm(0.2, "deg")},
                                     rotating=("TST-0202", "TST-0203"))
        outcomes = {row["obligation"]: row["outcome"] for row in result["outcomes"]}
        self.assertEqual(outcomes, {"motion.play@IF-001": "pass", "motion.play@TST-0202": "pass",
                                    "motion.play@TST-0203": "unknown"})
        self.assertEqual((code, result["result"]), (1, "unknown"))
        [gap] = result["findings"]
        self.assertEqual((gap["obligation"], gap["subject"]), ("motion.play@TST-0203", {"PN": "TST-0203"}))
        self.assertIn("no interface of it declares that motion", gap["uncertainty"])


def validator(definition: str):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = SKILLS / "cad-record" / "schemas"
    resources = [(c["$id"], Resource.from_contents(c))
                 for c in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(schemas.glob("*.schema.json")))]
    return Draft202012Validator({"$ref": f"https://srobroek.com/cad/schemas/manufacturing.schema.json#/$defs/{definition}"},
                                registry=Registry().with_resources(resources))


if __name__ == "__main__":
    unittest.main()
