# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Fidelity self-check over per-part STEP files (B-rep validity, form metrics, joint bores) and, for a built
version, the process-dispatched manufacturing checks of every made component and route operation.

Usage:
    uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-design/scripts/form_check.py --hw HW --parts-dir DIR [--fidelity F0|F1|F2|F3]
        [--joint-tol 0.05] [--cnc-tool-radius 1.0] [--fdm-min-radius 1.0]
    uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-design/scripts/form_check.py --hw HW --version vN|latest
        [--fidelity ...] [--replace | --out .cad-work/PATH] [--on YYYY-MM-DD]

--version runs the checks below on the version's installed parts (versions/<V>/parts, record from its
record/ snapshot), then, for a record version 2 snapshot, process_checks: each obligation of
manufacturing_intent.required_checks this producer owns (process.<kind>.*, purchase.*, route.*,
optical.route; physically validated ones belong to the evidence ledger) gets one outcome, judged on the
exported manufacturing component files (versions/<V>/manufacturing/<PN>[/components/<COMP>]) in their
export pose, against capability-profile calibrations only. The packet
(manufacturing-check-result.schema.json) is published as hw/checks/<V>/process/result.json (reused when
the recipe matches; --replace for another), or written to --out instead. A record version 1 snapshot is
`not_assessed` with record.migration_required.

Parts: one STEP per part in DIR, named `<PN>.step` (cad-motion `--parts-dir`
rules). Process comes from hw/parts/<PN>/part.yaml (`fdm`, `cnc`, `purchased`); a bought part
(`make_buy: buy`, such as a pin made to drawing) is checked in its bought form, as `purchased`.
Joints: hw/assembly.yaml. When DIR holds build_reference.py's `build.json`, its
in-memory facts per part feed the two kernel checks below. `product.yaml`
`manufacturing.min_wall_mm` overrides the minimum wall per process (`fdm`, `cnc`; each
above 0 mm); an invalid override is an error (exit 2), never a silent default.

Per part (stdout JSON `parts`):
    form_class             part.yaml value (solid when absent)
    min_wall               F2+: {required_mm} alone when the process has no minimum
                           (purchased, or cnc without an override); else the scan:
                           thickness_mm and location_mm (null when no wall was
                           measured), faces, faces_sampled, samples, measured, tapered,
                           shallow with shallow_thickness_mm and shallow_location_mm,
                           capped_faces, spacing_mm and seconds (see minimum_wall)
    valid                  BRepCheck_Analyzer verdict on the STEP read-back
    solids, volume_mm3, area_mm2
    kernel_valid           BRepCheck verdict on the in-memory shape (build.json)
    axis_planar_fraction   area share of planar faces normal to X, Y or Z
    obb_fill_fraction      solid volume / oriented bounding-box volume
    obb_thickness_fraction shortest OBB extent / second-shortest OBB extent
    blend_faces            convex cylinder and torus faces of radius <= 10 mm that turn
                           through <= 120 degrees (fillets, not lug ends or shafts)
    freeform_faces         B-spline, Bezier, revolved, extruded or offset faces
    min_convex_radius      smallest convex blend radius (FDM edge rule)
    min_concave_radius     smallest concave cylinder radius that is no bore wall
                           (cad-model holes.bore_walls): the CNC corner rule

Checks (each failure is one `findings` entry):
    brep_invalid            any fidelity: a part is not a valid B-rep, in the STEP
                            read-back (`source: step`) or in memory (`source: kernel`;
                            a STEP read-back can heal an unorientable face)
    step_readback           any fidelity: readback_rel_error above cad-model's 1e-6;
                            loose fillet edges (0.01 to 0.1 mm tolerance) move it ~1e-5
    joint_bore_missing      F1+: a joint's parent or child has no cylinder face whose
                            axis is parallel to the joint axis and passes within
                            --joint-tol of the joint origin; made parts need a bore
                            (concave), purchased parts a shank (convex)
    block_like              F2+, made solid parts only: (axis_planar_fraction >= 0.80 or
                            blend_faces + freeform_faces == 0), with both OBB exemptions
    min_wall                F2+, a process with a minimum (fdm 1.2 mm, or the override):
                            the shortest sampled wall is below it
    min_wall_unmeasured     F2+, the same parts: no sample reached an opposing face, so
                            the scan proves nothing
    fdm_exterior_radius     F2+, fdm parts: min_convex_radius < --fdm-min-radius
    cnc_internal_radius     F2+, cnc parts: min_concave_radius < --cnc-tool-radius

    The form checks (block_like, min_wall, min_wall_unmeasured, fdm_exterior_radius,
    cnc_internal_radius) are universal heuristics: diagnostics with `blocking: false` (decision 3).
    Only brep_invalid, step_readback and joint_bore_missing carry `blocking: true`.

stdout: JSON {status, fidelity, parts, joints, findings[, version, manufacturing]}; `manufacturing` holds
the packet's status, complete, bundle, outcome counts, open obligations and its major and critical
findings. stderr: diagnostics.
Exit 0 = no blocking finding and (--version) a passing packet, 1 = a blocking finding or a failing,
unknown or incomplete packet (or a record that needs migration), 2 = usage or infrastructure error.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import time
import traceback
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
LAYOUT_PATH = Path(__file__).resolve().parents[2] / "cad-record" / "scripts" / "layout.py"
CHECK_PACKET_PATH = LAYOUT_PATH.with_name("check_packet.py")
PROCESS_SUBJECT = "process"  # hw/checks/<V>/process: this producer's bundle subject
BLOCKING_CHECKS = frozenset({"brep_invalid", "step_readback", "joint_bore_missing"})  # the rest are diagnostics

POSE_PATH = Path(__file__).resolve().parents[2] / "cad-motion" / "scripts" / "pose.py"
CAD_MODEL_SCRIPTS = Path(__file__).resolve().parents[2] / "cad-model" / "scripts"
KERNEL_FACTS = "build.json"
FIDELITY = ("F0", "F1", "F2", "F3")
BLOCK_PLANAR_FRACTION = 0.80
BLOCK_MIN_FILL_FRACTION = 0.20
BLOCK_MAX_THICKNESS_FRACTION = 0.10
BLEND_MAX_RADIUS = 10.0
BLEND_MAX_SPAN = math.radians(120.0)  # a fillet turns through its edge angle; a lug end turns 180 degrees
AXIS_TOL = 1e-4  # sine of the allowed angle between a cylinder axis and a joint axis
MIN_WALL_BY_PROCESS = {"fdm": 1.2}  # built-in minimum wall, mm: a 0.4 mm FDM nozzle's three perimeters
MIN_WALL_PROCESSES = ("fdm", "cnc")  # the keys product.yaml manufacturing.min_wall_mm may set
WALL_SPACING_FRACTION = 0.5  # sample spacing along each UV direction, as a fraction of the minimum wall
WALL_FACE_SAMPLE_CAP = 4096  # samples per face; a larger face is sampled coarser and counted in capped_faces
WALL_METRIC_PROBES = 8  # first-derivative probes per UV direction that size a face's sample grid
WALL_EDGE_SKIP_TOLERANCES = 4.0  # samples this many B-rep tolerances from a trim boundary are skipped
WALL_EDGE_SKIP_MIN_MM = 1e-4
WALL_OPPOSING_DOT = -0.94  # an exit normal within 20 degrees of antiparallel to the sample normal
WALL_SAME_EXIT_MM = 1e-6  # hits this close along a ray are one exit, through an edge or a vertex
WALL_PERSIST_REACH = 2.0  # in minimum walls: how thick a neighbour through the same exit face may be
WALL_PROBE_REACH = 3.0  # in minimum walls, per axis: no persistence probe strays farther from its sample
WALL_RAY_OFFSET_MM = 1e-5
WALL_CLASSIFY_TOL_MM = 1e-7


class RecordError(Exception):
    """A design-record value form_check reads is invalid."""


def load_pose() -> Any:
    spec = importlib.util.spec_from_file_location("cad_motion_pose", POSE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["cad_motion_pose"] = module
    spec.loader.exec_module(module)
    return module


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Sequence[float]) -> float:
    return math.sqrt(_dot(a, a))


def _xyz(v: Any) -> tuple[float, float, float]:  # an OCP gp_Pnt, gp_Vec or gp_Dir
    return (v.X(), v.Y(), v.Z())


def faces_of(shape: Any) -> list[Any]:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    cast = getattr(TopoDS, "Face_s", None) or TopoDS.Face  # OCP 7.9 static vs OCP 8 namespace function
    faces = []
    while explorer.More():
        faces.append(cast(explorer.Current()))
        explorer.Next()
    return faces


def solids_of(shape: Any) -> list[Any]:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    cast = getattr(TopoDS, "Solid_s", None) or TopoDS.Solid
    solids = []
    while explorer.More():
        solids.append(cast(explorer.Current()))
        explorer.Next()
    return solids


def unit_normal(surface: Any, u: float, v: float, reversed_: bool) -> tuple[Any, Any]:
    """The point and outward (material-leaving) unit normal at (u, v); None where the normal is undefined."""
    from OCP.gp import gp_Pnt, gp_Vec

    point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
    surface.D1(u, v, point, du, dv)
    normal = du.Crossed(dv)
    magnitude = normal.Magnitude()
    if magnitude <= 1e-9 * du.Magnitude() * dv.Magnitude():  # a pole, an apex or a degenerate patch
        return point, None
    normal.Divide(magnitude)
    if reversed_:
        normal.Reverse()
    return point, normal


class FaceSamples:
    """Wall samples on one trimmed face: the cell centres of a UV grid no coarser than `spacing`.

    The fastest parametric speed over a WALL_METRIC_PROBES² probe grid sizes each UV
    direction, so the grid is no coarser than `spacing` where the surface stretches
    most; WALL_FACE_SAMPLE_CAP bounds the samples per face (`capped`). The grid spans
    the face's full UV range, so a periodic seam does not decide where samples land, and
    a seam is no trim boundary. A point within WALL_EDGE_SKIP_TOLERANCES of the face's
    B-rep tolerance (its edges and vertices included) from a trim boundary is no sample.
    """

    def __init__(self, face: Any, bounds: tuple[float, float, float, float], spacing: float) -> None:
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.IntTools import IntTools_FClass2d
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_REVERSED, TopAbs_VERTEX

        self.surface = BRepAdaptor_Surface(face, True)
        self.reversed = face.Orientation() == TopAbs_REVERSED
        self.bounds = bounds
        tolerance = max(BRep_Tool.Tolerance_s(face), BRep_Tool.MaxTolerance_s(face, TopAbs_EDGE),
                        BRep_Tool.MaxTolerance_s(face, TopAbs_VERTEX))
        skip = max(WALL_EDGE_SKIP_TOLERANCES * tolerance, WALL_EDGE_SKIP_MIN_MM)
        # Points within `skip` of a trim boundary classify ON.
        self.trim = IntTools_FClass2d(face, min(self.surface.UResolution(skip), self.surface.VResolution(skip)))
        self.nu, self.nv, self.capped = self._cells(spacing)

    def _cells(self, spacing: float) -> tuple[int, int, bool]:
        from OCP.gp import gp_Pnt, gp_Vec

        umin, umax, vmin, vmax = self.bounds
        point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
        speed_u = speed_v = 0.0
        for i in range(WALL_METRIC_PROBES):
            for j in range(WALL_METRIC_PROBES):
                self.surface.D1(umin + (umax - umin) * (i + 0.5) / WALL_METRIC_PROBES,
                                vmin + (vmax - vmin) * (j + 0.5) / WALL_METRIC_PROBES, point, du, dv)
                speed_u, speed_v = max(speed_u, du.Magnitude()), max(speed_v, dv.Magnitude())
        nu = max(1, math.ceil(speed_u * (umax - umin) / spacing))
        nv = max(1, math.ceil(speed_v * (vmax - vmin) / spacing))
        if nu * nv <= WALL_FACE_SAMPLE_CAP:
            return nu, nv, False
        scale = math.sqrt(WALL_FACE_SAMPLE_CAP / (nu * nv))
        nu, nv = max(1, int(nu * scale)), max(1, int(nv * scale))
        return min(nu, WALL_FACE_SAMPLE_CAP // nv), nv, True

    def at(self, u: float, v: float) -> tuple[Any, Any] | None:
        """The point and outward unit normal at (u, v); None outside the trimmed face or without a normal."""
        from OCP.gp import gp_Pnt2d
        from OCP.TopAbs import TopAbs_IN

        if self.trim.Perform(gp_Pnt2d(u, v)) != TopAbs_IN:
            return None
        point, normal = unit_normal(self.surface, u, v, self.reversed)
        return None if normal is None else (point, normal)

    def grid(self) -> Iterator[tuple[float, float, Any, Any]]:
        """(u, v, point, outward unit normal) at every cell centre inside the trimmed face."""
        umin, umax, vmin, vmax = self.bounds
        for i in range(self.nu):
            u = umin + (umax - umin) * (i + 0.5) / self.nu
            for j in range(self.nv):
                v = vmin + (vmax - vmin) * (j + 0.5) / self.nv
                sample = self.at(u, v)
                if sample is not None:
                    yield u, v, sample[0], sample[1]


class SolidRays:
    """Rays through one solid, each tested only against the faces near the face it samples.

    focus() loads the faces whose bounding boxes come within a radius of one face, once
    per face, so a ray's cost follows the faces near it rather than the whole solid.
    """

    def __init__(self, solid: Any) -> None:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        self.faces = faces_of(solid)
        self.boxes = []
        whole = Bnd_Box()
        for face in self.faces:
            box = Bnd_Box()
            BRepBndLib.Add_s(face, box, False)  # from the geometry: a mesh's box can miss the surface between nodes
            self.boxes.append(box)
            whole.Add(box)
        self.reach = math.sqrt(whole.SquareExtent())  # no chord through the solid is longer
        self.intersector: Any = None

    def focus(self, index: int, radius: float) -> None:
        """Load the faces whose bounding boxes come within `radius` of face `index` for the rays that follow."""
        from OCP.BRep import BRep_Builder
        from OCP.Bnd import Bnd_Box
        from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
        from OCP.TopoDS import TopoDS_Compound

        near = Bnd_Box()
        near.Add(self.boxes[index])
        near.Enlarge(radius)
        compound, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(compound)
        for face, box in zip(self.faces, self.boxes):
            if not near.IsOut(box):
                builder.Add(compound, face)
        self.intersector = IntCurvesFace_ShapeIntersector()
        self.intersector.Load(compound, WALL_CLASSIFY_TOL_MM)

    def crossings(self, start: Any, direction: Any, limit: float, count: int) -> list[tuple[float, list[tuple[Any, Any]]]]:
        """The first `count` boundary crossings of a ray within `limit`: (ray parameter, [(face, outward normal)]).

        Hits within WALL_SAME_EXIT_MM of each other are one crossing, through an edge or a vertex.
        """
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.gp import gp_Dir, gp_Lin
        from OCP.TopAbs import TopAbs_REVERSED

        hits = self.intersector
        hits.Perform(gp_Lin(start, gp_Dir(direction)), 0.0, limit)
        hits.SortResult()
        found: list[tuple[float, list[tuple[Any, Any]]]] = []
        for index in range(1, hits.NbPnt() + 1):
            w = hits.WParameter(index)
            if not found or w > found[-1][0] + WALL_SAME_EXIT_MM:
                if len(found) == count:
                    break
                found.append((w, []))
            face = hits.Face(index)
            _, normal = unit_normal(BRepAdaptor_Surface(face, False), hits.UParameter(index), hits.VParameter(index),
                                    face.Orientation() == TopAbs_REVERSED)
            if normal is not None:
                found[-1][1].append((face, normal))
        return found

    def chord(self, point: Any, normal: Any, limit: float) -> tuple[float | None, list[Any]]:
        """The length of the inward ray from a surface point to its first exit within `limit`, and the exit faces.

        (math.inf, []) when the ray stays in the solid that far. (None, []) when no face
        it leaves through there opposes `normal`: a taper, an acute edge wedge or a side
        face. Otherwise the exit faces listed are the ones that oppose `normal`.
        """
        found = self.crossings(point.Translated(normal.Multiplied(-WALL_RAY_OFFSET_MM)), normal.Reversed(), limit, 1)
        if not found:
            return math.inf, []
        exit_w, exits = found[0]
        opposing = [face for face, exit_normal in exits if normal.Dot(exit_normal) <= WALL_OPPOSING_DOT]
        return (exit_w + WALL_RAY_OFFSET_MM, opposing) if opposing else (None, [])  # the ray starts inside the face

    def persists(self, samples: FaceSamples, u: float, v: float, point: Any, normal: Any, length: float,
                 exits: list[Any], required_mm: float) -> bool:
        """Whether a chord below the minimum belongs to a wall, not to relief such as the lands of engraved text.

        A wall continues at least as far as it is thin. Along the face's U and V
        directions, on at least one side, one chord length away, the wall must go on: the
        face's own sample there leaves the solid through one of `exits`, at any thickness
        up to WALL_PERSIST_REACH minimum walls, or through another opposing face below the
        minimum. So a web thinnest at one spot, thickening away from it, still counts.
        Where that point leaves the face, a ray parallel to this chord stands in
        (thin_wall_at), so a wall also continues across face boundaries and parametric
        poles; relief ends there, at its own edge.
        """
        from OCP.gp import gp_Pnt, gp_Vec

        du, dv = gp_Vec(), gp_Vec()
        samples.surface.D1(u, v, gp_Pnt(), du, dv)
        for derivative, along_u in ((du, True), (dv, False)):
            step, tangent = length / derivative.Magnitude(), derivative.Normalized()
            for side in (1.0, -1.0):
                probe = samples.at(u + side * step, v) if along_u else samples.at(u, v + side * step)
                if probe is None:
                    foot = point.Translated(tangent.Multiplied(side * length))
                    goes_on = self.thin_wall_at(foot, normal, length, exits, required_mm)
                else:
                    found = self.crossings(probe[0].Translated(probe[1].Multiplied(-WALL_RAY_OFFSET_MM)),
                                           probe[1].Reversed(), WALL_PERSIST_REACH * required_mm, 1)
                    goes_on = bool(found) and self.continues(found[0], probe[1], exits, required_mm)
                if goes_on:
                    break
            else:
                return False
        return True

    @staticmethod
    def continues(crossing: tuple[float, list[tuple[Any, Any]]], normal: Any, exits: list[Any],
                  required_mm: float, entry_w: float = -WALL_RAY_OFFSET_MM) -> bool:
        """Whether an exit crossing goes on with a wall: through one of `exits` at any thickness, or through
        another face opposing `normal` less than `required_mm` after `entry_w`."""
        exit_w, faces = crossing
        return any(face.IsSame(exit_face) for face, _ in faces for exit_face in exits) or (
            exit_w - entry_w < required_mm and any(normal.Dot(exit_normal) <= WALL_OPPOSING_DOT for _, exit_normal in faces))

    def thin_wall_at(self, foot: Any, normal: Any, length: float, exits: list[Any], required_mm: float) -> bool:
        """Whether a ray along -normal through `foot` enters the solid within one chord length of `foot`,
        then leaves through one of `exits` or through an opposing face less than `required_mm` later."""
        found = self.crossings(foot.Translated(normal.Multiplied(length)), normal.Reversed(),
                               2.0 * length + required_mm, 2)
        if len(found) < 2 or found[0][0] > 2.0 * length:
            return False
        enter_w, entries = found[0]
        return any(normal.Dot(entry) > 0.0 and self.continues(found[1], entry, exits, required_mm, enter_w)
                   for _, entry in entries)


def minimum_wall(shape: Any, required_mm: float) -> dict[str, Any]:
    """The shortest sampled wall, with the coverage that bounds it.

    Each face gets a UV grid of samples (FaceSamples) no coarser than
    WALL_SPACING_FRACTION of `required_mm`, up to WALL_FACE_SAMPLE_CAP per face;
    `capped_faces` counts the faces sampled coarser. Each sample casts a ray along its
    inward normal (SolidRays.chord). Its first exit must oppose the sample, so two faces
    meeting at an acute edge never read as a thin wall; `tapered` counts the samples
    whose exit did not. A ray needs to reach only the running minimum: a sample with no
    shorter chord bounds the wall there at or above it, so `tapered` and `shallow` count
    only exits shorter than the running minimum. A shorter chord below `required_mm`
    must also persist (SolidRays.persists) to become the minimum; `shallow` counts the
    ones that did not, with the thinnest of them. At or above `required_mm` the shortest
    opposing chord is the minimum as found. `measured` counts the samples that measured
    or bounded a wall; zero means the scan measured no wall at all.
    """
    from OCP.BRepTools import BRepTools

    started = time.perf_counter()
    spacing = WALL_SPACING_FRACTION * required_mm
    best: tuple[float, Any] | None = None
    thinnest_shallow: tuple[float, Any] | None = None
    faces = faces_sampled = samples = measured = shallow = tapered = capped_faces = 0
    for solid in solids_of(shape):
        rays = SolidRays(solid)
        for index, face in enumerate(rays.faces):
            faces += 1
            bounds = BRepTools.UVBounds_s(face)
            if not all(math.isfinite(value) for value in bounds):
                continue
            face_samples = FaceSamples(face, bounds, spacing)
            capped_faces += face_samples.capped
            # The running minimum only shrinks, so this face's rays and probes stay within the radius.
            rays.focus(index, max(rays.reach if best is None else best[0], WALL_PROBE_REACH * required_mm))
            placed = 0
            for u, v, point, normal in face_samples.grid():
                placed += 1
                length, exits = rays.chord(point, normal, rays.reach if best is None else min(rays.reach, best[0]))
                if length is None:
                    tapered += 1
                    continue
                if length == math.inf and best is None:
                    continue  # a ray that never left the solid
                if best is not None and length >= best[0]:
                    measured += 1  # bounded by the running minimum
                    continue
                if length < required_mm and not rays.persists(face_samples, u, v, point, normal, length, exits,
                                                              required_mm):
                    shallow += 1
                    if thinnest_shallow is None or length < thinnest_shallow[0]:
                        thinnest_shallow = (length, point)
                    continue
                measured += 1
                best = (length, point)
            samples += placed
            faces_sampled += placed > 0
    return {"required_mm": required_mm,
            "thickness_mm": None if best is None else round(best[0], 6),
            "location_mm": None if best is None else rounded_xyz(best[1]),
            "faces": faces, "faces_sampled": faces_sampled, "samples": samples, "measured": measured,
            "tapered": tapered, "shallow": shallow,
            "shallow_thickness_mm": None if thinnest_shallow is None else round(thinnest_shallow[0], 6),
            "shallow_location_mm": None if thinnest_shallow is None else rounded_xyz(thinnest_shallow[1]),
            "capped_faces": capped_faces, "spacing_mm": round(spacing, 6),
            "seconds": round(time.perf_counter() - started, 3)}


def rounded_xyz(point: Any) -> list[float]:
    return [round(value, 6) + 0.0 for value in _xyz(point)]  # + 0.0 turns -0.0 into 0.0


def count_solids(shape: Any) -> int:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def part_form_class(hw: Path, pn: str) -> str:
    """Read the form-class contract through one accessor; absent defaults to solid."""
    import yaml

    path = hw / "parts" / pn / "part.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    value = data.get("form_class") if isinstance(data, dict) else None
    return value if value in {"solid", "frame", "plate", "skeleton", "shell"} else "solid"


def minimum_wall_thresholds(hw: Path) -> dict[str, float]:
    """Built-in minima with product.yaml manufacturing.min_wall_mm overrides; an invalid override is a RecordError.

    The override contract is cad-record's product schema: a non-empty mapping whose keys
    are MIN_WALL_PROCESSES, each a number above 0 mm.
    """
    import yaml

    path = hw / "product.yaml"
    product = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    manufacturing = product.get("manufacturing") if isinstance(product, dict) else None
    thresholds = dict(MIN_WALL_BY_PROCESS)
    if not isinstance(manufacturing, dict) or "min_wall_mm" not in manufacturing:
        return thresholds
    overrides = manufacturing["min_wall_mm"]
    if not isinstance(overrides, dict) or not overrides:
        raise RecordError(f"{path}: manufacturing.min_wall_mm must map {' and/or '.join(MIN_WALL_PROCESSES)} "
                          f"to a minimum wall in mm, got {overrides!r}")
    for process, value in overrides.items():
        if process not in MIN_WALL_PROCESSES:
            raise RecordError(f"{path}: manufacturing.min_wall_mm.{process}: no minimum wall for this process; "
                              f"allowed: {', '.join(MIN_WALL_PROCESSES)}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise RecordError(f"{path}: manufacturing.min_wall_mm.{process} must be a number above 0 mm, got {value!r}")
        thresholds[process] = float(value)
    return thresholds


def surface_sample(face: Any) -> tuple[Any, tuple[float, float, float], tuple[float, float, float], tuple[float, float, float, float]]:
    """Adaptor, a point and the outward (material-leaving) normal at the face's UV centre, and its UV bounds."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_REVERSED

    umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
    adaptor = BRepAdaptor_Surface(face)
    props = BRepLProp_SLProps(adaptor, (umin + umax) / 2.0, (vmin + vmax) / 2.0, 1, 1e-6)
    normal = _xyz(props.Normal()) if props.IsNormalDefined() else (0.0, 0.0, 0.0)
    if face.Orientation() == TopAbs_REVERSED:
        normal = (-normal[0], -normal[1], -normal[2])
    return adaptor, _xyz(props.Value()), normal, (umin, umax, vmin, vmax)


def face_area(face: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    area: float = props.Mass()  # OCP returns an untyped float
    return area


def shape_volume(shape: Any) -> float:
    from cadmodel import occ

    volume: float = occ.volume(shape)  # the adaptive integration cad-model's check uses
    return volume


def is_valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


def measure(shape: Any) -> dict[str, Any]:
    from cadmodel import holes, occ
    from OCP.GeomAbs import (
        GeomAbs_BezierSurface,
        GeomAbs_BSplineSurface,
        GeomAbs_Cylinder,
        GeomAbs_OffsetSurface,
        GeomAbs_Plane,
        GeomAbs_SurfaceOfExtrusion,
        GeomAbs_SurfaceOfRevolution,
        GeomAbs_Torus,
    )

    freeform = {GeomAbs_BezierSurface, GeomAbs_BSplineSurface, GeomAbs_OffsetSurface,
                GeomAbs_SurfaceOfExtrusion, GeomAbs_SurfaceOfRevolution}
    total = axis_planar = 0.0
    blends = freeform_count = 0
    min_convex = min_concave = math.inf
    cylinders = []
    bores = holes.bore_walls(shape)  # a bore is drilled, not a corner a cutter leaves
    obb_extents, _ = occ.oriented_bbox(shape)
    obb_volume = math.prod(obb_extents)
    shortest, second_shortest = sorted(obb_extents)[:2]
    thickness_fraction = shortest / second_shortest if second_shortest else 0.0
    for face in faces_of(shape):
        area = face_area(face)
        total += area
        adaptor, point, normal, (umin, umax, vmin, vmax) = surface_sample(face)
        kind = adaptor.GetType()
        if kind == GeomAbs_Plane:
            n = _xyz(adaptor.Plane().Axis().Direction())
            if max(abs(c) for c in n) > 1.0 - 1e-6:
                axis_planar += area
        elif kind == GeomAbs_Cylinder:
            cylinder = adaptor.Cylinder()
            axis = cylinder.Axis()
            loc, direction = _xyz(axis.Location()), _xyz(axis.Direction())
            offset = _sub(point, loc)
            radial = _sub(offset, tuple(_dot(offset, direction) * c for c in direction))
            convex = _dot(normal, radial) > 0.0
            radius = cylinder.Radius()
            span = umax - umin
            cylinders.append({"location": loc, "direction": direction, "radius": radius, "convex": convex})
            if convex and span <= BLEND_MAX_SPAN and radius <= BLEND_MAX_RADIUS:
                blends += 1
                min_convex = min(min_convex, radius)
            elif not convex and not any(face.IsSame(wall) for wall in bores):
                min_concave = min(min_concave, radius)
        elif kind == GeomAbs_Torus:
            torus = adaptor.Torus()
            axis = torus.Axis()
            loc, direction = _xyz(axis.Location()), _xyz(axis.Direction())
            offset = _sub(point, loc)
            radial = _sub(offset, tuple(_dot(offset, direction) * c for c in direction))
            length = _norm(radial)
            if length > 1e-9:
                tube = tuple(loc[i] + radial[i] / length * torus.MajorRadius() for i in range(3))
                convex = _dot(normal, _sub(point, tube)) > 0.0
                if convex and vmax - vmin <= BLEND_MAX_SPAN and torus.MinorRadius() <= BLEND_MAX_RADIUS:
                    blends += 1
                    min_convex = min(min_convex, torus.MinorRadius())
        elif kind in freeform:
            freeform_count += 1
    return {
        "_obb_volume_mm3": obb_volume,
        "_obb_thickness_fraction": thickness_fraction,
        "area_mm2": round(total, 3),
        "axis_planar_fraction": round(axis_planar / total, 4) if total else 1.0,
        "blend_faces": blends,
        "freeform_faces": freeform_count,
        "min_convex_radius": None if math.isinf(min_convex) else round(min_convex, 4),
        "min_concave_radius": None if math.isinf(min_concave) else round(min_concave, 4),
        "_cylinders": cylinders,
    }


def bore_offset(cylinders: list[dict[str, Any]], axis: Sequence[float], origin: Sequence[float],
                kind: str) -> dict[str, Any] | None:
    """The `kind` cylinder (bore or shank) parallel to the joint axis whose line passes closest to the origin."""
    best = None
    for cylinder in cylinders:
        if ("shank" if cylinder["convex"] else "bore") != kind or _norm(_cross(cylinder["direction"], axis)) > AXIS_TOL:
            continue
        offset = _norm(_cross(_sub(origin, cylinder["location"]), cylinder["direction"]))
        if best is None or offset < best["offset_mm"]:
            best = {"offset_mm": round(offset, 6), "radius": round(cylinder["radius"], 4), "kind": kind}
    return best


# --- manufacturing-state probe (process_checks facts, record version 2) -----------------------------------------
# One exported manufacturing component, in the frame it was exported in (bed: its approved additive pose,
# build direction +Z; model: the native frame). process_checks/core.py Geometry documents every key.

PROBE_GRID = 8  # UV samples per direction on each face for normals, down-facing area and rays
PROBE_RAY_SAMPLES = 16  # samples per face whose visibility rays are cast along each requested direction
PROBE_TOL_MM = 1e-3  # a locator patch point this close to a face lies on it (cad-model components.BIND_TOLERANCE_MM)
PROBE_RAY_OFFSET_MM = 1e-4
PROBE_LEVEL_MM = 1e-6  # faces this close to the lowest z lie on the bed
FULL_TURN = 2.0 * math.pi - 1e-6


class ShapeRays:
    """First hits of rays against every face of one solid."""

    def __init__(self, solid: Any, faces: list[Any]) -> None:
        from cadmodel import occ
        from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector

        self.faces = faces
        low, high = occ.bbox(solid)
        self.reach = 2.0 * _norm(_sub(high, low)) + 1.0
        self.intersector = IntCurvesFace_ShapeIntersector()
        self.intersector.Load(solid, WALL_CLASSIFY_TOL_MM)

    def first(self, start: tuple[float, float, float], direction: tuple[float, float, float],
              limit: float | None = None) -> tuple[float, int] | None:
        """(distance, face index) of the first face the ray from `start` along `direction` meets, or None."""
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

        hits = self.intersector
        hits.Perform(gp_Lin(gp_Pnt(*start), gp_Dir(*direction)), 0.0, self.reach if limit is None else limit)
        best = None
        for index in range(1, hits.NbPnt() + 1):
            w = hits.WParameter(index)
            if w > 1e-9 and (best is None or w < best[0]):
                best = (w, hits.Face(index))
        if best is None:
            return None
        face = next((i for i, f in enumerate(self.faces) if f.IsSame(best[1])), -1)
        return best[0], face


def _surface_kind(adaptor: Any) -> str:
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus

    return {GeomAbs_Plane: "plane", GeomAbs_Cylinder: "cylinder", GeomAbs_Cone: "cone", GeomAbs_Sphere: "sphere",
            GeomAbs_Torus: "torus"}.get(adaptor.GetType(), "freeform")


def _face_samples(face: Any) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """(point, outward unit normal) on a PROBE_GRID x PROBE_GRID UV grid inside the trimmed face."""
    from OCP.BRepTools import BRepTools

    bounds = BRepTools.UVBounds_s(face)
    if not all(math.isfinite(value) for value in bounds):
        return []
    umin, umax, vmin, vmax = bounds
    grid = FaceSamples(face, bounds, max(umax - umin, vmax - vmin, 1.0) * 1e6)  # one cell: only its trim and normals
    found = []
    for i in range(PROBE_GRID):
        for j in range(PROBE_GRID):
            sample = grid.at(umin + (umax - umin) * (i + 0.5) / PROBE_GRID, vmin + (vmax - vmin) * (j + 0.5) / PROBE_GRID)
            if sample is not None:
                found.append((_xyz(sample[0]), _xyz(sample[1])))
    return found


def _edges(face: Any) -> list[tuple[tuple[float, ...], tuple[float, float, float], tuple[float, float, float]]]:
    """(rounded midpoint key, midpoint, chord direction) of each non-degenerate edge of `face`."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer

    from cadmodel import occ

    found = []
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = occ.downcast(explorer.Current(), "EDGE")
        explorer.Next()
        if BRep_Tool.Degenerated_s(edge):
            continue
        curve = BRepAdaptor_Curve(edge)
        first, last = curve.FirstParameter(), curve.LastParameter()
        mid = _xyz(curve.Value((first + last) / 2.0))
        chord = _sub(_xyz(curve.Value(last)), _xyz(curve.Value(first)))
        found.append((tuple(round(c, 5) for c in mid), mid, chord))
    return found


def _distance(point: tuple[float, float, float], shape: Any) -> float:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    measure_ = BRepExtrema_DistShapeShape(vertex, shape)
    return measure_.Value() if measure_.IsDone() else math.inf


def transform_locators(locators: dict[str, Any], transform: list[list[float]] | None) -> dict[str, Any]:
    """Native-frame feature locators moved by a sidecar placement transform (3x4 rows, native -> exported)."""
    if not transform:
        return locators
    rot = [row[:3] for row in transform]
    trans = [row[3] for row in transform]

    def apply(v: list[float], moved: bool) -> list[float]:
        return [sum(rot[i][k] * v[k] for k in range(3)) + (trans[i] if moved else 0.0) for i in range(3)]

    out = {}
    for ident, locator in locators.items():
        patches = [{**patch, "point_mm": apply(patch["point_mm"], True), "normal": apply(patch["normal"], False)}
                   for patch in locator.get("patches") or ()]
        out[ident] = {**locator, "patches": patches}
    return out


def _locator_width(locator: dict[str, Any]) -> float | None:
    """The distance between two opposed planar patches of a locator (a slot or a tab), else None."""
    planes = [p for p in locator.get("patches") or () if p.get("support_type") == "plane"]
    for i, a in enumerate(planes):
        for b in planes[i + 1:]:
            if _dot(a["normal"], b["normal"]) < -1.0 + 1e-6:
                return abs(_dot(_sub(b["point_mm"], a["point_mm"]), a["normal"]))
    return None


def probe_solid(solid: Any, index: int, locators: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """The process-check facts of one solid (see process_checks Geometry); face ids are s<index>f<n>."""
    from cadmodel import holes as holes_module
    from cadmodel import occ

    faces = faces_of(solid)
    ids = [f"s{index}f{n + 1}" for n in range(len(faces))]
    rays = ShapeRays(solid, faces)
    low, high = occ.bbox(solid)
    volume, centre = occ.mass_properties(solid)
    bed_z = low[2]
    rows: list[dict[str, Any]] = []
    adjacency: dict[tuple[float, ...], list[int]] = {}
    edges_of: list[list[Any]] = []
    for n, face in enumerate(faces):
        adaptor, point, normal, (umin, umax, vmin, vmax) = surface_sample(face)
        kind = _surface_kind(adaptor)
        f_low, f_high = occ.bbox(face)
        row: dict[str, Any] = {"id": ids[n], "type": kind, "area_mm2": round(face_area(face), 6),
                               "z_mm": [round(f_low[2], 6), round(f_high[2], 6)], "features": []}
        if kind == "plane":
            n_dir = _xyz(adaptor.Plane().Axis().Direction())
            outward = n_dir if _dot(n_dir, normal) >= 0 else tuple(-c for c in n_dir)
            row["normal"] = list(outward)
            row["offset_mm"] = round(_dot(point, outward), 6)
            if outward[2] < -1.0 + 1e-9 and f_high[2] <= bed_z + PROBE_LEVEL_MM:
                row["bed"] = True
            for key, axis in (("+x", 0), ("-x", 0), ("+y", 1), ("-y", 1), ("+z", 2), ("-z", 2)):
                sign = 1.0 if key[0] == "+" else -1.0
                level = high[axis] if sign > 0 else low[axis]
                if outward[axis] * sign > 1.0 - 1e-9 and abs((f_high if sign > 0 else f_low)[axis] - level) <= PROBE_LEVEL_MM:
                    row["on_bbox"] = key
        elif kind == "cylinder":
            cylinder = adaptor.Cylinder()
            loc, direction = _xyz(cylinder.Axis().Location()), _xyz(cylinder.Axis().Direction())
            offset = _sub(_xyz(adaptor.Value((umin + umax) / 2.0, (vmin + vmax) / 2.0)), loc)
            radial = _sub(offset, tuple(_dot(offset, direction) * c for c in direction))
            row["cylinder"] = {"location": list(loc), "axis": list(direction), "radius_mm": round(cylinder.Radius(), 6),
                               "convex": _dot(normal, radial) > 0.0, "span_rad": umax - umin, "length_mm": round(vmax - vmin, 6)}
        samples = _face_samples(face)
        row["normals"] = [[round(c, 6) for c in nrm] for _, nrm in samples[::max(1, len(samples) // 9)]][:9]
        row["_samples"] = samples
        rows.append(row)
        edges = _edges(face)
        edges_of.append(edges)
        for edge_key, _, _ in edges:
            adjacency.setdefault(edge_key, []).append(n)

    def neighbours(n: int) -> list[tuple[int, Any]]:
        return [(m, edge) for edge in edges_of[n] for m in adjacency.get(edge[0], ()) if m != n]

    starts: dict[int, set[int]] = {}
    for n, row in enumerate(rows):
        samples = row["_samples"]
        if row.get("bed") or not samples:
            continue
        weight = row["area_mm2"] / len(samples)
        angles: dict[int, float] = {}
        start = {"bed_mm2": 0.0, "part_mm2": 0.0}
        for point, nrm in samples:
            if nrm[2] >= -1e-6:
                continue
            degrees = math.degrees(math.asin(min(1.0, -nrm[2])))
            angles[round(degrees)] = angles.get(round(degrees), 0.0) + weight
            hit = rays.first((point[0], point[1], point[2] - PROBE_RAY_OFFSET_MM), (0.0, 0.0, -1.0),
                             point[2] - bed_z + 1.0)
            if hit is None:
                start["bed_mm2"] += weight
            else:
                start["part_mm2"] += weight
                starts.setdefault(n, set()).add(hit[1])
        if not angles:
            continue
        face_low = row["z_mm"][0]
        others = neighbours(n)
        kind, span = "overhang", None
        if face_low > bed_z + PROBE_LEVEL_MM and all(rows[m]["z_mm"][0] >= face_low - PROBE_LEVEL_MM for m, _ in others):
            kind = "island"  # a local lowest region off the bed: supports must start under it
        elif row["type"] == "plane" and row.get("normal", [0, 0, 0])[2] < -1.0 + 1e-6:
            # A horizontal ceiling anchored where a neighbour continues downward: two or more anchors bridge it.
            anchored = [edge[1] for m, edge in others if rows[m]["z_mm"][0] < face_low - PROBE_LEVEL_MM]
            if len(anchored) >= 2:
                span = max(_norm(_sub(a, b)) for a in anchored for b in anchored)
                kind = "bridge" if span > PROBE_TOL_MM else kind
        row["down"] = {"area_mm2": round(sum(angles.values()), 6), "max_angle_deg": float(max(angles)),
                       "angles": [[float(a), round(v, 6)] for a, v in sorted(angles.items())], "kind": kind,
                       "span_mm": None if span is None else round(span, 6),
                       "start": {"bed_mm2": round(start["bed_mm2"], 6), "part_mm2": round(start["part_mm2"], 6)}}
    for row in rows:
        samples = row.pop("_samples")
        tested = samples[::max(1, len(samples) // PROBE_RAY_SAMPLES)][:PROBE_RAY_SAMPLES]
        visible = {}
        for key, direction in (plan.get("directions") or {}).items():
            clear = sum(1 for point, nrm in tested if _dot(nrm, direction) >= -1e-9 and rays.first(
                tuple(point[i] + nrm[i] * PROBE_RAY_OFFSET_MM for i in range(3)), tuple(direction)) is None)
            visible[key] = round(clear / len(tested), 6) if tested else 0.0
        row["visible"] = visible

    features: dict[str, Any] = {}
    for ident, locator in sorted(locators.items()):
        matched: set[int] = set()
        for patch in locator.get("patches") or ():
            matched |= {n for n, face in enumerate(faces) if _distance(tuple(patch["point_mm"]), face) <= PROBE_TOL_MM}
        entry: dict[str, Any] = {"kind": locator.get("kind"), "faces": sorted(ids[n] for n in matched), "mapped": bool(matched)}
        width = _locator_width(locator)
        if width is not None:
            entry["width_mm"] = round(width, 6)
        features[ident] = entry
        for n in matched:
            rows[n]["features"].append(ident)
    for n, row in enumerate(rows):
        if "down" in row:
            row["down"]["start"]["features"] = sorted({f for m in starts.get(n, ()) if m >= 0 for f in rows[m]["features"]})

    bores = holes_module.bore_walls(solid)
    found_holes = []
    for hole in holes_module.holes(solid):
        axis, position = hole["axis"], hole["position"]
        feats = sorted({f for row in rows if "cylinder" in row and not row["cylinder"]["convex"]
                        and abs(row["cylinder"]["radius_mm"] - hole["diameter_mm"] / 2.0) <= 1e-3
                        and _norm(_cross(row["cylinder"]["axis"], axis)) <= 1e-6
                        and _norm(_cross(_sub(row["cylinder"]["location"], position), axis)) <= 1e-3
                        for f in row["features"]})
        item = {**hole, "features": feats, "floor_mm": None}
        if not hole["through"]:
            floor = tuple(position[i] + axis[i] * (hole["depth_mm"] + PROBE_RAY_OFFSET_MM) for i in range(3))
            exit_ = rays.first(floor, tuple(axis))
            item["floor_mm"] = None if exit_ is None else round(exit_[0] + PROBE_RAY_OFFSET_MM, 6)
        found_holes.append(item)
    corners, pins = [], []
    cylinders = [(n, row) for n, row in enumerate(rows) if "cylinder" in row]
    for n, row in cylinders:
        cyl = row["cylinder"]
        if not cyl["convex"] and not any(faces[n].IsSame(wall) for wall in bores):
            corners.append({"radius_mm": cyl["radius_mm"], "axis": cyl["axis"], "length_mm": cyl["length_mm"],
                            "features": row["features"]})
        if cyl["convex"] and cyl["span_rad"] >= FULL_TURN:
            pins.append({"diameter_mm": round(2.0 * cyl["radius_mm"], 6), "features": row["features"]})
    bends = []
    for n, outer in cylinders:
        for m, inner in cylinders:
            a, b = outer["cylinder"], inner["cylinder"]
            if not a["convex"] or b["convex"] or a["span_rad"] >= FULL_TURN or b["span_rad"] >= FULL_TURN:
                continue
            if _norm(_cross(a["axis"], b["axis"])) > 1e-6 or _norm(_cross(_sub(a["location"], b["location"]), a["axis"])) > 1e-4:
                continue
            thickness = a["radius_mm"] - b["radius_mm"]
            if thickness > 1e-6:
                bends.append({"inner_radius_mm": b["radius_mm"], "thickness_mm": round(thickness, 6), "axis": b["axis"],
                              "axis_point": b["location"], "features": sorted(set(outer["features"]) | set(inner["features"]))})
    walls = None
    if plan.get("wall_mm"):
        walls = minimum_wall(solid, plan["wall_mm"])
        walls["features"] = sorted({f for n, face in enumerate(faces) if walls["location_mm"] is not None
                                    and _distance(tuple(walls["location_mm"]), face) <= PROBE_TOL_MM
                                    for f in rows[n]["features"]})
    bed_faces = [row for row in rows if row.get("bed")]
    bed_low = bed_high = None
    for n, row in enumerate(rows):
        if row.get("bed"):
            f_low, f_high = occ.bbox(faces[n])
            bed_low = [min(bed_low[0], f_low[0]), min(bed_low[1], f_low[1])] if bed_low else [f_low[0], f_low[1]]
            bed_high = [max(bed_high[0], f_high[0]), max(bed_high[1], f_high[1])] if bed_high else [f_high[0], f_high[1]]
    return {"solids": 1, "shells": occ.count(solid, "SHELL"), "volume_mm3": round(volume, 6),
            "bbox_mm": {"min": rounded_xyz_tuple(low), "max": rounded_xyz_tuple(high)}, "centre_mm": rounded_xyz_tuple(centre),
            "bed": {"z_mm": round(bed_z, 6), "contact_area_mm2": round(sum(r["area_mm2"] for r in bed_faces), 6),
                    "contact_min": bed_low, "contact_max": bed_high},
            "faces": rows, "holes": found_holes, "corners": corners, "pins": pins, "bends": bends, "walls": walls,
            "features": features, "prismatic_axis": _prismatic_axis(rows, low, high)}


def rounded_xyz_tuple(point: tuple[float, float, float]) -> list[float]:
    return [round(c, 6) + 0.0 for c in point]


def _prismatic_axis(rows: list[dict[str, Any]], low: Any, high: Any) -> list[float] | None:
    """An axis every face is parallel to (planes with a perpendicular normal, cylinders along it), apart
    from end caps normal to it on the solid's two extreme planes; None when the solid is no constant section."""
    corners = [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
    candidates = [row["normal"] for row in rows if "normal" in row] + [row["cylinder"]["axis"] for row in rows if "cylinder" in row]
    for axis in candidates:
        ends = (min(_dot(c, axis) for c in corners), max(_dot(c, axis) for c in corners))
        fits = True
        for row in rows:
            if "normal" in row:
                c = _dot(row["normal"], axis)
                if abs(c) > 1.0 - 1e-6:  # an end cap: it must lie on one of the two extreme planes
                    level = row["offset_mm"] * (1.0 if c > 0 else -1.0)
                    fits = min(abs(level - ends[0]), abs(level - ends[1])) <= PROBE_LEVEL_MM
                else:
                    fits = abs(c) < 1e-6
            elif "cylinder" in row:
                fits = _norm(_cross(row["cylinder"]["axis"], axis)) < 1e-6
            else:
                fits = False
            if not fits:
                break
        if fits:
            return [round(c, 9) + 0.0 for c in axis]
    return None


def merge_probes(members: list[dict[str, Any]], frame: str, build_axis: list[float] | None) -> dict[str, Any]:
    """One component's facts from its member solids: every list concatenated, the worst bed contact and
    wall kept, features mapped on any member. Equivalent members are probed one by one, never as a compound."""
    first = members[0]
    worst_bed = min(members, key=lambda m: m["bed"]["contact_area_mm2"])
    walls = [m["walls"] for m in members if m["walls"] is not None]
    measured = [w for w in walls if w["thickness_mm"] is not None]
    features: dict[str, Any] = {}
    for member in members:
        for ident, entry in member["features"].items():
            merged = features.setdefault(ident, {**entry, "faces": []})
            merged["faces"] = sorted(set(merged["faces"]) | set(entry["faces"]))
            merged["mapped"] = merged["mapped"] or entry["mapped"]
    axes = {tuple(m["prismatic_axis"]) if m["prismatic_axis"] else None for m in members}
    low = [min(m["bbox_mm"]["min"][i] for m in members) for i in range(3)]
    high = [max(m["bbox_mm"]["max"][i] for m in members) for i in range(3)]
    return {"format": 1, "frame": frame, "build_axis_native": build_axis,
            "solids": len(members), "shells": sum(m["shells"] for m in members),
            "volume_mm3": round(sum(m["volume_mm3"] for m in members), 6), "bbox_mm": {"min": low, "max": high},
            "centre_mm": worst_bed["centre_mm"], "bed": worst_bed["bed"],
            "faces": [f for m in members for f in m["faces"]], "holes": [h for m in members for h in m["holes"]],
            "corners": [c for m in members for c in m["corners"]], "pins": [p for m in members for p in m["pins"]],
            "bends": [b for m in members for b in m["bends"]],
            "walls": (min(measured, key=lambda w: w["thickness_mm"]) if measured else (walls[0] if walls else None)),
            "features": features, "unmapped": sorted(i for i, e in features.items() if not e["mapped"]),
            "prismatic_axis": first["prismatic_axis"] if len(axes) == 1 else None}


def probe(shape: Any, *, frame: str, locators: dict[str, Any], plan: dict[str, Any],
          build_axis: list[float] | None = None) -> dict[str, Any]:
    """The process_checks facts of one component `shape` (all its member solids, each probed alone)."""
    members = [probe_solid(solid, index + 1, locators, plan) for index, solid in enumerate(solids_of(shape))]
    if not members:
        raise RecordError("the component file holds no solid")
    return merge_probes(members, frame, build_axis)


def processes(hw: Path) -> dict[str, str]:
    import yaml

    found = {}
    for record in sorted((hw / "parts").glob("*/part.yaml")):
        data = yaml.safe_load(record.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("process"), str):
            bought = data.get("make_buy") == "buy"   # made to drawing or catalogue: checked in its bought form
            found[record.parent.name] = "purchased" if bought else data["process"]
    return found


def kernel_facts(parts_dir: Path) -> dict[str, dict[str, Any]]:
    path = parts_dir / KERNEL_FACTS
    if not path.is_file():
        return {}
    parts = json.loads(path.read_text(encoding="utf-8")).get("parts", {})
    return {pn: row for pn, row in parts.items() if "kernel_valid" in row and "kernel_volume_mm3" in row}


def check(args: argparse.Namespace, pose: Any) -> dict[str, Any]:
    from cadmodel.stepfile import READBACK_REL

    level = FIDELITY.index(args.fidelity)
    wall_thresholds = minimum_wall_thresholds(args.hw)  # an invalid override stops the check before any part loads
    loaded = pose.load_definitions(args.hw, args.parts_dir)
    assembly = pose.load_assembly(args.hw)
    process = processes(args.hw)
    facts = kernel_facts(args.parts_dir)
    form_classes = {pn: part_form_class(args.hw, pn) for pn in loaded}
    findings: list[dict[str, Any]] = []
    parts: dict[str, Any] = {}
    cylinders: dict[str, list[dict[str, Any]]] = {}
    for pn, part in sorted(loaded.items()):
        metrics = measure(part.shape)
        cylinders[pn] = metrics.pop("_cylinders")
        obb_volume = metrics.pop("_obb_volume_mm3")
        thickness_fraction = metrics.pop("_obb_thickness_fraction")
        volume = shape_volume(part.shape)
        row = {"name": part.name, "process": process.get(pn), "form_class": form_classes[pn],
               "valid": is_valid(part.shape), "solids": count_solids(part.shape),
               "volume_mm3": round(volume, 3),
               "obb_fill_fraction": round(volume / obb_volume, 4) if obb_volume else 0.0,
               "obb_thickness_fraction": round(thickness_fraction, 4), **metrics}
        if not row["valid"]:
            findings.append({"check": "brep_invalid", "part": pn, "source": "step"})
        parts[pn] = row
        if pn in facts:
            kernel_v = facts[pn]["kernel_volume_mm3"]
            row["kernel_valid"] = facts[pn]["kernel_valid"]
            row["readback_rel_error"] = abs(volume - kernel_v) / max(abs(kernel_v), 1e-12)
            if not row["kernel_valid"]:
                findings.append({"check": "brep_invalid", "part": pn, "source": "kernel",
                                 "errors": facts[pn].get("kernel_errors", [])})
            if row["readback_rel_error"] > READBACK_REL:
                findings.append({"check": "step_readback", "part": pn, "rel_error": row["readback_rel_error"],
                                 "max": READBACK_REL, "kernel_volume_mm3": kernel_v, "step_volume_mm3": volume})
        if level < 2:
            continue
        required_wall = wall_thresholds.get(row["process"])  # none for purchased parts or a process without a minimum
        row["min_wall"] = {"required_mm": None}
        if required_wall is not None:
            wall = row["min_wall"] = minimum_wall(part.shape, required_wall)
            if wall["thickness_mm"] is None:
                findings.append({"check": "min_wall_unmeasured", "part": pn, "min_mm": required_wall,
                                 "faces": wall["faces"], "faces_sampled": wall["faces_sampled"],
                                 "samples": wall["samples"]})
            elif wall["thickness_mm"] < required_wall:
                findings.append({"check": "min_wall", "part": pn, "thickness_mm": wall["thickness_mm"],
                                 "location_mm": wall["location_mm"], "min_mm": required_wall})
        if row["process"] == "purchased":
            continue
        has_blocky_faces = (row["axis_planar_fraction"] >= BLOCK_PLANAR_FRACTION
                            or row["blend_faces"] + row["freeform_faces"] == 0)
        if (form_classes[pn] == "solid" and has_blocky_faces
                and row["obb_fill_fraction"] >= BLOCK_MIN_FILL_FRACTION
                and row["obb_thickness_fraction"] > BLOCK_MAX_THICKNESS_FRACTION):
            findings.append({"check": "block_like", "part": pn, "axis_planar_fraction": row["axis_planar_fraction"],
                             "blend_faces": row["blend_faces"], "freeform_faces": row["freeform_faces"],
                             "obb_fill_fraction": row["obb_fill_fraction"],
                             "obb_thickness_fraction": row["obb_thickness_fraction"]})
        if row["process"] == "fdm" and row["min_convex_radius"] is not None and row["min_convex_radius"] < args.fdm_min_radius:
            findings.append({"check": "fdm_exterior_radius", "part": pn, "radius": row["min_convex_radius"],
                             "min": args.fdm_min_radius})
        if row["process"] == "cnc" and row["min_concave_radius"] is not None and row["min_concave_radius"] < args.cnc_tool_radius:
            findings.append({"check": "cnc_internal_radius", "part": pn, "radius": row["min_concave_radius"],
                             "min": args.cnc_tool_radius})
    joints: dict[str, Any] = {}
    if level >= 1:
        for joint in assembly.joints:
            row = {"type": joint.type, "axis": list(joint.axis), "origin": list(joint.origin), "parts": {}}
            for oid in (joint.parent, joint.child):
                pn = assembly.pn(oid)
                ref = assembly.inventory.get(oid).ref if oid in assembly.inventory else pn
                # The joint frame is in assembly coordinates; a placed occurrence's bores are in its PN's frame.
                rest = (pose.Rigid.from_placement(assembly.inventory.get(oid).placement).inverse()
                        if oid in assembly.inventory else pose.Rigid.identity())
                axis = pose.Rigid(rest.rot, (0.0, 0.0, 0.0)).apply(joint.axis)
                kind = "shank" if process.get(pn) == "purchased" else "bore"
                found = bore_offset(cylinders[pn], axis, rest.apply(joint.origin), kind) if pn in cylinders else None
                row["parts"][ref] = found
                if found is None or found["offset_mm"] > args.joint_tol:
                    findings.append({"check": "joint_bore_missing", "joint": joint.id, "part": ref, "kind": kind,
                                     "nearest": found, "tol_mm": args.joint_tol})
            joints[joint.id] = row
    for finding in findings:
        finding["blocking"] = finding["check"] in BLOCKING_CHECKS
    blocking = any(finding["blocking"] for finding in findings)
    return {"status": "fail" if blocking else "pass", "fidelity": args.fidelity, "parts": parts,
            "joints": joints, "findings": findings}


def load_layout() -> Any:
    """cad-record layout.py under its shared module name."""
    module = sys.modules.get("cad_record_layout")
    if module is None:
        spec = importlib.util.spec_from_file_location("cad_record_layout", LAYOUT_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["cad_record_layout"] = module
        spec.loader.exec_module(module)
    return module


def load_check_packet() -> Any:
    """cad-record check_packet.py, the packet contract every producer publishes through, by its shared name."""
    module = sys.modules.get("cad_record_check_packet")
    if module is None:
        spec = importlib.util.spec_from_file_location("cad_record_check_packet", CHECK_PACKET_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["cad_record_check_packet"] = module
        spec.loader.exec_module(module)
    return module


def load_process_checks() -> Any:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import process_checks

    return process_checks


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_geometry(pc: Any, record: Any, folder: Path, label: str, manifest: dict[str, Any],
                     today: date) -> dict[tuple[str, str], Any]:
    """The probe facts of every exported manufacturing component of a built version, keyed (PN, COMP).
    Each component file is read alone; native feature locators are moved by its placement transform."""
    from cadmodel import manufacture

    def document(key: str) -> dict[str, Any]:
        name = manifest.get(key)
        return json.loads((folder / name).read_text(encoding="utf-8")).get("parts", {}) if name else {}

    same, different = document("drawing_features"), document("manufacturing_features")
    found = {}
    for pn, entry in sorted((manifest.get("manufacturing") or {}).items()):
        if pn not in record.parts or not entry.get("path") or not entry.get("components"):
            continue  # an uncovered process has no output; its handlers report the gap
        state = (entry.get("state") or {}).get("relationship")
        locators = ((different.get(pn) or {}).get("features") if state == "different" else same.get(pn)) or {}
        for component, facts in sorted(entry["components"].items()):
            name = next(n for n in facts["files"] if n.endswith("part.step"))
            path = folder / entry["path"] / name
            part, problem = manufacture.read_part(path, pn)
            if part is None:
                raise RecordError(f"{path}: {problem}")
            placement = facts.get("placement") or {}
            transform = placement.get("transform")
            frame = placement.get("frame") or "model"
            build_axis = list(transform[2][:3]) if frame == "bed" and transform else None  # R^T z: the build axis, native
            measured = probe(part.shape, frame=frame, locators=transform_locators(locators, transform),
                             plan=pc.probe_plan(record, pn, component, today=today), build_axis=build_axis)
            evidence = ({"path": f"hw/versions/{label}/{entry['path']}/{name}", "sha256": _sha256(path)},)
            found[(pn, component)] = pc.Geometry(pn, component, measured, evidence)
    return found


def check_version(args: argparse.Namespace, pose: Any) -> tuple[dict[str, Any], int]:
    """The legacy fidelity checks on a built version's installed parts, then the process-dispatched
    manufacturing checks of its record snapshot and manufacturing outputs, published as the version's
    hw/checks/<V>/process bundle (or written to the scratch --out path instead)."""
    from cadmodel import record as model_record

    layout = load_layout()
    label, folder, record_dir = layout.built_version(args.hw, args.version)
    legacy = argparse.Namespace(**{**vars(args), "hw": record_dir, "parts_dir": folder / "parts"})
    report = check(legacy, pose)
    report["version"] = label
    pc = load_process_checks()
    record = model_record.read_snapshot(model_record.read_folder(record_dir)).intent
    blocking = report["status"] == "fail"
    if record.version != pc.intent().RECORD_VERSION:
        problem = pc.intent().migration_problem(dict(record.product), f"hw/versions/{label}/record/product.yaml")
        report["manufacturing"] = {"status": "not_assessed", "record_version": record.version,
                                   "problems": [problem.finding()] if problem is not None else []}
        return report, 1
    today = args.on or date.today()
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    code = hashlib.sha256((pc.code_sha256() + _sha256(Path(__file__))).encode("ascii")).hexdigest()
    recipe = {"producer": pc.PRODUCER, "code_sha256": code, "probe_format": pc.PROBE_FORMAT, "on": today.isoformat()}
    params = {"V": label, "S": PROCESS_SUBJECT}
    reused = None if args.out else layout.reusable(args.hw, "bundle.checks", params, recipe, replace=args.replace)
    if reused is not None:
        packet, bundle = json.loads((reused / "result.json").read_text(encoding="utf-8")), reused
    else:
        geometry = version_geometry(pc, record, folder, label, manifest, today)
        packet = pc.assess(record, geometry, today=today).packet(
            label, {"version_manifest": _sha256(manifest_path), "producer_code": code})
        load_check_packet().validate(packet)  # never seal or hand on a packet that breaks its schema
        if args.out:
            bundle = layout.scratch_path(args.hw, args.out, "--out")
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
        else:
            stage = layout.staging(args.hw, "bundle.checks")
            (stage / "result.json").write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
            bundle = layout.publish(args.hw, "bundle.checks", params, stage, recipe=recipe, producer=pc.PRODUCER,
                                    status=packet["result"], replace=args.replace).path
    counts: dict[str, int] = {}
    for outcome in packet["outcomes"]:
        counts[outcome["outcome"]] = counts.get(outcome["outcome"], 0) + 1
    report["manufacturing"] = {
        "status": packet["result"], "complete": packet["complete"], "bundle": str(bundle), "reused": reused is not None,
        "outcomes": dict(sorted(counts.items())),
        "open": [{"obligation": o["obligation"], "outcome": o["outcome"]} for o in packet["outcomes"] if o["outcome"] in ("fail", "unknown")],
        "findings": [{key: f[key] for key in ("id", "code", "severity", "outcome", "subject")}
                     for f in packet["findings"] if f["severity"] in ("critical", "major") and f["outcome"] in ("fail", "unknown")],
    }
    return report, 1 if blocking or packet["result"] != "pass" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cad-design fidelity self-check and process-dispatched manufacturing checks")
    parser.add_argument("--hw", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--parts-dir", type=Path, help="per-part STEP files: the fidelity self-check only")
    source.add_argument("--version", help="a built version (or latest): fidelity checks plus the manufacturing checks")
    parser.add_argument("--fidelity", choices=FIDELITY, default="F2")
    parser.add_argument("--joint-tol", type=float, default=0.05)
    parser.add_argument("--cnc-tool-radius", type=float, default=1.0)
    parser.add_argument("--fdm-min-radius", type=float, default=1.0)
    parser.add_argument("--replace", action="store_true", help="with --version: retain and replace a bundle of another recipe")
    parser.add_argument("--out", type=Path, help="with --version: write the packet to this .cad-work path instead of publishing")
    parser.add_argument("--on", type=date.fromisoformat, help="with --version: the date calibrations must be valid on (default today)")
    args = parser.parse_args(argv)
    if args.version is None and (args.replace or args.out or args.on):
        parser.error("--replace, --out and --on need --version")
    pose = load_pose()
    sys.path.insert(0, str(CAD_MODEL_SCRIPTS))
    try:
        with pose.stdout_to_stderr():
            if args.version is None:
                report = check(args, pose)
                code = 1 if report["status"] == "fail" else 0
            else:
                report, code = check_version(args, pose)
    except (pose.MotionError, RecordError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 2
    except Exception as error:
        layout, packets = sys.modules.get("cad_record_layout"), sys.modules.get("cad_record_check_packet")
        if (layout is not None and isinstance(error, layout.LayoutError)) or (
                packets is not None and isinstance(error, packets.PacketError)):
            print(json.dumps({"status": "error", "error": str(error)}))
            return 2
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"status": "error", "error": f"{type(error).__name__}: {error}"}))
        return 2
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
