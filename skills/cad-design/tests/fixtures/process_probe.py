# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "pyyaml>=6.0"]
# ///
"""Probe small OCP shapes with form_check's manufacturing-state probe and print the facts per case.

Shapes (mm, the frame they are probed in):
  shelf   a 10 x 10 x 30 post on the bed with a 20 x 10 x 5 shelf at z 20..25 on its +X side
  seat    a 30 x 20 x 20 block on the bed with a horizontal 10 mm bore along Y at x 15, z 10
  pocket  a 40 x 30 x 25 block with a 20 x 10 pocket 20 deep from the top, its vertical corners R2
  plate   a 40 x 30 x 3 plate with a through 6 mm hole at x 20, y 15
  pair    two 10 x 10 x 2 plates 20 mm apart: one component of two equivalent members
Usage: uv run --script process_probe.py
stdout: {case: probe facts (process_checks Geometry)}.
"""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cad-model" / "scripts"))
spec = importlib.util.spec_from_file_location("cad_design_form_check", SCRIPTS / "form_check.py")
form_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(form_check)

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse  # noqa: E402
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder  # noqa: E402
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402

from cadmodel import occ  # noqa: E402


def box(x0, y0, z0, x1, y1, z1):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def cylinder(point, direction, radius, height):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*point), gp_Dir(*direction)), radius, height).Shape()


def fuse(a, b):
    return BRepAlgoAPI_Fuse(a, b).Shape()


def cut(a, b):
    return BRepAlgoAPI_Cut(a, b).Shape()


def rounded_pocket(x0, y0, z0, x1, y1, z1, radius):
    """A pocket tool box whose vertical edges are rounded to `radius`."""
    shape = box(x0, y0, z0, x1, y1, z1)
    fillet = BRepFilletAPI_MakeFillet(shape)
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        edge = occ.downcast(explorer.Current(), "EDGE")
        low, high = occ.bbox(edge)
        if abs(low[0] - high[0]) < 1e-9 and abs(low[1] - high[1]) < 1e-9:
            fillet.Add(radius, edge)
        explorer.Next()
    return fillet.Shape()


def plane(ident, point, normal, kind="face"):
    return {ident: {"kind": kind, "frame": "manufacture",
                    "patches": [{"support_type": "plane", "point_mm": list(point), "normal": list(normal)}]}}


def bore(ident, point, normal, kind="bore"):
    return {ident: {"kind": kind, "frame": "manufacture",
                    "patches": [{"support_type": "cylinder", "point_mm": list(point), "normal": list(normal)}]}}


CASES = {
    "shelf": (fuse(box(0, 0, 0, 10, 10, 30), box(10, 0, 20, 30, 10, 25)), "bed",
              {**plane("shelf_underside", (20, 5, 20), (0, 0, -1)), **plane("shelf_top", (20, 5, 25), (0, 0, 1))}, {}),
    "seat": (cut(box(0, 0, 0, 30, 20, 20), cylinder((15, -1, 10), (0, 1, 0), 5, 22)), "bed",
             bore("bearing_seat", (15, 10, 15), (0, 0, -1)), {}),
    "pocket": (cut(box(0, 0, 0, 40, 30, 25), rounded_pocket(10, 10, 5, 30, 20, 26, 2.0)), "model",
               plane("pocket_floor", (20, 15, 5), (0, 0, 1)), {"directions": {"+z": [0.0, 0.0, 1.0]}}),
    "plate": (cut(box(0, 0, 0, 40, 30, 3), cylinder((20, 15, -1), (0, 0, 1), 3, 5)), "model",
              bore("ream_bore", (23, 15, 1.5), (-1, 0, 0)), {"directions": {"+z": [0.0, 0.0, 1.0], "-x": [-1.0, 0.0, 0.0]}}),
    "pair": (occ.compound([box(0, 0, 0, 10, 10, 2), box(30, 0, 0, 40, 10, 2)]), "bed", {}, {}),
}

report = {}
for name, (shape, frame, locators, plan) in CASES.items():
    report[name] = form_check.probe(shape, frame=frame, locators=locators, plan=plan)
print(json.dumps(report))
