#!/usr/bin/env python3
"""drawing_features hook capture, feature-specific fit endpoints, heat-set inserts and locating fits in check.

Run: python3 -m unittest discover -s cad/skills/cad-model/tests -p 'test_drawing_features.py' -v
The capture tests build small two-part projects through `cad_model.py run` in both kernels and need
`uv`; the locator, endpoint and heat-set rule tests import cadmodel directly and need no OCP.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SCRIPTS = HERE.parent / "scripts"
CLI = SCRIPTS / "cad_model.py"

sys.path.insert(0, str(SCRIPTS))
from cadmodel import build, check, runner  # noqa: E402 - stdlib-only imports; OCP loads on first use

ASSEMBLY = """joints:
  - {id: J-001, type: revolute, parent: TST-0001, child: TST-0002, axis: [0, 0, 1], origin: [0, 0, 20],
     min: -90, max: 90, nominal: 90}
"""
ARM = {"length": 60.0, "width": 20.0, "thickness": 5.0, "z0": 20.0, "bore": 6.0, "boss": 4.0}


def locator(**changes: object) -> dict:
    value = {"kind": "bore", "frame": "assembly_zero",
             "patches": [{"support_type": "cylinder", "point_mm": [3.0, 0.0, 10.0], "normal": [-1.0, 0.0, 0.0]}]}
    value.update(changes)
    return value


class FeatureLocatorContract(unittest.TestCase):
    """runner.feature_locators: what a drawing_features result may hold."""

    def test_valid_result_is_copied_as_plain_json(self) -> None:
        result = runner.feature_locators({
            "hub_bore": locator(anchors={"entry": {"point_mm": (0, 0, 20), "direction": [0, 0, -1]}}),
            "bed_face": locator(kind="plane_face", relations={"perpendicular_to": ["hub_bore"]},
                                patches=[{"support_type": "plane", "point_mm": [0, 0, 0], "normal": [0, 0, -1],
                                          "region": "head_side_cheek"}]),
        })
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(result["hub_bore"]["anchors"]["entry"], {"point_mm": [0.0, 0.0, 20.0], "direction": [0.0, 0.0, -1.0]})
        self.assertEqual(result["bed_face"]["patches"][0]["region"], "head_side_cheek")

    def test_invalid_results_are_contract_errors(self) -> None:
        cases = {
            "empty": {},
            "not a mapping": [locator()],
            "camel-case id": {"HubBore": locator()},
            "local frame": {"hub_bore": locator(frame="part_local")},
            "unknown locator key": {"hub_bore": locator(value=5.0)},
            "no patches": {"hub_bore": locator(patches=[])},
            "unknown support": {"hub_bore": locator(patches=[{"support_type": "nurbs", "point_mm": [0, 0, 0], "normal": [0, 0, 1]}])},
            "kernel object as point": {"hub_bore": locator(patches=[{"support_type": "plane", "point_mm": object(), "normal": [0, 0, 1]}])},
            "non-finite point": {"hub_bore": locator(patches=[{"support_type": "plane", "point_mm": [0, float("nan"), 0], "normal": [0, 0, 1]}])},
            "normal not unit": {"hub_bore": locator(patches=[{"support_type": "plane", "point_mm": [0, 0, 0], "normal": [0, 0, 2]}])},
            "relation to unknown feature": {"hub_bore": locator(relations={"coaxial_with": ["journal"]})},
            "relation to itself": {"hub_bore": locator(relations={"coaxial_with": ["hub_bore"]})},
        }
        for name, value in cases.items():
            with self.subTest(name), self.assertRaises(runner.ContractError):
                runner.feature_locators(value)

    def test_manufacture_features_locate_the_different_state_in_its_own_frame(self) -> None:
        flat = runner.feature_locators({"outer_face": locator(frame=runner.MANUFACTURE_FRAME)},
                                       runner.MANUFACTURE_FRAME, "manufacture_features")
        self.assertEqual(flat["outer_face"]["frame"], "manufacture")
        with self.assertRaisesRegex(runner.ContractError, r"manufacture_features\['outer_face'\]\.frame must be 'manufacture'"):
            runner.feature_locators({"outer_face": locator()}, runner.MANUFACTURE_FRAME, "manufacture_features")
        with self.assertRaisesRegex(runner.ContractError, "model_frame 'manufacture' must be one of"):
            runner.feature_locators({"outer_face": locator(frame="manufacture")}, "manufacture")

    def test_manufacture_features_needs_a_different_state(self) -> None:
        hook = lambda params, shape: {}  # noqa: E731
        self.assertIs(runner._manufacture_features_hook(mock.Mock(manufacture_features=hook), "different"), hook)
        for declared in ("same", None):
            with self.subTest(declared=declared), self.assertRaisesRegex(runner.ContractError, "reuses drawing_features"):
                runner._manufacture_features_hook(mock.Mock(manufacture_features=hook), declared)
        with self.assertRaisesRegex(runner.ContractError, "not callable"):
            runner._manufacture_features_hook(mock.Mock(manufacture_features="flat"), "different")


def write_interfaces(hw: Path, limits: str) -> None:
    (hw / "interfaces.yaml").write_text(f"interfaces:\n  - id: IF-001\n    limits:\n{limits}", encoding="utf-8")


class FitEndpointsAndHeatSets(unittest.TestCase):
    """check.declared_press_fits endpoints, check.declared_heat_sets and check.declared_locating_fits; no OCP."""

    def test_feature_endpoints_give_the_part_pair_and_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            write_interfaces(hw, "      press_fit:\n        fit: 5 H7/p6\n"
                                 "        hole: {PN: TST-0002, feature: elbow_bore, region: head_side_cheek}\n"
                                 "        shaft: {PN: TST-0001, feature: press_band}\n")
            declared = check.declared_press_fits(hw)
        self.assertEqual(declared, [{
            "pair": ["TST-0001", "TST-0002"], "fit": "5 H7/p6", "classification": "interference", "interface": "IF-001",
            "source": "interfaces.yaml#/interfaces/0/limits/press_fit",
            "endpoints": {"hole": {"PN": "TST-0002", "feature": "elbow_bore", "region": "head_side_cheek"},
                          "shaft": {"PN": "TST-0001", "feature": "press_band"}}}])

    def test_pair_and_endpoints_together_or_one_endpoint_are_usage_errors(self) -> None:
        cases = {
            "both": "      press_fit: {fit: 5 H7/p6, pair: [TST-0001, TST-0002], hole: {PN: TST-0002, feature: bore}, "
                    "shaft: {PN: TST-0001, feature: band}}\n",
            "hole only": "      press_fit: {fit: 5 H7/p6, hole: {PN: TST-0002, feature: bore}}\n",
            "same part": "      press_fit: {fit: 5 H7/p6, hole: {PN: TST-0002, feature: bore}, shaft: {PN: TST-0002, feature: band}}\n",
            "no feature": "      press_fit: {fit: 5 H7/p6, hole: {PN: TST-0002}, shaft: {PN: TST-0001, feature: band}}\n",
        }
        for name, limits in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                write_interfaces(Path(tmp), limits)
                with self.assertRaises(build.UsageError):
                    check.declared_press_fits(Path(tmp))

    def test_heat_set_entries_are_read_host_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            write_interfaces(hw, "      heat_set:\n"
                                 "        - {pair: [TST-0002, TST-0001], hole_d: 4.0, insert: M3 x 5.7 brass insert,\n"
                                 "           source: 'https://example.com/m3', hole_tol: [0.0, 0.1], depth_min: 6.0}\n")
            declared = check.declared_heat_sets(hw)
            self.assertEqual(check.declared_press_fits(hw), [])
        self.assertEqual(declared, [{"pair": ["TST-0001", "TST-0002"], "host": "TST-0002", "insert": "M3 x 5.7 brass insert",
                                     "hole_d": 4.0, "interface": "IF-001",
                                     "source": "interfaces.yaml#/interfaces/0/limits/heat_set/0"}])

    def test_malformed_heat_set_is_a_usage_error(self) -> None:
        for limits in ("      heat_set: {pair: [TST-0002, TST-0001]}\n",
                       "      heat_set:\n        - {pair: [TST-0002, TST-0002], hole_d: 4.0, insert: M3}\n",
                       "      heat_set:\n        - {pair: [TST-0002, TST-0001], hole_d: 0, insert: M3}\n"):
            with self.subTest(limits), tempfile.TemporaryDirectory() as tmp:
                write_interfaces(Path(tmp), limits)
                with self.assertRaises(build.UsageError):
                    check.declared_heat_sets(Path(tmp))

    def interference(self, measured_mm3: float, heat_sets: list[dict], allowed: set = frozenset()) -> dict:
        geometry = {("TST-0001", "TST-0002"): {"clearance_mm": -0.2 if measured_mm3 else 0.0, "interference_mm3": measured_mm3,
                                               "method": "exact"}}
        with mock.patch.object(check, "load_pose", return_value=mock.Mock(INTERFERENCE_MM3=0.001)):
            return check.interference(geometry, None, set(), set(allowed), (), heat_sets)

    def test_declared_heat_set_allows_overlap_without_allow_interference(self) -> None:
        insert = {"pair": ["TST-0001", "TST-0002"], "host": "TST-0002", "insert": "M3", "hole_d": 4.0,
                  "interface": "IF-001", "source": "interfaces.yaml#/interfaces/0/limits/heat_set/0"}
        result = self.interference(2.5, [insert])
        self.assertEqual((result["status"], result["pairs"][0]["allowed"]), ("pass", True))
        self.assertEqual(result["declared_heat_sets"], [insert])
        self.assertEqual((result["declared_pairs"], result["declared_fit_findings"]), ([], []))
        self.assertEqual(self.interference(0.0, [insert])["status"], "pass", "an insert drawn at its bore size is fine")
        self.assertEqual(self.interference(2.5, [])["status"], "fail", "an undeclared overlap still fails")

    def test_locating_fits_come_from_the_pair_policy_and_only_an_interference_class_allows_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            write_interfaces(hw, "      locating_fit: {fit: 6 H7/p6, pair: [TST-0002, TST-0001]}\n")
            dowel = check.declared_locating_fits(hw)
            write_interfaces(hw, "      locating_fit: {fit: 6 H7/g6, pair: [TST-0002, TST-0001]}\n")
            sliding = check.declared_locating_fits(hw)
            write_interfaces(hw, "      locating_fit: {fit: TST dowel per vendor limits, pair: [TST-0002, TST-0001]}\n")
            vendor = check.declared_locating_fits(hw)
            self.assertEqual(check.declared_press_fits(hw), [], "a locating fit is not a press fit")
        self.assertEqual(dowel, [{"pair": ["TST-0001", "TST-0002"], "fit": "6 H7/p6", "classification": "interference",
                                  "interface": "IF-001", "source": "interfaces.yaml#/interfaces/0/limits/locating_fit"}])
        self.assertEqual((sliding[0]["classification"], vendor[0]["classification"]), ("clearance", "unclassified"))
        geometry = {("TST-0001", "TST-0002"): {"clearance_mm": -0.1, "interference_mm3": 2.0, "method": "exact"}}
        with mock.patch.object(check, "load_pose", return_value=mock.Mock(INTERFERENCE_MM3=0.001)):
            allowed = check.interference(geometry, None, set(), set(), locating_fits=dowel)
            refused = {name: check.interference(geometry, None, set(), set(), locating_fits=fits)["status"]
                       for name, fits in (("clearance", sliding), ("unclassified", vendor))}
            unrealized = check.interference({("TST-0001", "TST-0002"): {"clearance_mm": 0.05, "interference_mm3": 0.0,
                                                                         "method": "exact"}},
                                            None, set(), set(), locating_fits=dowel)
        self.assertEqual((allowed["status"], allowed["pairs"][0]["allowed"], allowed["declared_locating_fits"]), ("pass", True, dowel))
        self.assertEqual(refused, {"clearance": "fail", "unclassified": "fail"})
        self.assertEqual((unrealized["status"], unrealized["declared_fit_findings"][0]["type"]), ("fail", "declared_fit_not_realized"))


def set_model(hw: Path, pn: str, kernel: str, fixture: str, title: str, params: dict | None = None) -> None:
    folder = hw / "parts" / pn
    folder.mkdir(parents=True, exist_ok=True)
    source = (FIXTURES / kernel / fixture).read_text(encoding="utf-8")
    if params is not None:
        source = re.sub(r"^PARAMS = .*$", f"PARAMS = {params!r}", source, count=1, flags=re.M)
    (folder / "model.py").write_text(source, encoding="utf-8")
    (folder / "part.yaml").write_text(f"PN: {pn}\nrev: X1\ntitle: {title}\nmaterial: PETG\nprocess: fdm\nfinish: none\n"
                                      "mass_target: 100\nmake_buy: make\nsupplier: null\n", encoding="utf-8")


def project(root: Path, name: str, base: tuple[str, str], arm_kernel: str) -> Path:
    hw = root / name / "hw"
    hw.mkdir(parents=True)
    (hw / "product.yaml").write_text("id: features\n", encoding="utf-8")
    (hw / "assembly.yaml").write_text(ASSEMBLY, encoding="utf-8")
    set_model(hw, "TST-0001", base[0], base[1], "Base block")
    set_model(hw, "TST-0002", arm_kernel, "arm.py", "Swing arm", ARM)
    return hw


def run(hw: Path, label: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    proc = subprocess.run(["uv", "run", "--quiet", "--script", str(CLI), "run", str(hw), "--label", label],
                          capture_output=True, text=True, check=False)  # no timeout: cold OCP imports take minutes
    return proc, (json.loads(proc.stdout) if proc.stdout.strip() else {})


class DrawingFeatureCapture(unittest.TestCase):
    """`run` writes the hook results into the version's drawing-features.json, hashed in its manifest."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def assert_captured(self, hw: Path, label: str, base_kernel: str) -> dict:
        folder = hw / "versions" / label
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["drawing_features"], "drawing-features.json")
        sidecar = folder / "drawing-features.json"
        self.assertEqual(manifest["files"]["drawing-features.json"], hashlib.sha256(sidecar.read_bytes()).hexdigest())
        latest = json.loads((hw / "versions" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["drawing_features"], f"versions/{label}/drawing-features.json")
        features = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual((features["label"], features["frames"]), (label, {"TST-0001": "assembly_zero"}))
        self.assertEqual(sorted(features["parts"]), ["TST-0001"], "the arm defines no hook")
        base = features["parts"]["TST-0001"]
        self.assertEqual(sorted(base), ["base_bore", "bed_face"], base_kernel)
        bed = base["bed_face"]["patches"]
        self.assertEqual(len(bed), 1)
        self.assertAlmostEqual(bed[0]["point_mm"][2], 0.0, places=6)
        self.assertEqual([round(abs(v), 6) for v in bed[0]["normal"]], [0.0, 0.0, 1.0])
        for patch in base["base_bore"]["patches"]:  # on the Ø6 bore wall, normal radial
            x, y, _ = patch["point_mm"]
            self.assertAlmostEqual((x * x + y * y) ** 0.5, 3.0, places=6)
            self.assertAlmostEqual(patch["normal"][2], 0.0, places=6)
        return base

    def test_hook_results_are_captured_in_both_kernels_and_absent_without_a_hook(self) -> None:
        cq_hw = project(self.root, "cq", ("cadquery", "base.py"), "build123d")
        proc, result = run(cq_hw, "v1")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        manifest = json.loads((cq_hw / "versions" / "v1" / "manifest.json").read_text(encoding="utf-8"))
        latest = json.loads((cq_hw / "versions" / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("drawing_features", manifest)
        self.assertNotIn("drawing_features", latest)
        self.assertFalse((cq_hw / "versions" / "v1" / "drawing-features.json").exists())

        set_model(cq_hw, "TST-0001", "cadquery", "base_features.py", "Base block")
        proc, result = run(cq_hw, "v2")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertEqual(result["status"], "pass")
        cq_base = self.assert_captured(cq_hw, "v2", "cadquery")
        self.assertEqual(cq_base["base_bore"]["relations"], {"perpendicular_to": ["bed_face"]})
        v1 = json.loads((cq_hw / "versions" / "v1" / "metrics.json").read_text(encoding="utf-8"))["parts"]["TST-0001"]
        v2 = json.loads((cq_hw / "versions" / "v2" / "metrics.json").read_text(encoding="utf-8"))["parts"]["TST-0001"]
        self.assertEqual(v1["holes"], v2["holes"], "the hook leaves hole metrics unchanged")
        self.assertAlmostEqual(v1["volume_mm3"], v2["volume_mm3"], places=6)

        bd_hw = project(self.root, "bd", ("build123d", "base_features.py"), "cadquery")
        proc, _ = run(bd_hw, "v1")
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        bd_base = self.assert_captured(bd_hw, "v1", "build123d")
        self.assertEqual(bd_base["base_bore"]["anchors"], {"entry": {"point_mm": [0.0, 0.0, 20.0], "direction": [0.0, 0.0, -1.0]}})

    def test_a_hook_returning_a_kernel_object_fails_the_part(self) -> None:
        hw = project(self.root, "leaky", ("cadquery", "base_leaky_features.py"), "cadquery")
        proc, result = run(hw, "v1")
        self.assertEqual(proc.returncode, 1, proc.stderr[-3000:])
        self.assertEqual([(f["pn"], f["check"], f["type"]) for f in result["findings"]], [("TST-0001", "build", "ContractError")])
        self.assertIn("point_mm must be three finite numbers", result["findings"][0]["message"])
        self.assertFalse((hw / "versions" / "v1").exists())


if __name__ == "__main__":
    unittest.main()
