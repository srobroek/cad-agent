# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "pyyaml>=6.0"]
# ///
"""Bind the solids of small box compounds to declared components with cadmodel.components.bind.

Solids: BODY 0..40 x 0..20 x 0..10; TAB 60..70 x 0..5 x 0..2; TWIN, the same tab at x 80..90; ODD, an
8 mm long tab at x 80..88; OVERLAP, a box sharing x 35..45 with BODY. A case is a shape and its
declared components (id, witness or feature locator, multiplicity).
Usage: uv run --script components_probe.py
stdout: {case: {"bound": {id: {"binding", "members": [volume_mm3...]}}} or {"refused": message}}.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from cadmodel import components, occ  # noqa: E402


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


BODY, TAB = box(0, 0, 0, 40, 20, 10), box(60, 0, 0, 70, 5, 2)
TWIN, ODD, OVERLAP = box(80, 0, 0, 90, 5, 2), box(80, 0, 0, 88, 5, 2), box(35, 0, 0, 45, 5, 2)
TAB_TOP = {"tab_top": {"kind": "face", "frame": "assembly_zero",
                       "patches": [{"support_type": "plane", "point_mm": [65.0, 2.5, 2.0], "normal": [0.0, 0.0, 1.0]}]}}


def spec(ident: str, witness=None, locator=None, multiplicity: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=ident, witness=tuple(witness) if witness else None, locator=locator,
                           multiplicity=multiplicity)


body, tab = spec("body", (20, 10, 5)), spec("tab", (65, 2.5, 1))
CASES = {
    "sole_solid": (BODY, [], None),
    "two_loose_solids": (occ.compound([BODY, TAB]), [body, tab], None),
    "locator": (occ.compound([BODY, TAB]), [body, spec("tab", locator={"feature": "tab_top"})], TAB_TOP),
    "undeclared_solids": (occ.compound([BODY, TAB]), [], None),
    "unbound_solid": (occ.compound([BODY, TAB]), [body], None),
    "witness_on_surface": (occ.compound([BODY, TAB]), [body, spec("tab", (60, 2.5, 1))], None),
    "witness_outside": (occ.compound([BODY, TAB]), [body, spec("tab", (75, 2.5, 1))], None),
    "duplicate": (occ.compound([BODY, TAB]), [body, spec("again", (10, 10, 5))], None),
    "missing_feature": (occ.compound([BODY, TAB]), [body, spec("tab", locator={"feature": "slot"})], TAB_TOP),
    "overlap": (occ.compound([BODY, OVERLAP]), [body, spec("lap", (40, 2.5, 1))], None),
    "equivalent_group": (occ.compound([BODY, TAB, TWIN]), [body, spec("tab", (65, 2.5, 1), multiplicity=2)], None),
    "non_equivalent_group": (occ.compound([BODY, TAB, ODD]), [body, spec("tab", (65, 2.5, 1), multiplicity=2)], None),
    "ambiguous_group": (occ.compound([BODY, TAB, TWIN, box(100, 0, 0, 110, 5, 2)]),
                        [body, spec("tab", (65, 2.5, 1), multiplicity=2)], None),
}

report = {}
for name, (shape, declared, features) in CASES.items():
    try:
        bound = components.bind(shape, declared, features)
    except components.BindingError as error:
        report[name] = {"refused": str(error)}
        continue
    report[name] = {"bound": {item.id: {"binding": item.binding, "members": [round(m.volume_mm3, 6) for m in item.members],
                                        "check": components.check_points(item.shape, item.points,
                                                                         "in" if item.binding == "witness" else "on")}
                              for item in bound}}
print(json.dumps(report))
