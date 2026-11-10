# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "pyyaml>=6.0"]
# ///
"""Write the assembly-access test geometry (plan section 11 row 15) as STEP AP242.

Usage: uv run --script access_fixture.py OUT_DIR

Every part is modelled in place (assembly_zero, identity placement), millimetres:
  TST-0101 housing   x, y -30..30, z 0..20; a bearing bore r 11 from z 8 up, and a screw hole r 1.6
                     at (20, 0) from z 10 up
  TST-0102 bearing   a ring r 5..11, z 8..16, seated on the bore floor
  TST-0103 cover     x, y -30..30, z 20..24; a clearance hole r 1.8 at (20, 0)
  TST-0104 screw     shank r 1.5, z 12..24, head r 2.8, z 24..27, at (20, 0)
  TST-0105 bracket   x 14..40, y -8..8, z 40..44 over the screw, with a hole r 3.5 at (20, 0): the screw
                     head (r 2.8) passes it, a socket (r 4.5) does not
socket.step is the tool envelope in the screw's frame at the engaged pose: a hex-socket stand-in, a
tube r 3..4.5 over the head from z 24.5 to 30, then a bar r 4.5 up to z 80.

OUT_DIR/parts/<PN>.step and OUT_DIR/socket.step.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

POSE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "pose.py"
_spec = importlib.util.spec_from_file_location("cad_motion_pose", POSE_PATH)
assert _spec and _spec.loader
pose = importlib.util.module_from_spec(_spec)
sys.modules["cad_motion_pose"] = pose
_spec.loader.exec_module(pose)

Z = (0.0, 0.0, 1.0)
TITLES = {"TST-0101": "Housing", "TST-0102": "Bearing", "TST-0103": "Cover", "TST-0104": "Cover screw",
          "TST-0105": "Bracket"}


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def cylinder(x: float, y: float, z: float, radius: float, height: float):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*Z)), radius, height).Shape()


def fuse(*shapes):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    result = shapes[0]
    for shape in shapes[1:]:
        result = BRepAlgoAPI_Fuse(result, shape).Shape()
    return result


def cut(shape, *tools):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    for tool in tools:
        shape = BRepAlgoAPI_Cut(shape, tool).Shape()
    return shape


def shapes() -> dict[str, object]:
    return {
        "TST-0101": cut(box(-30, -30, 0, 30, 30, 20), cylinder(0, 0, 8, 11, 13), cylinder(20, 0, 10, 1.6, 11)),
        "TST-0102": cut(cylinder(0, 0, 8, 11, 8), cylinder(0, 0, 7, 5, 10)),
        "TST-0103": cut(box(-30, -30, 20, 30, 30, 24), cylinder(20, 0, 19, 1.8, 6)),
        "TST-0104": fuse(cylinder(20, 0, 12, 1.5, 12), cylinder(20, 0, 24, 2.8, 3)),
        "TST-0105": cut(box(14, -8, 40, 40, 8, 44), cylinder(20, 0, 39, 3.5, 6)),
    }


def socket():
    return fuse(cut(cylinder(20, 0, 24.5, 4.5, 5.5), cylinder(20, 0, 24, 3.0, 7)), cylinder(20, 0, 30, 4.5, 50))


def main(out: Path) -> int:
    (out / "parts").mkdir(parents=True, exist_ok=True)
    for pn, shape in shapes().items():
        pose.write_part_step(out / "parts" / f"{pn}.step", pose.Part(pn, f"{pn} {TITLES[pn]}", shape))
    pose.write_part_step(out / "socket.step", pose.Part("socket", "TST hex socket envelope", socket()))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
