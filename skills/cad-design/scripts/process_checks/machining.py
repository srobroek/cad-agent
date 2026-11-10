"""CNC machining handler: stock, setups and workholding, tool access, features and precision.

Tools come from the operation's setup.access.tools and the capability profile's tools, each with its
sourced diameter and reach; achievable tolerance and finish come only from calibrations. A collision-free
nominal model is never called machinable: access PASS covers the radii, reach, approach sides and
undercuts measured, not CAM, fixture or chatter behaviour.

Calibration characteristics read: min_wall (mm, wall >= upper), dimensional_deviation (mm, scope.feature
a critical-surface role where given), surface_roughness (um, criterion <= upper).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .additive import wall_check
from .core import (FAIL, NOT_APPLICABLE, PASS, POLICY, UNKNOWN, Calibrated, Context, Draft, Geometry, Result,
                   access_directions, combine, direction_key, dot, gated, length, policy, source_dict, text, unknown)

FINISH_PURPOSES = frozenset({"stock", "finish"})


@dataclass(frozen=True)
class Tool:
    name: str
    diameter: tuple[float, float] | None  # mm bounds
    reach: tuple[float, float] | None  # mm bounds
    source: dict[str, Any] | None


def _quantity(ctx: Context, data: Any) -> tuple[tuple[float, float] | None, dict[str, Any] | None]:
    if not isinstance(data, Mapping) or data.get("status") != "known":
        return None, None
    bounds = ctx.mi.quantity(data["value"]).to("mm").bounds
    sources = data.get("sources") or []
    return bounds, (source_dict(sources[0]) if sources else None)


def tools(ctx: Context) -> list[Tool]:
    found = []
    access = ((ctx.operation.setup or {}).get("access") or {})
    for item in access.get("tools") or ():
        diameter, source = _quantity(ctx, item.get("diameter"))
        reach, reach_source = _quantity(ctx, item.get("reach"))
        found.append(Tool(item.get("description", "tool"), diameter, reach, source or reach_source))
    profile = ctx.profile
    for item in (profile.raw.get("tools") or ()) if profile is not None else ():
        parameters = item.get("parameters") or {}
        diameter, source = _quantity(ctx, parameters.get("diameter"))
        reach, reach_source = _quantity(ctx, parameters.get("reach"))
        found.append(Tool(item.get("id", "tool"), diameter, reach, source or reach_source))
    return found


def sides(ctx: Context) -> tuple[list[tuple[str, Any]], list[str]]:
    access = ((ctx.operation.setup or {}).get("access") or {})
    return access_directions(access.get("sides") or ())


def stock(ctx: Context) -> Result:
    method = "cnc stock: identified material, grade, dimensions and machinability approval"
    op = ctx.operation
    data = (op.setup or {}).get("stock")
    findings: list[Draft] = []
    if data is None:
        earlier = ctx.before()
        if earlier or op.input_state:
            source = earlier[-1].id if earlier else op.input_state
            return Result(PASS, method, f"the input is the output state of {source}; its stock is that operation's")
        findings.append(Draft("process.cnc.stock_unknown", "major", UNKNOWN, unknown("no stock recorded"),
                              policy("stock", "equals", "material, grade and dimensions recorded"), "part.yaml route setup",
                              "the operation names no stock", "stock availability and machinability are unknown",
                              remedy="record the stock material profile, grade, form and dimensions",
                              recheck=["process.cnc.stock"], earliest_stage="parts"))
        return Result(UNKNOWN, method, "no stock", findings)
    parts: list[tuple[str, str]] = []
    material = data.get("material_profile")
    if not isinstance(material, str) or material not in ctx.record.materials:
        findings.append(Draft("process.cnc.stock_material", "major", UNKNOWN, text(f"stock material {material}"),
                              policy("stock_material_profile", "equals", "a material profile of the record"),
                              "part.yaml route setup", "record content", "the stock material is not identified",
                              remedy="name the stock's material profile", recheck=["process.cnc.stock"], earliest_stage="parts"))
        parts.append((UNKNOWN, "stock material unknown"))
    elif ctx.material_id and material != ctx.material_id:
        findings.append(Draft("process.cnc.stock_material", "major", FAIL, text(f"stock {material}"),
                              policy("stock_material_profile", "equals", ctx.material_id), "part.yaml route setup",
                              "record content", f"the stock is {material} but the component's material profile is {ctx.material_id}",
                              remedy="make the stock and the part's material profile the same grade",
                              recheck=["process.cnc.stock", "route.consistency"], earliest_stage="parts"))
        parts.append((FAIL, "stock material differs from the part"))
    else:
        parts.append((PASS, f"stock {material}"))
    for key in ("grade", "dimensions"):
        fact = data.get(key)
        known = isinstance(fact, Mapping) and fact.get("status") == "known"
        if key == "grade" and not known and isinstance(material, str) and material in ctx.record.materials:
            profile = ctx.record.materials[material]
            known = profile.level == "grade" and profile.grade.known
        if not known:
            findings.append(Draft(f"process.cnc.stock_{key}", "minor", UNKNOWN, unknown(f"stock {key} not recorded"),
                                  policy(f"stock_{key}", "equals", "known"), "part.yaml route setup", "record content",
                                  f"the stock {key} is unknown: availability and machinability cannot be confirmed",
                                  remedy=f"record the stock {key}", recheck=["process.cnc.stock"], earliest_stage="parts"))
            parts.append((UNKNOWN, f"stock {key} unknown"))
    parts.append(_approval(ctx, material if isinstance(material, str) else ctx.material_id, findings))
    thickness, _ = _quantity(ctx, data.get("thickness"))
    g = ctx.geometry
    box = g.facts.get("bbox_mm") if g is not None else None
    if g is not None and thickness is not None and box:
        smallest = min(box["max"][i] - box["min"][i] for i in range(3))
        if thickness[0] < smallest - 1e-9:
            findings.append(Draft("process.cnc.stock_size", "major", FAIL, length(thickness[0]),
                                  policy("stock_thickness", ">=", round(smallest, 6), "mm"),
                                  g.method("bounding box"), "exact B-rep box",
                                  "the stock is thinner than the part's smallest extent",
                                  remedy="choose thicker stock", recheck=["process.cnc.stock"], earliest_stage="parts"))
            parts.append((FAIL, "stock thinner than the part"))
    return combine(method, parts, findings)


def _approval(ctx: Context, material: str | None, findings: list[Draft]) -> tuple[str, str]:
    profile, mi, kind = ctx.capability, ctx.mi, ctx.operation.kind
    approvals = profile.materials
    if mi.is_fact(approvals):
        findings.append(Draft(f"process.{kind}.material_approval", "minor", UNKNOWN,
                              unknown("material approvals unknown"),
                              unknown(approvals.reason or "the capability profile records no material approvals"),
                              "capability profile materials", "not recorded",
                              "machinability of this material on this machine is unknown",
                              remedy="record which materials the machine or supplier is approved for",
                              recheck=[f"process.{kind}"], earliest_stage="parts"))
        return UNKNOWN, "material approvals unknown"
    entry = next((a for a in approvals if a.get("material") == material), None)
    if entry is None:
        findings.append(Draft(f"process.{kind}.material_approval", "major", UNKNOWN, text(f"material {material}"),
                              policy("material_approval", "equals", "approved for this capability"),
                              "capability profile materials", "no approval entry",
                              f"{profile.id} is not approved for {material}",
                              remedy=f"approve {material} on {profile.id} with evidence, or choose an approved material",
                              recheck=[f"process.{kind}"], earliest_stage="parts"))
        return UNKNOWN, f"{material} not approved"
    if entry.get("status") == "forbidden":
        findings.append(Draft(f"process.{kind}.material_forbidden", "major", FAIL, text(f"material {material}"),
                              {"property": "material_approval", "operator": "equals", "value": "approved",
                               "source": source_dict(entry["source"])},
                              "capability profile materials", "recorded approval",
                              f"{profile.id} forbids {material}: {entry.get('reason')}",
                              remedy="choose another material or capability", recheck=[f"process.{kind}"],
                              earliest_stage="parts"))
        return FAIL, f"{material} forbidden"
    modes = entry.get("operations")
    if modes and "machine" not in modes:
        findings.append(Draft("process.cnc.material_operation", "major", FAIL, text(f"approved for {', '.join(modes)}"),
                              {"property": "material_operations", "operator": "one_of", "value": ["machine"],
                               "source": source_dict(entry["source"])}, "capability profile materials", "recorded approval",
                              f"{material} is approved on {profile.id} only for {', '.join(modes)}",
                              remedy="approve machining of this material", recheck=["process.cnc.stock"], earliest_stage="parts"))
        return FAIL, "not approved for machining"
    return PASS, f"{material} approved on {profile.id}"


def setup(ctx: Context) -> Result:
    method = "cnc setup: workholding, datums, axes against access sides and clamping on critical surfaces"
    op = ctx.operation
    data = op.setup or {}
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    workholding = data.get("workholding")
    if not workholding or not (isinstance(workholding.get("method"), Mapping) and workholding["method"].get("status") == "known"):
        findings.append(Draft("process.cnc.workholding_unknown", "major", UNKNOWN, unknown("no clamp plan"),
                              policy("workholding", "equals", "a recorded method, contact regions and fixture"),
                              "part.yaml route setup", "the operation records no workholding",
                              "how the part is held, where clamps touch it and how much it deflects are unknown",
                              remedy="record the workholding method, fixture, contact regions, clamping force and allowable "
                                     "deformation", recheck=["process.cnc.setup"], earliest_stage="detail"))
        parts.append((UNKNOWN, "missing clamp plan"))
    else:
        critical = ctx.critical_features()
        clamped = [t.get("feature") for t in workholding.get("contact_regions") or ()]
        bad = [f for f in clamped if f in critical and critical[f].role not in ("datum", "locating")]
        for feature in bad:
            findings.append(Draft("process.cnc.clamp_on_critical", "major", FAIL,
                                  text(f"clamp contact on {critical[feature].role} surface {feature}"),
                                  policy("clamp_contact", "equals", "datum or non-critical surfaces only"),
                                  "part.yaml route setup workholding", "record content",
                                  f"clamping marks or distorts the {critical[feature].role} surface {feature}",
                                  remedy="clamp on datum or non-critical surfaces, or machine the surface in a later setup",
                                  recheck=["process.cnc.setup"], feature=feature))
        parts.append((FAIL if bad else PASS, "clamping on critical surfaces" if bad else "workholding recorded"))
        force, deformation = workholding.get("clamping_force"), workholding.get("allowable_deformation")
        if not (isinstance(force, Mapping) and force.get("status") == "known"
                and isinstance(deformation, Mapping) and deformation.get("status") == "known"):
            findings.append(Draft("process.cnc.clamping_distortion", "minor", UNKNOWN, unknown("clamping force or limit"),
                                  policy("clamping", "equals", "force and allowable deformation recorded"),
                                  "part.yaml route setup workholding", "not recorded",
                                  "clamping distortion is not assessed", remedy="record the clamping force and allowable deformation",
                                  recheck=["process.cnc.setup"]))
            parts.append((UNKNOWN, "clamping distortion unassessed"))
    if not data.get("datums"):
        findings.append(Draft("process.cnc.datums_unknown", "minor", UNKNOWN, unknown("no datums"),
                              policy("setup_datums", "equals", "recorded"), "part.yaml route setup", "not recorded",
                              "the datum strategy of this setup is unknown", remedy="record the datums this setup locates on",
                              recheck=["process.cnc.setup", "process.cnc.precision"]))
        parts.append((UNKNOWN, "datums unknown"))
    parsed, bad = sides(ctx)
    axes = ctx.capability.parameters.get("axes")
    count = axes.value.value if axes is not None and axes.known and hasattr(axes.value, "value") else None
    if count is not None and count < 4 and len({key for key, _ in parsed}) > 1:
        findings.append(Draft("process.cnc.setup_count", "minor", FAIL, {"value": len(parsed), "units": "count"},
                              {"property": "access_sides_per_setup", "operator": "<=", "value": 1, "units": "count",
                               "source": source_dict(axes.sources[0]) if axes.sources else dict(POLICY)},
                              "capability axes against the operation's access sides", "record content",
                              f"a {int(count)}-axis machine reaches one side per setup; this operation names {len(parsed)}",
                              remedy="record one operation per setup, with its datum transfer", recheck=["process.cnc.setup"]))
        parts.append((FAIL, "several sides in one setup"))
    elif count is None and parsed:
        parts.append((UNKNOWN, "machine axes unknown"))
        findings.append(Draft("process.cnc.axes_unknown", "minor", UNKNOWN, unknown("machine axes"),
                              unknown("the capability profile records no axes"), "capability profile parameters",
                              "not recorded", "whether the sides are reachable in one setup is unknown",
                              remedy="record the machine's axes", recheck=["process.cnc.setup"]))
    return combine(method, parts, findings)


def access(ctx: Context) -> Result:
    method = "cnc tool access: internal radii, reach, approach sides, drilled holes and undercuts"
    g = ctx.measured("tool access")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    parsed, bad = sides(ctx)
    if not parsed or bad:
        findings.append(Draft("process.cnc.access_sides", "major", UNKNOWN,
                              text(f"sides {', '.join(bad) or 'not recorded'}"),
                              policy("access_sides", "one_of", ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]),
                              "part.yaml route setup access", "sides are axis directions of the native frame",
                              "which features a tool can reach is unknown",
                              remedy="record the access sides of the setup as +X .. -Z", recheck=["process.cnc.access"]))
        return combine(method, [(UNKNOWN, "access sides unknown")], findings)
    available = tools(ctx)
    parts += _corners(g, available, findings)
    parts += _drilled(ctx, g, available, parsed, findings)
    parts.append(_undercuts(g, parsed, findings))
    return combine(method, parts, findings,
                   uncertainty="straight tool axes along the access sides; holder geometry beyond reach is not modelled")


def _corners(g: Geometry, available: list[Tool], findings: list[Draft]) -> list[tuple[str, str]]:
    corners = g.corners()
    if not corners:
        return []
    sized = [(t, t.diameter) for t in available if t.diameter is not None]
    if not sized:
        findings.append(Draft("process.cnc.tools_unknown", "major", UNKNOWN, unknown("no tool diameters"),
                              unknown("the operation and capability record no tool diameter"),
                              "part.yaml setup access tools and capability tools", "not recorded",
                              "internal radii and pocket reach are unassessed",
                              remedy="record the cutters with sourced diameter and reach", recheck=["process.cnc.access"]))
        return [(UNKNOWN, "tools unknown")]
    parts: list[tuple[str, str]] = []
    for corner in corners:
        radius, depth = corner["radius_mm"], corner.get("length_mm") or 0.0
        feature = _one(corner.get("features"))
        fits = [t for t, diameter in sized if diameter[1] / 2.0 <= radius + 1e-9]
        if not fits:
            smallest, diameter = min(sized, key=lambda item: item[1][1])
            findings.append(Draft("process.cnc.internal_radius", "major", FAIL, length(radius),
                                  {"property": "internal_radius", "operator": ">=", "value": round(diameter[1] / 2.0, 6),
                                   "units": "mm", "source": smallest.source or dict(POLICY)},
                                  g.method("concave corner cylinders"), "exact cylinder radius; tool diameter as sourced",
                                  f"an internal corner of radius {radius:g} mm is smaller than the smallest cutter radius "
                                  f"({smallest.name}, {diameter[1]:g} mm)",
                                  remedy="enlarge the corner radius to the cutter radius or add a smaller cutter",
                                  alternatives=[{"text": "dog-bone or relief corners", "tradeoffs": "visible relief; changes the mating part's clearance"},
                                                {"text": "EDM or a laser-cut plate", "tradeoffs": "another process route and its own capability"}],
                                  recheck=["process.cnc.access"], feature=feature))
            parts.append((FAIL, f"corner R{radius:g} below the smallest cutter radius"))
            continue
        reaching = [(t, t.reach) for t in fits if t.reach is not None]
        if not reaching:
            findings.append(Draft("process.cnc.reach_unknown", "minor", UNKNOWN, length(depth),
                                  unknown("no reach recorded for the cutters that fit the corner"),
                                  g.method("concave corner cylinders"), "not recorded",
                                  f"whether a cutter reaches the {depth:g} mm deep corner is unknown",
                                  remedy="record the cutters' reach (stick-out beyond the holder)",
                                  recheck=["process.cnc.access"], feature=feature))
            parts.append((UNKNOWN, "reach unknown"))
            continue
        best, reach = max(reaching, key=lambda item: item[1][0])
        if reach[0] + 1e-9 < depth:
            findings.append(Draft("process.cnc.reach", "major", FAIL, length(depth),
                                  {"property": "corner_depth", "operator": "<=", "value": round(reach[0], 6), "units": "mm",
                                   "source": best.source or dict(POLICY)},
                                  g.method("concave corner cylinder length"), "corner length stands for the pocket depth",
                                  f"the {depth:g} mm deep corner of radius {radius:g} mm is beyond the reach of the cutter that fits it "
                                  f"({best.name}, reach {reach[0]:g} mm): its holder would collide with the part",
                                  remedy="enlarge the corner radius so a longer cutter fits, reduce the pocket depth, "
                                         "or use a longer-reach tool",
                                  alternatives=[{"text": "machine from the opposite side in a second setup",
                                                 "tradeoffs": "datum transfer and a second clamping"}],
                                  recheck=["process.cnc.access"], feature=feature))
            parts.append((FAIL, f"corner {depth:g} mm deep beyond reach"))
            continue
        parts.append((PASS, f"corner R{radius:g} x {depth:g} mm reached by {best.name}"))
    return parts


def _drilled(ctx: Context, g: Geometry, available: list[Tool], parsed: list[Any], findings: list[Draft]) -> list[tuple[str, str]]:
    targets = ctx.targets()
    parts: list[tuple[str, str]] = []
    for hole in g.holes():
        feats = set(hole.get("features") or ())
        if targets and not feats & targets:
            continue
        approach = [(-c) for c in hole["axis"]]
        ways = [approach] + ([list(hole["axis"])] if hole.get("through") else [])
        reachable = [key for key, d in parsed for way in ways if dot(d, way) > 0.9998]
        feature = _one(feats)
        if not reachable:
            findings.append(Draft("process.cnc.hole_access", "major", FAIL,
                                  text(f"Ø{hole['diameter_mm']:g} hole entered along {direction_key(approach) or approach}"),
                                  policy("hole_approach", "one_of", [key for key, _ in parsed]), g.method("bore recognition"),
                                  "straight drill axis", "no access side of this setup reaches the hole",
                                  remedy="add a setup from the hole's side, or move the hole", recheck=["process.cnc.access"],
                                  feature=feature))
            parts.append((FAIL, f"hole {hole.get('id')} unreachable"))
            continue
        sized = [(t, t.diameter) for t in available if t.diameter is not None]
        drill = [t for t, diameter in sized
                 if abs(diameter[0] - hole["diameter_mm"]) <= 0.01 and abs(diameter[1] - hole["diameter_mm"]) <= 0.01]
        mill = [t for t, diameter in sized if diameter[1] < hole["diameter_mm"] - 1e-9]
        usable = drill or mill
        if not sized:
            parts.append((UNKNOWN, f"hole {hole.get('id')} tools unknown"))
            continue
        if not usable:
            findings.append(Draft("process.cnc.hole_tool", "major", FAIL, length(hole["diameter_mm"]),
                                  policy("tool_diameter", "<=", hole["diameter_mm"], "mm",
                                         "a drill of the hole size or a smaller cutter to interpolate it"),
                                  g.method("bore recognition"), "tools as recorded",
                                  f"no recorded tool makes the Ø{hole['diameter_mm']:g} hole",
                                  remedy="use a standard drill size or add the tool", recheck=["process.cnc.access"], feature=feature))
            parts.append((FAIL, f"no tool for hole {hole.get('id')}"))
            continue
        reach = [t.reach[0] for t in usable if t.reach is not None]
        if reach and max(reach) + 1e-9 < hole["depth_mm"]:
            findings.append(Draft("process.cnc.hole_depth", "major", FAIL, length(hole["depth_mm"]),
                                  policy("hole_depth", "<=", round(max(reach), 6), "mm", "the reach of the tools that make it"),
                                  g.method("bore recognition"), "tools as recorded",
                                  f"the {hole['depth_mm']:g} mm deep hole is beyond the tools' reach",
                                  remedy="shorten the hole, drill from both sides or add a longer tool",
                                  recheck=["process.cnc.access"], feature=feature))
            parts.append((FAIL, f"hole {hole.get('id')} beyond reach"))
        elif not reach:
            parts.append((UNKNOWN, f"hole {hole.get('id')} reach unknown"))
        else:
            parts.append((PASS, f"hole {hole.get('id')} reached from {reachable[0]}"))
    return parts


def _undercuts(g: Geometry, parsed: list[Any], findings: list[Draft]) -> tuple[str, str]:
    keys = [direction_key(d) for _, d in parsed]
    hidden = []
    for face in g.faces():
        visible = face.get("visible") or {}
        if any(k not in visible for k in keys):
            return UNKNOWN, "visibility not probed for every access side"
        if face.get("on_bbox"):
            continue  # a face on the stock boundary is stock, not a machined surface
        if max(visible[k] for k in keys) <= 0.0:
            hidden.append(face)
    if not hidden:
        return PASS, "every machined face is visible from an access side"
    feats = sorted({f for face in hidden for f in face.get("features") or ()})
    findings.append(Draft("process.cnc.undercut", "major", FAIL,
                          text(f"{len(hidden)} face(s) visible from no access side ({', '.join(feats) or 'unnamed'})"),
                          policy("face_visibility", "equals", "visible along an access side"),
                          g.method("rays from face samples along each access side"), "sampled rays; side-cutting tools not modelled",
                          "undercut surfaces cannot be reached by a straight tool from this setup",
                          remedy="add a setup or a special tool (T-slot, lollipop), or remove the undercut",
                          recheck=["process.cnc.access"], feature=feats[0] if len(feats) == 1 else None))
    return FAIL, "undercuts"


def features(ctx: Context) -> Result:
    method = "cnc features: thin walls, blind-hole floors, finishing allowance, deburring and inspection access"
    g = ctx.measured("machined features")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts = [wall_check(ctx, g, findings)]
    cal = ctx.calibrated("min_wall")
    for hole in g.holes():
        floor = hole.get("floor_mm")
        if hole.get("through") or floor is None:
            continue
        if not cal.known:
            parts.append((UNKNOWN, "blind-hole floors not calibrated"))
            continue
        need = cal.bounds("mm")[1]
        if floor < need - 1e-9:
            findings.append(Draft("process.cnc.breakthrough", "major", FAIL, length(floor), cal.criterion("floor_thickness", ">=", need),
                                  g.method("ray from the blind-hole floor along its axis"), "drill point not modelled",
                                  f"the floor under blind hole {hole.get('id')} is thinner than the calibrated wall: it can break through",
                                  remedy="shorten the hole or thicken the material under it", recheck=["process.cnc.features"],
                                  feature=_one(hole.get("features"))))
            parts.append((FAIL, f"hole {hole.get('id')} floor"))
        else:
            parts.append((PASS, f"hole {hole.get('id')} floor {floor:g} mm"))
    parts += _finishing(ctx, findings)
    if not any(w in " ".join(ctx.operation.raw.get("post_processing") or ()).lower() for w in ("deburr", "break edge", "chamfer")):
        findings.append(Draft("process.cnc.deburr", "minor", UNKNOWN, unknown("no deburring step"),
                              policy("post_processing", "one_of", ["deburr"]), "part.yaml route post_processing", "not recorded",
                              "machined edges keep burrs", remedy="record deburring in post_processing",
                              recheck=["process.cnc.features"]))
        parts.append((UNKNOWN, "deburring not planned"))
    inspection = (ctx.operation.setup or {}).get("inspection_access")
    if not (isinstance(inspection, Mapping) and inspection.get("status") == "known") and ctx.surfaces():
        findings.append(Draft("process.cnc.inspection_access", "minor", UNKNOWN, unknown("inspection access"),
                              policy("inspection_access", "equals", "known"), "part.yaml route setup", "not recorded",
                              "whether the critical features can be measured in or after this setup is unknown",
                              remedy="record the inspection access", recheck=["process.cnc.features", "process.inspection"]))
        parts.append((UNKNOWN, "inspection access unknown"))
    return combine(method, parts, findings)


def _finishing(ctx: Context, findings: list[Draft]) -> list[tuple[str, str]]:
    """A critical surface stated after_finish that a later operation finishes needs this operation's allowance."""
    parts = []
    for feature, surface in ctx.critical_features().items():
        if surface.dimension_state == "as_manufactured" or feature not in ctx.targets():
            continue
        later = [op for op in ctx.after() if feature in ctx.targets(op)]
        if not later:
            continue
        allowance = [a for _, a in ctx.allowances(feature, [ctx.operation]) if a.get("purpose") in FINISH_PURPOSES]
        if not allowance:
            findings.append(Draft("process.cnc.finishing_allowance", "major", FAIL, unknown("no allowance"),
                                  policy("finishing_allowance", "equals", "recorded for the feature"), "part.yaml route allowances",
                                  "record content", f"{later[0].id} finishes {feature} but {ctx.operation.id} leaves no allowance for it",
                                  remedy=f"record the stock left on {feature} for {later[0].id}", feature=feature,
                                  recheck=["process.cnc.features", f"process.{later[0].kind}.allowance"]))
            parts.append((FAIL, f"{feature} has no finishing allowance"))
        else:
            parts.append((PASS, f"{feature} allowance for {later[0].id}"))
    return parts


def precision(ctx: Context) -> Result:
    method = "cnc precision: critical-surface criteria against calibrated deviation and roughness, and the datum strategy"
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    targets = ctx.targets()
    surfaces = [s for s in ctx.surfaces() if (s.feature in targets if targets else s.dimension_state == "as_manufactured")]
    if not surfaces:
        return Result(NOT_APPLICABLE, method, "no critical surface is finished by this operation")
    if not (ctx.operation.setup or {}).get("datums"):
        findings.append(Draft("process.cnc.datum_strategy", "minor", UNKNOWN, unknown("no datums"),
                              policy("setup_datums", "equals", "recorded"), "part.yaml route setup", "not recorded",
                              "the datum the critical features are machined from is unknown",
                              remedy="record the setup datums", recheck=["process.cnc.precision"]))
        parts.append((UNKNOWN, "datum strategy unknown"))
    for surface in surfaces:
        criteria = ctx.surface_raw(surface).get("criteria") or ()
        if not criteria:
            findings.append(Draft("process.cnc.criterion_missing", "minor", UNKNOWN, unknown("no criterion"),
                                  unknown("the critical surface states no tolerance or finish"), "part.yaml critical surfaces",
                                  "not recorded", f"whether {surface.feature} is achievable is unknown",
                                  remedy=f"record the {surface.feature} criteria", recheck=["process.cnc.precision"],
                                  feature=surface.feature))
            parts.append((UNKNOWN, f"{surface.feature} criteria missing"))
            continue
        for criterion in criteria:
            parts.append(_achievable(ctx, surface, criterion, findings))
    return combine(method, parts, findings)


def _achievable(ctx: Context, surface: Any, criterion: Any, findings: list[Draft]) -> tuple[str, str]:
    prop, operator = criterion.get("property"), criterion.get("operator")
    units = criterion.get("units") or "mm"
    if prop == "surface_roughness" and operator == "<=":
        cal = ctx.calibrated("surface_roughness")
        limit = criterion["value"]
        if not cal.known:
            return _uncalibrated(ctx, surface, cal, findings)
        achieved = cal.limits.to(units).bounds[1]
        if achieved > limit + 1e-12:
            findings.append(Draft("process.cnc.finish", "major", FAIL, {"value": achieved, "units": units}, criterion,
                                  "calibrated roughness against the criterion", f"calibration {cal.calibration.id}",
                                  f"the calibrated finish does not meet the {surface.feature} roughness limit",
                                  remedy="add a finishing pass or operation, or relax the criterion with the design owner",
                                  recheck=["process.cnc.precision"], feature=surface.feature))
            return FAIL, f"{surface.feature} roughness"
        return PASS, f"{surface.feature} roughness"
    if operator != "between" or ctx.mi.unit(units)[0] != "length":
        return UNKNOWN, f"{surface.feature} {prop} has no capability comparison"
    cal = ctx.calibrated("dimensional_deviation", feature=surface.role)
    if not cal.known:
        cal = ctx.calibrated("dimensional_deviation")
    if not cal.known:
        return _uncalibrated(ctx, surface, cal, findings)
    lower, upper = (ctx.mi.convert(v, units, "mm") for v in criterion["value"])
    low, high = cal.bounds("mm")
    if high - low > (upper - lower) + 1e-9:
        findings.append(Draft("process.cnc.tolerance", "major", FAIL, {"lower": low, "upper": high, "units": "mm"},
                              {**criterion, "units": units}, "calibrated deviation band against the tolerance band",
                              f"calibration {cal.calibration.id}; centring assumed perfect",
                              f"the calibrated scatter is wider than the {surface.feature} {prop} tolerance",
                              remedy="add a finishing operation (reaming, grinding) with its own calibration, or widen the "
                                     "tolerance with the design owner", recheck=["process.cnc.precision"], feature=surface.feature))
        return FAIL, f"{surface.feature} tolerance"
    return PASS, f"{surface.feature} tolerance within the calibrated scatter"


def _uncalibrated(ctx: Context, surface: Any, cal: Calibrated, findings: list[Draft]) -> tuple[str, str]:
    findings.append(Draft("process.cnc.precision_uncalibrated", "major", UNKNOWN, unknown("achievable accuracy"),
                          cal.criterion(cal.characteristic), "capability calibrations", "; ".join(cal.reasons),
                          f"whether {surface.feature} is achievable is unknown: machine resolution and general-tolerance "
                          "labels never answer it",
                          remedy="record a calibration (test cuts, a capability study or supplier evidence) for this feature",
                          recheck=["process.cnc.precision"], feature=surface.feature))
    return UNKNOWN, f"{surface.feature} accuracy not calibrated"


def _one(feats: Any) -> str | None:
    feats = sorted(set(feats or ()))
    return feats[0] if len(feats) == 1 else None


CNC = {"stock": gated(stock), "setup": gated(setup), "access": gated(access), "features": gated(features),
       "precision": gated(precision)}
