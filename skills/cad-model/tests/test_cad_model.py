#!/usr/bin/env python3
"""cad-model tests: a two-part project built, checked, and diffed in both kernels, plus check rules.

Run: python3 -m unittest discover -s cad/skills/cad-model/tests -v
Needs `uv`; the first run creates three uv environments (orchestrator, CadQuery, build123d) and
their first OCP imports take minutes. Wall times are printed to stderr. The rule tests import
cadmodel directly and need neither uv nor OCP.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "cad-drawings" / "scripts"))
import freecad_runtime  # noqa: E402
FREECADCMD = freecad_runtime.find_freecadcmd()
FREECAD_PRESENT = FREECADCMD is not None and FREECADCMD.is_file() and os.access(FREECADCMD, os.X_OK)
FIXTURES = HERE / "fixtures"
SCRIPTS = HERE.parent / "scripts"
CLI = SCRIPTS / "cad_model.py"
LIFECYCLE_SCRIPT = HERE.parents[1] / "cad-flow" / "scripts" / "process_lifecycle.py"
BOM_SCRIPT = HERE.parents[1] / "cad-bom" / "scripts" / "bom.py"
KERNELS = ("cadquery", "build123d")
FINDING_KEYS = {"pn", "check", "type", "message", "where"}

sys.path.insert(0, str(SCRIPTS))
from cadmodel import build, check, diff, fonts, manufacture, mesh, occ, orientation, runner  # noqa: E402 - stdlib-only imports; OCP loads on first use

AP242 = "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING"
SCHEMA = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'\s*\)\s*\)")
ASSEMBLY = """joints:
  - {id: J-001, type: revolute, parent: TST-0001, child: TST-0002, axis: [0, 0, 1], origin: [0, 0, 20],
     min: -90, max: 90, nominal: 90}
"""
PARTS = {"TST-0001": ("Base block", "PETG", "base.py"), "TST-0002": ("Swing arm", "Aluminum 6061-T6", "arm.py")}
ARM = {"length": 60.0, "width": 20.0, "thickness": 5.0, "z0": 20.0, "bore": 6.0, "boss": 4.0}
SAFE_CASES = {"SAF-0001": "cut", "SAF-0002": "fuse", "SAF-0003": "intersect",
              "SAF-0004": "intersect_disjoint", "SAF-0005": "invalid_input"}

# Analytic volumes (mm^3). Base: 40 x 40 x 20 minus a 6 mm bore. Arm: (length + 10) x 20 x 5 plate,
# plus an r4 x 5 boss, minus an r3 x 5 bore.
BASE_V = 40 * 40 * 20 - math.pi * 9 * 20
ARM_V1 = 70 * 20 * 5 + math.pi * 16 * 5 - math.pi * 9 * 5
# v3 drops the arm 5 mm into the base; at the nominal 90 deg the overlap is x -10..10, y -10..20,
# z 15..20 minus the shared bore.
OVERLAP_V3 = 20 * 30 * 5 - math.pi * 9 * 5

# The flat-strap project: TST-0003, a 90 deg arc of a 2 x 10 mm TPU strap (inner radius 8) on the base
# top, printed as its flat blank, the arc's developed length at mid-thickness. Its volume is 90 pi mm^3
# in both states. The printer is only 5 mm tall, so the blank (14.1 x 2 x 10 as modelled) must turn
# its 2 mm thickness up: permutation xzy, printer X reversed to keep the rotation proper.
STRAP = "TST-0003"
STRAP_PARAMS = {"inner_radius": 8.0, "thickness": 2.0, "width": 10.0, "angle": 90.0, "z0": 20.0, "flat": "flat"}
STRAP_V = math.pi / 4 * (10 ** 2 - 8 ** 2) * 10
FLAT_LENGTH = math.pi / 2 * 9
FLAT_STATE = ("manufacture: {relationship: different, state: flat, reason: elastic_bending, decision: DR-001,"
              " volume_rel_tolerance: 1.0e-6}\n")
FLAT_PRODUCT = "id: strap\nbuild_volume: {x_mm: 60, y_mm: 60, z_mm: 5, margin_mm: 0}\n"
STRAP_JOINT = ("joints:\n  - {{id: J-001, type: fixed, parent: TST-0001, child: {pn}, axis: [0, 0, 1],"
               " origin: [0, 0, 0], min: 0, max: 0, nominal: 0}}\n")


def assert_vectors(case: unittest.TestCase, got: list[float], want: list[float], places: int) -> None:
    case.assertEqual(len(got), len(want))
    for g, w in zip(got, want):
        case.assertAlmostEqual(g, w, places=places)


def assert_same_holes(case: unittest.TestCase, got: list[dict], want: list[dict], places: int) -> None:
    """Hole lists equal: ids, diameters, and `through` exactly; axis, position, and depth to `places`."""
    case.assertEqual([(h["id"], h["diameter_mm"], h["through"]) for h in got],
                     [(h["id"], h["diameter_mm"], h["through"]) for h in want])
    for g, w in zip(got, want):
        assert_vectors(case, g["axis"], w["axis"], places)
        assert_vectors(case, g["position"], w["position"], places)
        case.assertAlmostEqual(g["depth_mm"], w["depth_mm"], places=places)


def set_model(hw: Path, pn: str, kernel: str, template: str, params: dict, title: str, material: str,
              process: str = "cnc", extra: str = "", make_buy: str = "make") -> None:
    folder = hw / "parts" / pn
    folder.mkdir(parents=True, exist_ok=True)
    source = (FIXTURES / kernel / template).read_text(encoding="utf-8")
    source = re.sub(r"^PARAMS = .*$", f"PARAMS = {params!r}", source, count=1, flags=re.M)
    (folder / "model.py").write_text(source, encoding="utf-8")
    (folder / "part.yaml").write_text(
        f"PN: {pn}\nrev: X1\ntitle: {title}\nmaterial: {material}\nprocess: {process}\nfinish: none\n"
        f"mass_target: 100\nmake_buy: {make_buy}\nsupplier: null\n{extra}", encoding="utf-8")


def two_part_project(root: Path, kernel: str) -> Path:
    hw = root / kernel / "hw"
    hw.mkdir(parents=True)
    (hw / "product.yaml").write_text("id: twoarm\n", encoding="utf-8")
    (hw / "assembly.yaml").write_text(ASSEMBLY, encoding="utf-8")
    set_model(hw, "TST-0001", kernel, "base.py", {"size": 40.0, "height": 20.0, "bore": 6.0}, *PARTS["TST-0001"][:2])
    return hw


def set_strap(hw: Path, kernel: str, flat: str = "flat", manufacture: str = FLAT_STATE, pn: str = STRAP) -> None:
    set_model(hw, pn, kernel, "strap.py", {**STRAP_PARAMS, "flat": flat}, "Strap", "TPU 95A", "fdm", manufacture)


def flat_project(root: Path, kernel: str, name: str = "flat-hw") -> Path:
    """The base block (CNC, made as installed) with the TPU strap fixed on its top (FDM, printed flat)."""
    hw = root / kernel / name
    hw.mkdir(parents=True)
    (hw / "product.yaml").write_text(FLAT_PRODUCT, encoding="utf-8")
    (hw / "assembly.yaml").write_text(STRAP_JOINT.format(pn=STRAP), encoding="utf-8")
    set_model(hw, "TST-0001", kernel, "base.py", {"size": 40.0, "height": 20.0, "bore": 6.0}, *PARTS["TST-0001"][:2])
    set_strap(hw, kernel)
    return hw


def set_arm(hw: Path, kernel: str, template: str = "arm.py", bought: bool = False, **changes: float) -> None:
    """The swing arm, made to its model; `bought` makes it a catalogue part, which has no manufacturing output."""
    params = {**ARM, **changes}
    if template == "arm_invalid.py":
        params = {k: params[k] for k in ("length", "width", "thickness", "z0")}
    record = ("purchased", "procurement: {mode: catalog}\n", "buy") if bought else ("cnc", "", "make")
    set_model(hw, "TST-0002", kernel, template, params, *PARTS["TST-0002"][:2], *record)


def safe_project(root: Path, kernel: str) -> Path:
    hw = root / kernel / "safe-hw"
    hw.mkdir(parents=True)
    (hw / "product.yaml").write_text("id: safe\n", encoding="utf-8")
    joints = "".join(f"  - {{id: J-00{i}, type: fixed, parent: SAF-0001, child: {pn}, axis: [0, 0, 1], origin: [0, 0, 0],"
                     f" min: 0, max: 0, nominal: 0}}\n" for i, pn in enumerate(list(SAFE_CASES)[1:], start=1))
    (hw / "assembly.yaml").write_text("joints:\n" + joints, encoding="utf-8")
    for pn, case in SAFE_CASES.items():
        set_model(hw, pn, kernel, "safe_case.py", {"case": case}, f"Safe edit {case}", "PETG")
    return hw


def cli(*args: str) -> tuple[subprocess.CompletedProcess[str], dict, float]:
    """cad_model.py with `args`; a check gets a pair cache of its own unless `args` name one."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as cache:
        if args[0] == "check" and not {"--cache", "--no-cache"} & set(args):
            args = (*args, "--cache", cache)
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(CLI), *args],
                              capture_output=True, text=True, check=False)  # no timeout: cold OCP imports take minutes
    wall = time.perf_counter() - started
    print(f"[cad-model wall] {wall:7.1f} s  {' '.join(args[:3])}", file=sys.stderr)
    return proc, (json.loads(proc.stdout) if proc.stdout.strip() else {}), wall


def kernel_sequence(root: Path, kernel: str) -> dict:
    """Build v1..v4 of the two-part project, the safe-edit project and the flat-strap project in one kernel.

    v4 is tried first with the arm made to an invalid model: that run is refused and nothing is
    sealed. v4 is then built with the same invalid arm bought from a catalogue, for `check` to catch."""
    hw = two_part_project(root, kernel)
    runs = {}
    for label, arm in (("v1", {}), ("v2", {"length": 80.0}), ("v3", {"z0": 15.0})):
        set_arm(hw, kernel, **arm)
        runs[label] = cli("run", str(hw), "--label", label)
        runs[f"{label}-model"] = (hw / "parts" / "TST-0002" / "model.py").read_text(encoding="utf-8")
    set_arm(hw, kernel, "arm_invalid.py")
    runs["v4-refused"] = cli("run", str(hw), "--label", "v4")
    runs["v4-refused-state"] = {
        "latest": json.loads((hw / "versions" / "latest.json").read_text(encoding="utf-8"))["label"],
        "versions": sorted(p.name for p in (hw / "versions").iterdir())}
    set_arm(hw, kernel, "arm_invalid.py", bought=True)
    runs["v4"] = cli("run", str(hw), "--label", "v4")
    runs["v4-model"] = (hw / "parts" / "TST-0002" / "model.py").read_text(encoding="utf-8")
    runs["checks"] = {label: cli("check", label, "--hw", str(hw)) for label in ("v1", "v2", "v3", "v4")}
    runs["diff"] = cli("diff", "v1", "v2", "--hw", str(hw))
    runs["safe"] = cli("run", str(safe_project(root, kernel)), "--label", "v1")
    runs["hw"] = hw
    runs["flat-hw"] = flat_project(root, kernel)
    runs["flat"] = cli("run", str(runs["flat-hw"]), "--label", "v1")
    runs["flat-check"] = cli("check", "v1", "--hw", str(runs["flat-hw"]))
    return runs


_SHARED: dict = {}


def all_runs() -> dict:
    """Both kernels' sequences, built once per test process and concurrently."""
    if not _SHARED:
        _SHARED["tmp"] = tempfile.TemporaryDirectory()
        root = Path(_SHARED["tmp"].name)
        with ThreadPoolExecutor(max_workers=len(KERNELS)) as pool:
            futures = {k: pool.submit(kernel_sequence, root, k) for k in KERNELS}
            _SHARED["runs"] = {k: f.result() for k, f in futures.items()}
    return _SHARED["runs"]




class DeterministicTextTests(unittest.TestCase):
    """The runner requires explicit model font paths and injects the verified shared font."""

    def test_runner_refuses_text_without_font_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model.py"
            model.write_text('KERNEL = "cadquery"\nPARAMS = {}\n'
                             'def build(params):\n    return cq.Workplane("XY").text("LMP", 8, -0.6)\n',
                             encoding="utf-8")
            module = runner._load_model("TST-0001", model)
            with self.assertRaisesRegex(runner.ContractError, r"text\(\) must pass an explicit font file path"):
                runner._validate_text_fonts(module)

    def test_runner_accepts_explicit_cadquery_and_build123d_font_paths(self) -> None:
        calls = (("cadquery", 'cq.Workplane("XY").text("LMP", 8, -0.6, fontPath="font.ttf")'),
                 ("build123d", 'bd.Text("LMP", 8, font_path="font.ttf")'))
        for kernel, source in calls:
            with self.subTest(kernel=kernel), tempfile.TemporaryDirectory() as tmp:
                model = Path(tmp) / "model.py"
                model.write_text(f"KERNEL = {kernel!r}\nPARAMS = {{}}\n{source}\n", encoding="utf-8")
                module = type("Model", (), {"__file__": str(model)})()
                runner._validate_text_fonts(module)

    @unittest.skipUnless(FREECAD_PRESENT, "verified bundled Osifont requires FreeCAD 1.1.4 freecadcmd")
    def test_verified_osifont_path_matches_the_runner_contract(self) -> None:
        font = Path(fonts.osifont_path())
        self.assertEqual(hashlib.sha256(font.read_bytes()).hexdigest(), fonts.OSIFONT_SHA256)

    def test_osifont_resolver_checks_the_expected_font_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = root / "bin" / "freecadcmd"
            font = root / "share" / fonts.OSIFONT_RELATIVE_PATH
            command.parent.mkdir(parents=True)
            command.write_text("", encoding="utf-8")
            font.parent.mkdir(parents=True)
            font.write_bytes(b"verified osifont test bytes")
            digest = hashlib.sha256(font.read_bytes()).hexdigest()
            with mock.patch.object(fonts, "_freecadcmd", return_value=command), \
                 mock.patch.object(fonts, "OSIFONT_SHA256", digest):
                resolved = Path(fonts._osifont_for(command))
            self.assertEqual(resolved, font.resolve())


    def test_freecad_lookup_uses_the_preflight_candidate_order(self) -> None:
        with mock.patch.dict(os.environ, {"FREECADCMD": "/selected/freecadcmd"}):
            self.assertEqual(fonts._freecadcmd(), Path("/selected/freecadcmd"))

    def test_osifont_resolver_rejects_unexpected_font_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = root / "bin" / "freecadcmd"
            font = root / "share" / fonts.OSIFONT_RELATIVE_PATH
            command.parent.mkdir(parents=True)
            command.write_text("", encoding="utf-8")
            font.parent.mkdir(parents=True)
            font.write_bytes(b"unexpected font bytes")
            with mock.patch.object(fonts, "OSIFONT_SHA256", fonts.OSIFONT_SHA256):
                with self.assertRaisesRegex(RuntimeError, "unsupported TechDraw Osifont"):
                    fonts._osifont_for(command)

    def test_missing_freecad_font_fails_instead_of_using_system_font(self) -> None:
        with mock.patch.object(fonts, "_freecadcmd", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "cannot find the preflight-selected FreeCAD"):
                fonts.osifont_path.__wrapped__()

    @unittest.skipUnless(FREECAD_PRESENT, "font geometry tests require FreeCAD 1.1.4 bundled Osifont")
    def test_text_fixtures_build_identically_in_both_kernels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:  # run_kernels writes each part's STEP to its "step" path
            requests = {kernel: [{"pn": f"TXT-{i:04d}", "name": "Text fixture",
                                 "model": str(FIXTURES / kernel / "text.py"), "step": str(Path(tmp) / f"{kernel}.step")}]
                        for i, kernel in enumerate(KERNELS, start=1)}
            results = build.run_kernels(requests)
        shapes = {kernel: next(iter(result["parts"].values())) for kernel, result in results.items()}
        for kernel, part in shapes.items():
            self.assertTrue(part["ok"], part)
        volumes = {kernel: part["kernel_volume_mm3"] for kernel, part in shapes.items()}
        self.assertLess(abs(volumes["cadquery"] - volumes["build123d"]) / volumes["cadquery"], 1e-7)

def tearDownModule() -> None:  # noqa: N802 - unittest hook name
    if "tmp" in _SHARED:
        _SHARED["tmp"].cleanup()

class KernelChecks:
    """The acceptance cases, run once per kernel by the subclasses below."""
    kernel = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = all_runs()[cls.kernel]

    def test_versions_build_with_the_record_layout(self) -> None:
        hw = self.runs["hw"]
        for label in ("v1", "v2", "v3", "v4"):
            proc, result, _ = self.runs[label]
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
            self.assertEqual(result["status"], "pass")
            folder = hw / "versions" / label
            for name in ("assembly.step", "assembly.glb", "metrics.json", "manifest.json", "part-map.json",
                         "parts/TST-0001.step", "parts/TST-0002.step"):
                self.assertTrue((folder / name).is_file(), f"{label} {name}")
            self.assertEqual((folder / "assembly.glb").read_bytes()[:4], b"glTF")
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["parts"]["TST-0002"]["kernel"], self.kernel)
            self.assertIn(self.kernel, manifest["tools"][self.kernel])
            self.assertEqual(manifest["pose"]["values"], {"J-001": 90.0})
            self.assertEqual(manifest["joint_pairs"], [{"pair": ["TST-0001", "TST-0002"], "joint": "J-001",
                                                        "type": "revolute"}])
        sources = {json.loads((hw / "versions" / v / "manifest.json").read_text())["parts"]["TST-0002"]["source_sha256"]
                   for v in ("v1", "v2", "v3", "v4")}
        self.assertEqual(len(sources), 4, "each arm edit has its own source hash")
        v2 = json.loads((hw / "versions" / "v2" / "manifest.json").read_text())
        self.assertEqual(v2["parts"]["TST-0002"]["params"]["length"], 80.0)
        latest = json.loads((hw / "versions" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["label"], "v4")
        self.assertEqual(latest["parts"]["TST-0002"], "versions/v4/parts/TST-0002.step")
        for key in ("assembly_step", "glb", "metrics", "part_map", "manifest"):
            self.assertTrue((hw / latest[key]).is_file(), key)
        self.assertFalse(list((hw / "parts").rglob("__pycache__")), "runner must not write bytecode into hw/")

    def test_existing_label_is_refused(self) -> None:
        proc, _, _ = cli("run", str(self.runs["hw"]), "--label", "v1")
        self.assertEqual(proc.returncode, 2, proc.stderr[-2000:])
        self.assertIn("immutable", proc.stderr)

    def test_explicit_kernel_must_match_every_part(self) -> None:
        other = next(k for k in KERNELS if k != self.kernel)
        proc, result, _ = cli("run", str(self.runs["hw"]), "--label", "v9", "--kernel", other)
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertEqual({(f["pn"], f["check"]) for f in result["findings"]}, {("TST-0001", "kernel"), ("TST-0002", "kernel")})
        self.assertTrue(all(f.keys() == FINDING_KEYS for f in result["findings"]), result["findings"])
        self.assertFalse((self.runs["hw"] / "versions" / "v9").exists())

    def test_metrics_volumes_and_mass(self) -> None:
        metrics = json.loads((self.runs["hw"] / "versions" / "v1" / "metrics.json").read_text())["parts"]
        self.assertAlmostEqual(metrics["TST-0001"]["volume_mm3"], BASE_V, places=4)
        self.assertAlmostEqual(metrics["TST-0002"]["volume_mm3"], ARM_V1, places=4)
        self.assertAlmostEqual(metrics["TST-0001"]["mass_g"], BASE_V * 1.27 / 1000, places=6)
        self.assertAlmostEqual(metrics["TST-0002"]["mass_g"], ARM_V1 * 2.70 / 1000, places=6)
        self.assertEqual(metrics["TST-0002"]["solids"], 1)
        for got, want in zip(metrics["TST-0002"]["bbox_mm"]["min"], [-10.0, -10.0, 20.0]):
            self.assertAlmostEqual(got, want, places=6)
        # Both through bores (Ø6 x 20 in the base, Ø6 x 5 in the arm); the arm's convex boss is not a hole.
        for pn, top, depth in (("TST-0001", 20.0, 20.0), ("TST-0002", 25.0, 5.0)):
            found = metrics[pn]["holes"]
            self.assertEqual([(h["id"], h["diameter_mm"], h["axis"], h["through"]) for h in found],
                             [("H1", 6.0, [0.0, 0.0, -1.0], True)], pn)
            assert_vectors(self, found[0]["position"], [0.0, 0.0, top], places=6)
            self.assertAlmostEqual(found[0]["depth_mm"], depth, places=6)

    def test_ap242_header_and_product_names_read_back(self) -> None:
        folder = self.runs["hw"] / "versions" / "v1"
        for path in (folder / "assembly.step", folder / "parts" / "TST-0001.step", folder / "parts" / "TST-0002.step"):
            match = SCHEMA.search(path.read_text(encoding="utf-8")[:3000])
            self.assertTrue(match and match.group(1).startswith(AP242), path)
        assembly = (folder / "assembly.step").read_text(encoding="utf-8")
        for name in ("TST-0001 Base block", "TST-0002 Swing arm", "twoarm v1"):
            self.assertIn(f"PRODUCT('{name}'", assembly)
        readback = self.runs["checks"]["v1"][1]["checks"]["step_readback"]
        self.assertEqual(readback["status"], "pass", json.dumps(readback, indent=1))
        arm = readback["parts"]["TST-0002"]
        self.assertEqual((arm["part_step_name"], arm["assembly_name"]), ("TST-0002 Swing arm", "TST-0002 Swing arm"))
        self.assertLessEqual(arm["part_step_rel_error"], 1e-6)
        self.assertLessEqual(arm["assembly_rel_error"], 1e-6)
        part_map = json.loads((folder / "part-map.json").read_text())["parts"]
        self.assertEqual(part_map["TST-0002"]["nodes"], ["TST-0002 Swing arm"])
        self.assertEqual(part_map["TST-0002"]["meshes"], ["TST-0002 Swing arm"])

    def test_check_reads_declarations_from_the_version_snapshot_not_hw(self) -> None:
        hw = self.runs["hw"]
        template = HERE.parents[2] / "examples" / "lamp-arm" / "hw" / "interfaces.yaml"
        original = (hw / "interfaces.yaml").read_bytes() if (hw / "interfaces.yaml").is_file() else None
        shutil.copy(template, hw / "interfaces.yaml")  # edited after v1 was built: not v1's declarations
        try:
            proc, result, _ = cli("check", "v1", "--hw", str(hw))
        finally:
            if original is None:
                (hw / "interfaces.yaml").unlink()
            else:
                (hw / "interfaces.yaml").write_bytes(original)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual((result["checks"]["interference"]["declared_pairs"], result["checks"]["interference"]["unknown_allowed"]),
                         ([], []))

    def test_clean_versions_pass_check(self) -> None:
        for label in ("v1", "v2"):
            proc, result, _ = self.runs["checks"][label]
            self.assertEqual(proc.returncode, 0, json.dumps(result.get("checks"), indent=1)[:4000] + proc.stderr[-2000:])
            self.assertEqual((result["status"], result["blocking_failures"]), ("pass", []))
        checks = self.runs["checks"]["v1"][1]["checks"]
        self.assertEqual(checks["connection"]["parts"]["TST-0002"]["status"], "touching")
        self.assertEqual(checks["interference"]["joint_pairs"][0]["pair"], ["TST-0001", "TST-0002"])
        self.assertTrue(all(p["watertight"] for p in checks["mesh_watertight"]["parts"].values()))
        self.assertEqual(self.runs["checks"]["v2"][1]["checks"]["solid_structure"]["parts"]["TST-0002"]["baseline"], "v1")

    def test_check_fails_on_deliberate_overlap(self) -> None:
        proc, result, _ = self.runs["checks"]["v3"]
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertEqual(result["blocking_failures"], ["interference"])
        hit = result["checks"]["interference"]["joint_pairs"][0]
        self.assertTrue(hit["interferes"])
        self.assertLess(abs(hit["interference_mm3"] - OVERLAP_V3) / OVERLAP_V3, 1e-6)

    def test_check_fails_on_invalid_shape(self) -> None:
        proc, result, _ = self.runs["checks"]["v4"]
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertIn("brep_validity", result["blocking_failures"])
        validity = result["checks"]["brep_validity"]["parts"]
        self.assertFalse(validity["TST-0002"]["valid"])
        self.assertIn("NotClosed", validity["TST-0002"]["kernel_errors"])
        self.assertTrue(validity["TST-0001"]["valid"])
        self.assertFalse(result["checks"]["mesh_watertight"]["parts"]["TST-0002"]["watertight"])

    def test_diff_reports_the_arm_extension(self) -> None:
        proc, result, _ = self.runs["diff"]
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual((result["added"], result["removed"], result["changed"], result["error"]),
                         ([], [], ["TST-0002"], []))
        arm, base = result["parts"]["TST-0002"], result["parts"]["TST-0001"]
        self.assertAlmostEqual(arm["volume_delta_mm3"], 20 * 20 * 5, places=4)
        for got, want in zip(arm["bbox_delta_mm"]["size"], [20.0, 0.0, 0.0]):
            self.assertAlmostEqual(got, want, places=6)
        boss = math.pi * 16 * 5  # the boss moves 20 mm with the arm end
        self.assertAlmostEqual(arm["a_only_mm3"], boss, places=3)
        self.assertAlmostEqual(arm["b_only_mm3"], 2000 + boss, places=3)
        self.assertAlmostEqual(arm["shared_mm3"], ARM_V1 - boss, places=3)
        self.assertEqual(base["status"], "unchanged")
        self.assertAlmostEqual(base["shared_mm3"], BASE_V, places=4)
        made = result["manufacturing"]
        self.assertEqual((made["changed"], made["parts"]["TST-0002"]["changed"]), (["TST-0002"], ["geometry"]))
        self.assertEqual(made["parts"]["TST-0001"]["status"], "unchanged")

    def test_safe_edits_reject_impossible_results(self) -> None:
        expected = {"SAF-0001": "safe_cut rejected an impossible result",
                    "SAF-0002": "safe_fuse rejected an impossible result",
                    "SAF-0003": "safe_intersect rejected an impossible result",
                    "SAF-0004": "safe_intersect rejected an empty result",
                    "SAF-0005": "safe_cut source is not a valid B-rep"}
        proc, result, _ = self.runs["safe"]
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        found = {f["pn"]: f for f in result["findings"]}
        self.assertEqual(sorted(found), sorted(expected))
        for pn, text in expected.items():
            self.assertEqual(found[pn].keys(), FINDING_KEYS)
            self.assertEqual(found[pn]["type"], "SafeEditError", found[pn])
            self.assertIn(text, found[pn]["message"])
            self.assertTrue(found[pn]["where"].startswith("model.py:"), found[pn])
        self.assertFalse((self.runs["hw"].parent / "safe-hw" / "versions" / "v1").exists())

    def test_an_invalid_made_part_blocks_the_version(self) -> None:
        proc, result, _ = self.runs["v4-refused"]
        self.assertEqual(proc.returncode, 1, proc.stderr[-3000:])
        self.assertEqual([(f["pn"], f["check"], f["type"]) for f in result["findings"]],
                         [("TST-0002", "manufacture", "ManufactureError")])
        self.assertEqual(result["findings"][0].keys(), FINDING_KEYS)
        self.assertIn("native: valid", result["findings"][0]["message"])
        self.assertIn("native", result["manufacturing"]["TST-0002"]["blocking_failures"])
        # nothing sealed, no partial folder left, latest still names v3
        self.assertEqual(self.runs["v4-refused-state"], {"latest": "v3", "versions": ["latest.json", "v1", "v2", "v3"]})

    def test_versions_snapshot_the_record_they_were_built_from(self) -> None:
        hw = self.runs["hw"]
        v2 = hw / "versions" / "v2"
        manifest = json.loads((v2 / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["record"]), ["record/assembly.yaml", "record/parts/TST-0001/part.yaml",
                                                      "record/parts/TST-0002/part.yaml", "record/product.yaml"])
        for name, sha in manifest["record"].items():
            self.assertEqual(manifest["files"][name], sha)
        # v2's arm was made; the current record buys it (v4), and the snapshot keeps the old state
        self.assertIn("make_buy: make", (v2 / "record" / "parts" / "TST-0002" / "part.yaml").read_text(encoding="utf-8"))
        self.assertIn("make_buy: buy", (hw / "parts" / "TST-0002" / "part.yaml").read_text(encoding="utf-8"))
        v4 = json.loads((hw / "versions" / "v4" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(v4["manufacturing"]), ["TST-0001"])  # a catalogue buy has no manufacturing output
        self.assertEqual(self.runs["checks"]["v1"][1]["checks"]["artifact_integrity"]["status"], "pass")

    def test_the_flat_strap_is_built_in_its_manufacturing_state(self) -> None:
        proc, result, _ = self.runs["flat"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        folder = self.runs["flat-hw"] / "versions" / "v1"
        made = folder / "manufacturing"
        self.assertEqual(sorted(p.name for p in (made / STRAP).iterdir()), ["manufacturing.json", "part.step", "part.stl"])
        self.assertEqual(sorted(p.name for p in (made / "TST-0001").iterdir()), ["manufacturing.json", "part.step"])
        stl = (made / STRAP / "part.stl").read_bytes()
        self.assertEqual(len(stl), 84 + 50 * struct.unpack_from("<I", stl, 80)[0], "binary STL")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        strap, base = manifest["manufacturing"][STRAP], manifest["manufacturing"]["TST-0001"]
        self.assertEqual((strap["PN"], strap["rev"], strap["material"], strap["product"]),
                         (STRAP, "X1", "TPU 95A", f"{STRAP} Strap"))
        self.assertEqual(strap["state"], {"relationship": "different", "name": "flat", "reason": "elastic_bending",
                                          "decision": "DR-001", "source": "build_manufacture"})
        self.assertAlmostEqual(strap["native"]["kernel_volume_mm3"], STRAP_V, places=6)
        # the base block has no hook: its installed definition is reused, not built again
        self.assertEqual((base["state"]["relationship"], base["state"]["source"]), ("same", "installed definition"))
        self.assertEqual(base["native"]["kernel_volume_mm3"], base["installed"]["kernel_volume_mm3"])
        self.assertEqual(base["placement"]["transform"], [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]])
        placement = strap["placement"]
        self.assertEqual((placement["method"], placement["orientation"], placement["determinant"]),
                         ("axis_aligned", "xzy", 1.0))
        self.assertEqual([row[:3] for row in placement["transform"]], [[-1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]])
        for name, sha in strap["files"].items():
            self.assertEqual(hashlib.sha256((made / STRAP / name).read_bytes()).hexdigest(), sha, name)
            self.assertEqual(manifest["files"][f"manufacturing/{STRAP}/{name}"], sha)

        proc, checked, _ = self.runs["flat-check"]
        section = checked.get("checks", {}).get("manufacturing")
        self.assertEqual(proc.returncode, 0, json.dumps(section, indent=1)[:4000] + proc.stderr[-2000:])
        self.assertEqual((section["status"], section["complete"], section["blocking_failures"]), ("pass", True, []))
        rows = section["parts"][STRAP]["checks"]
        self.assertEqual({name: row["status"] for name, row in rows.items()},
                         dict.fromkeys(("outputs", "record", "native", "identity", "step", "mesh", "placement",
                                        "components"), "pass"))
        self.assertEqual((rows["step"]["units"], rows["step"]["product"], rows["step"]["solids"]), (["mm"], f"{STRAP} Strap", 1))
        self.assertLessEqual(rows["step"]["rel_error"], 1e-6)
        self.assertAlmostEqual(rows["identity"]["allowed_mm3"], STRAP_V * 1e-6, places=10)
        self.assertLessEqual(rows["identity"]["difference_mm3"], rows["identity"]["allowed_mm3"])
        self.assertEqual((rows["mesh"]["watertight"], rows["mesh"]["winding_consistent"], rows["mesh"]["shells"]),
                         (True, True, 1))
        low, high = rows["placement"]["bbox_mm"]["min"], rows["placement"]["bbox_mm"]["max"]
        assert_vectors(self, [high[i] - low[i] for i in range(3)], [FLAT_LENGTH, 10.0, 2.0], places=6)
        assert_vectors(self, [(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]], [30.0, 30.0, 0.0], places=6)
        self.assertNotIn("mesh", section["parts"]["TST-0001"]["checks"])
        self.assertEqual(checked["checks"]["build_volume"]["parts"][STRAP]["status"], "pass")
        self.assertEqual(checked["checks"]["artifact_integrity"], {"status": "pass", "blocking": True, "problems": []})

    def test_only_the_changed_part_is_built_and_unchanged_outputs_are_stored_once(self) -> None:
        """v2-v4 each change only the arm: the base is reused byte for byte, as hard links to one stored
        copy, and each version adds new inodes only for the arm's outputs; every version still verifies."""
        hw = self.runs["hw"]
        self.assertEqual(self.runs["v1"][1]["cache"], {"parts": {"built": 2, "reused": 0},
                                                     "manufacturing": {"built": 2, "reused": 0}})
        for label in ("v2", "v3", "v4"):
            with self.subTest(label=label):
                result = self.runs[label][1]
                self.assertEqual(result["cache"]["parts"], {"built": 1, "reused": 1})
                self.assertEqual({pn: part["build"] for pn, part in result["parts"].items()},
                                 {"TST-0001": "reused", "TST-0002": "built"})
                self.assertEqual(result["manufacturing"]["TST-0001"]["build"], "reused")
        versions = [hw / "versions" / label for label in ("v1", "v2", "v3", "v4")]
        for rel in ("parts/TST-0001.step", "manufacturing/TST-0001/part.step",
                    "manufacturing/TST-0001/manufacturing.json"):
            for folder in versions[1:]:
                with self.subTest(rel=rel, version=folder.name):
                    self.assertEqual((folder / rel).read_bytes(), (versions[0] / rel).read_bytes())
                    self.assertTrue(os.path.samefile(folder / rel, versions[0] / rel))

        def inodes(folder: Path) -> dict[int, str]:
            return {p.stat().st_ino: p.relative_to(folder).as_posix()
                    for sub in ("parts", "manufacturing") for p in (folder / sub).rglob("*") if p.is_file()}
        added = {inode: rel for inode, rel in inodes(versions[1]).items() if inode not in inodes(versions[0])}
        self.assertEqual(sorted(added.values()), ["manufacturing/TST-0002/manufacturing.json",
                                                  "manufacturing/TST-0002/part.step", "parts/TST-0002.step"])
        layout = build.record.load_layout()
        for folder in versions:
            self.assertEqual(layout.verify(folder), [], folder.name)
        store = hw / ".cad" / "parts"
        base = versions[0] / "parts" / "TST-0001.step"
        self.assertTrue(os.path.samefile(base, store / build.store.file_sha(base)))
        manifest = json.loads((versions[0] / "manifest.json").read_text(encoding="utf-8"))
        for rel in ("parts/TST-0001.step", "parts/TST-0002.step", "manufacturing/TST-0002/part.step"):
            with self.subTest(read_only=rel):  # TST-0002 was rebuilt in v2-v4; v1's bytes and mode stay
                self.assertEqual((versions[0] / rel).stat().st_mode & 0o777, 0o444)
                self.assertEqual(build.store.file_sha(versions[0] / rel), manifest["files"][rel])
        self.assertEqual((store / ".gitignore").read_text(encoding="utf-8"), "*\n")


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class CadQueryTests(KernelChecks, unittest.TestCase):
    kernel = "cadquery"


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class Build123dTests(KernelChecks, unittest.TestCase):
    kernel = "build123d"


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class CrossKernelTests(unittest.TestCase):
    def test_kernels_produce_identical_json(self) -> None:
        runs = all_runs()
        a, b = (json.loads((runs[k]["hw"] / "versions" / "v1" / "metrics.json").read_text()) for k in KERNELS)
        self.assertEqual(a.keys(), b.keys())
        for pn in a["parts"]:
            self.assertEqual(a["parts"][pn].keys(), b["parts"][pn].keys())
            self.assertLess(abs(a["parts"][pn]["volume_mm3"] - b["parts"][pn]["volume_mm3"]) / BASE_V, 1e-6)
            assert_same_holes(self, a["parts"][pn]["holes"], b["parts"][pn]["holes"], places=3)
        checks = [runs[k]["checks"]["v1"][1] for k in KERNELS]
        self.assertEqual(checks[0].keys(), checks[1].keys())
        self.assertEqual(checks[0]["checks"].keys(), checks[1]["checks"].keys())
        self.assertEqual(runs[KERNELS[0]]["v1"][1].keys(), runs[KERNELS[1]]["v1"][1].keys())

    def test_kernels_place_the_flat_strap_alike(self) -> None:
        runs = all_runs()
        entries = [json.loads((runs[k]["flat-hw"] / "versions" / "v1" / "manifest.json").read_text())["manufacturing"]
                   for k in KERNELS]
        self.assertEqual(entries[0].keys(), entries[1].keys())
        for pn in entries[0]:
            a, b = entries[0][pn], entries[1][pn]
            self.assertLess(abs(a["native"]["kernel_volume_mm3"] - b["native"]["kernel_volume_mm3"]) / STRAP_V, 1e-6)
            self.assertEqual((a["placement"]["method"], a["placement"]["orientation"]),
                             (b["placement"]["method"], b["placement"]["orientation"]))
            for ra, rb in zip(a["placement"]["transform"], b["placement"]["transform"]):
                assert_vectors(self, ra, rb, places=6)


def version_state(hw: Path) -> dict:
    """What a failed run must leave unchanged: the latest label and the entries of hw/versions/."""
    latest = hw / "versions" / "latest.json"
    return {"latest": json.loads(latest.read_text(encoding="utf-8"))["label"] if latest.is_file() else None,
            "versions": sorted(p.name for p in (hw / "versions").iterdir())}


def tree_hashes(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class ManufacturingStateTests(unittest.TestCase):
    """The flat-strap project (CadQuery only): refused manufacturing declarations and states never seal a
    version, a manufacturing-only change shows in diff, a tampered STL fails check, and a sealed version
    whose latest pointer could not be written is kept and pointed at without a rebuild."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.hw = hw = flat_project(root, "cadquery")
        cls.runs, cls.states = {"v1": cli("run", str(hw), "--label", "v1")}, {}
        base = {"size": 40.0, "height": 20.0, "bore": 6.0}

        def attempt(name: str, extra: tuple[str, ...] = ()) -> None:
            """Run v2 with `extra` parts fixed to the base too, then remove them and their joints again."""
            joints = [STRAP_JOINT.format(pn=pn).replace("J-001", f"J-{i:03d}") for i, pn in enumerate((STRAP, *extra), 1)]
            (hw / "assembly.yaml").write_text("joints:\n" + "".join(j.removeprefix("joints:\n") for j in joints),
                                              encoding="utf-8")
            cls.runs[name] = cli("run", str(hw), "--label", "v2")
            cls.states[name] = version_state(hw)
            (hw / "assembly.yaml").write_text(STRAP_JOINT.format(pn=STRAP), encoding="utf-8")
            for pn in extra:
                shutil.rmtree(hw / "parts" / pn)

        # The hook without a declaration, a declaration without a hook, a hook that is not callable.
        set_strap(hw, "cadquery", manufacture="")
        set_model(hw, "TST-0004", "cadquery", "base.py", base, "Spacer", "PETG", "fdm", FLAT_STATE)
        set_strap(hw, "cadquery", pn="TST-0005")
        with (hw / "parts" / "TST-0005" / "model.py").open("a", encoding="utf-8") as model:
            model.write('\nbuild_manufacture = "flat"\n')
        attempt("contract", ("TST-0004", "TST-0005"))
        # A different state without a volume tolerance, refused before any kernel runs.
        set_strap(hw, "cadquery", manufacture=FLAT_STATE.replace(", volume_rel_tolerance: 1.0e-6", ""))
        attempt("tolerance")
        # A blank 10 % too long, a blank in two pieces, and a blank that is an open shell.
        set_strap(hw, "cadquery", flat="long")
        set_strap(hw, "cadquery", flat="split", pn="TST-0004")
        set_strap(hw, "cadquery", flat="open", pn="TST-0005")
        attempt("identity", ("TST-0004", "TST-0005"))
        # A manufacturing-only change: the declared tolerance; the installed definitions stay identical.
        set_strap(hw, "cadquery", manufacture=FLAT_STATE.replace("1.0e-6", "2.0e-6"))
        cls.runs["v2"] = cli("run", str(hw), "--label", "v2")
        cls.runs["diff"] = cli("diff", "v1", "v2", "--hw", str(hw))
        # One triangle cut from a sealed STL: a crack, and bytes the manifest no longer matches.
        tampered = root / "tampered" / "hw"
        shutil.copytree(hw, tampered)
        stl = tampered / "versions" / "v2" / "manufacturing" / STRAP / "part.stl"
        data = stl.read_bytes()
        stl.chmod(0o644)  # sealed outputs are read-only (0444): tampering takes a deliberate chmod
        stl.write_bytes(data[:80] + struct.pack("<I", struct.unpack_from("<I", data, 80)[0] - 1) + data[84:-50])
        cls.runs["tampered"] = cli("check", "v2", "--hw", str(tampered))
        # The pointer write fails after v3 is sealed: latest.json is a directory.
        (hw / "versions" / "latest.json").unlink()
        (hw / "versions" / "latest.json").mkdir()
        cls.runs["v3"] = cli("run", str(hw), "--label", "v3")
        cls.states["v3"] = version_state(hw)
        cls.v3_files = tree_hashes(hw / "versions" / "v3")
        (hw / "versions" / "latest.json").rmdir()
        cls.runs["stale"] = cli("latest", str(hw), "--label", "v2")
        cls.runs["latest"] = cli("latest", str(hw), "--label", "v3")
        # Each manufacturing-only input on its own, in a copy, so the pointer cases above keep their state.
        cls.rebuilds = rebuilds = root / "rebuilds" / "hw"
        shutil.copytree(hw, rebuilds)
        (rebuilds / "product.yaml").write_text(FLAT_PRODUCT.replace("x_mm: 60", "x_mm: 61"), encoding="utf-8")
        cls.runs["volume"] = cli("run", str(rebuilds), "--label", "v4")
        set_strap(rebuilds, "cadquery", manufacture=FLAT_STATE.replace("1.0e-6", "2.0e-6").replace(
            "}\n", ", mesh: {linear_deflection_mm: 0.1, angular_deflection_rad: 0.2}}\n"))
        cls.runs["deflection"] = cli("run", str(rebuilds), "--label", "v5")
        set_model(rebuilds, "TST-0001", "cadquery", "base.py", base, "Base block", "Aluminum 6061-T6")
        cls.runs["material"] = cli("run", str(rebuilds), "--label", "v6")
        set_model(rebuilds, "TST-0001", "cadquery", "base.py", base, "Base block", "Aluminum 6061-T6", "purchased")
        cls.runs["process"] = cli("run", str(rebuilds), "--label", "v7")
        step = rebuilds / "versions" / "v7" / "parts" / "TST-0001.step"
        (rebuilds / ".cad" / "parts" / build.store.file_sha(step)).unlink()  # as if store gc ran
        cls.runs["missing"] = cli("run", str(rebuilds), "--label", "v8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def findings(self, name: str) -> dict:
        proc, result, _ = self.runs[name]
        self.assertEqual(proc.returncode, 1, proc.stdout[-2000:] + proc.stderr[-3000:])
        self.assertEqual(self.states[name], {"latest": "v1", "versions": ["latest.json", "v1"]}, name)
        self.assertTrue(all(f.keys() == FINDING_KEYS for f in result["findings"]), result["findings"])
        return {f["pn"]: f for f in result["findings"]}

    def test_a_hook_needs_its_declaration_and_a_declaration_its_callable_hook(self) -> None:
        self.assertEqual(self.runs["v1"][0].returncode, 0, self.runs["v1"][0].stderr[-3000:])
        found = self.findings("contract")
        self.assertEqual(sorted(found), [STRAP, "TST-0004", "TST-0005"])
        self.assertTrue(all(f["type"] == "ContractError" for f in found.values()), found)
        self.assertIn("does not declare manufacture.relationship: different", found[STRAP]["message"])
        self.assertIn("defines no build_manufacture", found["TST-0004"]["message"])
        self.assertIn("not callable", found["TST-0005"]["message"])

    def test_a_different_state_without_a_tolerance_is_refused_before_the_kernels(self) -> None:
        found = self.findings("tolerance")
        self.assertEqual((found[STRAP]["check"], found[STRAP]["type"]), ("manufacture", "ContractError"))
        self.assertIn("without a volume tolerance", found[STRAP]["message"])
        self.assertNotIn("runners", self.runs["tolerance"][1])

    def test_volume_drift_solid_count_and_an_invalid_state_block_the_version(self) -> None:
        found = self.findings("identity")
        self.assertEqual(sorted(found), [STRAP, "TST-0004", "TST-0005"])
        self.assertTrue(all((f["check"], f["type"]) == ("manufacture", "ManufactureError") for f in found.values()))
        self.assertIn("identity: volume_within_tolerance", found[STRAP]["message"])
        self.assertIn("same_solids", found["TST-0004"]["message"])
        self.assertIn("native: valid", found["TST-0005"]["message"])
        identity = self.runs["identity"][1]["manufacturing"][STRAP]["checks"]["identity"]
        self.assertAlmostEqual(identity["difference_mm3"], STRAP_V * 0.1, places=6)
        self.assertAlmostEqual(identity["allowed_mm3"], STRAP_V * 1e-6, places=10)

    def test_a_manufacturing_only_change_shows_in_diff(self) -> None:
        self.assertEqual(self.runs["v2"][0].returncode, 0, self.runs["v2"][0].stderr[-3000:])
        proc, result, _ = self.runs["diff"]
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual((result["changed"], result["added"], result["removed"]), ([], [], []))
        self.assertEqual(result["manufacturing"]["changed"], [STRAP])
        self.assertEqual(result["manufacturing"]["parts"][STRAP]["changed"], ["tolerance"])

    def test_a_record_only_change_makes_only_the_outputs_it_changes(self) -> None:
        """v2 changes only the strap's declared tolerance: both definitions and the base's manufacturing
        outputs are reused and no kernel starts; the strap's outputs are made again. v3 changes nothing,
        so every part output is the very file v2 holds, and the manifest still names the kernel."""
        result = self.runs["v2"][1]
        self.assertEqual(result["cache"], {"parts": {"built": 0, "reused": 2},
                                           "manufacturing": {"built": 1, "reused": 1}})
        self.assertEqual({pn: row["build"] for pn, row in result["manufacturing"].items()},
                         {"TST-0001": "reused", STRAP: "built"})
        self.assertNotIn("cadquery_import_s", result["timings_s"])
        v1, v2, v3 = (self.hw / "versions" / label for label in ("v1", "v2", "v3"))
        self.assertTrue(os.path.samefile(v1 / "parts" / f"{STRAP}.step", v2 / "parts" / f"{STRAP}.step"))
        self.assertTrue(os.path.samefile(v1 / "manufacturing" / "TST-0001" / "part.step",
                                         v2 / "manufacturing" / "TST-0001" / "part.step"))
        self.assertFalse(os.path.samefile(v1 / "manufacturing" / STRAP / "manufacturing.json",
                                          v2 / "manufacturing" / STRAP / "manufacturing.json"))
        outputs = [p for sub in ("parts", "manufacturing") for p in sorted((v2 / sub).rglob("*")) if p.is_file()]
        self.assertEqual(len(outputs), 7)
        for path in outputs:
            self.assertTrue(os.path.samefile(path, v3 / path.relative_to(v2)), path.relative_to(v2))
        tools = json.loads((v3 / "manifest.json").read_text(encoding="utf-8"))["tools"]["cadquery"]
        self.assertEqual(tools["cadquery"], "2.8.0")
        self.assertNotIn("import_s", tools)  # the kernel never started

    def test_manufacturing_outputs_follow_their_own_inputs(self) -> None:
        """A printer volume, an STL deflection, a material or a process change remakes exactly the outputs
        it changes; no definition is built again."""
        for name, remade in (("volume", {STRAP}), ("deflection", {STRAP}), ("material", {"TST-0001"}),
                             ("process", {"TST-0001"})):
            with self.subTest(change=name):
                proc, result, _ = self.runs[name]
                self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-3000:])
                self.assertEqual(result["cache"]["parts"], {"built": 0, "reused": 2})
                self.assertEqual({pn for pn, row in result["manufacturing"].items() if row["build"] == "built"},
                                 remade)

    def test_a_part_whose_stored_object_went_is_built_again(self) -> None:
        proc, result, _ = self.runs["missing"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-3000:])
        self.assertEqual({pn: part["build"] for pn, part in result["parts"].items()},
                         {"TST-0001": "built", STRAP: "reused"})
        self.assertEqual(build.record.load_layout().verify(self.rebuilds / "versions" / "v7"), [])

    def test_a_cracked_stl_fails_check(self) -> None:
        proc, result, _ = self.runs["tampered"]
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertIn("manufacturing", result["blocking_failures"])
        self.assertIn("artifact_integrity", result["blocking_failures"])
        row = result["checks"]["manufacturing"]["parts"][STRAP]
        self.assertEqual(row["blocking_failures"], ["mesh", "outputs"])
        self.assertEqual(row["checks"]["outputs"]["changed"], ["part.stl"])
        self.assertIn("watertight", row["checks"]["mesh"]["failed"])
        self.assertEqual(result["checks"]["manufacturing"]["parts"]["TST-0001"]["status"], "pass")

    def test_a_sealed_version_keeps_and_reconciles_a_failed_latest_pointer(self) -> None:
        proc, _, _ = self.runs["v3"]
        self.assertEqual(proc.returncode, 2, proc.stderr[-2000:])
        self.assertIn("v3 is sealed", proc.stderr)
        self.assertIn("cad_model.py latest", proc.stderr)
        self.assertEqual(self.states["v3"]["versions"], ["latest.json", "v1", "v2", "v3"])  # no .latest.json.tmp left
        stale, _, _ = self.runs["stale"]
        self.assertEqual(stale.returncode, 2, stale.stderr[-2000:])
        self.assertIn("not the newest", stale.stderr)
        proc, result, _ = self.runs["latest"]
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(result["verified_files"], len(self.v3_files) - 1)
        latest = json.loads((self.hw / "versions" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual((latest["label"], latest["manufacturing"][STRAP]), ("v3", f"versions/v3/manufacturing/{STRAP}"))
        self.assertEqual(tree_hashes(self.hw / "versions" / "v3"), self.v3_files)


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class AttachedPartTests(unittest.TestCase):
    """An --attach part 1 mm from its parent floats with no movable joint to excuse it (CadQuery only)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hw = two_part_project(Path(cls.tmp.name), "cadquery")
        set_arm(cls.hw, "cadquery")
        # The arm template at z0 -6: a plate z -6..-1 under the base block (z 0..20), a 1 mm gap.
        set_model(cls.hw, "TST-0003", "cadquery", "arm.py", {**ARM, "z0": -6.0}, "Spacer plate", "PETG")
        cls.built = cli("run", str(cls.hw), "--label", "v1", "--attach", "TST-0003=TST-0001")
        cls.checked = cli("check", "v1", "--hw", str(cls.hw))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_attached_part_floating_from_its_parent_fails_connection(self) -> None:
        self.assertEqual(self.built[0].returncode, 0, self.built[0].stderr[-3000:])
        manifest = json.loads((self.hw / "versions" / "v1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn({"pair": ["TST-0001", "TST-0003"], "joint": "attach:TST-0003", "type": "fixed"},
                      manifest["joint_pairs"])
        proc, result, _ = self.checked
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertEqual(result["blocking_failures"], ["connection"])
        rows = result["checks"]["connection"]["parts"]
        spacer = rows["TST-0003"]
        self.assertEqual((spacer["status"], spacer["nearest"], spacer["joint"], spacer["ok"]),
                         ("floating", "TST-0001", None, False))
        self.assertAlmostEqual(spacer["distance_mm"], 1.0, places=6)
        self.assertEqual((rows["TST-0002"]["status"], rows["TST-0002"]["ok"]), ("touching", True))
        attached = result["checks"]["connection"]["fixed_joints"]
        self.assertEqual([(f["joint"], f["ok"]) for f in attached], [("attach:TST-0003", False)])
        self.assertAlmostEqual(attached[0]["distance_mm"], 1.0, places=6)

    def test_model_folder_without_a_pn_name_is_a_contract_finding(self) -> None:
        stray = self.hw / "parts" / "spare_bracket"
        stray.mkdir()
        shutil.copy(self.hw / "parts" / "TST-0001" / "model.py", stray / "model.py")
        try:
            proc, result, _ = cli("run", str(self.hw), "--label", "v2", "--attach", "TST-0003=TST-0001")
        finally:
            shutil.rmtree(stray)
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertEqual([(f["pn"], f["check"], f["type"], f["where"]) for f in result["findings"]],
                         [("spare_bracket", "contract", "ContractError", None)])
        self.assertEqual(result["findings"][0].keys(), FINDING_KEYS)
        self.assertFalse((self.hw / "versions" / "v2").exists())


def process_runs(pid: int) -> bool:
    """Whether process `pid` runs; a zombie waiting to be reaped has ended."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def descendants(pid: int) -> dict[int, str]:
    """Every live descendant of process `pid`: pid -> command line."""
    table = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    children: dict[int, list[tuple[int, str]]] = {}
    for line in table.splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2:
            children.setdefault(int(fields[1]), []).append((int(fields[0]), fields[2] if len(fields) > 2 else ""))
    found, todo = {}, [pid]
    while todo:
        for child, command in children.get(todo.pop(), []):
            found[child] = command
            todo.append(child)
    return found


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class CheckPairCacheTests(unittest.TestCase):
    """check through cad-motion's pair evaluator: base, arm and attached spacer, then a v2 that
    changes only the arm (CadQuery only). The arm sits on the base and the spacer 1 mm below it, so
    the arm and the spacer are 21 mm apart: fast mode clears that pair on its bounds."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hw = two_part_project(Path(cls.tmp.name), "cadquery")
        set_arm(cls.hw, "cadquery")
        set_model(cls.hw, "TST-0003", "cadquery", "arm.py", {**ARM, "z0": -6.0}, "Spacer plate", "PETG")
        cls.built = [cli("run", str(cls.hw), "--label", "v1", "--attach", "TST-0003=TST-0001")]
        set_arm(cls.hw, "cadquery", length=80.0)
        cls.built.append(cli("run", str(cls.hw), "--label", "v2", "--attach", "TST-0003=TST-0001"))
        cache = str(Path(cls.tmp.name) / "cache")
        cls.runs = {name: cli("check", label, "--hw", str(cls.hw), *option) for name, label, option in (
            ("v1 no cache", "v1", ["--no-cache"]), ("v1 cold", "v1", ["--cache", cache]),
            ("v1 warm", "v1", ["--cache", cache]), ("v2 seeded", "v2", ["--cache", cache]),
            ("v2 no cache", "v2", ["--no-cache"]), ("v1 exact", "v1", ["--no-cache", "--exact"]),
            ("v2 changed", "v2", ["--no-cache", "--changed-only"]),
            ("v2 since v1", "v2", ["--no-cache", "--changed-only", "--since-version", "v1"]),
            ("v1 changed", "v1", ["--no-cache", "--changed-only"]),
            ("v2 since v9", "v2", ["--no-cache", "--changed-only", "--since-version", "v9"]))}
        cls.checks = {name: result for name, (_, result, _) in cls.runs.items()}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    @staticmethod
    def without_timings(result: dict) -> dict:
        return {k: v for k, v in result.items() if k != "timings_s"}

    def kept(self, name: str) -> dict:
        return self.without_timings(self.checks[name])

    def counts(self, name: str) -> tuple[int, int]:
        timings = self.checks[name]["timings_s"]
        return timings["kernel_evaluations"], timings["exact_hits"]

    def test_a_cold_and_a_warm_cache_report_what_no_cache_does(self) -> None:
        for proc, result, _ in self.built:
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(self.kept("v1 cold"), self.kept("v1 no cache"))
        self.assertEqual(self.kept("v1 warm"), self.kept("v1 no cache"))
        self.assertEqual((self.counts("v1 cold"), self.counts("v1 warm")), ((2, 0), (0, 2)))
        self.assertEqual(self.checks["v1 cold"]["timings_s"]["cleared"]["box"], 1)

    def test_the_next_version_measures_only_the_pairs_of_its_changed_part(self) -> None:
        self.assertEqual(self.kept("v2 seeded"), self.kept("v2 no cache"))
        self.assertEqual(self.counts("v2 seeded"), (1, 1))

    def test_fast_and_exact_checks_agree(self) -> None:
        fast, exact = self.checks["v1 no cache"], self.checks["v1 exact"]
        self.assertEqual((fast["evaluation"]["mode"], exact["evaluation"]["mode"]), ("fast", "exact"))
        self.assertEqual({k: c["status"] for k, c in fast["checks"].items()},
                         {k: c["status"] for k, c in exact["checks"].items()})
        self.assertEqual(fast["checks"]["connection"], exact["checks"]["connection"])
        self.assertEqual(fast["checks"]["interference"]["min_clearance"], exact["checks"]["interference"]["min_clearance"])
        exact_rows = {tuple(r["pair"]): r for r in exact["checks"]["interference"]["pairs"]}
        for row in fast["checks"]["interference"]["pairs"]:
            other = exact_rows[tuple(row["pair"])]
            with self.subTest(pair=row["pair"], method=row["method"]):
                self.assertEqual(other["method"], "exact")
                if row["method"] == "exact":
                    self.assertEqual((row["clearance_mm"], row["interference_mm3"]),
                                     (other["clearance_mm"], other["interference_mm3"]))
                else:  # a far pair's bounds bracket the exact distance
                    self.assertLessEqual(row["lower_mm"], other["clearance_mm"])
                    self.assertTrue(row["upper_mm"] is None or other["clearance_mm"] <= row["upper_mm"])
                    self.assertEqual((row["clearance_mm"], other["interference_mm3"]), (None, 0.0))
        self.assertEqual(self.counts("v1 exact"), (3, 0))

    def swept(self, label: str, cache: Path) -> dict:
        """fixtures/share_probe.py: the version's pairs at the nominal pose, loaded and placed as the sweep does."""
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "share_probe.py"), str(self.hw), label,
                               str(cache)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return json.loads(proc.stdout)

    def test_the_sweeps_nominal_pose_and_the_check_share_exact_readings(self) -> None:
        check_first, sweep_first = Path(self.tmp.name) / "check-first", Path(self.tmp.name) / "sweep-first"
        checked = cli("check", "v1", "--hw", str(self.hw), "--cache", str(check_first))[1]
        rows = checked["checks"]["interference"]["pairs"] + checked["checks"]["interference"]["joint_pairs"]
        exact = {" ".join(r["pair"]): [r["clearance_mm"], r["interference_mm3"]] for r in rows if r["method"] == "exact"}
        self.assertEqual(len(exact), 2)
        swept = self.swept("v1", check_first)
        self.assertEqual((swept["exact_hits"], swept["kernel_evaluations"]), (2, 1))  # only the cleared pair is new
        self.assertEqual({key: swept["readings"][key] for key in exact}, exact)
        everything = self.swept("v1", sweep_first)
        again = cli("check", "v1", "--hw", str(self.hw), "--cache", str(sweep_first))[1]
        # every pair is a cache hit, the far pair's exact reading too; the verdicts are the check's own
        self.assertEqual((again["timings_s"]["kernel_evaluations"], again["timings_s"]["exact_hits"]), (0, 3))
        self.assertEqual({k: c["status"] for k, c in again["checks"].items()},
                         {k: c["status"] for k, c in checked["checks"].items()})
        again_rows = again["checks"]["interference"]["pairs"] + again["checks"]["interference"]["joint_pairs"]
        self.assertEqual({" ".join(r["pair"]): [r["clearance_mm"], r["interference_mm3"]] for r in again_rows
                          if " ".join(r["pair"]) in exact}, exact)
        self.assertEqual({key: everything["readings"][key] for key in exact}, exact)

    def test_a_changed_only_check_measures_only_the_changed_parts_pairs(self) -> None:
        for name in ("v2 changed", "v2 since v1"):
            with self.subTest(name=name):
                result = self.checks[name]
                scope = result["scope"]
                self.assertEqual((scope["kind"], scope["complete"], scope["baseline"]), ("changed_pairs", False, "v1"))
                self.assertEqual((scope["selected_pairs"], scope["unchanged_pairs"], scope["reasons"]),
                                 (2, 1, {"geometry": 2}))
                self.assertEqual(scope["changes"]["geometry_changed"], ["TST-0002"])
                rows = result["checks"]["interference"]["pairs"] + result["checks"]["interference"]["joint_pairs"]
                self.assertEqual(sorted(tuple(r["pair"]) for r in rows), [("TST-0001", "TST-0002"), ("TST-0002", "TST-0003")])
                self.assertEqual(result["checks"]["interference"]["min_clearance"], None)
                self.assertEqual(result["checks"]["connection"]["status"], "skipped")
                self.assertEqual(self.counts(name), (1, 0))  # the unchanged base-spacer pair is not evaluated
        self.assertEqual(self.checks["v2 changed"]["scope"]["reasons"], self.checks["v2 since v1"]["scope"]["reasons"])

    def test_a_changed_only_check_without_a_baseline_selects_every_pair(self) -> None:
        scope = self.checks["v1 changed"]["scope"]
        self.assertEqual((scope["kind"], scope["complete"], scope["baseline"], scope["selected_pairs"]),
                         ("changed_pairs", False, None, 3))
        self.assertEqual(scope["full_selection"], "no built version before v1: every pair is selected")

    def test_a_baseline_that_is_not_built_is_an_error(self) -> None:
        proc, result, _ = self.runs["v2 since v9"]
        self.assertEqual((proc.returncode, result), (2, {}))
        self.assertIn("v9", proc.stderr)

    def test_a_check_refuses_while_the_same_check_runs_and_runs_beside_another(self) -> None:
        code = (f"import importlib.util, sys, time\nspec = importlib.util.spec_from_file_location('l', {str(LIFECYCLE_SCRIPT)!r})\n"
                "module = importlib.util.module_from_spec(spec)\nsys.modules['l'] = module\nspec.loader.exec_module(module)\n"
                f"module.claim_run({str(self.hw)!r}, 'static', scope='v1 all-pairs')\nprint('claimed', flush=True)\n"
                "time.sleep(600)\n")
        holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "claimed")
        proc, result, _ = cli("check", "v1", "--hw", str(self.hw), "--no-cache")
        self.assertEqual((proc.returncode, result), (2, {}))
        self.assertIn(f"a static run of v1 all-pairs still runs as process {holder.pid}; stop it with "
                      f"`kill -TERM {holder.pid}`", proc.stderr)
        for args in (("v2",), ("v1", "--changed-only")):  # another version, another selection: run side by side
            with self.subTest(args=args):
                proc, result, _ = cli("check", *args, "--hw", str(self.hw), "--no-cache")
                self.assertIn(proc.returncode, (0, 1), proc.stderr[-2000:])
                self.assertEqual(result["label"], args[0])

    def test_killing_a_check_outright_leaves_no_worker_running(self) -> None:
        check = subprocess.Popen(["uv", "run", "--quiet", "--script", str(CLI), "check", "v1", "--hw", str(self.hw),
                                  "--no-cache", "--exact", "--jobs", "2"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(check.wait)
        self.addCleanup(lambda: check.poll() is None and os.killpg(check.pid, signal.SIGKILL))
        deadline, family = time.monotonic() + 600, {}
        while time.monotonic() < deadline and check.poll() is None:
            family = descendants(check.pid)
            if sum("multiprocessing" in command for command in family.values()) >= 2:
                break
            time.sleep(0.05)
        else:
            self.fail(f"no worker processes appeared before the check ended: {family}")
        parent = next(pid for pid, command in family.items() if "cad_model.py" in command and "multiprocessing" not in command)
        os.kill(parent, signal.SIGKILL)  # the check itself: nothing of it can clean up
        for pid, command in family.items():
            deadline = time.monotonic() + 15
            while process_runs(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(process_runs(pid), f"{command} outlived the killed check")


BASE, ARM_PN, PIN = "TST-0001", "TST-0002", "TST-0003"


def joints(*specs: tuple[str, str, str, str]) -> list[dict]:
    """manifest.json `joint_pairs` entries from (joint id, type, parent, child)."""
    return [{"pair": sorted([a, b]), "joint": jid, "type": kind} for jid, kind, a, b in specs]


def gaps(pairs: dict[tuple[str, str], float | None]) -> dict:
    """check's pair geometry from exact clearances; no shared volume, a failed row where the gap is None."""
    return {pair: {"clearance_mm": gap, "interference_mm3": 0.0, "method": "exact"} if gap is not None
            else {"clearance_mm": None, "interference_mm3": None, "method": None, "error": "not measured"}
            for pair, gap in pairs.items()}


INSERT = "TST-0003"
# One part-local insert placed six times around the base block (x/y -20..20, z 0..20), each held to it
# by a fixed joint. The insert is the base template at size 4, height 4, bore 1: a block x/y -2..2,
# z 0..4, bored through along Z. Each entry: translation_mm, rotation (None for none).
INSERTS = {
    "ins-1": ([10, 10, 18], None),    # 2 mm into the base top: the one declared press fit
    "ins-2": ([-10, 10, 18], None),   # the same overlap, with no fit declared
    "ins-3": ([10, -10, 20], None),   # on the base top ...
    "ins-4": ([11, -10, 20], None),   # ... sharing x 9..12 with ins-3
    "ins-5": ([-10, -10, 22], {"axis": [1, 0, 0], "angle_deg": 90}),  # turned about +X: y -14..-10, z 20..24
    "ins-6": ([0, 22, 8], None),      # against the base's +Y side
}
INSERT_V = 4 * 4 * 4 - math.pi * 0.25 * 4
PRESSED_V = 4 * 4 * 2 - math.pi * 0.25 * 2      # an insert 2 mm into the base, less its own bore
STACKED_V = 3 * 4 * 4 - 2 * math.pi * 0.25 * 4  # ins-3 and ins-4 over x 9..12, less both bores


def insert_project(root: Path) -> Path:
    """The base block and the six INSERTS, with a press fit declared on `@ins-1` only (CadQuery)."""
    hw = root / "inserts" / "hw"
    hw.mkdir(parents=True)
    (hw / "product.yaml").write_text("id: inserts\n", encoding="utf-8")
    occurrences = "".join(
        f"  - {{id: {oid}, PN: {INSERT}, placement: {{translation_mm: {move}"
        + (f", rotation: {{axis: {turn['axis']}, angle_deg: {turn['angle_deg']}}}" if turn else "") + "}}\n"
        for oid, (move, turn) in INSERTS.items())
    members = ", ".join(f'"@{oid}"' for oid in INSERTS)
    fixed = "".join(f'  - {{id: J-00{n}, type: fixed, parent: {BASE}, child: "@{oid}", axis: [0, 0, 1], '
                    f"origin: [0, 0, 0], min: 0, max: 0, nominal: 0}}\n" for n, oid in enumerate(INSERTS, start=1))
    (hw / "assembly.yaml").write_text(
        f"occurrences:\n{occurrences}subassemblies:\n  - {{id: frame, title: Base and inserts, parts: [{BASE}], "
        f"occurrences: [{members}]}}\njoints:\n{fixed}", encoding="utf-8")
    (hw / "interfaces.yaml").write_text(
        f'interfaces:\n  - id: IF-001\n    limits:\n      press_fit: {{fit: 5 H7/p6, pair: [{BASE}, "@ins-1"]}}\n',
        encoding="utf-8")
    set_model(hw, BASE, "cadquery", "base.py", {"size": 40.0, "height": 20.0, "bore": 6.0}, *PARTS[BASE][:2])
    set_model(hw, INSERT, "cadquery", "base.py", {"size": 4.0, "height": 4.0, "bore": 1.0}, "Insert", "PETG")
    with (hw / "parts" / INSERT / "part.yaml").open("a", encoding="utf-8") as part:
        part.write("model_frame: part_local\n")
    return hw


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class OccurrenceBuildTests(unittest.TestCase):
    """One part-local PN placed six times: one definition, six placed instances (CadQuery only)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hw = insert_project(Path(cls.tmp.name))
        cls.folder = cls.hw / "versions" / "v1"
        cls.built = cli("run", str(cls.hw), "--label", "v1")
        cls.checked = cli("check", "v1", "--hw", str(cls.hw))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.assertEqual(self.built[0].returncode, 0, self.built[0].stderr[-3000:])

    def version_json(self, name: str) -> dict:
        return json.loads((self.folder / name).read_text(encoding="utf-8"))

    def pair_rows(self) -> dict[tuple[str, ...], dict]:
        interference = self.checked[1]["checks"]["interference"]
        return {tuple(row["pair"]): row for row in interference["pairs"] + interference["joint_pairs"]}

    def test_assembly_step_has_one_definition_and_one_component_per_occurrence(self) -> None:
        readback = self.checked[1]["checks"]["step_readback"]
        self.assertEqual(readback["status"], "pass", json.dumps(readback, indent=1)[:3000])
        self.assertEqual((readback["definitions"], readback["components"], readback["unbound_components"],
                          readback["missing_occurrences"]), (2, 7, [], []))
        self.assertEqual(readback["occurrences"]["ins-5"]["component"], f"{INSERT} Insert [@ins-5]")
        self.assertEqual(self.version_json("manifest.json")["parts"][INSERT]["occurrences"], list(INSERTS))

    def test_check_measures_every_occurrence_pair_including_same_pn_pairs(self) -> None:
        rows = self.pair_rows()
        self.assertEqual(set(rows), set(itertools.combinations(sorted([BASE, *INSERTS]), 2)))  # 21, 15 same-PN
        stacked = rows[("ins-3", "ins-4")]
        self.assertEqual((stacked["refs"], stacked["interferes"], stacked["allowed"], stacked["ok"]),
                         (["@ins-3", "@ins-4"], True, False, False))
        self.assertAlmostEqual(stacked["interference_mm3"], STACKED_V, places=3)

    def test_a_fit_declared_on_one_insert_does_not_excuse_the_other(self) -> None:
        proc, result, _ = self.checked
        rows = self.pair_rows()
        declared, undeclared = rows[(BASE, "ins-1")], rows[(BASE, "ins-2")]
        self.assertEqual((declared["interferes"], declared["allowed"], declared["ok"]), (True, True, True))
        self.assertEqual((undeclared["interferes"], undeclared["allowed"], undeclared["ok"]), (True, False, False))
        for row in (declared, undeclared):
            self.assertAlmostEqual(row["interference_mm3"], PRESSED_V, places=3)
        self.assertEqual([item["pair"] for item in result["checks"]["interference"]["declared_pairs"]],
                         [[BASE, "ins-1"]])
        self.assertEqual(sorted(pair for pair, row in rows.items() if not row["ok"]),
                         [(BASE, "ins-2"), ("ins-3", "ins-4")])
        self.assertEqual((proc.returncode, result["blocking_failures"]), (1, ["interference"]), proc.stderr[-2000:])

    def test_one_glb_definition_is_two_nodes_and_the_second_resolves_to_its_own(self) -> None:
        part_map = self.version_json("part-map.json")
        gltf = mesh.gltf_json(self.folder / "assembly.glb")
        first, second = (part_map["occurrences"][oid] for oid in ("ins-1", "ins-2"))
        self.assertEqual((first["nodes"], second["nodes"]),
                         ([f"{INSERT} Insert [@ins-1]"], [f"{INSERT} Insert [@ins-2]"]))
        self.assertEqual((first["meshes"], second["meshes"]), ([f"{INSERT} Insert"], [f"{INSERT} Insert"]))
        self.assertNotEqual(first["node_indices"], second["node_indices"])
        one, two = (gltf["nodes"][entry["node_indices"][0]] for entry in (first, second))
        self.assertEqual(two["name"], f"{INSERT} Insert [@ins-2]")
        positions = [gltf["meshes"][node["mesh"]]["primitives"][0]["attributes"]["POSITION"] for node in (one, two)]
        self.assertEqual(positions[0], positions[1])  # one definition's triangles, instanced by both nodes
        # @ins-2 sits at (-10, 10, 18) mm, Z up; the GLB is in metres, Y up: (x, z, -y) / 1000.
        assert_vectors(self, two["translation"], [-0.010, 0.018, -0.010], places=9)
        self.assertEqual(part_map["parts"][INSERT]["occurrences"], sorted(INSERTS))

    def test_metrics_keep_the_unit_definition_and_place_each_instance(self) -> None:
        metrics = self.version_json("metrics.json")
        unit = metrics["parts"][INSERT]
        self.assertEqual(unit["model_frame"], "part_local")
        self.assertAlmostEqual(unit["volume_mm3"], INSERT_V, places=4)
        assert_same_holes(self, unit["holes"], [{"id": "H1", "diameter_mm": 1.0, "axis": [0.0, 0.0, -1.0],
                                                 "position": [0.0, 0.0, 4.0], "depth_mm": 4.0, "through": True}], 6)
        totals = metrics["totals"]
        self.assertEqual(totals["quantity"], {BASE: 1, INSERT: 6})
        self.assertAlmostEqual(totals["volume_mm3"], BASE_V + 6 * INSERT_V, places=3)
        self.assertAlmostEqual(totals["mass_g"], metrics["parts"][BASE]["mass_g"] + 6 * unit["mass_g"], places=6)
        # ins-5: Rx(90) takes (x, y, z) to (x, -z, y), then moves by (-10, -10, 22).
        turned = metrics["occurrences"]["ins-5"]
        assert_vectors(self, turned["bbox_mm"]["min"], [-12.0, -14.0, 20.0], places=6)
        assert_vectors(self, turned["bbox_mm"]["max"], [-8.0, -10.0, 24.0], places=6)
        assert_same_holes(self, turned["holes"], [{"id": "H1", "diameter_mm": 1.0, "axis": [0.0, 1.0, 0.0],
                                                   "position": [-10.0, -14.0, 22.0], "depth_mm": 4.0,
                                                   "through": True}], 6)
        assert_vectors(self, metrics["occurrences"]["ins-2"]["bbox_mm"]["min"], [-12.0, 8.0, 18.0], places=6)

    def test_bom_rolls_the_six_instances_into_one_row_of_quantity_six(self) -> None:
        out = self.hw.parent / ".cad-work" / "test" / "bom.csv"  # bom.py accepts an explicit --out only there
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(BOM_SCRIPT), str(self.folder / "assembly.step"),
                               "--hw", str(self.hw), "--out", str(out)], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        with out.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({row["PN"]: row["qty"] for row in rows}, {BASE: "1", INSERT: "6"})


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class PairClearanceBuildTests(unittest.TestCase):
    """The swing arm built 0.8 mm above the base, on its revolute joint, against a clearance
    requirement on that joint-connected pair (CadQuery only): 1.0 mm in v1, 0.5 mm in v2. check
    reads the requirement from the version's record snapshot."""

    LABELS = {1.0: "v1", 0.5: "v2"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hw = two_part_project(Path(cls.tmp.name), "cadquery")
        set_arm(cls.hw, "cadquery", z0=20.8)  # the plate's underside 0.8 mm above the base top at z 20
        cls.built = []
        for minimum, label in cls.LABELS.items():
            (cls.hw / "interfaces.yaml").write_text(
                "interfaces:\n  - {id: IF-001, from: arm, to: base, type: clearance, description: The arm clears the base.,"
                f" pair: [{ARM_PN}, {BASE}], min_clearance_mm: {minimum}}}\n", encoding="utf-8")
            cls.built.append(cli("run", str(cls.hw), "--label", label))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def checked(self, minimum: float) -> tuple[subprocess.CompletedProcess[str], dict]:
        for proc, _, _ in self.built:
            self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        proc, result, _ = cli("check", self.LABELS[minimum], "--hw", str(self.hw))
        return proc, result

    def test_a_requirement_above_the_gap_blocks_and_one_below_it_passes(self) -> None:
        proc, result = self.checked(1.0)
        self.assertEqual(proc.returncode, 1, proc.stderr[-2000:])
        self.assertEqual(result["blocking_failures"], ["pair_clearance"])
        # J-001 joins the pair, so interference lists it as a joint pair; the requirement holds all the same.
        self.assertEqual([row["pair"] for row in result["checks"]["interference"]["joint_pairs"]], [[BASE, ARM_PN]])
        (row,) = result["checks"]["pair_clearance"]["requirements"]
        self.assertEqual({key: row[key] for key in ("pair", "refs", "required_mm", "status", "interface", "source")},
                         {"pair": [BASE, ARM_PN], "refs": [BASE, ARM_PN], "required_mm": 1.0, "status": "fail",
                          "interface": "IF-001", "source": "interfaces.yaml#/interfaces/0"})
        self.assertAlmostEqual(row["clearance_mm"], 0.8, places=6)

        proc, result = self.checked(0.5)
        self.assertEqual(proc.returncode, 0, json.dumps(result.get("checks"), indent=1)[:4000] + proc.stderr[-2000:])
        (row,) = result["checks"]["pair_clearance"]["requirements"]
        self.assertEqual((result["checks"]["pair_clearance"]["status"], row["status"], row["required_mm"]),
                         ("pass", "pass", 0.5))
        self.assertAlmostEqual(row["clearance_mm"], 0.8, places=6)


class BoxPart:
    """A test double for a box-shaped part: `extents` along the unit `axes` (rows, in the model frame).

    `measured` patches the OCP measurements cadmodel.orientation takes (the model box, the oriented
    box, and the box after a rotation) with the exact values for this box."""

    def __init__(self, extents: tuple[float, float, float], axes=orientation.IDENTITY) -> None:
        self.extents, self.axes = extents, axes

    def box(self, rotation=orientation.IDENTITY) -> tuple[tuple[float, ...], tuple[float, ...]]:
        size = [sum(e * abs(sum(rotation[i][j] * a[j] for j in range(3))) for e, a in zip(self.extents, self.axes))
                for i in range(3)]
        return tuple(-s / 2 for s in size), tuple(s / 2 for s in size)

    def measured(self, oriented: bool = True) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(occ, "bbox", side_effect=lambda shape: self.box()))
        stack.enter_context(mock.patch.object(orientation, "turned_box", side_effect=lambda shape, rot: self.box(rot)))
        stack.enter_context(mock.patch.object(occ, "oriented_bbox", return_value=(self.extents, self.axes))
                            if oriented else
                            mock.patch.object(occ, "oriented_bbox", side_effect=AssertionError("fallback should not run")))
        return stack


def fdm_record(tmp: str, build_volume: str) -> Path:
    hw = Path(tmp)
    (hw / "parts" / "TST-0001").mkdir(parents=True)
    (hw / "product.yaml").write_text(build_volume, encoding="utf-8")
    (hw / "parts" / "TST-0001" / "part.yaml").write_text("process: fdm\n", encoding="utf-8")
    return hw


class CheckRuleTests(unittest.TestCase):
    """check's verdicts on given measurements; needs no OCP."""
    @staticmethod
    def readback_checks(*, valid: bool = True, ok: bool = True, integrity: str = "pass", made: dict | None = None) -> dict:
        row = {"checks": {"native": {"status": "pass"}, "identity": {"status": "pass"}, "step": {"status": "pass"},
                          "components": {"components": {"left": {"checks": {"binding": {"status": "pass"}}},
                                                        "right": {"checks": {"binding": {"status": "fail"}}}}}}}
        return {"brep_validity": {"parts": {"TST-0001": {"valid": valid, "kernel_errors": [], "step_errors": [] if valid else ["BRepCheck_NotClosed"]}}},
                "step_readback": {"parts": {"TST-0001": {"ok": ok}}}, "artifact_integrity": {"status": integrity},
                "manufacturing": {"parts": {"TST-0001": row} if made is None else made}}

    def test_readback_outcomes_judge_each_component_on_validity_readback_state_and_binding(self) -> None:
        """cad.readback is the cad-model check producer: an invalid B-rep or read-back fails, a component that
        does not bind fails alone, a part without a manufacturing output stays unknown, never passed."""
        def obligation(component: str | None) -> mock.Mock:
            subject = {"PN": "TST-0001", **({"component": component} if component else {})}
            return mock.Mock(id="cad.readback@TST-0001" + (f".{component}" if component else ""), subject=subject,
                             level="cad_valid")

        def outcomes(checks: dict, *components: str | None) -> dict:
            return {row["obligation"]: row["outcome"] for row in check.readback_outcomes([obligation(c) for c in components], checks)}

        self.assertEqual(outcomes(self.readback_checks(), "left", "right"),
                         {"cad.readback@TST-0001.left": "pass", "cad.readback@TST-0001.right": "fail"})
        self.assertEqual(outcomes(self.readback_checks(), None), {"cad.readback@TST-0001": "fail"})
        self.assertEqual(outcomes(self.readback_checks(valid=False), "left"), {"cad.readback@TST-0001.left": "fail"})
        self.assertEqual(outcomes(self.readback_checks(ok=False), "left"), {"cad.readback@TST-0001.left": "fail"})
        self.assertEqual(outcomes(self.readback_checks(integrity="fail"), "left"), {"cad.readback@TST-0001.left": "fail"})
        [row] = check.readback_outcomes([obligation(None)], self.readback_checks(made={}))
        self.assertEqual(row["outcome"], "unknown")
        self.assertIn("no manufacturing output", row["uncertainty"])
        legacy = self.readback_checks()
        legacy["manufacturing"]["parts"]["TST-0001"]["checks"]["identity"]["status"] = "warn"
        self.assertEqual(outcomes(legacy, "left"), {"cad.readback@TST-0001.left": "unknown"})

    def test_build_volume_accepts_axis_permutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = fdm_record(tmp, "build_volume: {x_mm: 35, y_mm: 10, z_mm: 5, margin_mm: 0}\n")
            with BoxPart((35.0, 10.0, 5.0)).measured(oriented=False):
                result = check.build_volume(hw, {"TST-0001": mock.Mock(shape=mock.Mock())})
        self.assertEqual(result["status"], "pass")
        row = result["parts"]["TST-0001"]
        self.assertEqual((row["orientation"], row["method"], row["size_mm"]),
                         ("xyz", "axis_aligned", [35.0, 10.0, 5.0]))

    def test_build_volume_chooses_lowest_print_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = fdm_record(tmp, "build_volume: {x_mm: 35, y_mm: 20, z_mm: 10, margin_mm: 0}\n")
            with BoxPart((35.0, 10.0, 20.0)).measured(oriented=False):
                result = check.build_volume(hw, {"TST-0001": mock.Mock(shape=mock.Mock())})
        row = result["parts"]["TST-0001"]
        self.assertEqual((row["orientation"], row["size_mm"]), ("xzy", [35.0, 20.0, 10.0]))
        self.assertEqual(row["rotation"], [[-1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]])
        self.assertEqual(row["determinant"], 1.0)

    def test_build_volume_falls_back_for_rotated_plate(self) -> None:
        half = math.sqrt(0.5)
        with tempfile.TemporaryDirectory() as tmp:
            hw = fdm_record(tmp, "build_volume: {x_mm: 256, y_mm: 256, z_mm: 256, margin_mm: 5}\n")
            with BoxPart((240.0, 240.0, 5.0), ((half, half, 0.0), (-half, half, 0.0), (0.0, 0.0, 1.0))).measured():
                result = check.build_volume(hw, {"TST-0001": mock.Mock(shape=mock.Mock())})
        row = result["parts"]["TST-0001"]
        self.assertEqual((result["status"], row["orientation"], row["method"]), ("pass", "oriented", "oriented"))
        self.assertEqual(row["size_mm"], [240.0, 240.0, 5.0])
        self.assertEqual(row["axes"][2], [0.0, 0.0, 1.0])
        self.assertAlmostEqual(row["determinant"], 1.0, places=12)

    def test_build_volume_warns_without_blocking_if_aabb_and_oriented_box_do_not_fit(self) -> None:
        """An envelope fit is a diagnostic, never process feasibility: an oversized part warns, never blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            hw = fdm_record(tmp, "build_volume: {x_mm: 256, y_mm: 256, z_mm: 256, margin_mm: 5}\n")
            with BoxPart((300.0, 20.0, 20.0)).measured():
                result = check.build_volume(hw, {"TST-0001": mock.Mock(shape=mock.Mock())})
        self.assertEqual((result["status"], result["blocking"], result["blocking_failures"], result["warned"]),
                         ("warn", False, [], ["TST-0001"]))
        self.assertIn("never process feasibility", result["note"])
        row = result["parts"]["TST-0001"]
        self.assertEqual((row["status"], row["approved"], row["pose"]), ("warn", False, "auto_fit_proposal"))
        self.assertIn("TST-0001", row["message"])
        self.assertIn("(300.0, 20.0, 20.0)", row["message"])
        self.assertIn("oriented box", row["message"])

    def test_build_volume_measures_the_approved_pose_and_tries_no_other(self) -> None:
        envelope = orientation.BuildVolume((50.0, 50.0, 20.0), 0.0, orientation.ORIENTATIONS, "capability tst envelope")
        for size, status in (((40.0, 30.0, 10.0), "pass"), ((40.0, 30.0, 30.0), "warn")):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp:
                hw = fdm_record(tmp, "id: test\n")
                placement = {"approved": True, "pose": "approved", "operation": "print",
                             "build_volume": envelope.describe()}
                low, high = (0.0, 0.0, 0.0), size
                with mock.patch.object(occ, "bbox", return_value=(low, high)), \
                     mock.patch.object(orientation, "fit", side_effect=AssertionError("an approved pose is not searched")):
                    result = check.build_volume(hw, {"TST-0001": mock.Mock(shape=mock.Mock())},
                                                {"TST-0001": {"placement": placement}})
                row = result["parts"]["TST-0001"]
                self.assertEqual((result["blocking"], row["status"], row["approved"], row["envelope_source"]),
                                 (False, status, True, "capability tst envelope"))

    def test_build_volume_warns_when_not_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = fdm_record(tmp, "id: test\n")
            result = check.build_volume(hw, {"TST-0001": mock.Mock(shape=mock.Mock())})
        self.assertEqual((result["status"], result["blocking"]), ("warn", False))
        self.assertIn("TST-0001", result["parts"])

    def test_missing_interfaces_file_means_no_declared_fits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check.declared_press_fits(Path(tmp)), [])
    def test_malformed_interfaces_remains_a_usage_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            (hw / "interfaces.yaml").write_text("interfaces: [\n", encoding="utf-8")
            with self.assertRaisesRegex(build.UsageError, "cannot read interfaces"):
                check.declared_press_fits(hw)



    def test_declared_interference_fit_allows_overlap_and_reports_source(self) -> None:
        pair = (BASE, ARM_PN)
        declared = [{"pair": list(pair), "fit": "5 H7/p6", "classification": "interference",
                     "interface": "IF-001", "source": "interfaces.yaml#/interfaces/0/limits/press_fit"}]
        geometry = {pair: {"clearance_mm": -0.01, "interference_mm3": 0.1, "method": "exact"}}
        with mock.patch.object(check, "load_pose", return_value=mock.Mock(INTERFERENCE_MM3=0.001)):
            result = check.interference(geometry, None, set(), set(), declared)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["declared_pairs"], declared)
        self.assertTrue(result["pairs"][0]["allowed"])

    def test_undeclared_overlap_still_fails(self) -> None:
        geometry = {(BASE, ARM_PN): {"clearance_mm": -0.01, "interference_mm3": 0.1, "method": "exact"}}
        with mock.patch.object(check, "load_pose", return_value=mock.Mock(INTERFERENCE_MM3=0.001)):
            result = check.interference(geometry, None, set(), set())
        self.assertEqual((result["status"], result["pairs"][0]["ok"]), ("fail", False))

    def test_declared_interference_fit_with_clearance_is_a_finding(self) -> None:
        pair = (BASE, ARM_PN)
        declared = [{"pair": list(pair), "fit": "5 H7/p6", "classification": "interference",
                     "interface": "IF-001", "source": "interfaces.yaml#/interfaces/0/limits/press_fit"}]
        geometry = {pair: {"clearance_mm": 0.01, "interference_mm3": 0.0, "method": "exact"}}
        with mock.patch.object(check, "load_pose", return_value=mock.Mock(INTERFERENCE_MM3=0.001)):
            result = check.interference(geometry, None, set(), set(), declared)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["declared_fit_findings"][0]["type"], "declared_fit_not_realized")
        self.assertEqual(result["declared_fit_findings"][0]["source"], declared[0]["source"])

    @staticmethod
    def requirement(pair: tuple[str, str], minimum: float, margin: float = 0.0) -> dict:
        return {"pair": list(pair), "min_clearance_mm": minimum, "margin_mm": margin, "interface": "IF-002",
                "source": "interfaces.yaml#/interfaces/1"}

    def test_a_pair_requirement_fails_above_the_measured_clearance_and_passes_at_or_below_it(self) -> None:
        geometry = gaps({(BASE, ARM_PN): 0.8})
        for minimum, status in ((1.0, "fail"), (0.8, "pass"), (0.5, "pass")):
            with self.subTest(minimum=minimum):
                result = check.pair_clearance(geometry, [self.requirement((BASE, ARM_PN), minimum)], {ARM_PN: "@arm"})
                self.assertEqual((result["status"], result["blocking"]), (status, True))
                self.assertEqual(result["requirements"], [
                    {"pair": [BASE, ARM_PN], "refs": [BASE, "@arm"], "required_mm": minimum, "margin_mm": 0.0,
                     "clearance_mm": 0.8, "method": "exact", "status": status, "interface": "IF-002",
                     "source": "interfaces.yaml#/interfaces/1"}])

    def test_a_clearance_inside_its_margin_warns_and_never_fails(self) -> None:
        geometry = gaps({(BASE, ARM_PN): 0.55})
        for minimum, margin, status in ((0.5, 0.1, "warn"), (0.45, 0.1, "pass"), (0.55, 0.1, "warn"), (0.6, 0.1, "fail")):
            with self.subTest(minimum=minimum, margin=margin):
                result = check.pair_clearance(geometry, [self.requirement((BASE, ARM_PN), minimum, margin)])
                (row,) = result["requirements"]
                self.assertEqual((result["status"], row["status"], row.get("warning")),
                                 (status, status, "margin" if status == "warn" else None))

    def test_a_cleared_pair_passes_on_its_lower_bound_and_carries_no_distance(self) -> None:
        geometry = {(BASE, ARM_PN): {"clearance_mm": None, "interference_mm3": None, "method": "mesh",
                                     "lower_mm": 3.2, "upper_mm": 3.5}}
        result = check.pair_clearance(geometry, [self.requirement((BASE, ARM_PN), 1.0, 0.1)])
        (row,) = result["requirements"]
        self.assertEqual((result["status"], row["clearance_mm"], row["method"], row["lower_mm"]), ("pass", None, "mesh", 3.2))
        unproved = check.pair_clearance(geometry, [self.requirement((BASE, ARM_PN), 3.2, 0.1)])
        self.assertEqual(unproved["status"], "fail")
        with mock.patch.object(check, "load_pose", return_value=mock.Mock(INTERFERENCE_MM3=0.001)):
            found = check.interference(geometry, None, set(), set())
        self.assertEqual((found["status"], found["pairs"][0]["interferes"], found["min_clearance"]), ("pass", False, None))

    def test_a_pair_requirement_compares_the_unrounded_clearance(self) -> None:
        result = check.pair_clearance(gaps({(BASE, ARM_PN): 0.79996}), [self.requirement((BASE, ARM_PN), 0.8)])
        self.assertEqual(result["status"], "fail")

    def test_an_unmeasured_or_absent_pair_fails_its_requirement(self) -> None:
        geometry = gaps({(BASE, ARM_PN): None})
        geometry[(BASE, ARM_PN)]["error"] = "Standard_Failure"
        result = check.pair_clearance(geometry, [self.requirement((BASE, ARM_PN), 0.5), self.requirement((BASE, PIN), 0.5)])
        self.assertEqual(result["status"], "fail")
        self.assertEqual([(row["status"], row.get("error")) for row in result["requirements"]],
                         [("fail", "Standard_Failure"), ("fail", "the version has no such occurrence pair")])
        self.assertEqual(check.pair_clearance(geometry, [])["status"], "pass")

    def test_a_cleared_pair_that_could_be_an_occurrences_nearest_is_refined(self) -> None:
        bound = lambda lower, upper: {"clearance_mm": None, "interference_mm3": None, "method": "mesh",  # noqa: E731
                                      "lower_mm": lower, "upper_mm": upper}
        exact = lambda pair, gap: gaps({pair: gap})[pair]  # noqa: E731
        rows = {(BASE, ARM_PN): exact((BASE, ARM_PN), 0.0), (BASE, PIN): bound(4.0, 4.4), (ARM_PN, PIN): bound(4.5, None)}
        # BASE and the arm touch; the pin's nearest is the base (at most 4.4), never the arm (at least 4.5)
        self.assertEqual(check._nearest_candidates(rows), [(BASE, PIN)])
        rows[(BASE, PIN)] = exact((BASE, PIN), 4.2)
        self.assertEqual(check._nearest_candidates(rows), [])
        rows[(BASE, ARM_PN)] = exact((BASE, ARM_PN), 5.0)  # now the arm's nearest may be the pin
        self.assertEqual(check._nearest_candidates(rows), [(ARM_PN, PIN)])

    def test_a_clearance_requirement_record_resolves_through_the_pair_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            path = hw / "interfaces.yaml"
            requirement = ("interfaces:\n  - {id: IF-001, from: arm, to: base, type: clearance, description: Gap.,"
                           " pair: [%s, %s], min_clearance_mm: %s}\n")
            path.write_text(requirement % (ARM_PN, BASE, 0.5), encoding="utf-8")
            self.assertEqual(check.declared_clearances(hw), [{"pair": [BASE, ARM_PN], "min_clearance_mm": 0.5,
                                                              "interface": "IF-001",
                                                              "source": "interfaces.yaml#/interfaces/0"}])
            path.write_text(requirement % (ARM_PN, BASE, 0), encoding="utf-8")
            with self.assertRaisesRegex(build.UsageError, "a clearance requirement needs"):
                check.declared_clearances(hw)
            path.write_text(requirement % ('"@arm-a"', BASE, 0.5), encoding="utf-8")
            with self.assertRaisesRegex(build.UsageError, "no assembly record"):
                check.declared_clearances(hw)

    def test_press_fit_record_is_classified_by_cad_fits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            (hw / "interfaces.yaml").write_text(
                "interfaces:\n"
                "  - id: IF-001\n"
                "    limits:\n"
                "      press_fit: {fit: 5 H7/p6, pair: [TST-0001, TST-0002]}\n",
                encoding="utf-8",
            )
            declared = check.declared_press_fits(hw)
        self.assertEqual(declared, [{"pair": [BASE, ARM_PN], "fit": "5 H7/p6", "classification": "interference",
                                    "interface": "IF-001",
                                    "source": "interfaces.yaml#/interfaces/0/limits/press_fit"}])

    def test_template_press_pairs_are_declared_and_classified(self) -> None:
        template = HERE.parents[2] / "examples" / "lamp-arm" / "hw"
        declared = check.declared_press_fits(template)
        self.assertEqual({tuple(item["pair"]) for item in declared}, {
            ("LMP-0002", "LMP-0005"), ("LMP-0002", "LMP-0006"), ("LMP-0003", "LMP-0007")})
        self.assertTrue(all(item["classification"] == "interference" for item in declared))

    def test_an_installed_skill_tree_classifies_press_fits_with_its_own_cad_fits(self) -> None:
        """An installed skill tree need not be named `cad` or sit inside this repository."""
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "installed" / "cad-agent-skill-tree"
            shutil.copytree(HERE.parents[2], plugin, ignore=shutil.ignore_patterns("__pycache__"))
            hw = Path(tmp) / "hw"
            hw.mkdir()
            (hw / "interfaces.yaml").write_text(
                "interfaces:\n  - id: IF-001\n    limits:\n      press_fit: {fit: 5 H7/p6, pair: [TST-0001, TST-0002]}\n",
                encoding="utf-8")
            probe = ("import json, sys\nfrom pathlib import Path\nfrom cadmodel import check\n"
                     "print(json.dumps({'fits': str(check.load_pair_policy().FITS_SCRIPT), "
                     "'declared': check.declared_press_fits(Path(sys.argv[1]))}))\n")
            scripts = plugin / "skills" / "cad-model" / "scripts"
            proc = subprocess.run([sys.executable, "-c", probe, str(hw)], cwd=scripts, capture_output=True, text=True,
                                  check=False, timeout=60, env={**os.environ, "PYTHONPATH": str(scripts)})
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            result = json.loads(proc.stdout)
            self.assertTrue(Path(result["fits"]).is_relative_to(plugin.resolve()), result["fits"])
        self.assertEqual([(item["pair"], item["classification"]) for item in result["declared"]],
                         [([BASE, ARM_PN], "interference")])

    def test_an_allowed_pair_whose_measurement_failed_still_fails(self) -> None:
        geometry = gaps({(BASE, ARM_PN): None})
        geometry[(BASE, ARM_PN)]["error"] = "Standard_Failure"
        result = check.interference(geometry, None, set(), {(BASE, ARM_PN)})
        self.assertEqual((result["status"], result["pairs"][0]["ok"]), ("fail", False))

    def test_a_declared_fit_naming_an_unknown_part_fails(self) -> None:
        result = check.interference(gaps({(BASE, ARM_PN): 0.0}), None, set(), {(BASE, "TST-0009")})
        self.assertEqual((result["status"], result["unknown_allowed"]), ("fail", [[BASE, "TST-0009"]]))

    def test_expected_solids_naming_an_unknown_part_fails(self) -> None:
        result = check.solid_structure({}, {"TST-0009": 1}, None, None)
        self.assertEqual((result["status"], result["unknown_parts"]), ("fail", ["TST-0009"]))

    def test_a_float_across_a_movable_joint_is_its_clearance(self) -> None:
        result = check.connection(gaps({(BASE, ARM_PN): 0.1}), [BASE, ARM_PN],
                                  joints(("J-001", "revolute", BASE, ARM_PN)))
        row = result["parts"][ARM_PN]
        self.assertEqual((result["status"], row["status"], row["joint"]), ("pass", "floating", "J-001"))

    def test_a_float_to_a_rigidly_attached_parent_fails(self) -> None:
        result = check.connection(gaps({(BASE, ARM_PN): 0.0, (BASE, PIN): 1.0, (ARM_PN, PIN): 5.0}),
                                  [BASE, ARM_PN, PIN],
                                  joints(("J-001", "revolute", BASE, ARM_PN), (f"attach:{PIN}", "fixed", BASE, PIN)))
        row = result["parts"][PIN]
        self.assertEqual((result["status"], row["status"], row["joint"], row["ok"]), ("fail", "floating", None, False))

    def test_a_pin_fixed_in_the_parent_carries_the_joint_clearance(self) -> None:
        # The pin is pressed into the base; the arm turns on it with 0.1 mm clearance, 0.5 mm above the base.
        result = check.connection(gaps({(BASE, ARM_PN): 0.5, (BASE, PIN): 0.0, (ARM_PN, PIN): 0.1}),
                                  [BASE, ARM_PN, PIN],
                                  joints(("J-001", "revolute", BASE, ARM_PN), ("J-002", "fixed", BASE, PIN)))
        row = result["parts"][ARM_PN]
        self.assertEqual((result["status"], row["nearest"], row["joint"]), ("pass", PIN, "J-001"))

    def test_a_fixed_pin_with_clearance_in_its_parent_fails_though_it_floats_on_the_joint(self) -> None:
        # The record fixes the pin to the base, but it sits 0.108 mm clear of the base and 0.0125 mm in the arm.
        result = check.connection(gaps({(BASE, ARM_PN): 0.5, (BASE, PIN): 0.108, (ARM_PN, PIN): 0.0125}),
                                  [BASE, ARM_PN, PIN],
                                  joints(("J-001", "revolute", BASE, ARM_PN), ("J-002", "fixed", BASE, PIN)))
        self.assertEqual((result["parts"][PIN]["joint"], result["parts"][PIN]["ok"]), ("J-001", True))
        self.assertEqual([(f["joint"], f["ok"]) for f in result["fixed_joints"]], [("J-002", False)])
        self.assertEqual(result["status"], "fail")


class OrientationRuleTests(unittest.TestCase):
    """cadmodel.orientation: one proper rotation per fit, measured after it is applied; needs no OCP."""

    def test_every_permutation_is_a_proper_rotation_onto_its_named_axes(self) -> None:
        for name in orientation.ORIENTATIONS:
            with self.subTest(orientation=name):
                rotation = orientation.permutation_rotation(name)
                self.assertEqual(orientation.determinant(rotation), 1.0)
                self.assertTrue(orientation.is_proper(rotation))
                self.assertEqual([abs(rotation[i]["xyz".index(axis)]) for i, axis in enumerate(name)], [1.0] * 3)

    def test_a_mirror_a_scale_or_a_shear_is_not_proper(self) -> None:
        for rows in (((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                     ((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
                     ((1.0, 0.1, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))):
            self.assertFalse(orientation.is_proper(rows), rows)

    def test_an_asymmetric_volume_takes_the_oriented_box_axes_in_the_order_that_fits(self) -> None:
        """The tall axis of a tilted plate fits only along printer X; testing its oriented-box extents in
        their own order and then lowering the height would have claimed a fit 280 mm deep in Y."""
        a2 = tuple(v / math.sqrt(3) for v in (1.0, 1.0, 1.0))
        a0 = tuple(v / math.sqrt(2) for v in (1.0, -1.0, 0.0))
        a1 = tuple(v / math.sqrt(6) for v in (1.0, 1.0, -2.0))
        part = BoxPart((10.0, 40.0, 280.0), (a0, a1, a2))
        volume = orientation.BuildVolume((300.0, 50.0, 300.0), 0.0, orientation.ORIENTATIONS)
        with part.measured():
            _, found, _ = orientation.fit(mock.Mock(), volume)
        self.assertEqual((found.method, found.orientation), ("oriented", "oriented"))
        assert_vectors(self, list(found.size_mm), [280.0, 40.0, 10.0], places=9)
        self.assertTrue(orientation.fits(found.size_mm, volume.usable_mm))
        self.assertTrue(orientation.is_proper(found.rotation))

    def test_a_declared_volume_reads_from_the_product_record(self) -> None:
        self.assertIsNone(orientation.BuildVolume.declared({"id": "x"}))
        volume = orientation.BuildVolume.declared({"build_volume": {"x_mm": 256, "y_mm": 200, "z_mm": 100,
                                                                    "margin_mm": 5, "orientations": ["xyz", "zyx"]}})
        self.assertEqual((volume.usable_mm, volume.orientations), ((246.0, 190.0, 90.0), ("xyz", "zyx")))
        with self.assertRaises(ValueError):
            orientation.BuildVolume.declared({"build_volume": {"x_mm": "wide", "y_mm": 1, "z_mm": 1}})


def snapshot_of(records: dict, product: dict | None = None):
    """The record snapshot of `records` (PN -> part.yaml document) and `product`, as `run` reads one."""
    import yaml

    files = {f"parts/{pn}/part.yaml": yaml.safe_dump(data).encode() for pn, data in records.items()}
    files["product.yaml"] = yaml.safe_dump(product or {"id": "test"}).encode()
    return build.record.read_snapshot(files)


class ManufactureRuleTests(unittest.TestCase):
    """Which parts get manufacturing outputs and which declarations are refused; needs no OCP."""

    @staticmethod
    def part(pn: str, **fields) -> dict:
        return {"PN": pn, "rev": "X1", "title": "Part", "material": "PETG", "process": "fdm", "finish": "none",
                "mass_target": 1, "make_buy": "make", "supplier": None, **fields}

    def test_custom_made_parts_are_planned_and_catalogue_buys_are_not(self) -> None:
        records = {"TST-0001": self.part("TST-0001"),
                   "TST-0002": self.part("TST-0002", make_buy="buy", process="cnc",
                                         procurement={"mode": "made_to_drawing"}),
                   "TST-0003": self.part("TST-0003", make_buy="buy", process="purchased", procurement={"mode": "catalog"})}
        plans, refused, uncovered = manufacture.plan(snapshot_of(records))
        self.assertEqual((sorted(plans), refused, uncovered), (["TST-0001", "TST-0002"], [], {}))
        self.assertEqual(plans["TST-0001"].policy.outputs, ("part.step", "part.stl", "manufacturing.json"))
        self.assertEqual(plans["TST-0002"].policy.outputs, ("part.step", "manufacturing.json"))
        self.assertEqual((plans["TST-0001"].policy.relationship, plans["TST-0001"].policy.volume_tolerance_mm3(1000.0)),
                         ("same", 1000.0 * 1e-9))

    def test_a_different_state_needs_its_declaration_and_a_positive_tolerance(self) -> None:
        state = {"relationship": "different", "state": "flat", "reason": "elastic_bending", "decision": "DR-001"}
        records = {"TST-0001": self.part("TST-0001", manufacture=state),
                   "TST-0002": self.part("TST-0002", manufacture={**state, "decision": None, "volume_rel_tolerance": 1e-6}),
                   "TST-0003": self.part("TST-0003", manufacture={**state, "volume_rel_tolerance": 0.0}),
                   "TST-0004": self.part("TST-0004", manufacture={**state, "volume_abs_tolerance_mm3": 0.5})}
        plans, refused, _ = manufacture.plan(snapshot_of(records))
        self.assertEqual(sorted(plans), ["TST-0004"])
        reasons = dict(refused)
        self.assertIn("without a volume tolerance", reasons["TST-0001"])
        self.assertIn("also needs decision", reasons["TST-0002"])
        self.assertIn("tolerance refused", reasons["TST-0003"])
        self.assertEqual(plans["TST-0004"].policy.volume_tolerance_mm3(100.0), 0.5)

    def test_diff_names_a_manufacturing_only_change(self) -> None:
        entry = {"state": {"relationship": "different", "name": "flat"}, "tolerance": {"volume_rel": 1e-6, "volume_abs_mm3": 0.0},
                 "mesh": {"linear_deflection_mm": 0.05}, "build_volume": None,
                 "native": {"kernel_volume_mm3": 100.0, "kernel_solids": 1},
                 "placement": {"method": "axis_aligned", "orientation": "xzy", "transform": [[1.0, 0.0, 0.0, 0.0]] * 3}}
        looser = {**entry, "tolerance": {"volume_rel": 2e-6, "volume_abs_mm3": 0.0}}
        result = diff.compare_manufacturing({"TST-0001": entry, "TST-0002": entry}, {"TST-0001": looser, "TST-0003": entry})
        self.assertEqual(result["changed"], ["TST-0001", "TST-0002", "TST-0003"])
        self.assertEqual(result["parts"]["TST-0001"]["changed"], ["tolerance"])
        self.assertEqual((result["parts"]["TST-0002"]["status"], result["parts"]["TST-0003"]["status"]), ("removed", "added"))
        self.assertFalse(diff.compare_manufacturing(None, {})["available"])

    def test_latest_names_each_manufacturing_folder(self) -> None:
        manifest = {"parts": {"TST-0001": {"step": "parts/TST-0001.step"}},
                    "manufacturing": {"TST-0001": {"path": "manufacturing/TST-0001"}, "TST-0002": {"path": None}}}
        latest = build.latest_pointer("v3", manifest)
        self.assertEqual(latest["manufacturing"], {"TST-0001": "versions/v3/manufacturing/TST-0001"})
        self.assertEqual(latest["parts"], {"TST-0001": "versions/v3/parts/TST-0001.step"})


def projects():
    """fixtures/manufacturing_projects.py: record version 2 projects written with cad-record's fixture builder."""
    name = "cad_model_manufacturing_projects"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, FIXTURES / "manufacturing_projects.py")
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


HALF = math.sqrt(0.5)
TURNED = {"status": "approved", "rotation": {"quaternion": [HALF, HALF, 0, 0]}, "decision": "DR-001"}  # +90 deg about X
TURNED_ROWS = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))


class ManufacturingIntentRuleTests(unittest.TestCase):
    """Record version 2 snapshots, manufacturing plans and their keys, export poses and profile mass on
    synthetic TST records; needs PyYAML, no OCP."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hw = Path(self.tmp.name) / "hw"

    def write(self, documents: dict) -> Any:
        """Write `documents` as the loose project and return its snapshot as `run` reads it."""
        mp = projects()
        shutil.rmtree(self.hw, ignore_errors=True)
        mp.shared().write_record(self.hw, documents)
        mp.set_model(self.hw, "cadquery", [])
        return build.record.read_snapshot(build.record.snapshot(self.hw, [mp.PN]))

    def plan(self, **record: Any) -> tuple[Any, dict, list, dict]:
        snapshot = self.write(projects().documents(**record))
        return (snapshot, *manufacture.plan(snapshot))

    def test_a_version_snapshots_requirements_design_records_and_source_bytes_but_no_ledger(self) -> None:
        mp = projects()
        mp.loose_project(self.hw, "cadquery", [])
        for ledger in ("findings.yaml", "evidence.yaml"):
            (self.hw / "manufacturing" / ledger).write_text("entries: []\n", encoding="utf-8")
        files = build.record.snapshot(self.hw, [mp.PN])
        self.assertEqual(sorted(files), ["assembly.yaml", "interfaces.yaml", "manufacturing/capabilities.yaml",
                                         "manufacturing/materials.yaml", "manufacturing/selection.yaml", mp.SOURCE,
                                         f"parts/{mp.PN}/part.yaml", "product.yaml", "requirements.yaml"])
        self.assertEqual(files[mp.SOURCE], mp.SOURCE_BYTES)
        self.assertEqual(build.record.read_snapshot(files).version, 2)

    def test_the_outputs_key_covers_material_capability_route_orientation_and_source_inputs(self) -> None:
        mp = projects()
        base = self.plan()[1][mp.PN].intent["inputs_sha256"]
        variants = {"density": {"density": 1.3}, "no density": {"with_density": False}, "orientation": {"orientation": TURNED},
                    "process and route": {"process": "cnc"}, "components": {"parts_of": mp.components()}}
        seen = {base}
        for name, record in variants.items():
            with self.subTest(change=name):
                inputs = self.plan(**record)[1][mp.PN].intent["inputs_sha256"]
                self.assertNotIn(inputs, seen)
                seen.add(inputs)
        documents = mp.documents()
        documents["manufacturing/capabilities.yaml"]["profiles"][0]["envelope"]["z"] = \
            mp.shared().known(mp.shared().quantity(60.0, "mm"), "measured", [mp.shared().source("calibration")])
        self.assertNotIn(manufacture.plan(self.write(documents))[0][mp.PN].intent["inputs_sha256"], seen)
        documents = mp.documents()
        documents["_sources"] = {mp.SOURCE: b"%PDF-1.4 a different datasheet revision\n"}
        self.assertNotIn(manufacture.plan(self.write(documents))[0][mp.PN].intent["inputs_sha256"], seen)
        self.plan()
        for ledger in ("findings.yaml", "evidence.yaml"):  # post-check ledgers never enter a version's inputs
            (self.hw / "manufacturing" / ledger).write_text("entries: [{id: EV-001}]\n", encoding="utf-8")
        snapshot = build.record.read_snapshot(build.record.snapshot(self.hw, [mp.PN]))
        self.assertEqual(manufacture.plan(snapshot)[0][mp.PN].intent["inputs_sha256"], base)

    def test_an_approved_orientation_is_the_export_pose_and_nothing_else_is_approved(self) -> None:
        mp = projects()
        _, plans, refused, _ = self.plan(orientation=TURNED)
        self.assertEqual(refused, [])
        chosen, volume = manufacture.pose(plans[mp.PN], None, None)
        self.assertEqual((chosen.status, chosen.approved, chosen.operation, chosen.decision),
                         ("approved", True, "print", "DR-001"))
        for got, want in zip(chosen.rotation, TURNED_ROWS):
            assert_vectors(self, list(got), list(want), places=12)
        self.assertEqual((volume.size_mm, volume.source), (mp.ENVELOPE_MM, "capability tst_printer envelope"))
        cases = {"proposed": ({"orientation": {"status": "proposed", "rotation": {"axis": [0, 0, 1], "angle_deg": 90}}},
                              None, "proposed"),
                 "unknown": ({"orientation": mp.shared().unknown("orientation not decided")}, None, "unknown"),
                 "unplanned": ({"parts_of": mp.components(), "print_components": ["body"]}, "tab", "unknown")}
        for name, (record, component, status) in cases.items():
            with self.subTest(case=name):
                chosen, _ = manufacture.pose(self.plan(**record)[1][mp.PN], component, None)
                self.assertEqual((chosen.status, chosen.approved), (status, False))
        self.assertIn("no additive operation", manufacture.pose(
            self.plan(parts_of=mp.components(), print_components=["body"])[1][mp.PN], "tab", None)[0].reason)
        chosen, volume = manufacture.pose(self.plan(process="cnc")[1][mp.PN], None, None)
        self.assertEqual((chosen.status, chosen.rotation, volume), ("model_frame", orientation.IDENTITY, None))

    def test_a_multiprocess_route_plans_its_cut_state_with_no_print_output(self) -> None:
        """Plan section 11 row 12, model part: laser, ream, coat and inspect plan one model-frame part.step;
        no operation is additive, so nothing is meshed, oriented or set in an envelope."""
        mp = projects()
        _, plans, refused, uncovered = self.plan(process="laser")
        self.assertEqual((refused, uncovered), ([], {}))
        plan = plans[mp.PN]
        self.assertEqual((plan.additive, plan.policy.outputs, plan.envelopes), (False, ("part.step", "manufacturing.json"), {}))
        self.assertEqual([(op["id"], op["kind"], op["known_kind"]) for op in plan.intent["route"]],
                         [(ident, kind, True) for ident, kind in mp.MULTIPROCESS_ROUTE])
        chosen, volume = manufacture.pose(plan, None, None)
        self.assertEqual((chosen.status, chosen.rotation, volume), ("model_frame", orientation.IDENTITY, None))

    def test_a_rotation_that_is_not_proper_is_refused_before_any_kernel_runs(self) -> None:
        for got in (orientation.rotation_of({"quaternion": [HALF, HALF, 0, 0]}),
                    orientation.rotation_of({"axis": [2, 0, 0], "angle_deg": 90})):
            for row, want in zip(got, TURNED_ROWS):
                assert_vectors(self, list(row), list(want), places=12)
        for bad in ({"quaternion": [1, 1, 0, 0]}, {"quaternion": [1, 0, 0]}, {"axis": [0, 0, 0], "angle_deg": 5}, {}):
            with self.subTest(rotation=bad), self.assertRaises(ValueError):
                orientation.rotation_of(bad)
        _, plans, refused, _ = self.plan(orientation={**TURNED, "rotation": {"quaternion": [1, 1, 0, 0]}})
        self.assertEqual(plans, {})
        self.assertIn("print orientation: orientation quaternion norm", dict(refused)[projects().PN])

    def test_a_record_version_1_part_keeps_its_auto_fit_labelled_unapproved(self) -> None:
        part = ManufactureRuleTests.part("TST-0001")
        snapshot = snapshot_of({"TST-0001": part}, {"id": "t", "build_volume": {"x_mm": 35, "y_mm": 10, "z_mm": 5}})
        plans, _, _ = manufacture.plan(snapshot)
        self.assertEqual(plans["TST-0001"].intent, {"record_version": 1, "status": "unknown",
                                                   "reason": build.record.load_intent().LEGACY_REASON})
        with BoxPart((35.0, 10.0, 5.0)).measured(oriented=False):
            chosen, volume = manufacture.pose(plans["TST-0001"], None, mock.Mock())
        self.assertEqual((chosen.status, chosen.approved, chosen.method, chosen.name, volume.source),
                         ("unapproved_auto_fit", False, "axis_aligned", "xyz", "product.yaml build_volume"))
        self.assertIn("never an approved orientation", chosen.reason)

    def test_unbindable_solids_stay_unknown_for_a_version_1_part_and_refuse_a_version_2_part(self) -> None:
        """A record version 1 part declares no components: its several solids still export whole, their
        identity unknown (legacy) with every solid listed, never guessed; a version 2 part refuses."""
        legacy = manufacture.plan(snapshot_of({"TST-0001": ManufactureRuleTests.part("TST-0001", process="cnc")}))[0]
        entry = {"PN": "TST-0001", "product": "TST-0001 Part", "mesh": None, "outputs": ["part.step", "manufacturing.json"]}
        solid = mock.Mock(facts=lambda: {"volume_mm3": 1.0})
        refusal = manufacture.components.BindingError("TST-0001: 2 solids, but part.yaml intent.components declares none")
        target = Path(self.tmp.name) / "out"
        with mock.patch.object(manufacture.components, "bind", side_effect=refusal), \
                mock.patch.object(manufacture.components, "solids", return_value=[solid, solid]), \
                mock.patch.object(manufacture, "_write") as write:
            found = manufacture.export(target, legacy["TST-0001"], entry, mock.Mock(), None)
            write.assert_called_once()
            identity = found["component_identity"]
            self.assertEqual((identity["status"], identity["solids"]), ("unknown", [{"volume_mm3": 1.0}] * 2))
            self.assertTrue(identity["reason"].startswith(build.record.load_intent().LEGACY_REASON), identity["reason"])
            self.assertEqual((found["components"], found["component_outputs"], found["placement"]["pose"]),
                             ({}, [], "model_frame"))
            _, plans, _, _ = self.plan()
            with self.assertRaisesRegex(manufacture.components.BindingError, "declares none"):
                manufacture.export(target, plans[projects().PN], entry, mock.Mock(), None)

    def test_declared_components_set_the_solid_count_check_expects(self) -> None:
        """A record version 2 part made as installed declares its definition's solid count by its
        components' multiplicities, which replaces the previous version's count; --expect-solids still
        wins; a different state, a sole solid and a record version 1 snapshot declare none."""
        import yaml

        mp = projects()

        def declared(files: dict) -> dict:
            folder = Path(tempfile.mkdtemp(dir=self.tmp.name)) / "record"
            build.record.write_snapshot(folder, files)
            return check.declared_solids(folder)

        kit = dict(self.write(mp.documents(parts_of=mp.components(tab_multiplicity=2))).files)
        self.assertEqual(declared(kit), {mp.PN: 3})
        part = yaml.safe_load(kit[f"parts/{mp.PN}/part.yaml"])
        part["manufacture"] = {"relationship": "different", "state": "flat", "reason": "elastic_bending",
                               "decision": "DR-001", "volume_rel_tolerance": 1e-6}
        self.assertEqual(declared({**kit, f"parts/{mp.PN}/part.yaml": yaml.safe_dump(part).encode()}), {})
        self.assertEqual(declared(dict(self.write(mp.documents()).files)), {})
        self.assertEqual(declared(dict(snapshot_of({"TST-0001": ManufactureRuleTests.part("TST-0001")}).files)), {})
        parts, baseline = {mp.PN: mock.Mock(shape="kit")}, {mp.PN: {"solids": 2}}
        with mock.patch.object(check.occ, "count", return_value=3):
            row = check.solid_structure(parts, {}, baseline, "v1", {mp.PN: 3})["parts"][mp.PN]
            self.assertEqual((row["ok"], row["expected_solids"], row["baseline"]), (True, 3, "intent.components"))
            row = check.solid_structure(parts, {mp.PN: 2}, baseline, "v1", {mp.PN: 3})["parts"][mp.PN]
            self.assertEqual((row["ok"], row["expected_solids"], row["baseline"]), (False, 2, "expected"))
            row = check.solid_structure(parts, {}, baseline, "v1")["parts"][mp.PN]
            self.assertEqual((row["ok"], row["baseline"]), (False, "v1"))

    def test_an_unsupported_process_builds_without_any_invented_file(self) -> None:
        snapshot = snapshot_of({"TST-0001": ManufactureRuleTests.part("TST-0001", process="other")})
        plans, refused, uncovered = manufacture.plan(snapshot)
        self.assertEqual((plans, refused, sorted(uncovered)), ({}, [], ["TST-0001"]))
        self.assertIn("coverage is unknown", uncovered["TST-0001"])
        entries, rows, reused = manufacture.make(self.hw, self.hw, "v1", plans, uncovered, {}, {}, {}, {}, None, {}, {})
        self.assertEqual({key: entries["TST-0001"][key] for key in ("path", "status", "complete", "coverage")},
                         {"path": None, "status": "warn", "complete": False, "coverage": "unknown"})
        self.assertEqual((rows["TST-0001"]["status"], reused), ("warn", set()))
        self.assertFalse(self.hw.exists(), "no folder or file is written for it")

    def test_mass_comes_from_the_profile_density_and_a_missing_density_stays_unknown(self) -> None:
        mp = projects()
        snapshot, *_ = self.plan(material="PETG")  # a legacy table name: a record version 2 part never reads the table
        mass = build.profile_mass(snapshot, mp.PN, 8000.0, (20.0, 10.0, 5.0), None)
        self.assertAlmostEqual(mass["mass_g"], 8000.0 * mp.DENSITY_G_CM3 / 1000.0, places=9)
        self.assertEqual((mass["density_g_cm3"], mass["centre_of_mass_mm"], mass["mass"]["source"]),
                         (mp.DENSITY_G_CM3, [20.0, 10.0, 5.0], "material_profiles"))
        (density,) = mass["mass"]["densities"]
        self.assertEqual((density["profile"], density["status"], density["basis"], density["value"]),
                         ("tst_polymer", "resolved", "supplier_typical", {"value": mp.DENSITY_G_CM3, "units": "g_cm3"}))
        self.assertEqual(density["sources"][0]["artifact"], mp.SOURCE)
        snapshot, *_ = self.plan(material="PETG", with_density=False)
        mass = build.profile_mass(snapshot, mp.PN, 8000.0, (20.0, 10.0, 5.0), None)
        self.assertEqual((mass["mass_g"], mass["density_g_cm3"], mass["centre_of_mass_mm"], mass["mass"]["status"]),
                         (None, None, None, "unknown"))
        self.assertIn("records no known density", mass["mass"]["reasons"][0])
        documents = mp.documents()
        documents[f"parts/{mp.PN}/part.yaml"]["intent"]["material_profile"] = mp.shared().unknown("grade not chosen")
        mass = build.profile_mass(self.write(documents), mp.PN, 8000.0, (20.0, 10.0, 5.0), None)
        self.assertEqual((mass["mass_g"], mass["mass"]["densities"][0]["status"]), (None, "unknown"))
        self.assertIn("grade not chosen", mass["mass"]["reasons"][0])

    def test_components_of_different_profiles_weight_the_centre_of_mass_and_an_interval_bounds_mass(self) -> None:
        mp, fx = projects(), projects().shared()
        documents = mp.documents()
        documents["manufacturing/materials.yaml"]["profiles"].append(
            fx.material_profile("tst_metal", properties=[fx.material_property("density", 8.0, "g_cm3")]))
        snapshot = self.write(documents)

        def entry(state: str, tab_profile: str) -> dict:
            def facts(profile: str, volume: float, centre: list[float]) -> dict:
                return {"material_profile": profile, "volume_mm3": volume,
                        "members": [{"volume_mm3": volume, "centre_of_volume_mm": centre}]}
            return {"state": {"relationship": state}, "components": {
                "body": facts("tst_polymer", 8000.0, [20.0, 10.0, 5.0]), "tab": facts(tab_profile, 100.0, [65.0, 2.5, 1.0])}}

        mass = build.profile_mass(snapshot, mp.PN, 8100.0, (0.0, 0.0, 0.0), entry("same", "tst_metal"))
        total = 8000.0 * 1.2 / 1000.0 + 100.0 * 8.0 / 1000.0
        self.assertAlmostEqual(mass["mass_g"], total, places=9)
        assert_vectors(self, mass["centre_of_mass_mm"],
                       [(9.6 * c1 + 0.8 * c2) / total for c1, c2 in zip([20.0, 10.0, 5.0], [65.0, 2.5, 1.0])], places=9)
        self.assertIsNone(mass["density_g_cm3"], "no one density describes the part")
        different = build.profile_mass(snapshot, mp.PN, 8100.0, (0.0, 0.0, 0.0), entry("different", "tst_metal"))
        self.assertAlmostEqual(different["mass_g"], total, places=9)
        self.assertIsNone(different["centre_of_mass_mm"])
        self.assertIn("no installed correspondence", different["mass"]["reasons"][0])
        documents["manufacturing/materials.yaml"]["profiles"][0]["properties"][0]["value"]["value"] = \
            fx.quantity(lower=1.1, upper=1.3, units="g_cm3")
        bounded = build.profile_mass(self.write(documents), mp.PN, 8000.0, (20.0, 10.0, 5.0), None)
        self.assertEqual((bounded["mass_g"], bounded["centre_of_mass_mm"]), (None, None))
        self.assertAlmostEqual(bounded["mass"]["value_g"]["lower"], 8.8, places=9)
        self.assertAlmostEqual(bounded["mass"]["value_g"]["upper"], 10.4, places=9)

    def test_the_legacy_density_table_is_a_labelled_density_only_estimate(self) -> None:
        import yaml

        table = yaml.safe_load(build.LEGACY_DENSITIES.read_text(encoding="utf-8"))
        self.assertEqual(sorted(table), ["densities_g_cm3"], "the table holds densities and nothing else")
        mass = build.legacy_mass("PETG", 1000.0, (1.0, 2.0, 3.0), build.load_densities())
        self.assertAlmostEqual(mass["mass_g"], 1.27, places=12)
        self.assertEqual((mass["mass"]["source"], mass["mass"]["basis"], mass["centre_of_mass_mm"]),
                         ("legacy_density_table", "estimate", [1.0, 2.0, 3.0]))
        self.assertIn("never supplies strength", mass["mass"]["note"])
        unknown = build.legacy_mass("TST unobtainium", 1000.0, (1.0, 2.0, 3.0), build.load_densities())
        self.assertEqual((unknown["mass_g"], unknown["centre_of_mass_mm"], unknown["mass"]["status"]),
                         (None, None, "unknown"))

    def test_totals_give_an_assembly_centre_of_mass_only_when_every_occurrence_has_one(self) -> None:
        metrics = {"TST-0001": {"volume_mm3": 10.0, "mass_g": 2.0}, "TST-0002": {"volume_mm3": 5.0, "mass_g": None}}
        placed = {"a": {"PN": "TST-0001", "centre_of_mass_mm": [0.0, 0.0, 0.0]},
                  "b": {"PN": "TST-0001", "centre_of_mass_mm": [10.0, 0.0, 0.0]}}
        totals = build.totals(metrics, placed, {"TST-0001": 2})
        self.assertEqual((totals["mass_g"], totals["centre_of_mass_mm"]), (4.0, [5.0, 0.0, 0.0]))
        totals = build.totals(metrics, {**placed, "c": {"PN": "TST-0002", "centre_of_mass_mm": None}},
                              {"TST-0001": 2, "TST-0002": 1})
        self.assertEqual((totals["mass_g"], totals["centre_of_mass_mm"], totals["occurrences_without_density"]),
                         (4.0, None, ["c"]))

    def test_diff_names_an_intent_or_component_change(self) -> None:
        entry = {"intent": {"inputs_sha256": "a" * 64}, "components": {"main": {"multiplicity": 1}}}
        changed = diff.compare_manufacturing(
            {"TST-0001": entry, "TST-0002": entry},
            {"TST-0001": {**entry, "intent": {"inputs_sha256": "b" * 64}},
             "TST-0002": {**entry, "components": {"main": {"multiplicity": 2}}}})
        self.assertEqual((changed["parts"]["TST-0001"]["changed"], changed["parts"]["TST-0002"]["changed"]),
                         (["intent"], ["components"]))



class VersionLabelTests(unittest.TestCase):
    def test_a_label_at_or_below_the_newest_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            (hw / "versions" / "v3").mkdir(parents=True)
            (hw / "versions" / "v3" / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(build.UsageError, "pick v4"):
                build.run(hw, "v2", "auto", [])

    @staticmethod
    def published(hw: Path) -> Path:
        """A sealed v1 under hw/versions/, as a finished run leaves it."""
        final = hw / "versions" / "v1"
        (final / "parts").mkdir(parents=True)
        (final / "manifest.json").write_text('{"label": "v1"}', encoding="utf-8")
        (final / "parts" / "TST-0001.step").write_bytes(b"ISO-10303-21; v1")
        return final

    @staticmethod
    def tree(folder: Path) -> dict[str, bytes]:
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}

    @staticmethod
    def exists_message(final: Path) -> str:
        return f"^version v1 already exists at {re.escape(str(final))}; .*choose a new label$"

    def test_an_existing_label_is_refused_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp).resolve()  # run resolves hw/; the message names the resolved version path
            final = self.published(hw)
            before = self.tree(final)
            with self.assertRaisesRegex(build.UsageError, self.exists_message(final)):
                build.run(hw, "v1", "auto", [])
            self.assertEqual(self.tree(final), before)
            self.assertEqual(sorted(p.name for p in (hw / "versions").iterdir()), ["v1"], "no staging left behind")

    def test_a_version_published_after_the_check_is_kept(self) -> None:
        """Another run sealed the same label while this one built: the final rename refuses, never merges."""
        with tempfile.TemporaryDirectory() as tmp:
            hw = Path(tmp)
            work = hw / "versions" / f".v1.partial-{os.getpid()}"
            (work / "parts").mkdir(parents=True)
            (work / "manifest.json").write_text('{"label": "v1", "run": "late"}', encoding="utf-8")
            final = self.published(hw)
            before = self.tree(final)
            with self.assertRaisesRegex(build.UsageError, self.exists_message(final)):
                build.publish(work, final, "v1")
            self.assertEqual(self.tree(final), before)


class DiffRuleTests(unittest.TestCase):
    def test_a_kernel_failure_is_an_error_not_a_change(self) -> None:
        metrics = {"volume_mm3": 100.0, "bbox_mm": {"min": [0.0] * 3, "max": [1.0] * 3, "size": [1.0] * 3}, "solids": 1}
        with mock.patch.object(occ, "occupied", side_effect=occ.GeometryError("occupied-volume partition failed")):
            row = diff.compare_part(metrics, metrics, object(), object())
        self.assertEqual((row["status"], row["a_only_mm3"]), ("error", None))
        self.assertIn("partition failed", row["error"])


FAKE_RUNNER = """# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import atexit, json, pathlib, sys, time
request = json.load(sys.stdin)
part = request["parts"][0]
{body}
pathlib.Path(request["result"]).write_text(json.dumps({{"kernel": "fake", "tools": {{}}, "import_s": 0.0, "parts": {{}}}}))
"""
FAKE_BODIES = {
    "waits_for_peer": "deadline = time.monotonic() + 30\n"
                      "while not pathlib.Path(part['marker']).exists():\n"
                      "    if time.monotonic() > deadline:\n"
                      "        sys.exit(3)\n"
                      "    time.sleep(0.05)",
    "signals_peer": "pathlib.Path(part['marker']).touch()",
    "fails": "sys.exit(2)",
    "writes_late": "time.sleep(4)\npathlib.Path(part['step']).parent.mkdir(parents=True, exist_ok=True)\n"
                   "pathlib.Path(part['step']).write_text('late')",
    # OCCT's STEP-writer statistics on stdout, around a decoy JSON line, and an unterminated line at exit.
    "stdout_noise": "print('****** Statistics on Transfer (Write) ******')\n"
                    "print(json.dumps({'kernel': 'decoy', 'tools': {}, 'import_s': 0.0, 'parts': {}}))\n"
                    "atexit.register(sys.stdout.write, '****** Transfer Mode = 0  I.E.  As Is ******')",
    "exits_without_result": "sys.exit(0)",
}


@unittest.skipUnless(shutil.which("uv"), "uv is required to start the runners")
class RunnerLifecycleTests(unittest.TestCase):
    """`run_kernels` with stand-in runners: they run at once, a failure stops the others, and the
    result comes from the file the request names, whatever the runner prints."""

    def runners(self, root: Path, cadquery: str, build123d: str) -> dict[str, Path]:
        paths = {}
        for kernel, body in (("cadquery", cadquery), ("build123d", build123d)):
            paths[kernel] = root / f"{body}.py"
            paths[kernel].write_text(FAKE_RUNNER.format(body=FAKE_BODIES[body]), encoding="utf-8")
        return paths

    def test_each_runner_gets_its_request_without_waiting_for_the_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = str(root / "build123d-started")
            with mock.patch.dict(build.RUNNERS, self.runners(root, "waits_for_peer", "signals_peer")):
                results = build.run_kernels({"cadquery": [{"pn": "TST-0001", "marker": marker}],
                                             "build123d": [{"pn": "TST-0002", "marker": marker}]})
        self.assertEqual(sorted(results), ["build123d", "cadquery"])

    def test_a_failed_runner_stops_the_other_before_it_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            late = root / ".v1.partial" / "parts" / "TST-0002.step"
            with mock.patch.dict(build.RUNNERS, self.runners(root, "fails", "writes_late")):
                started = time.monotonic()
                with self.assertRaisesRegex(build.UsageError, "cadquery runner exited 2"):
                    build.run_kernels({"cadquery": [{"pn": "TST-0001"}],
                                       "build123d": [{"pn": "TST-0002", "step": str(late)}]})
                self.assertLess(time.monotonic() - started, 4.0)
            time.sleep(6)
            self.assertFalse(late.parent.exists(), "the stopped runner recreated the partial folder")

    def test_the_result_file_is_immune_to_stray_stdout_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(build.RUNNERS, self.runners(Path(tmp), "stdout_noise", "stdout_noise")):
                results = build.run_kernels({"cadquery": [{"pn": "TST-0001"}], "build123d": [{"pn": "TST-0002"}]})
        for kernel, result in results.items():
            with self.subTest(kernel=kernel):
                self.assertEqual(result["kernel"], "fake")
                self.assertIn("Transfer Mode = 0", result["console_tail"])

    def test_a_runner_that_exits_without_its_result_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(build.RUNNERS, self.runners(Path(tmp), "exits_without_result", "signals_peer")):
                with self.assertRaisesRegex(build.UsageError, "cadquery runner wrote no result"):
                    build.run_kernels({"cadquery": [{"pn": "TST-0001"}]})


@unittest.skipUnless(shutil.which("uv"), "uv is required for the trimesh environment")
class MeshWatertightTests(unittest.TestCase):
    """A sphere-pole triangle collapses when welded: it must neither fail a closed mesh nor close an open one."""

    def test_collapsed_triangles_are_dropped_and_holes_still_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "mesh_probe.py"), tmp],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        report = json.loads(proc.stdout)
        self.assertEqual((report["TST-0001"]["watertight"], report["TST-0001"]["collapsed_triangles"]), (True, 1))
        self.assertEqual((report["TST-0002"]["watertight"], report["TST-0002"]["collapsed_triangles"]), (False, 1))


@unittest.skipUnless(shutil.which("uv"), "uv is required for the OCP environment")
class HoleMetricsTests(unittest.TestCase):
    """Bores are read from the STEP read-back: a through and a blind hole are listed, a boss is not."""

    def test_through_and_blind_holes_are_listed_and_a_boss_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "holes_probe.py"), tmp],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        report = json.loads(proc.stdout)
        assert_same_holes(self, report["TST-0001"], [
            {"id": "H1", "diameter_mm": 3.0, "axis": [0.0, 0.0, -1.0], "position": [30.0, 15.0, 8.0],
             "depth_mm": 4.0, "through": False},
            {"id": "H2", "diameter_mm": 5.0, "axis": [0.0, 0.0, -1.0], "position": [10.0, 15.0, 8.0],
             "depth_mm": 8.0, "through": True},
        ], places=6)
        self.assertEqual(report["TST-0002"], [])


@unittest.skipUnless(shutil.which("uv"), "uv is required for the OCP environment")
class ContainmentTests(unittest.TestCase):
    """A part buried in another, no faces touching, interferes whether the container reads back as
    a SOLID or as a COMPOUND, in either argument order (fixtures/containment_probe.py)."""

    def test_a_part_buried_in_a_solid_or_a_compound_part_interferes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "containment_probe.py"), tmp],
                                  capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        report = json.loads(proc.stdout)
        self.assertEqual(report["shape_types"], {"TST-0301": "TopAbs_COMPOUND", "TST-0302": "TopAbs_SOLID",
                                                 "TST-0303": "TopAbs_SOLID"})
        for container in ("solid", "compound"):
            for order in ("container_first", "buried_first"):
                with self.subTest(container=container, order=order):
                    row = report[container][order]
                    self.assertEqual((row["clearance_mm"], row["status"]), (0.0, "fail"))
                    self.assertAlmostEqual(row["interference_mm3"], 8.0, places=6)


@unittest.skipUnless(shutil.which("uv"), "uv is required for the OCP environment")
class ComponentBindingTests(unittest.TestCase):
    """cadmodel.components.bind on box compounds (fixtures/components_probe.py): every solid binds exactly
    once by witness or feature locator, equivalent members group, and ambiguous identity is refused."""

    def test_every_solid_binds_once_and_ambiguous_identity_is_refused(self) -> None:
        proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "components_probe.py")],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        report = json.loads(proc.stdout)
        bound = {name: {ident: (row["binding"], row["members"], row["check"]) for ident, row in case["bound"].items()}
                 for name, case in report.items() if "bound" in case}
        self.assertEqual(bound, {
            "sole_solid": {"main": ("implicit", [8000.0], True)},
            "two_loose_solids": {"body": ("witness", [8000.0], True), "tab": ("witness", [100.0], True)},
            "locator": {"body": ("witness", [8000.0], True), "tab": ("locator", [100.0], True)},
            "equivalent_group": {"body": ("witness", [8000.0], True), "tab": ("witness", [100.0, 100.0], True)}})
        refused = {name: case["refused"] for name, case in report.items() if "refused" in case}
        expected = {"undeclared_solids": "declares none", "unbound_solid": "is bound by no component",
                    "witness_on_surface": "of the surface of", "witness_outside": "inside no solid",
                    "duplicate": "both bind", "missing_feature": "has no locator", "overlap": "overlap by 50 mm3",
                    "non_equivalent_group": "non-equivalent grouping", "ambiguous_group": "ambiguous grouping"}
        self.assertEqual(sorted(refused), sorted(expected))
        for name, text in expected.items():
            self.assertIn(text, refused[name], name)


class LooseComponentChecks:
    """Plan section 11 row 8 in one kernel: a part of two loose solids whose smaller one no route operation
    plans is still bound, exported and judged on its own; non-equivalent grouping and an ambiguous
    witness are refused and seal nothing; an equivalent group exports every member."""
    kernel = ""
    TABS = [[60.0, 10.0, 5.0, 2.0]]

    @classmethod
    def setUpClass(cls) -> None:
        mp = projects()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hw = hw = mp.loose_project(Path(cls.tmp.name) / "hw", cls.kernel, cls.TABS, parts_of=mp.components(),
                                       print_components=["body"])
        cls.runs = {"v1": cli("run", str(hw), "--label", "v1")}
        mp.set_model(hw, cls.kernel, cls.TABS + [[80.0, 8.0, 5.0, 2.0]])
        mp.rewrite_part(hw, parts_of=mp.components(tab_multiplicity=2), print_components=["body"])
        cls.runs["non-equivalent"] = cli("run", str(hw), "--label", "v2")
        mp.set_model(hw, cls.kernel, cls.TABS)
        mp.rewrite_part(hw, parts_of=mp.components(tab_witness=[60.0, 2.5, 1.0]), print_components=["body"])
        cls.runs["ambiguous"] = cli("run", str(hw), "--label", "v2")
        cls.states = version_state(hw)
        mp.set_model(hw, cls.kernel, cls.TABS + [[80.0, 10.0, 5.0, 2.0]])
        mp.rewrite_part(hw, parts_of=mp.components(tab_multiplicity=2), print_components=["body"])
        cls.runs["v2"] = cli("run", str(hw), "--label", "v2")
        cls.checks = {label: cli("check", label, "--hw", str(hw)) for label in ("v1", "v2")}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def entry(self, label: str) -> dict:
        manifest = json.loads((self.hw / "versions" / label / "manifest.json").read_text(encoding="utf-8"))
        return manifest["manufacturing"]["TST-0001"]

    def test_the_unplanned_smaller_solid_is_bound_exported_and_judged_on_its_own(self) -> None:
        proc, result, _ = self.runs["v1"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        made = result["manufacturing"]["TST-0001"]
        self.assertEqual((made["components"], made["status"], made["complete"]), (["body", "tab"], "warn", False))
        entry = self.entry("v1")
        self.assertEqual(entry["component_outputs"], ["components/body/part.step", "components/body/part.stl",
                                                      "components/tab/part.step", "components/tab/part.stl"])
        body, tab = entry["components"]["body"], entry["components"]["tab"]
        self.assertEqual((body["operations"], body["planned"], body["placement"]["pose"]), (["print"], True, "approved"))
        self.assertEqual((tab["operations"], tab["planned"], tab["placement"]["pose"]), ([], False, "unknown"))
        self.assertAlmostEqual(tab["volume_mm3"], 100.0, places=6)
        folder = self.hw / "versions" / "v1" / entry["path"]
        for name in entry["component_outputs"]:
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), entry["files"][name], name)
        metrics = json.loads((self.hw / "versions" / "v1" / "metrics.json").read_text(encoding="utf-8"))["parts"]["TST-0001"]
        self.assertAlmostEqual(metrics["mass_g"], 8100.0 * projects().DENSITY_G_CM3 / 1000.0, places=6)
        proc, checked, _ = self.checks["v1"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        rows = checked["checks"]["manufacturing"]["parts"]["TST-0001"]["checks"]["components"]["components"]
        self.assertEqual(rows["body"]["status"], "pass")
        self.assertEqual((rows["tab"]["status"], rows["tab"]["checks"]["placement"]["status"]), ("warn", "warn"))
        self.assertEqual((rows["tab"]["checks"]["step"]["solids"], rows["tab"]["checks"]["binding"]["status"]), (1, "pass"))
        self.assertEqual(checked["checks"]["artifact_integrity"]["status"], "pass")
        self.assertFalse(checked["checks"]["build_volume"]["blocking"])

    def test_non_equivalent_grouping_and_an_ambiguous_witness_are_refused(self) -> None:
        for name, text in (("non-equivalent", "non-equivalent grouping"), ("ambiguous", "of the surface of")):
            with self.subTest(run=name):
                proc, result, _ = self.runs[name]
                self.assertEqual(proc.returncode, 1, proc.stderr[-3000:])
                (found,) = result["findings"]
                self.assertEqual((found["pn"], found["check"]), ("TST-0001", "manufacture"))
                self.assertIn(text, found["message"])
        self.assertEqual(self.states, {"latest": "v1", "versions": ["latest.json", "v1"]})

    def test_an_equivalent_group_exports_every_member(self) -> None:
        """v2 adds a second congruent tab and declares the tab twice: every member is exported, and the
        third solid is the count the record declares, not a change from v1's two."""
        proc, _, _ = self.runs["v2"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        tab = self.entry("v2")["components"]["tab"]
        self.assertEqual(tab["multiplicity"], 2)
        assert_vectors(self, [member["volume_mm3"] for member in tab["members"]], [100.0, 100.0], places=6)
        proc, checked, _ = self.checks["v2"]
        self.assertEqual(proc.returncode, 0, json.dumps(checked.get("blocking_failures")) + proc.stderr[-2000:])
        structure = checked["checks"]["solid_structure"]["parts"]["TST-0001"]
        self.assertEqual((structure["solids"], structure["expected_solids"], structure["baseline"]),
                         (3, 3, "intent.components"))
        rows = checked["checks"]["manufacturing"]["parts"]["TST-0001"]["checks"]["components"]["components"]
        self.assertEqual(rows["tab"]["checks"]["step"]["solids"], 2)


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class CadQueryLooseComponentTests(LooseComponentChecks, unittest.TestCase):
    kernel = "cadquery"


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class Build123dLooseComponentTests(LooseComponentChecks, unittest.TestCase):
    kernel = "build123d"


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class ManufacturingInputCacheTests(unittest.TestCase):
    """A record version 2 part (CadQuery only): a material, orientation or process change remakes its
    manufacturing outputs and never its B-rep; the post-check ledgers change nothing. v5 changes the
    printed part to a laser cut that is reamed, coated and inspected (plan section 11 row 12, model part)."""

    @classmethod
    def setUpClass(cls) -> None:
        mp = projects()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.hw = hw = mp.loose_project(Path(cls.tmp.name) / "hw", "cadquery", [])
        cls.runs = {"v1": cli("run", str(hw), "--label", "v1")}
        mp.rewrite_part(hw, density=1.3)
        cls.runs["v2"] = cli("run", str(hw), "--label", "v2")
        mp.rewrite_part(hw, density=1.3, orientation=TURNED)
        cls.runs["v3"] = cli("run", str(hw), "--label", "v3")
        for ledger in ("findings.yaml", "evidence.yaml"):
            (hw / "manufacturing" / ledger).write_text("dispositions: []\nentries: []\n", encoding="utf-8")
        cls.runs["v4"] = cli("run", str(hw), "--label", "v4")
        mp.rewrite_part(hw, density=1.3, process="laser")
        cls.runs["v5"] = cli("run", str(hw), "--label", "v5")
        cls.checks = {label: cli("check", label, "--hw", str(hw)) for label in ("v1", "v5")}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def manifest(self, label: str) -> dict:
        return json.loads((self.hw / "versions" / label / "manifest.json").read_text(encoding="utf-8"))

    def test_each_input_change_remakes_only_the_manufacturing_outputs(self) -> None:
        for label in ("v1", "v2", "v3", "v4", "v5"):
            self.assertEqual(self.runs[label][0].returncode, 0, label + self.runs[label][0].stderr[-3000:])
        self.assertEqual(self.runs["v1"][1]["cache"]["manufacturing"], {"built": 1, "reused": 0})
        for label in ("v2", "v3", "v5"):
            with self.subTest(label=label):
                self.assertEqual(self.runs[label][1]["cache"], {"parts": {"built": 0, "reused": 1},
                                                                "manufacturing": {"built": 1, "reused": 0}})
        self.assertEqual(self.runs["v4"][1]["cache"], {"parts": {"built": 0, "reused": 1},
                                                       "manufacturing": {"built": 0, "reused": 1}})
        masses = {label: self.runs[label][1]["parts"]["TST-0001"]["mass_g"] for label in ("v1", "v2")}
        self.assertAlmostEqual(masses["v1"], 8000.0 * 1.2 / 1000.0, places=6)
        self.assertAlmostEqual(masses["v2"], 8000.0 * 1.3 / 1000.0, places=6)
        v3 = self.manifest("v3")["manufacturing"]["TST-0001"]["placement"]
        for got, want in zip(v3["transform"], TURNED_ROWS):
            assert_vectors(self, got[:3], list(want), places=9)
        v5 = self.manifest("v5")["manufacturing"]["TST-0001"]
        self.assertEqual((v5["outputs"], v5["placement"]["frame"]), (["part.step", "manufacturing.json"], "model"))
        self.assertNotIn("record/manufacturing/evidence.yaml", self.manifest("v4")["record"])

    def test_the_version_snapshot_and_approved_pose_bind_its_evidence(self) -> None:
        manifest = self.manifest("v1")
        self.assertEqual(manifest["record_version"], 2)
        for name in ("requirements.yaml", "manufacturing/materials.yaml", projects().SOURCE):
            self.assertIn(f"record/{name}", manifest["record"])
        self.assertEqual((self.hw / "versions" / "v1" / "record" / projects().SOURCE).read_bytes(), projects().SOURCE_BYTES)
        proc, checked, _ = self.checks["v1"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        section = checked["checks"]["manufacturing"]
        self.assertEqual((section["status"], section["complete"]), ("pass", True))
        row = checked["checks"]["build_volume"]["parts"]["TST-0001"]
        self.assertEqual((row["status"], row["approved"], row["envelope_source"]),
                         ("pass", True, "capability tst_printer envelope"))

    def test_the_check_publishes_the_readback_packet_evidence_reads(self) -> None:
        """check is the cad.readback producer: the passing version's sealed packet passes its expected
        readback obligation, cad-record evidence.py collects it as the version's packet, and a second
        check of the same version reuses the bundle."""
        proc, checked, _ = self.checks["v1"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        self.assertEqual((checked["packet"]["bundle"], checked["packet"]["result"], checked["packet"]["complete"]),
                         ("hw/checks/v1/cad", "pass", True))
        packet = json.loads((self.hw / "checks" / "v1" / "cad" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual([(o["obligation"], o["outcome"]) for o in packet["outcomes"]], [("cad.readback@TST-0001", "pass")])
        self.assertEqual([o["id"] for o in packet["expected"]], ["cad.readback@TST-0001"])
        evidence = sys.modules.get("cad_record_evidence")
        if evidence is None:
            spec = importlib.util.spec_from_file_location("cad_record_evidence",
                                                          HERE.parents[1] / "cad-record" / "scripts" / "evidence.py")
            evidence = importlib.util.module_from_spec(spec)
            sys.modules["cad_record_evidence"] = evidence
            spec.loader.exec_module(evidence)
        manifest_sha = hashlib.sha256((self.hw / "versions" / "v1" / "manifest.json").read_bytes()).hexdigest()
        packets, findings = evidence.collect_packets(self.hw, "v1", manifest_sha)
        self.assertEqual(findings, [])
        self.assertIn("hw/checks/v1/cad", [p.bundle for p in packets])
        again = cli("check", "v1", "--hw", str(self.hw))
        self.assertEqual((again[0].returncode, again[1]["packet"]["reused"]), (0, True), again[0].stderr[-2000:])

    def test_a_multiprocess_route_exports_its_state_once_naming_every_operation_in_order(self) -> None:
        """The laser-cut, reamed, coated and inspected part exports one model-frame part.step and no print
        file or pose: no FDM substitute; its component and intent name every route operation in order."""
        self.assertEqual(self.runs["v5"][0].returncode, 0, self.runs["v5"][0].stderr[-3000:])
        entry = self.manifest("v5")["manufacturing"]["TST-0001"]
        route = projects().MULTIPROCESS_ROUTE
        self.assertEqual([(op["id"], op["kind"]) for op in entry["intent"]["route"]], list(route))
        self.assertEqual(entry["intent"]["route"][0]["capability"], "tst_laser")
        main = entry["components"]["main"]
        self.assertEqual((main["binding"], main["operations"], main["planned"], main["files"]),
                         ("implicit", [ident for ident, _ in route], True, ["part.step"]))
        self.assertEqual((main["placement"]["pose"], main["placement"]["frame"], main["placement"]["build_volume"]),
                         ("model_frame", "model", None))
        self.assertEqual(sorted(p.name for p in (self.hw / "versions" / "v5" / entry["path"]).iterdir()),
                         ["manufacturing.json", "part.step"])
        proc, checked, _ = self.checks["v5"]
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        section = checked["checks"]["manufacturing"]
        self.assertEqual((section["status"], section["complete"]), ("pass", True))
        rows = section["parts"]["TST-0001"]["checks"]
        self.assertNotIn("mesh", rows)
        self.assertEqual(rows["components"]["components"]["main"]["status"], "pass")
        self.assertNotIn("TST-0001", checked["checks"]["build_volume"].get("parts", {}))


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class LegacyOutputTests(unittest.TestCase):
    """Plan section 11 row 19, model part: a record version 1 build stays readable and claims nothing new."""

    def test_a_legacy_build_is_labelled_and_an_older_manifest_still_checks(self) -> None:
        runs = all_runs()["cadquery"]
        _, result, _ = runs["flat"]
        self.assertEqual([p["code"] for p in result["record"]["problems"]], ["record.migration_required"])
        folder = runs["flat-hw"] / "versions" / "v1"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        strap = manifest["manufacturing"][STRAP]
        self.assertEqual((strap["intent"]["record_version"], strap["intent"]["status"]), (1, "unknown"))
        self.assertEqual((strap["placement"]["pose"], strap["placement"]["approved"]), ("unapproved_auto_fit", False))
        main = strap["components"]["main"]  # explicit unknown (legacy): no profile, route or plan is claimed
        self.assertEqual((main["material_profile"], main["operations"], main["planned"], strap["component_identity"]),
                         ({"status": "unknown", "reason": build.record.load_intent().LEGACY_REASON}, None, None,
                          {"status": "bound"}))
        self.assertEqual(json.loads((folder / "metrics.json").read_text(encoding="utf-8"))["mass_source"],
                         "legacy_density_table")
        features = json.loads((folder / manifest["manufacturing_features"]).read_text(encoding="utf-8"))
        self.assertEqual((features["parts"][STRAP]["frame"], features["parts"][STRAP]["correspondence"]["manufacture_only"]),
                         ("manufacture", ["outer_face"]))
        with tempfile.TemporaryDirectory() as tmp:  # strip what sidecar schema 2 adds: an older version's manifest
            older = Path(tmp) / "hw"
            shutil.copytree(runs["flat-hw"], older, symlinks=True)
            path = older / "versions" / "v1" / "manifest.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data["manufacturing"].values():
                for key in ("intent", "components", "component_outputs", "component_identity"):
                    entry.pop(key, None)
                for key in ("frame", "pose", "approved", "operation", "decision", "reason", "build_volume"):
                    entry["placement"].pop(key, None)
            path.chmod(0o644)
            path.write_text(json.dumps(data), encoding="utf-8")
            proc, checked, _ = cli("check", "v1", "--hw", str(older))
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        self.assertEqual((checked["checks"]["manufacturing"]["status"], checked["checks"]["manufacturing"]["complete"]),
                         ("pass", True))


@unittest.skipUnless(shutil.which("uv"), "uv is required for the OCP environment")
class StaticSuspectTests(unittest.TestCase):
    """check's pair geometry checks every kernel reading against its own bounds, as the sweep does:
    a degenerate 0.0 mm reading of two cubes 8.04 mm apart (the stand and clutch of the engine
    project) is a suspect, never an interference (fixtures/suspect_probe.py)."""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(["uv", "run", "--quiet", "--script", str(FIXTURES / "suspect_probe.py"), tmp],
                                  capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr[-2000:]
        cls.report = json.loads(proc.stdout)

    def test_a_resolved_suspect_passes_on_its_bounds_and_is_reported(self) -> None:
        found = self.report["exact_first"]
        self.assertEqual((found["interference"], found["clearance"]), ("pass", "pass"))
        self.assertEqual((found["row"]["method"], found["row"]["clearance_mm"]), ("mesh (exact suspect)", None))
        self.assertLessEqual(found["row"]["lower_mm"], 8.04)
        (suspect,) = found["suspects"]
        self.assertEqual((suspect["distance_mm"], suspect["resolved"]), (0.0, True))
        self.assertEqual([r["label"] for r in suspect["reevaluated"]], ["+0.01 mm", "-0.01 mm", "swapped"])
        self.assertTrue(all(abs(r["distance_mm"] - 8.04) <= 0.01 for r in suspect["reevaluated"]))

    def test_an_unresolved_or_exact_required_suspect_blocks(self) -> None:
        for name in ("exact_every", "required_first"):
            with self.subTest(name=name):
                found = self.report[name]
                self.assertEqual((found["interference"], found["clearance"], found["row"]["method"]),
                                 ("fail", "fail", None))
                self.assertIn("suspect reading 0.0 mm", found["row"]["error"])
        self.assertFalse(self.report["exact_every"]["suspects"][0]["resolved"])

    def test_fast_mode_clears_the_far_pair_without_a_reading(self) -> None:
        found = self.report["fast"]
        self.assertEqual((found["interference"], found["clearance"], found["kernel_evaluations"]), ("pass", "pass", 0))
        self.assertEqual((found["row"]["clearance_mm"], found["suspects"]), (None, []))
        self.assertGreater(found["row"]["lower_mm"], 8.0)


HELPER_MODEL = '''import os
import sys
from pathlib import Path

import cadquery as cq

KERNEL = "cadquery"
PARAMS = {"size": 40.0}
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lib"))
import cad_helper  # noqa: E402 - a project helper below hw/


def build(params):
    if os.environ.get("CAD_TEST_EDIT_SOURCES"):  # an edit that lands while the part builds
        (HERE / "sources" / "mode.txt").write_text("edited\\n", encoding="utf-8")
    return cq.Workplane("XY").box(params["size"] * cad_helper.SCALE, params["size"], 20.0)
'''


@unittest.skipUnless(shutil.which("uv"), "uv is required for the kernel environments")
class RebuildTriggerTests(unittest.TestCase):
    """What makes a cached part build again (CadQuery only), run from a copy of the plugin: a changed plugin
    module, a changed project helper the model imports, and a sources/ edit that lands during the build,
    which must leave no record a later run with the same inputs could reuse."""

    @classmethod
    def setUpClass(cls) -> None:
        from cadmodel import scriptenv

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        skills = root / "plugin" / "skills"
        for name in ("cad-model", "cad-motion", "cad-record", "cad-flow", "cad-drawings"):
            shutil.copytree(HERE.parents[1] / name, skills / name, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        copy = skills / "cad-model" / "scripts" / "cad_model.py"
        hw = two_part_project(root, "cadquery")
        set_arm(hw, "cadquery")
        (hw / "lib").mkdir()
        (hw / "lib" / "cad_helper.py").write_text("SCALE = 1.0\n", encoding="utf-8")
        part = hw / "parts" / "TST-0001"
        (part / "model.py").write_text(HELPER_MODEL, encoding="utf-8")
        mode = part / "sources" / "mode.txt"
        mode.parent.mkdir()
        mode.write_text("edit\n", encoding="utf-8")

        def run(label: str, **env: str) -> tuple[int, dict, str]:
            proc = subprocess.run(scriptenv.script_command(copy, "run", str(hw), "--label", label),
                                  capture_output=True, text=True, check=False, env={**os.environ, **env})
            return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else {}), proc.stderr

        cls.runs = {"v1": run("v1")}
        occ_copy = skills / "cad-model" / "scripts" / "cadmodel" / "occ.py"
        occ_copy.write_text(occ_copy.read_text(encoding="utf-8") + "\n# a plugin edit\n", encoding="utf-8")
        cls.runs["plugin"] = run("v2")
        (hw / "lib" / "cad_helper.py").write_text("SCALE = 1.0  # a helper edit\n", encoding="utf-8")
        cls.runs["helper"] = run("v3")
        mode.write_text("race\n", encoding="utf-8")  # new inputs, so the next run keys a new build
        cls.runs["race"] = run("v4", CAD_TEST_EDIT_SOURCES="1")
        cls.edited = mode.read_text(encoding="utf-8")
        mode.write_text("race\n", encoding="utf-8")  # back to the bytes the racing run keyed its build by
        cls.runs["after-race"] = run("v4")
        cls.runs["unchanged"] = run("v5")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def builds(self, name: str) -> dict[str, str]:
        code, result, stderr = self.runs[name]
        self.assertEqual(code, 0, stderr[-3000:])
        return {pn: part["build"] for pn, part in result["parts"].items()}

    def test_a_changed_plugin_module_rebuilds_the_parts(self) -> None:
        self.assertEqual(self.builds("v1"), {"TST-0001": "built", "TST-0002": "built"})
        self.assertEqual(self.builds("plugin"), {"TST-0001": "built", "TST-0002": "built"})
        self.assertEqual(self.runs["plugin"][1]["cache"]["manufacturing"], {"built": 2, "reused": 0})

    def test_a_changed_project_helper_rebuilds_the_part_that_imports_it(self) -> None:
        self.assertEqual(self.builds("helper")["TST-0001"], "built")

    def test_a_sources_edit_during_the_build_leaves_no_reusable_record(self) -> None:
        code, _, stderr = self.runs["race"]
        self.assertEqual((code, self.edited), (2, "edited\n"), stderr[-3000:])
        self.assertIn("part sources changed while the version was built", stderr)
        self.assertIn("['TST-0001'] was not cached", stderr)
        self.assertEqual(self.builds("after-race"), {"TST-0001": "built", "TST-0002": "reused"})
        self.assertEqual(self.builds("unchanged"), {"TST-0001": "reused", "TST-0002": "reused"})


def other_filesystem() -> Path | None:
    """A writable folder on another filesystem than the temporary folder: CAD_TEST_OTHER_FS, else /dev/shm."""
    here = os.stat(tempfile.gettempdir()).st_dev
    for candidate in (os.environ.get("CAD_TEST_OTHER_FS"), "/dev/shm"):
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK) and os.stat(candidate).st_dev != here:
            return Path(candidate)
    return None


@unittest.skipUnless(shutil.which("uv") and other_filesystem(),
                     "needs a writable folder on another filesystem: CAD_TEST_OTHER_FS, or /dev/shm on Linux")
class CrossFilesystemTests(unittest.TestCase):
    """The build_manufacture states folder (TMPDIR) on another filesystem than hw/: no hard link joins them,
    so the store copies there and back, and both the build and its reuse seal verified versions."""

    def test_a_states_folder_on_another_filesystem_builds_and_reuses_through_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory(dir=other_filesystem()) as states:
            hw = flat_project(Path(tmp), "cadquery")
            self.assertNotEqual(os.stat(states).st_dev, os.stat(hw).st_dev)
            results = []
            for label in ("v1", "v2"):
                if label == "v2":  # only the strap's tolerance: the definitions and the strap's state are reused
                    set_strap(hw, "cadquery", manufacture=FLAT_STATE.replace("1.0e-6", "2.0e-6"))
                proc = subprocess.run(["uv", "run", "--quiet", "--script", str(CLI), "run", str(hw), "--label", label],
                                      capture_output=True, text=True, check=False, env={**os.environ, "TMPDIR": states})
                self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-3000:])
                results.append(json.loads(proc.stdout))
            self.assertEqual(results[1]["cache"], {"parts": {"built": 0, "reused": 2},
                                                   "manufacturing": {"built": 1, "reused": 1}})
            for label in ("v1", "v2"):
                self.assertEqual(build.record.load_layout().verify(hw / "versions" / label), [], label)


if __name__ == "__main__":
    unittest.main()
