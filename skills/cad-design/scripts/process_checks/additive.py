"""Additive handlers: FDM and resin. Criteria come only from the operation's capability-profile
calibrations for the component's material; the plugin's old universal FDM numbers stay form_check
diagnostics. Geometry is the exported component in its approved pose (bed frame, build direction +Z).

Calibration characteristics read (scope: material, and orientation where given):
  overhang_angle            deg from vertical, the unsupported range demonstrated: criterion angle <= lower
  bridge_span               mm, the unsupported bridge demonstrated: criterion span <= lower
  min_wall                  mm, the thinnest reliable wall: criterion wall >= upper; scope.feature names a
                            feature class (tooth_tip, chamfer, decorative, ...) assessed apart from structure
  min_feature               mm, the smallest reliable pin or boss diameter: criterion diameter >= upper
  hole_diameter_deviation   mm, as-made minus nominal; scope.orientation Z (axis along the build) or XY
  dimensional_deviation     mm, as-made minus nominal of a planar or profile critical feature
  profile_deviation         mm, gear flank or cam profile deviation (scope.feature flank or cam)
  elephant_foot             mm, first-layer flare of bed-contact features
  warp                      mm, flatness deviation of a bed footprint up to scope.size
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .core import (FAIL, NOT_APPLICABLE, PASS, UNKNOWN, Calibrated, Context, Draft, Geometry, Result, angle, combine, gated,
                   length, policy, source_dict, text, unknown)

FINISHING_KINDS = frozenset({"reaming", "finishing", "cnc", "drilling"})  # operations that can restore a surface
EXEMPT_CLASSES = frozenset({"tooth_tip", "flank", "chamfer", "fillet", "decorative", "text", "emboss", "tip", "knurl"})
SOLUBLE_WORDS = ("soluble", "pva", "bvoh", "hips", "interface", "breakaway")
HOLE_CLASSES = {"Z": "axis along the build direction", "XY": "axis in the layer plane", "inclined": "inclined axis"}
EPS = 1e-9


def _k(ctx: Context) -> str:
    kind: str = ctx.operation.kind
    return kind


def hole_class(axis: Any) -> str:
    """Z, XY or inclined: the hole axis against the build direction (+Z of the bed frame)."""
    cos = abs(float(axis[2]))
    if cos >= math.cos(math.radians(1.0)):
        return "Z"
    if cos <= math.sin(math.radians(1.0)):
        return "XY"
    return "inclined"


def _approved(ctx: Context) -> tuple[bool, str]:
    data = ctx.operation.orientation
    mi = ctx.mi
    if data is None:
        return False, "no orientation recorded"
    if mi.is_fact(data):
        return False, f"orientation {data.status}: {data.reason or 'no reason recorded'}"
    status = data.get("status")
    return status == "approved", f"orientation {status}"


# --- orientation ------------------------------------------------------------------------------------------------

def orientation(ctx: Context) -> Result:
    kind = _k(ctx)
    method = f"{kind} orientation: approved pose, first-layer contact, stability and envelope"
    approved, why = _approved(ctx)
    if not approved:
        draft = Draft(f"process.{kind}.orientation_unapproved", "major", UNKNOWN, text(why),
                      policy("orientation_status", "equals", "approved"), "part.yaml route orientation", why,
                      "first-layer contact, stability, supports and layer direction are not judged in the pose it will print in",
                      remedy="approve the print orientation (route orientation status approved with its decision); "
                             "an envelope auto-fit is never an approved orientation",
                      recheck=[f"process.{kind}", "route.operation_plan"], earliest_stage="detail")
        return Result(UNKNOWN, method, f"orientation not approved ({why})", [draft])
    g = ctx.measured("first-layer contact and stability")
    if isinstance(g, Result):
        return g
    if g.frame != "bed":
        draft = Draft(f"process.{kind}.pose_unmeasured", "major", UNKNOWN, text(f"exported frame {g.frame}"),
                      policy("export_frame", "equals", "bed"), g.method("pose"),
                      "the component was not exported in its approved pose",
                      "first-layer contact and stability are not measured", remedy="rebuild the version with the approved orientation",
                      recheck=[f"process.{kind}.orientation"], earliest_stage="detail")
        return Result(UNKNOWN, method, "pose not measured", [draft])
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    bed = g.facts.get("bed") or {}
    contact = float(bed.get("contact_area_mm2") or 0.0)
    if contact <= EPS:
        findings.append(Draft(f"process.{kind}.first_layer", "major", FAIL, text(f"bed contact {contact:g} mm2"),
                              policy("bed_contact_area", ">=", 0.0, "mm", "a face must lie on the bed"),
                              g.method("planar faces at the lowest z"), "faces within 1e-6 mm of the bed plane",
                              "the part rests on an edge or a point: the first layer has nothing to bond to",
                              remedy="reorient so a planar face lies on the bed, or add a sacrificial flat",
                              alternatives=[{"text": "print on a raft or supports", "tradeoffs": "supported underside finish and removal"}],
                              recheck=[f"process.{kind}.orientation", f"process.{kind}.support"], earliest_stage="detail"))
        parts.append((FAIL, "no first-layer contact"))
    else:
        parts.append((PASS, f"first-layer contact {contact:.3g} mm2"))
        centre, low, high = g.facts.get("centre_mm"), bed.get("contact_min"), bed.get("contact_max")
        if centre and low and high:
            inside = low[0] - EPS <= centre[0] <= high[0] + EPS and low[1] - EPS <= centre[1] <= high[1] + EPS
            if inside:
                parts.append((PASS, "centre of volume above the bed contact box"))
            else:
                supports = ctx.operation.supports
                supported = isinstance(supports, Mapping) and supports.get("strategy") != "none"
                findings.append(Draft(f"process.{kind}.stability", "minor" if supported else "major",
                                      PASS if supported else FAIL,
                                      text(f"centre of volume at x {centre[0]:.3f}, y {centre[1]:.3f} outside the contact box"),
                                      policy("centre_of_volume", "equals", "inside the bed contact footprint"),
                                      g.method("centre of volume against the bed contact bounding box"),
                                      "the contact bounding box over-states the convex footprint; print forces are not modelled",
                                      "the part can tip or peel during printing" if not supported
                                      else "stability relies on the planned supports",
                                      remedy=None if supported else "reorient, widen the base or plan stabilising supports or a brim",
                                      recheck=[f"process.{kind}.orientation"], earliest_stage="detail"))
                parts.append((PASS if supported else FAIL, "stability relies on supports" if supported else "unstable pose"))
    parts.append(_envelope(ctx, g, findings))
    _layer_note(ctx, g, findings)
    return combine(method, parts, findings)


def _envelope(ctx: Context, g: Geometry, findings: list[Draft]) -> tuple[str, str]:
    kind, profile = _k(ctx), ctx.profile
    raw = (profile.raw.get("envelope") if profile is not None else None) or {}
    known: list[float] = []
    for axis in "xyz":
        item = raw.get(axis)
        if not isinstance(item, Mapping) or item.get("status") != "known":
            break
        known.append(ctx.mi.quantity(item["value"]).to("mm").bounds[0])
    sizes = known if len(known) == 3 else None
    if sizes is None:
        findings.append(Draft(f"process.{kind}.envelope_unknown", "minor", UNKNOWN, unknown("no measured envelope"),
                              unknown("the capability profile records no x, y and z envelope"),
                              "capability profile envelope", "the usable envelope is not recorded",
                              "the approved pose is not checked against the machine",
                              remedy="record the measured envelope in the capability profile",
                              recheck=[f"process.{kind}.orientation"], earliest_stage="parts"))
        return UNKNOWN, "envelope unknown"
    box = g.facts.get("bbox_mm") or {}
    size = [box["max"][i] - box["min"][i] for i in range(3)] if box else None
    if size is None:
        return UNKNOWN, "component size not measured"
    over = [("xyz"[i], size[i], sizes[i]) for i in range(3) if size[i] > sizes[i] + 1e-9]
    if over:
        axis, value, limit = over[0]
        findings.append(Draft(f"process.{kind}.envelope", "major", FAIL, length(value),
                              {"property": f"envelope_{axis}", "operator": "<=", "value": limit, "units": "mm",
                               "source": _envelope_source(profile, axis)},
                              g.method("bounding box in the approved pose"), "exact B-rep bounding box",
                              f"the component does not fit the machine along {axis} in its approved pose",
                              remedy="reorient (with a new approval), split with controlled joints or choose a larger machine",
                              recheck=[f"process.{kind}.orientation"], earliest_stage="detail"))
        return FAIL, "outside the envelope"
    return PASS, "inside the measured envelope in the approved pose"


def _envelope_source(profile: Any, axis: str) -> dict[str, Any]:
    sources = profile.raw["envelope"][axis].get("sources") or []
    return source_dict(sources[0]) if sources else {"kind": "user", "reference": profile.id, "origin": "capability profile"}


def _layer_note(ctx: Context, g: Geometry, findings: list[Draft]) -> None:
    if "structural" not in ctx.functions():
        return
    axis = g.facts.get("build_axis_native")
    where = f"native direction [{', '.join(f'{c:.3f}' for c in axis)}]" if axis else "the approved pose's +Z"
    findings.append(Draft(f"process.{_k(ctx)}.layer_direction", "info", UNKNOWN,
                          text(f"layers stack along {where}"), unknown("interlayer strength against the load directions"),
                          "approved orientation", "the load directions and the interlayer allowables are not part of this check",
                          "a load across the layer planes is carried by interlayer bonding, the weakest direction",
                          remedy="assess layer direction against the load cases with orientation-specific properties",
                          recheck=["engineering.anisotropy"], earliest_stage="analysis", link=False))


# --- supports ---------------------------------------------------------------------------------------------------

def support(ctx: Context) -> Result:
    kind = _k(ctx)
    method = f"{kind} support plan: sampled down-facing normals, downward support rays and the recorded strategy"
    supports = ctx.operation.supports
    mi = ctx.mi
    if supports is None or mi.is_fact(supports):
        why = "no support strategy recorded (absent is unknown, never none)" if supports is None \
            else f"support strategy {supports.status}: {supports.reason or 'no reason recorded'}"
        draft = Draft(f"process.{kind}.support_unplanned", "major", UNKNOWN, text(why),
                      policy("support_strategy", "one_of", ["none", "reachable_noncritical", "finished_critical"]),
                      "part.yaml route supports", why, "where supports start, touch and come off is unknown",
                      remedy="record the support strategy, its contact and start regions and the removal tools and access",
                      recheck=[f"process.{kind}.support"], earliest_stage="detail")
        return Result(UNKNOWN, method, "support strategy unknown", [draft])
    g = ctx.measured("overhangs, bridges and islands")
    if isinstance(g, Result):
        return g
    strategy = supports.get("strategy")
    contacts = {t.get("feature") for t in supports.get("contact_regions") or ()}
    starts = {t.get("feature") for t in supports.get("start_regions") or ()}
    critical = ctx.critical_features()
    overhang = ctx.calibrated("overhang_angle")
    bridge = ctx.calibrated("bridge_span")
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    material = str(supports.get("material") or "")
    soluble = any(word in material.lower() for word in SOLUBLE_WORDS)

    reported: set[str] = set()
    for feature in sorted(contacts & set(critical)):
        if strategy != "finished_critical":
            findings.append(_critical_contact(ctx, feature, critical[feature], soluble, "declared as a non-critical contact"))
            parts.append((FAIL, f"critical {feature} declared as a non-critical support contact"))
            reported.add(feature)

    needing: list[tuple[Mapping[str, Any], str]] = []
    undecided: list[tuple[Mapping[str, Any], str]] = []
    planned: list[tuple[Mapping[str, Any], str]] = []
    for face in g.faces():
        down = face.get("down") or {}
        area = float(down.get("area_mm2") or 0.0)
        if area <= EPS:
            continue
        reason = _needs_support(down, overhang, bridge)
        if reason is None:
            continue
        (undecided if reason == "undecided" else needing).append((face, reason))
    for face, reason in needing + undecided:
        feats = set(face.get("features") or ())
        down = face["down"]
        hit = sorted(feats & set(critical))
        start = down.get("start") or {}
        start_hit = sorted(set(start.get("features") or ()) & set(critical))
        limit = _limit_criterion("bridge" if down.get("kind") == "bridge" else "overhang", overhang, bridge)
        covered = strategy == "finished_critical" and set(hit + start_hit) <= contacts
        if reason == "undecided" and not (hit or start_hit):
            if feats & contacts:
                planned.append((face, reason))
                continue
            findings.append(Draft(f"process.{kind}.overhang_uncalibrated", "minor", UNKNOWN,
                                  angle(down.get("max_angle_deg") or 0.0), limit,
                                  g.method("sampled down-facing normals"),
                                  "; ".join(overhang.reasons + bridge.reasons) or "not calibrated",
                                  "which down-facing surfaces need support is unknown",
                                  remedy="calibrate the unsupported overhang angle and bridge span for this material and machine, "
                                         "or plan supports on this surface",
                                  recheck=[f"process.{kind}.support"], earliest_stage="detail", feature=_one(feats)))
            parts.append((UNKNOWN, f"face {face.get('id')} overhang not calibrated"))
            continue
        if reason == "undecided" and not covered:
            for feature in sorted(set(hit + start_hit) - reported):
                reported.add(feature)
                findings.append(Draft(f"process.{kind}.support_on_critical_unknown", "major", UNKNOWN,
                                      angle(down.get("max_angle_deg") or 0.0), limit,
                                      g.method("sampled down-facing normals"), "; ".join(overhang.reasons + bridge.reasons),
                                      f"whether supports touch the {critical[feature].role} surface {feature} is unknown",
                                      remedy="calibrate the unsupported overhang angle and bridge span, or reorient so "
                                             f"{feature} faces up or sideways", recheck=[f"process.{kind}.support"],
                                      earliest_stage="detail", feature=feature))
                parts.append((UNKNOWN, f"support on critical {feature} undecided"))
            continue
        if hit or start_hit:
            for feature in sorted(set(hit + start_hit) - reported):
                reported.add(feature)
                verdict = _critical_exception(ctx, feature, critical[feature], supports, contacts, soluble, findings,
                                              starting=feature in start_hit and feature not in hit)
                parts.append((verdict, f"support on critical {feature}"))
            continue
        if strategy == "none":
            findings.append(_unsupported(ctx, g, face, reason, overhang, bridge))
            parts.append((FAIL, f"face {face.get('id')} needs support and the strategy is none"))
            continue
        if not feats:
            findings.append(Draft(f"process.{kind}.support_region_unnamed", "minor", UNKNOWN,
                                  text(f"{down.get('area_mm2', 0):.3g} mm2 down-facing at z {face.get('z_mm')}"),
                                  _limit_criterion(reason, overhang, bridge), g.method("sampled down-facing normals"),
                                  "no feature locator names this surface", "supports here are neither planned nor excluded",
                                  remedy="name the surface with a feature locator and list it as a support contact region",
                                  recheck=[f"process.{kind}.support"], earliest_stage="detail"))
            parts.append((UNKNOWN, f"face {face.get('id')} unnamed"))
            continue
        if not feats & contacts:
            findings.append(Draft(f"process.{kind}.support_region_unplanned", "minor", UNKNOWN,
                                  text(f"{', '.join(sorted(feats))} needs support ({reason})"),
                                  _limit_criterion(reason, overhang, bridge), g.method("sampled down-facing normals"),
                                  "the support plan does not list this surface", "the support plan is incomplete",
                                  remedy=f"add {', '.join(sorted(feats))} to the contact regions, or reorient",
                                  recheck=[f"process.{kind}.support"], earliest_stage="detail", feature=_one(feats)))
            parts.append((UNKNOWN, f"{', '.join(sorted(feats))} not in the plan"))
            continue
        planned.append((face, reason))
        if (start.get("part_mm2") or 0) > EPS:
            on = sorted(set(start.get("features") or ()) - starts - contacts)
            if on or not start.get("features"):
                findings.append(Draft(f"process.{kind}.support_start", "minor", UNKNOWN,
                                      text(f"supports start on the part ({start.get('part_mm2'):.3g} mm2)"),
                                      policy("support_start", "equals", "on the bed or a recorded start region"),
                                      g.method("downward rays from down-facing samples"), "ray samples, not a slicer's support",
                                      "supports standing on the part mark the surface they start on",
                                      remedy=f"record start regions{' ' + ', '.join(on) if on else ''} or reorient",
                                      recheck=[f"process.{kind}.support"], earliest_stage="detail", feature=_one(feats)))
                parts.append((UNKNOWN, "support start not recorded"))
    if planned:
        names = sorted({f for face, _ in planned for f in face.get("features") or ()})
        findings.append(Draft(f"process.{kind}.overhang_planned", "info", PASS,
                              text(f"{len(planned)} surface(s) beyond the unsupported limit: {', '.join(names)}"),
                              _limit_criterion(planned[0][1], overhang, bridge),
                              g.method("sampled down-facing normals"),
                              "support removal damage, distortion and finish are not predicted from area",
                              "supports are planned on reachable non-critical surfaces",
                              recheck=[f"process.{kind}.support"], earliest_stage="detail"))
        findings.append(_trial(ctx, f"support removal at {', '.join(names)} and the supported surface finish"))
        parts.append((PASS, f"supports planned on {', '.join(names)}"))
    if not needing and not undecided:
        parts.append((PASS, "no down-facing surface beyond the calibrated unsupported limits"))
    return combine(method, parts, findings,
                   uncertainty="sampled normals and rays; support toolpaths and removal are not simulated")


def _one(feats: set[str]) -> str | None:
    return sorted(feats)[0] if len(feats) == 1 else None


def _needs_support(down: Any, overhang: Calibrated, bridge: Calibrated) -> str | None:
    """island, bridge or overhang when the face needs support; undecided when no calibration says; None otherwise.
    A bridge is judged by its span alone, an overhang by the area beyond the unsupported angle."""
    kind = down.get("kind")
    if kind == "island":
        return "island"
    if kind == "bridge":
        span = down.get("span_mm")
        if not bridge.known or span is None:
            return "undecided"
        return "bridge" if span > bridge.bounds("mm")[0] + 1e-9 else None
    if not overhang.known:
        return "undecided" if (down.get("max_angle_deg") or 0.0) > 1e-6 else None
    limit = overhang.bounds("deg")[0]
    over = sum(area for deg, area in down.get("angles") or () if deg > limit + 1e-9)
    return "overhang" if over > EPS else None


def _limit_criterion(reason: str, overhang: Calibrated, bridge: Calibrated) -> dict[str, Any]:
    if reason == "bridge":
        return bridge.criterion("bridge_span", "<=")
    if reason == "island":
        return policy("island", "equals", "supported", note="a region that starts in mid-air always needs support")
    return overhang.criterion("overhang_angle", "<=")


def _unsupported(ctx: Context, g: Geometry, face: Any, reason: str, overhang: Calibrated, bridge: Calibrated) -> Draft:
    kind, down = _k(ctx), face["down"]
    measured = length(down["span_mm"]) if reason == "bridge" and down.get("span_mm") is not None \
        else angle(down.get("max_angle_deg") or 0.0)
    return Draft(f"process.{kind}.overhang", "major", FAIL, measured, _limit_criterion(reason, overhang, bridge),
                 g.method("sampled down-facing normals"), "sampled normals; edge anchoring approximates bridges",
                 f"a {reason} beyond the calibrated unsupported limit with support strategy none sags or fails to print",
                 remedy="plan supports on reachable non-critical surfaces, or reorient (preferred over splitting)",
                 alternatives=[{"text": "local relief: a chamfer or teardrop under the overhang",
                                "tradeoffs": "changes the geometry; needs the design owner's approval"},
                               {"text": "split the part", "tradeoffs": "adds joints, datums, fasteners, a tolerance stack and load paths"}],
                 recheck=[f"process.{kind}.support"], earliest_stage="detail", feature=_one(set(face.get("features") or ())))


def _critical_contact(ctx: Context, feature: str, surface: Any, soluble: bool, why: str) -> Draft:
    kind = _k(ctx)
    note = "; soluble or interface supports alone do not establish precision" if soluble else ""
    return Draft(f"process.{kind}.support_on_critical", "major", FAIL,
                 text(f"support contact on {surface.role} surface {feature} ({why})"),
                 policy("support_contact", "equals", "none on precision fits and running or contact surfaces",
                        note="an exception needs a finishing operation, allowance, tool access and inspection"),
                 ctx.geometry.method("down-facing samples and support rays") if ctx.geometry else "part.yaml route supports",
                 "support scars and their removal are not predicted from geometry",
                 f"the {surface.role} surface {feature} keeps support scars and removal damage: its fit and finish are not "
                 f"what the design states{note}",
                 remedy="reorient to keep supports off it, or plan finished_critical supports with a finishing operation, "
                        "allowance, removal access and inspection",
                 alternatives=[{"text": "machine or ream the surface after printing",
                                "tradeoffs": "a secondary operation, datum transfer and finishing allowance"},
                               {"text": "a purchased bushing or insert at the surface",
                                "tradeoffs": "adds a part, an interface and a retention check"}],
                 recheck=[f"process.{kind}.support", "feature.fit", "feature.inspection"], earliest_stage="detail",
                 feature=feature)


def _critical_exception(ctx: Context, feature: str, surface: Any, supports: Any, contacts: set[str], soluble: bool,
                        findings: list[Draft], *, starting: bool) -> str:
    """FAIL unless finished_critical names this surface with a later finishing operation, an allowance,
    removal access and inspection ids the route holds; a planned exception still needs physical validation."""
    if supports.get("strategy") != "finished_critical" or feature not in contacts:
        why = "supports would start on it" if starting else "unplanned"
        findings.append(_critical_contact(ctx, feature, surface, soluble, why))
        return FAIL
    later = [op for op in ctx.after() if op.kind in FINISHING_KINDS and feature in ctx.targets(op)]
    inspections = ctx.inspections()
    missing = []
    if not later:
        missing.append("a later finishing operation that targets it")
    if not supports.get("allowance"):
        missing.append("an allowance")
    if not (supports.get("removal") or {}).get("access"):
        missing.append("removal access")
    ids = list(supports.get("inspection") or ())
    if not ids or any(i not in inspections for i in ids):
        missing.append("inspection entries of the route")
    if missing:
        findings.append(Draft(f"process.{_k(ctx)}.support_exception", "major", FAIL,
                              text(f"finished_critical support on {feature} lacks {', '.join(missing)}"),
                              policy("support_exception", "equals", "finishing operation, allowance, access and inspection"),
                              "part.yaml route supports and operations", "record content only",
                              f"the supported {surface.role} surface {feature} is not restored to its stated fit",
                              remedy=f"record {', '.join(missing)}, or keep supports off {feature}",
                              recheck=[f"process.{_k(ctx)}.support", "route.operation_plan"], earliest_stage="detail",
                              feature=feature))
        return FAIL
    findings.append(Draft(f"process.{_k(ctx)}.support_exception", "info", PASS,
                          text(f"finished_critical support on {feature}: finished by {later[0].id}"),
                          policy("support_exception", "equals", "finishing operation, allowance, access and inspection"),
                          "part.yaml route supports and operations", "the restored surface is not yet measured",
                          f"{feature} is finished after support removal; only scoped surface and dimensional validation "
                          "accepts it", recheck=["feature.inspection"], earliest_stage="detail", feature=feature))
    findings.append(_trial(ctx, f"the finished {surface.role} surface {feature} after support removal"))
    return PASS


def _trial(ctx: Context, what: str) -> Draft:
    kind = _k(ctx)
    return Draft(f"process.{kind}.evidence_required", "info", UNKNOWN, unknown("no physical evidence assessed here"),
                 unknown("a coupon, slicer review or print trial"), "geometric analysis",
                 "geometry does not validate toolpaths or a successful print",
                 f"{what} is unvalidated until printed", remedy="review the slicer preview and print a coupon or trial",
                 recheck=[f"process.{kind}.trial"], earliest_stage="release", link=False)


# --- geometry ---------------------------------------------------------------------------------------------------

def geometry(ctx: Context) -> Result:
    kind = _k(ctx)
    method = f"{kind} geometry against calibrated criteria: walls by feature class, small features, holes and critical features"
    g = ctx.measured("walls, small features and holes")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts = [wall_check(ctx, g, findings), small_feature_check(ctx, g, findings)]
    parts += hole_fits(ctx, g, findings)
    parts += _critical(ctx, g, findings)
    parts += _receiving(ctx, g, findings)
    return combine(method, parts, findings, uncertainty="sampled wall scan; calibration scatter as recorded")


def wall_check(ctx: Context, g: Geometry, findings: list[Draft]) -> tuple[str, str]:
    """The thinnest wall against the calibrated min_wall of the operation's material; feature classes such as
    tooth tips, chamfers and decorations are judged against their own calibration, never exempted silently."""
    kind = _k(ctx)
    walls = g.facts.get("walls") or {}
    cal = ctx.calibrated("min_wall")
    if not cal.known:
        findings.append(Draft(f"process.{kind}.wall_uncalibrated", "minor", UNKNOWN,
                              length(walls["thickness_mm"]) if walls.get("thickness_mm") is not None else unknown("not measured"),
                              cal.criterion("wall_thickness", ">="), g.method("minimum-wall scan"),
                              "; ".join(cal.reasons), "the structural wall criterion is unknown",
                              remedy="calibrate the minimum reliable wall for this material, process and orientation; "
                                     "no universal value applies (form_check's 1.2 mm is a diagnostic)",
                              recheck=[f"process.{kind}.geometry"],
                              earliest_stage="parts"))
        return UNKNOWN, "wall criterion not calibrated"
    need = cal.bounds("mm")[1]
    thickness = walls.get("thickness_mm")
    if thickness is None:
        findings.append(Draft(f"process.{kind}.wall_unmeasured", "minor", UNKNOWN, unknown("no wall measured"),
                              cal.criterion("wall_thickness", ">=", need), g.method("minimum-wall scan"),
                              f"{walls.get('measured', 0)} samples reached an opposing face", "walls are unassessed",
                              remedy="inspect the walls with a section or another probe", recheck=[f"process.{kind}.geometry"]))
        return UNKNOWN, "walls not measured"
    if thickness >= need - 1e-9:
        return PASS, f"thinnest wall {thickness:.3f} mm >= calibrated {need:g} mm"
    feats = set(walls.get("features") or ())
    classes = {(g.facts.get("features") or {}).get(f, {}).get("kind") for f in feats} | \
        {s.role for f, s in ctx.critical_features().items() if f in feats}
    exempt = sorted(c for c in classes if c in EXEMPT_CLASSES)
    if exempt:
        feature_cal = ctx.calibrated("min_wall", feature=exempt[0])
        if feature_cal.known and thickness >= feature_cal.bounds("mm")[1] - 1e-9:
            findings.append(Draft(f"process.{kind}.wall_feature_class", "info", UNKNOWN, length(thickness),
                                  feature_cal.criterion("wall_thickness", ">=", feature_cal.bounds("mm")[1]),
                                  g.method("minimum-wall scan"), "the scan reports one global minimum",
                                  f"the thinnest wall is on a {exempt[0]} feature within its own calibration; structural walls "
                                  "thicker than it are not separated by this scan",
                                  remedy="section the structural regions to measure their walls", recheck=[f"process.{kind}.geometry"]))
            return UNKNOWN, "structural walls not separated from the exempt feature"
        findings.append(Draft(f"process.{kind}.wall_feature_class", "minor", UNKNOWN, length(thickness),
                              feature_cal.criterion("wall_thickness", ">="), g.method("minimum-wall scan"),
                              f"{exempt[0]} features are assessed apart from structural walls",
                              f"the thinnest wall is on a {exempt[0]} feature with no calibration of its own",
                              remedy=f"calibrate min_wall with scope feature {exempt[0]}, or justify the exception in a decision",
                              recheck=[f"process.{kind}.geometry"], feature=sorted(feats)[0] if feats else None))
        return UNKNOWN, f"{exempt[0]} wall not calibrated"
    location = walls.get("location_mm")
    findings.append(Draft(f"process.{kind}.wall", "major", FAIL, length(thickness),
                          cal.criterion("wall_thickness", ">=", need), g.method("minimum-wall scan"),
                          "sampled scan: the true minimum can be up to the sample spacing thinner",
                          f"a structural wall {thickness:.3f} mm thick at {location} is below the calibrated minimum",
                          remedy="thicken the wall, or reorient if the calibration is orientation-specific",
                          recheck=[f"process.{kind}.geometry"], feature=sorted(feats)[0] if len(feats) == 1 else None))
    return FAIL, "structural wall below the calibrated minimum"


def small_feature_check(ctx: Context, g: Geometry, findings: list[Draft]) -> tuple[str, str]:
    kind = _k(ctx)
    pins = g.pins()
    if not pins:
        return NOT_APPLICABLE, ""
    cal = ctx.calibrated("min_feature")
    smallest = min(pins, key=lambda p: p["diameter_mm"])
    if not cal.known:
        findings.append(Draft(f"process.{kind}.small_feature_uncalibrated", "minor", UNKNOWN, length(smallest["diameter_mm"]),
                              cal.criterion("feature_diameter", ">="), g.method("convex full cylinders"),
                              "; ".join(cal.reasons), "small pins and bosses are unassessed",
                              remedy="calibrate the smallest reliable pin or boss", recheck=[f"process.{kind}.geometry"]))
        return UNKNOWN, "small features not calibrated"
    need = cal.bounds("mm")[1]
    if smallest["diameter_mm"] >= need - 1e-9:
        return PASS, f"smallest pin {smallest['diameter_mm']:g} mm >= {need:g} mm"
    findings.append(Draft(f"process.{kind}.small_feature", "major", FAIL, length(smallest["diameter_mm"]),
                          cal.criterion("feature_diameter", ">=", need), g.method("convex full cylinders"),
                          "cylinder radius is exact", "the feature is below the calibrated smallest reliable size",
                          remedy="enlarge it or replace it with a purchased pin", recheck=[f"process.{kind}.geometry"],
                          feature=_one(set(smallest.get("features") or ()))))
    return FAIL, "small feature below the calibrated minimum"


def hole_fits(ctx: Context, g: Geometry, findings: list[Draft], characteristic: str = "hole_diameter_deviation",
              oriented: bool = True) -> list[tuple[str, str]]:
    """Each critical bore of the component: a finished one needs a later finishing operation; an as-made
    one needs the calibrated `characteristic` (as-made minus nominal, by build orientation when `oriented`)
    to keep the bore inside its stated diameter limits."""
    kind = _k(ctx)
    critical = ctx.critical_features()
    parts: list[tuple[str, str]] = []
    for hole in g.holes():
        feats = set(hole.get("features") or ()) & set(critical)
        orientation = hole_class(hole["axis"])
        if not feats:
            continue  # a non-critical hole's as-made size governs no fit; compensation stays a slicer setting
        feature = sorted(feats)[0]
        surface = critical[feature]
        if surface.dimension_state != "as_manufactured":
            later = [op for op in ctx.after() if op.kind in FINISHING_KINDS and feature in ctx.targets(op)]
            if later:
                parts.append((PASS, f"{feature} is finished by {later[0].id}"))
                continue
            findings.append(Draft(f"process.{kind}.finish_missing", "major", FAIL,
                                  text(f"{feature} is stated {surface.dimension_state}"),
                                  policy("finishing_operation", "equals", "a later operation that targets the feature"),
                                  "part.yaml route", "record content only",
                                  f"the {surface.role} {feature} is specified finished but no later operation finishes it",
                                  remedy=f"add a reaming or finishing operation targeting {feature} with its allowance",
                                  recheck=[f"process.{kind}.geometry", "route.operation_plan"], feature=feature))
            parts.append((FAIL, f"{feature} has no finishing operation"))
            continue
        cal = ctx.calibrated(characteristic, orientation=orientation if oriented else None)
        band = _diameter_band(ctx, surface)
        if not cal.known:
            findings.append(Draft(f"process.{kind}.hole_uncalibrated", "major", UNKNOWN, length(hole["diameter_mm"]),
                                  cal.criterion(characteristic), g.method("bore recognition"),
                                  "; ".join(cal.reasons),
                                  f"the as-made size of the {surface.role} {feature}"
                                  f"{' (' + HOLE_CLASSES[orientation] + ')' if oriented else ''} is unknown",
                                  remedy=f"make coupons in this material{' and orientation (' + orientation + ')' if oriented else ''}"
                                         " and record the calibration, or ream the bore",
                                  recheck=[f"process.{kind}.geometry"], feature=feature))
            parts.append((UNKNOWN, f"{feature} deviation not calibrated"))
            continue
        if band is None:
            findings.append(Draft(f"process.{kind}.hole_criterion_missing", "minor", UNKNOWN, length(hole["diameter_mm"]),
                                  unknown("no diameter criterion on the critical surface"), g.method("bore recognition"),
                                  "the fit's diameter limits are not recorded", f"whether {feature} holds its fit is unknown",
                                  remedy=f"record the {feature} diameter criterion (between, mm)",
                                  recheck=[f"process.{kind}.geometry"], feature=feature))
            parts.append((UNKNOWN, f"{feature} has no diameter criterion"))
            continue
        low, high = cal.bounds("mm")
        made = (hole["diameter_mm"] + low, hole["diameter_mm"] + high)
        if made[0] < band[0] - 1e-9 or made[1] > band[1] + 1e-9:
            findings.append(Draft(f"process.{kind}.hole_fit", "major", FAIL,
                                  {"lower": round(made[0], 6), "upper": round(made[1], 6), "units": "mm"},
                                  {"property": "diameter", "operator": "between", "value": list(band), "units": "mm",
                                   "source": band[2]}, g.method("bore recognition plus calibrated deviation"),
                                  f"calibration {cal.calibration.id} scatter{'; ' + HOLE_CLASSES[orientation] if oriented else ''}",
                                  f"the as-made {surface.role} {feature} cannot hold its stated diameter limits",
                                  remedy=f"ream or machine {feature} to size, or compensate the model and re-calibrate",
                                  alternatives=[{"text": "a purchased bushing", "tradeoffs": "adds a part and a press fit"}],
                                  recheck=[f"process.{kind}.geometry", "feature.fit"], feature=feature))
            parts.append((FAIL, f"{feature} outside its limits as made"))
        else:
            parts.append((PASS, f"{feature} within its limits with the calibrated {characteristic}"))
    return parts


def _diameter_band(ctx: Context, surface: Any) -> tuple[float, float, dict[str, Any]] | None:
    for item in ctx.surface_raw(surface).get("criteria") or ():
        if item.get("property") == "diameter" and item.get("operator") == "between":
            lower, upper = item["value"]
            units = item.get("units", "mm")
            convert = ctx.mi.convert
            return (convert(lower, units, "mm"), convert(upper, units, "mm"), item.get("source"))
    return None


def _critical(ctx: Context, g: Geometry, findings: list[Draft]) -> list[tuple[str, str]]:
    """Every critical surface that is not a bore: its feature must be located and its deviation calibrated."""
    kind = _k(ctx)
    holes = {f for hole in g.holes() for f in hole.get("features") or ()}
    parts: list[tuple[str, str]] = []
    for feature, surface in sorted(ctx.critical_features().items()):
        if g.feature(feature) is None:
            findings.append(Draft(f"process.{kind}.feature_unlocated", "major", UNKNOWN, unknown("no locator matched"),
                                  unknown("the feature's geometry"), g.method("feature locators"),
                                  "the critical surface has no manufacturing-state locator on this component",
                                  f"the {surface.role} {feature} is not assessed",
                                  remedy="add the feature to drawing_features (same state) or manufacture_features",
                                  recheck=[f"process.{kind}.geometry"], feature=feature))
            parts.append((UNKNOWN, f"{feature} not located"))
            continue
        if feature in holes or surface.dimension_state != "as_manufactured":
            continue
        characteristic = "profile_deviation" if surface.role in ("flank", "cam") else "dimensional_deviation"
        cal = ctx.calibrated(characteristic, feature=surface.role if characteristic == "profile_deviation" else None)
        on_bed = any(f.get("bed") for f in g.feature_faces(feature))
        if on_bed:
            foot = ctx.calibrated("elephant_foot")
            if not foot.known:
                findings.append(Draft(f"process.{kind}.elephant_foot", "minor", UNKNOWN, unknown("first-layer flare"),
                                      foot.criterion("elephant_foot"), g.method("bed contact faces"), "; ".join(foot.reasons),
                                      f"the {surface.role} {feature} starts at the bed, where the first layer flares",
                                      remedy="calibrate the elephant foot or chamfer the bed edge of the feature",
                                      recheck=[f"process.{kind}.geometry"], feature=feature))
                parts.append((UNKNOWN, f"{feature} elephant foot unknown"))
        if cal.known:
            parts.append((PASS, f"{feature} {characteristic} calibrated"))
            continue
        findings.append(Draft(f"process.{kind}.feature_uncalibrated", "major", UNKNOWN, unknown("as-made deviation"),
                              cal.criterion(characteristic), g.method("feature locators"), "; ".join(cal.reasons),
                              f"the as-printed accuracy of the {surface.role} {feature} is unknown",
                              remedy=f"calibrate {characteristic} for this material and orientation with coupons, or finish the surface",
                              recheck=[f"process.{kind}.geometry", "feature.fit"], feature=feature))
        parts.append((UNKNOWN, f"{feature} {characteristic} not calibrated"))
    return parts


def _receiving(ctx: Context, g: Geometry, findings: list[Draft]) -> list[tuple[str, str]]:
    """Inserts, heat-set inserts and press fits into this component's printed features."""
    kind, mi = _k(ctx), ctx.mi
    operations = ctx.record.assembly_operations
    if mi.is_fact(operations):
        return []
    parts: list[tuple[str, str]] = []
    for item in operations:
        if item.get("into") != ctx.pn or item.get("kind") not in ("press_fit", "heat_set", "insert"):
            continue
        for feature in (item.get("acceptance") or {}).get("features") or ():
            hole = next((h for h in g.holes() if feature in (h.get("features") or ())), None)
            if hole is None:
                continue  # the feature lies on another component or is not a bore of this one
            cal = ctx.calibrated("hole_diameter_deviation", orientation=hole_class(hole["axis"]))
            findings.append(Draft(f"process.{kind}.receiving_feature", "info" if cal.known else "major",
                                  UNKNOWN, length(hole["diameter_mm"]), cal.criterion("hole_diameter_deviation"),
                                  g.method("bore recognition"),
                                  "retention depends on the as-printed bore, the material's creep and the insert",
                                  f"{item.get('kind')} {item.get('id')} into {feature}: retention is unvalidated",
                                  remedy="test retention on a printed coupon with the actual insert or pin",
                                  recheck=["assembly.order", f"process.{kind}.trial"], earliest_stage="release",
                                  feature=feature, link=not cal.known))
            parts.append((PASS if cal.known else UNKNOWN, f"{feature} receives {item.get('kind')}"))
    return parts


# --- thermal distortion -----------------------------------------------------------------------------------------

def thermal_distortion(ctx: Context) -> Result:
    kind = _k(ctx)
    method = f"{kind} shrinkage, warp and first-layer flare against calibrations"
    g = ctx.measured("warp and shrinkage")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    bed = g.facts.get("bed") or {}
    low, high = bed.get("contact_min"), bed.get("contact_max")
    footprint = max(high[0] - low[0], high[1] - low[1]) if low and high else 0.0
    warp = ctx.calibrated("warp")
    if not warp.known:
        findings.append(Draft(f"process.{kind}.warp_uncalibrated", "minor", UNKNOWN, length(footprint),
                              warp.criterion("warp"), g.method("bed footprint"), "; ".join(warp.reasons),
                              "warp of the bed footprint is unknown",
                              remedy="calibrate warp for this material, enclosure and footprint size",
                              recheck=[f"process.{kind}.thermal_distortion"]))
        parts.append((UNKNOWN, "warp not calibrated"))
    else:
        size = warp.calibration.scope.get("size")
        covered = None
        if isinstance(size, Mapping):
            covered = ctx.mi.quantity(size).to("mm").bounds[1]
        if covered is not None and footprint > covered + 1e-9:
            findings.append(Draft(f"process.{kind}.warp_scope", "minor", UNKNOWN, length(footprint),
                                  warp.criterion("footprint", "<=", covered, "mm"), g.method("bed footprint"),
                                  "the calibration covers smaller footprints only",
                                  "warp of this footprint is outside the calibrated scope",
                                  remedy="calibrate warp at this footprint size", recheck=[f"process.{kind}.thermal_distortion"]))
            parts.append((UNKNOWN, "footprint beyond the warp calibration"))
        else:
            parts.append(_flatness(ctx, warp, findings))
    return combine(method, parts, findings, uncertainty="calibrated scatter; part-specific thermal history is not simulated")


def _flatness(ctx: Context, warp: Calibrated, findings: list[Draft]) -> tuple[str, str]:
    worst = warp.bounds("mm")[1]
    for feature, surface in ctx.critical_features().items():
        for item in ctx.surface_raw(surface).get("criteria") or ():
            if item.get("property") == "flatness" and item.get("operator") == "<=" \
                    and surface.dimension_state == "as_manufactured":
                limit = ctx.mi.convert(item["value"], item.get("units", "mm"), "mm")
                if worst > limit + 1e-9:
                    findings.append(Draft(f"process.{_k(ctx)}.warp", "major", FAIL, length(worst),
                                          {**item, "units": item.get("units", "mm")}, "calibrated warp against the criterion",
                                          f"calibration {warp.calibration.id}",
                                          f"the as-printed {feature} can exceed its flatness limit",
                                          remedy=f"finish {feature} after printing or reorient", feature=feature,
                                          recheck=[f"process.{_k(ctx)}.thermal_distortion"]))
                    return FAIL, f"{feature} flatness"
    return PASS, "warp calibrated for this footprint"


# --- resin ------------------------------------------------------------------------------------------------------

def drainage(ctx: Context) -> Result:
    method = "resin drainage: enclosed voids that trap uncured resin"
    g = ctx.measured("drainage and trapped volumes")
    if isinstance(g, Result):
        return g
    voids = int(g.facts.get("shells") or 0) - int(g.facts.get("solids") or 0)
    if voids > 0:
        draft = Draft("process.resin.trapped_volume", "major", FAIL, {"value": voids, "units": "count"},
                      policy("enclosed_voids", "<=", 0, "count"), g.method("shells per solid"), "exact B-rep shells",
                      "an enclosed cavity traps uncured resin and can crack the part during curing",
                      remedy="add drain and vent holes reachable for washing", recheck=["process.resin.drainage"])
        return Result(FAIL, method, "enclosed voids", [draft])
    return Result(PASS, method, "no enclosed void; open cups and suction regions are not assessed",
                  [_trial(ctx, "drainage of open cups and suction regions in the approved orientation")])


def post_cure(ctx: Context) -> Result:
    method = "resin wash and post-cure plan"
    profile, op = ctx.profile, ctx.operation
    missing = []
    for name in ("wash", "post_cure"):
        fact = profile.parameters.get(name) if profile is not None else None
        if fact is None or not fact.known:
            missing.append(f"capability {name}")
    if not op.raw.get("post_processing"):
        missing.append("the operation's post_processing")
    findings = [Draft("process.resin.ageing", "info", UNKNOWN, unknown("no ageing or stability evidence"),
                      unknown("ageing, brittleness and dimensional stability of the cured resin"), "record content",
                      "curing state, ageing and brittleness are not geometric", "long-term behaviour is unvalidated",
                      remedy="test cured coupons for the service conditions", recheck=["process.resin.trial"],
                      earliest_stage="release", link=False)]
    if missing:
        findings.append(Draft("process.resin.post_cure_unplanned", "minor", UNKNOWN, text(f"missing {', '.join(missing)}"),
                              policy("post_cure_plan", "equals", "wash, cure and post-processing recorded"),
                              "record content", "record content only", "cleaning and curing are unplanned",
                              remedy="record the wash and post-cure equipment and the post-processing steps",
                              recheck=["process.resin.post_cure"]))
        return Result(UNKNOWN, method, f"missing {', '.join(missing)}", findings)
    return Result(PASS, method, "wash, post-cure and post-processing recorded", findings)


FDM = {"orientation": gated(orientation), "geometry": gated(geometry), "support": gated(support),
       "thermal_distortion": gated(thermal_distortion)}
RESIN = {"orientation": gated(orientation), "geometry": gated(geometry), "support": gated(support),
         "drainage": drainage, "post_cure": gated(post_cure)}
