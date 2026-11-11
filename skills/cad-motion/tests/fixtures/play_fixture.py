# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "pyyaml>=6.0"]
# ///
"""Write the mechanism-play test geometry as STEP AP242.

Usage: uv run --script play_fixture.py OUT_DIR

Every part is modelled in place (assembly zero, identity placement), millimetres:
  TST-0201 base   x -10..45, y -25..25, z -10..0: the plate the arm pivots on (J-001, about z at the origin)
  TST-0202 arm    x 5..40, y -3..3, z 2..6: swings +-20 deg about z
  TST-0203 bridge x 15..25, y -20..20, z 6.5..9.5, fixed to the base (J-002): the arm passes under it with
                  0.5 mm to spare, so axial play of 1 mm (0.5 each side) or more closes the gap

OUT_DIR/parts/<PN>.step.
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

TITLES = {"TST-0201": "Base", "TST-0202": "Arm", "TST-0203": "Bridge"}


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def shapes() -> dict[str, object]:
    return {"TST-0201": box(-10, -25, -10, 45, 25, 0), "TST-0202": box(5, -3, 2, 40, 3, 6),
            "TST-0203": box(15, -20, 6.5, 25, 20, 9.5)}


def main(out: Path) -> int:
    (out / "parts").mkdir(parents=True, exist_ok=True)
    for pn, shape in shapes().items():
        pose.write_part_step(out / "parts" / f"{pn}.step", pose.Part(pn, f"{pn} {TITLES[pn]}", shape))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
