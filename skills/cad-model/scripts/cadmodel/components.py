"""The manufacturing components of a custom-made part: every disconnected solid of its manufacturing
state, bound once each to the component its record declares.

A part made of one solid is its own implicit component `main` (cad-record `part_route`). A part made of
several declares them in part.yaml `intent.components`: each binds one solid by an interior `witness`
point or by a `locator` naming a feature whose final-surface patch points lie on that solid, both in
the native frame of the manufacturing state (the model frame for a part made as installed). A
component with `multiplicity` n also takes the n - 1 other unbound solids congruent to its bound one,
so members of a deliberate loose kit are enumerated, never assumed. Identity is never guessed: a witness
on a surface or in two solids, a feature on several solids, two components on one solid, overlapping
solids, a solid no component binds, and a group without exactly its count of congruent members are all
refused (`BindingError`).

Solid indices are the exploration order of this one shape and name nothing outside it: no binding is
stored by index, and a check binds again from the witness and locator points on the sealed files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from cadmodel import occ

IMPLICIT = "main"  # the id of a sole solid's implicit component (cad-record manufacturing_intent.part_route)
BIND_TOLERANCE_MM = 1e-3  # a witness this close to a boundary is ambiguous; a feature point this close lies on it
CONGRUENT_REL = 1e-6  # relative agreement of the volume, area and principal moments of two group members

Vec = tuple[float, float, float]


class BindingError(ValueError):
    """The part's solids cannot be bound to its declared components without guessing."""


class ComponentSpec(Protocol):
    """The fields of a cad-record manufacturing_intent.Component that binding reads."""

    @property
    def id(self) -> str: ...

    @property
    def witness(self) -> Vec | None: ...

    @property
    def locator(self) -> Mapping[str, Any] | None: ...

    @property
    def multiplicity(self) -> int: ...


@dataclass(frozen=True)
class Solid:
    """One disconnected solid of a manufacturing state, with the rigid-motion invariants grouping compares."""

    index: int
    shape: Any
    volume_mm3: float
    area_mm2: float
    centre_mm: Vec
    low: Vec
    high: Vec
    moments: Vec

    def facts(self) -> dict[str, Any]:
        """JSON facts in the native frame of the state."""
        return {"volume_mm3": self.volume_mm3, "area_mm2": self.area_mm2, "centre_of_volume_mm": list(self.centre_mm),
                "bbox_mm": {"min": list(self.low), "max": list(self.high),
                            "size": [self.high[i] - self.low[i] for i in range(3)]},
                "principal_moments_mm5": list(self.moments)}


@dataclass(frozen=True)
class Bound:
    """A component and the solids it binds: its representative first, then the congruent group members."""

    id: str
    binding: str  # implicit, witness or locator
    members: tuple[Solid, ...]
    points: tuple[Vec, ...]  # the witness (inside) or the feature patch points (on the surface), native frame
    locator: Mapping[str, Any] | None

    @property
    def shape(self) -> Any:
        return self.members[0].shape if len(self.members) == 1 else occ.compound(m.shape for m in self.members)

    @property
    def volume_mm3(self) -> float:
        return sum(m.volume_mm3 for m in self.members)


def solids(shape: Any) -> list[Solid]:
    """Every solid of `shape`, measured, in exploration order."""
    found = []
    for index, solid in enumerate(occ.subshapes(shape, "SOLID")):
        volume, centre = occ.mass_properties(solid)
        low, high = occ.bbox(solid)
        found.append(Solid(index, solid, volume, occ.area(solid), centre, low, high, occ.principal_moments(solid)))
    return found


def _describe(solid: Solid) -> str:
    centre = ", ".join(f"{c:.3f}" for c in solid.centre_mm)
    return f"solid {solid.index} ({solid.volume_mm3:.3f} mm3 at {centre})"


def overlaps(found: Sequence[Solid]) -> list[tuple[int, int, float]]:
    """The (i, j, shared volume) of every two solids whose interiors overlap."""
    if len(found) < 2:
        return []
    total = sum(s.volume_mm3 for s in found)
    _, occupied = occ.occupied(occ.compound(s.shape for s in found))
    if occupied >= total - occ.volume_slack(total) * 10:
        return []
    shared = []
    for i, a in enumerate(found):
        for b in found[i + 1:]:
            if any(a.high[k] < b.low[k] or b.high[k] < a.low[k] for k in range(3)):
                continue
            common = occ.volume(occ.boolean("common", a.shape, [b.shape]))
            if common > occ.volume_slack(a.volume_mm3, b.volume_mm3) * 10:
                shared.append((a.index, b.index, common))
    return shared


def _congruent(a: Solid, b: Solid) -> bool:
    pairs = [(a.volume_mm3, b.volume_mm3), (a.area_mm2, b.area_mm2), *zip(a.moments, b.moments)]
    return all(abs(x - y) <= CONGRUENT_REL * max(abs(x), abs(y), 1e-12) for x, y in pairs)


def _by_witness(found: Sequence[Solid], ident: str, witness: Vec) -> Solid:
    states = {s.index: occ.classify(s.shape, witness, BIND_TOLERANCE_MM) for s in found}
    on = [found[i] for i, state in states.items() if state == "on"]
    inside = [found[i] for i, state in states.items() if state == "in"]
    where = ", ".join(f"{c:g}" for c in witness)
    if on:
        raise BindingError(f"component {ident}: witness ({where}) lies within {BIND_TOLERANCE_MM} mm of the surface of "
                           f"{_describe(on[0])}; a witness is a point well inside its solid")
    if len(inside) > 1:
        raise BindingError(f"component {ident}: witness ({where}) lies inside {len(inside)} solids "
                           f"({', '.join(_describe(s) for s in inside)}): they overlap")
    if not inside:
        raise BindingError(f"component {ident}: witness ({where}) lies inside no solid of the manufacturing state")
    return inside[0]


def _point(values: Sequence[float]) -> Vec:
    x, y, z = values
    return (x, y, z)


def _by_locator(found: Sequence[Solid], ident: str, locator: Mapping[str, Any],
                features: Mapping[str, Any] | None) -> tuple[Solid, tuple[Vec, ...]]:
    feature, region = locator.get("feature"), locator.get("region")
    if locator.get("component") not in (None, ident):
        raise BindingError(f"component {ident}: its locator names component {locator['component']}")
    if features is None or not isinstance(feature, str) or feature not in features:
        raise BindingError(f"component {ident}: feature {feature} has no locator in this manufacturing state "
                           "(drawing_features for a part made as installed, manufacture_features for a different state)")
    points = tuple(_point(p["point_mm"]) for p in features[feature]["patches"]
                   if region is None or p.get("region") == region)
    if not points:
        raise BindingError(f"component {ident}: feature {feature} has no patch in region {region}")
    holding = [s for s in found if all(occ.classify(s.shape, p, BIND_TOLERANCE_MM) == "on" for p in points)]
    if len(holding) != 1:
        raise BindingError(f"component {ident}: feature {feature} lies on {len(holding)} solids, not exactly one"
                           + (f" ({', '.join(_describe(s) for s in holding)})" if holding else ""))
    return holding[0], points


def bind(shape: Any, declared: Sequence[ComponentSpec], features: Mapping[str, Any] | None) -> tuple[Bound, ...]:
    """The components of the manufacturing state `shape`, bound from `declared` (cad-record
    manufacturing_intent.Component values, in record order; empty for the implicit sole component) and
    `features` (the state's feature locators by semantic ID, or None). BindingError when any solid's
    identity is ambiguous, overlapping, claimed twice or unclaimed."""
    found = solids(shape)
    if not found:
        raise BindingError("the manufacturing state holds no solid")
    shared = overlaps(found)
    if shared:
        raise BindingError("; ".join(f"{_describe(found[i])} and {_describe(found[j])} overlap by {v:.6g} mm3"
                                     for i, j, v in shared) + ": a part's components are disconnected solids")
    if not declared:
        if len(found) == 1:
            return (Bound(IMPLICIT, "implicit", (found[0],), (), None),)
        raise BindingError(f"the manufacturing state holds {len(found)} disconnected solids "
                           f"({', '.join(_describe(s) for s in found)}) and part.yaml intent.components declares none; "
                           "declare each with an interior witness point or a feature locator")
    owner: dict[int, str] = {}
    chosen: dict[str, tuple[Solid, str, tuple[Vec, ...]]] = {}
    for spec in declared:
        points: tuple[Vec, ...]
        if spec.witness is not None:
            solid, binding, points = _by_witness(found, spec.id, spec.witness), "witness", (spec.witness,)
        elif spec.locator is not None:
            (solid, points), binding = _by_locator(found, spec.id, spec.locator, features), "locator"
        else:
            raise BindingError(f"component {spec.id} declares neither a witness nor a locator")
        if solid.index in owner:
            raise BindingError(f"components {owner[solid.index]} and {spec.id} both bind {_describe(solid)}")
        owner[solid.index] = spec.id
        chosen[spec.id] = (solid, binding, points)
    members: dict[str, list[Solid]] = {spec.id: [chosen[spec.id][0]] for spec in declared}
    for spec in declared:
        if spec.multiplicity <= 1:
            continue
        representative = chosen[spec.id][0]
        pool = [s for s in found if s.index not in owner and _congruent(s, representative)]
        needed = spec.multiplicity - 1
        if len(pool) != needed:
            kind = "non-equivalent grouping" if len(pool) < needed else "ambiguous grouping"
            raise BindingError(f"component {spec.id}: {kind}: multiplicity {spec.multiplicity} needs {needed} other "
                               f"solids congruent to {_describe(representative)}, found {len(pool)}")
        for solid in pool:
            owner[solid.index] = spec.id
        members[spec.id] += pool
    unbound = [s for s in found if s.index not in owner]
    if unbound:
        raise BindingError(f"{', '.join(_describe(s) for s in unbound)} is bound by no component of part.yaml "
                           "intent.components; every solid of the manufacturing state binds exactly once")
    return tuple(Bound(spec.id, chosen[spec.id][1], tuple(members[spec.id]), chosen[spec.id][2],
                       spec.locator if chosen[spec.id][1] == "locator" else None) for spec in declared)


def check_points(shape: Any, points: Sequence[Vec], expected: str) -> bool:
    """Whether every point lies `expected` (`in` or `on`) against some solid of `shape`, and inside or on
    no second solid: the binding read again on a written file."""
    found = occ.subshapes(shape, "SOLID")
    for point in points:
        states = [occ.classify(solid, tuple(point), BIND_TOLERANCE_MM) for solid in found]  # type: ignore[arg-type]
        if states.count(expected) != 1 or sum(state != "out" for state in states) != 1:
            return False
    return True
