"""Test fixture: one part made of loose solids, CadQuery. A 40 x 20 x 10 body at the origin and one
box per PARAMS["tabs"] entry [x0, size x, size y, size z] on the same base plane, never touching it."""

import cadquery as cq

from cadmodel import occ

KERNEL = "cadquery"
PARAMS = {"tabs": [[60.0, 10.0, 5.0, 2.0]]}


def build(params: dict):
    solids = [cq.Solid.makeBox(40.0, 20.0, 10.0)]
    solids += [cq.Solid.makeBox(sx, sy, sz, cq.Vector(x0, 0.0, 0.0)) for x0, sx, sy, sz in params["tabs"]]
    return solids[0] if len(solids) == 1 else occ.compound([solid.wrapped for solid in solids])
