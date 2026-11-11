"""Test fixture: one part made of loose solids, build123d. A 40 x 20 x 10 body at the origin and one
box per PARAMS["tabs"] entry [x0, size x, size y, size z] on the same base plane, never touching it."""

from build123d import Align, Box, Pos

from cadmodel import occ

KERNEL = "build123d"
PARAMS = {"tabs": [[60.0, 10.0, 5.0, 2.0]]}
LOW = (Align.MIN, Align.MIN, Align.MIN)


def build(params: dict):
    solids = [Box(40.0, 20.0, 10.0, align=LOW)]
    solids += [Pos(x0, 0.0, 0.0) * Box(sx, sy, sz, align=LOW) for x0, sx, sy, sz in params["tabs"]]
    shapes = [occ.to_topods(solid) for solid in solids]
    return shapes[0] if len(shapes) == 1 else occ.compound(shapes)
