"""Secondary-operation handlers: drilling, reaming, tapping, finishing, coating, joining and inspection.

A secondary operation acts on an earlier state: it needs a predecessor, the datums it transfers from,
access to each target feature from a recorded side, and the allowance the earlier operation left. The
as-made variation the allowance must clean up is the predecessor's calibration, never a nominal size.
"""

from __future__ import annotations

from typing import Any

from .core import (FAIL, NOT_APPLICABLE, PASS, UNKNOWN, Context, Draft, Result, access_directions, combine, direction_key, dot,
                   gated, length, policy, text, unknown)
from .machining import tools

CLEANUP_PURPOSES = frozenset({"stock", "finish", "support_removal"})
# The calibration of the as-made deviation each primary kind leaves on a feature (by build orientation for additive).
AS_MADE = {"fdm": "hole_diameter_deviation", "resin": "hole_diameter_deviation", "laser": "kerf_deviation",
           "waterjet": "kerf_deviation", "plasma": "kerf_deviation", "cnc": "dimensional_deviation",
           "drilling": "dimensional_deviation", "casting": "dimensional_deviation", "moulding": "dimensional_deviation"}


def _datums(ctx: Context, findings: list[Draft]) -> tuple[str, str]:
    datums = list((ctx.operation.setup or {}).get("datums") or ())
    kind = ctx.operation.kind
    if not datums:
        findings.append(Draft(f"process.{kind}.datum_transfer", "major", UNKNOWN, unknown("no datums"),
                              policy("setup_datums", "equals", "the datum features this operation locates on"),
                              "part.yaml route setup", "not recorded",
                              f"how {ctx.operation.id} locates the part made by an earlier operation is unknown: the datum transfer "
                              "governs the position of everything it makes",
                              remedy="record the datums (features of the input state) this setup locates on",
                              recheck=[f"process.{kind}.access", "route.operation_plan"]))
        return UNKNOWN, "datum transfer unknown"
    known = {s.feature for s in ctx.surfaces()} | set((ctx.geometry.facts.get("features") or {}) if ctx.geometry else ())
    unknown_ids = [d for d in datums if d not in known]
    if unknown_ids:
        findings.append(Draft(f"process.{kind}.datum_unknown", "minor", UNKNOWN, text(f"datums {', '.join(unknown_ids)}"),
                              policy("setup_datums", "equals", "features of the part"), "critical surfaces and feature locators",
                              "no critical surface or locator names them", "the datum features cannot be traced",
                              remedy="name the datums as critical surfaces or feature locators", recheck=["route.operation_plan"]))
        return UNKNOWN, "datums untraced"
    return PASS, f"locates on {', '.join(datums)}"


def _predecessor(ctx: Context, findings: list[Draft]) -> tuple[str, str]:
    if ctx.before() or ctx.operation.input_state:
        return PASS, "acts on an earlier state"
    kind = ctx.operation.kind
    findings.append(Draft(f"process.{kind}.no_input", "major", FAIL, text(f"{ctx.operation.id} has no predecessor"),
                          policy("input_state", "equals", "an earlier operation's output"), "part.yaml route order",
                          "record content", f"a {kind} operation needs a part to act on; nothing makes it first",
                          remedy="put the shaping operation first, or record the input state", recheck=["route.operation_plan"]))
    return FAIL, "no input state"


def _target_ids(ctx: Context, findings: list[Draft]) -> set[str] | None:
    targets = ctx.targets()
    if not targets:
        kind = ctx.operation.kind
        findings.append(Draft(f"process.{kind}.targets_missing", "major", UNKNOWN, unknown("no targets"),
                              policy("targets", "equals", "the features the operation acts on"), "part.yaml route",
                              "not recorded", f"what {ctx.operation.id} acts on is unknown", remedy="record the target features",
                              recheck=[f"process.{kind}"]))
        return None
    return targets


def access(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} access: predecessor, datum transfer, and each target reached along a recorded side"
    findings: list[Draft] = []
    parts = [_predecessor(ctx, findings), _datums(ctx, findings)]
    targets = _target_ids(ctx, findings)
    if targets is None:
        return combine(method, parts + [(UNKNOWN, "no targets")], findings)
    sides = ((ctx.operation.setup or {}).get("access") or {}).get("sides") or ()
    parsed, bad = access_directions(sides)
    if not parsed or bad:
        findings.append(Draft(f"process.{kind}.access_unknown", "major", UNKNOWN, text(f"sides {', '.join(bad) or 'not recorded'}"),
                              policy("access_sides", "one_of", ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]), "part.yaml route setup access",
                              "not recorded", f"whether the tool of {ctx.operation.id} reaches {', '.join(sorted(targets))} is unknown",
                              remedy="record the access sides of the setup as +X .. -Z", recheck=[f"process.{kind}.access"]))
        return combine(method, parts + [(UNKNOWN, "access sides unknown")], findings)
    g = ctx.measured("target access")
    if isinstance(g, Result):
        return combine(method, parts + [(UNKNOWN, g.coverage)], findings + g.findings)
    reach = [t.reach[0] for t in tools(ctx) if t.reach is not None]
    for feature in sorted(targets):
        hole = next((h for h in g.holes() if feature in (h.get("features") or ())), None)
        if hole is not None:
            ways = [[-c for c in hole["axis"]]] + ([list(hole["axis"])] if hole.get("through") else [])
            hit = [key for key, d in parsed for way in ways if dot(d, way) > 0.9998]
            if not hit:
                findings.append(Draft(f"process.{kind}.target_access", "major", FAIL,
                                      text(f"{feature} entered along {direction_key(ways[0]) or ways[0]}"),
                                      policy("approach_side", "one_of", [k for k, _ in parsed]), g.method("bore recognition"),
                                      "straight tool axis", f"the {kind} tool cannot reach {feature} from the recorded sides",
                                      remedy=f"add a setup from {feature}'s side, or change the order so it is reachable",
                                      recheck=[f"process.{kind}.access"], feature=feature))
                parts.append((FAIL, f"{feature} unreachable"))
                continue
            if reach and max(reach) + 1e-9 < hole["depth_mm"]:
                findings.append(Draft(f"process.{kind}.target_reach", "major", FAIL, length(hole["depth_mm"]),
                                      policy("feature_depth", "<=", round(max(reach), 6), "mm", "the recorded tool reach"),
                                      g.method("bore recognition"), "tools as recorded", f"the tool does not reach the bottom of {feature}",
                                      remedy="a longer tool, or work from both ends", recheck=[f"process.{kind}.access"], feature=feature))
                parts.append((FAIL, f"{feature} beyond reach"))
                continue
            parts.append((PASS, f"{feature} reached from {hit[0]}"))
            continue
        faces = g.feature_faces(feature)
        if not faces:
            findings.append(Draft(f"process.{kind}.target_unlocated", "minor", UNKNOWN, unknown("no locator matched"),
                                  unknown("the target's geometry"), g.method("feature locators"), "no locator on this component",
                                  f"access to {feature} is not assessed", remedy="add a feature locator for the target",
                                  recheck=[f"process.{kind}.access"], feature=feature))
            parts.append((UNKNOWN, f"{feature} not located"))
            continue
        keys = [k for _, d in parsed if (k := direction_key(d)) is not None]  # a parsed side is an axis direction
        visible = [max((face.get("visible") or {}).get(k, -1.0) for k in keys) for face in faces]
        if any(v < 0 for v in visible):
            parts.append((UNKNOWN, f"{feature} visibility not probed"))
        elif max(visible) <= 0.0:
            findings.append(Draft(f"process.{kind}.target_access", "major", FAIL, text(f"{feature} hidden from {', '.join(keys)}"),
                                  policy("face_visibility", "equals", "visible along an access side"),
                                  g.method("rays from face samples"), "sampled rays", f"{feature} cannot be reached from the recorded sides",
                                  remedy="add a setup or change the order", recheck=[f"process.{kind}.access"], feature=feature))
            parts.append((FAIL, f"{feature} hidden"))
        else:
            parts.append((PASS, f"{feature} visible"))
    return combine(method, parts, findings)


def allowance(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} allowance: the stock the earlier operation left on each target against its calibrated as-made variation"
    findings: list[Draft] = []
    targets = _target_ids(ctx, findings)
    if targets is None:
        return combine(method, [(UNKNOWN, "no targets")], findings)
    parts: list[tuple[str, str]] = []
    earlier = ctx.before()
    for feature in sorted(targets):
        found = [(op, a) for op, a in ctx.allowances(feature, earlier) if a.get("purpose") in CLEANUP_PURPOSES]
        if not found:
            findings.append(Draft(f"process.{kind}.allowance_missing", "major", FAIL, unknown("no allowance"),
                                  policy("allowance", "equals", f"recorded by the operation before {ctx.operation.id}"),
                                  "part.yaml route allowances", "record content",
                                  f"{ctx.operation.id} finishes {feature} but no earlier operation leaves stock for it: it cannot clean up "
                                  "the as-made surface", remedy=f"record the allowance the earlier operation leaves on {feature}",
                                  recheck=[f"process.{kind}.allowance"], feature=feature))
            parts.append((FAIL, f"{feature} has no allowance"))
            continue
        source_op, item = found[-1]
        amount = ctx.mi.quantity(item["amount"]).to("mm")
        parts.append(_cleanup(ctx, feature, source_op, amount, item, findings))
    return combine(method, parts, findings)


def _cleanup(ctx: Context, feature: str, source_op: Any, amount: Any, item: Any, findings: list[Draft]) -> tuple[str, str]:
    """The allowance must cover the predecessor's positive as-made deviation, or the finishing tool leaves
    as-made surface behind."""
    characteristic = AS_MADE.get(source_op.kind)
    if characteristic is None:
        return UNKNOWN, f"{feature}: no as-made deviation contract for {source_op.kind}"
    profile = ctx.record.capabilities.get(source_op.capability) if isinstance(source_op.capability, str) else None
    orientation = None
    if source_op.kind in ("fdm", "resin"):
        geometry = ctx.geometries.get((ctx.pn, ctx.component))
        hole = next((h for h in (geometry.holes() if geometry else ()) if feature in (h.get("features") or ())), None)
        if hole is None:
            return UNKNOWN, f"{feature}: as-printed orientation unknown"
        from .additive import hole_class
        orientation = hole_class(hole["axis"])
    cal = ctx.calibrated(characteristic, profile=profile, orientation=orientation)
    kind = ctx.operation.kind
    if not cal.known:
        findings.append(Draft(f"process.{kind}.allowance_unverified", "minor", UNKNOWN, {**amount.as_dict()},
                              cal.criterion(characteristic), "allowance against the predecessor's calibration",
                              "; ".join(cal.reasons), f"whether {amount} on {feature} cleans up the as-made variation is unknown",
                              remedy=f"calibrate {characteristic} of {source_op.id}", recheck=[f"process.{kind}.allowance"], feature=feature))
        return UNKNOWN, f"{feature} allowance unverified"
    worst = cal.bounds("mm")[1]
    if amount.bounds[0] < worst - 1e-9:
        findings.append(Draft(f"process.{kind}.allowance_short", "major", FAIL, {**amount.as_dict()},
                              cal.criterion("allowance", ">=", worst, "mm"), "allowance against the predecessor's calibration",
                              f"calibration {cal.calibration.id}",
                              f"the {amount} allowance on {feature} is smaller than {source_op.id}'s positive as-made deviation: "
                              "some parts come out oversize before finishing",
                              remedy=f"leave more stock on {feature}", recheck=[f"process.{kind}.allowance"], feature=feature))
        return FAIL, f"{feature} allowance short"
    return PASS, f"{feature} allowance {amount} covers {source_op.id}'s deviation"


def engagement(ctx: Context) -> Result:
    method = "tapping: thread engagement depth against the operation's criterion"
    findings: list[Draft] = []
    targets = _target_ids(ctx, findings)
    if targets is None:
        return combine(method, [(UNKNOWN, "no targets")], findings)
    criteria = [c for c in ctx.operation.raw.get("criteria") or () if c.get("property") == "thread_engagement"]
    if not criteria:
        findings.append(Draft("process.tapping.engagement_unknown", "major", UNKNOWN, unknown("no engagement criterion"),
                              unknown("the thread and its required engagement"), "part.yaml route criteria", "not recorded",
                              "whether the thread holds its load is unknown",
                              remedy="record the thread and its required engagement (criterion thread_engagement >= mm)",
                              recheck=["process.tapping.engagement"]))
        return Result(UNKNOWN, method, "engagement criterion missing", findings)
    g = ctx.measured("thread engagement")
    if isinstance(g, Result):
        return g
    parts: list[tuple[str, str]] = []
    need = ctx.mi.convert(criteria[0]["value"], criteria[0].get("units", "mm"), "mm")
    for feature in sorted(targets):
        hole = next((h for h in g.holes() if feature in (h.get("features") or ())), None)
        if hole is None:
            parts.append((UNKNOWN, f"{feature} not a recognised bore"))
            continue
        if hole["depth_mm"] < need - 1e-9:
            findings.append(Draft("process.tapping.engagement", "major", FAIL, length(hole["depth_mm"]), {**criteria[0]},
                                  g.method("bore depth"), "full-depth thread assumed; tap lead-in not deducted",
                                  f"{feature} is too shallow for the required thread engagement",
                                  remedy="deepen the hole, use a thread insert or a through bolt", feature=feature,
                                  recheck=["process.tapping.engagement"]))
            parts.append((FAIL, f"{feature} engagement"))
        else:
            parts.append((PASS, f"{feature} depth {hole['depth_mm']:g} mm"))
    return combine(method, parts, findings)


def feature(ctx: Context) -> Result:
    method = "drilling: each target bore made by a recorded tool size within its reach"
    findings: list[Draft] = []
    targets = _target_ids(ctx, findings)
    if targets is None:
        return combine(method, [(UNKNOWN, "no targets")], findings)
    g = ctx.measured("drilled features")
    if isinstance(g, Result):
        return g
    available = [(t, t.diameter) for t in tools(ctx) if t.diameter is not None]
    parts: list[tuple[str, str]] = []
    for name in sorted(targets):
        hole = next((h for h in g.holes() if name in (h.get("features") or ())), None)
        if hole is None:
            parts.append((UNKNOWN, f"{name} not a recognised bore"))
            continue
        if not available:
            findings.append(Draft("process.drilling.tools_unknown", "minor", UNKNOWN, length(hole["diameter_mm"]),
                                  unknown("no drill sizes recorded"), "setup access tools and capability tools", "not recorded",
                                  "whether a standard drill makes the bore is unknown", remedy="record the drills",
                                  recheck=["process.drilling.feature"], feature=name))
            parts.append((UNKNOWN, f"{name} tools unknown"))
            continue
        match = [t for t, diameter in available
                 if abs(diameter[0] - hole["diameter_mm"]) <= 0.01 and abs(diameter[1] - hole["diameter_mm"]) <= 0.01]
        if not match:
            findings.append(Draft("process.drilling.size", "major", FAIL, length(hole["diameter_mm"]),
                                  policy("drill_diameter", "equals", hole["diameter_mm"], "mm", "a recorded drill of the bore size"),
                                  g.method("bore recognition"), "tools as recorded",
                                  f"no recorded drill makes the Ø{hole['diameter_mm']:g} mm {name}",
                                  remedy="use a standard drill size or record the drill", recheck=["process.drilling.feature"], feature=name))
            parts.append((FAIL, f"{name} has no drill"))
            continue
        reach = [t.reach[0] for t in match if t.reach is not None]
        if reach and max(reach) + 1e-9 < hole["depth_mm"]:
            findings.append(Draft("process.drilling.depth", "major", FAIL, length(hole["depth_mm"]),
                                  policy("hole_depth", "<=", round(max(reach), 6), "mm", "the drill's reach"),
                                  g.method("bore recognition"), "tools as recorded", f"the drill does not reach the bottom of {name}",
                                  remedy="a longer drill, or drill from both ends", recheck=["process.drilling.feature"], feature=name))
            parts.append((FAIL, f"{name} beyond reach"))
        else:
            parts.append((PASS if reach else UNKNOWN, f"{name} drilled by {match[0].name}"))
    return combine(method, parts, findings)


def coating_allowance(ctx: Context) -> Result:
    method = "coating allowance: every coated critical fit carries the coating thickness"
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    targets = ctx.targets()
    covered = [s for s in ctx.surfaces() if not targets or s.feature in targets]
    for surface in covered:
        if surface.dimension_state == "as_manufactured":
            continue  # its stated dimension is before coating
        found = [a for a in ctx.operation.allowances if a.get("feature") == surface.feature and a.get("purpose") == "coating"]
        if not found:
            findings.append(Draft("process.coating.fit_allowance", "major", FAIL, unknown("no coating allowance"),
                                  policy("coating_allowance", "equals", "the coating thickness per surface of each coated fit"),
                                  "part.yaml route allowances", "record content",
                                  f"the coating changes the {surface.role} {surface.feature} after it was sized: the stated fit "
                                  "no longer holds", remedy=f"record the coating allowance on {surface.feature}, or mask it",
                                  alternatives=[{"text": "mask the fitted surface", "tradeoffs": "uncoated, unprotected bore"}],
                                  recheck=["process.coating.allowance", "interface.fit", "interface.stack"], feature=surface.feature))
            parts.append((FAIL, f"{surface.feature} coated without allowance"))
        else:
            parts.append((PASS, f"{surface.feature} coating allowance recorded"))
    if not parts:
        return Result(NOT_APPLICABLE, method, "no coated critical fit")
    return combine(method, parts, findings,
                   uncertainty="the finished size after coating against its limits is the interface fit's check")


def coating_access(ctx: Context) -> Result:
    method = "coating plan: coating type, method and masking"
    profile = ctx.profile
    missing = [name for name in ("coating_type", "method")
               if profile is None or name not in profile.parameters or not profile.parameters[name].known]
    findings: list[Draft] = []
    parts = [_predecessor(ctx, findings)]
    if missing:
        findings.append(Draft("process.coating.plan_unknown", "minor", UNKNOWN, text(f"missing {', '.join(missing)}"),
                              policy("coating_plan", "equals", "coating type and method recorded"), "capability parameters",
                              "not recorded", "what coats which surfaces is unknown", remedy=f"record the {', '.join(missing)}",
                              recheck=["process.coating.access"]))
        parts.append((UNKNOWN, "coating plan unknown"))
    targets = ctx.targets()
    parts.append((PASS, f"coats {', '.join(sorted(targets))}" if targets else "coats the whole part (no masking recorded)"))
    return combine(method, parts, findings)


def joining_distortion(ctx: Context) -> Result:
    method = "joining distortion against a calibration"
    cal = ctx.calibrated("join_distortion")
    if cal.known:
        return Result(PASS, method, f"calibrated ({cal.calibration.id})")
    draft = Draft("process.joining.distortion_uncalibrated", "minor", UNKNOWN, unknown("join distortion"),
                  cal.criterion("join_distortion"), "capability calibrations", "; ".join(cal.reasons),
                  "the distortion the join leaves on aligned features is unknown",
                  remedy="calibrate join distortion with test joints, or machine after joining", recheck=["process.joining"])
    return Result(UNKNOWN, method, "join distortion unknown", [draft])


def inspection_access(ctx: Context) -> Result:
    method = "inspection access: each inspected feature located on the component"
    findings: list[Draft] = []
    entries = list(ctx.operation.inspections)
    if not entries:
        findings.append(Draft("process.inspection.plan_missing", "major", UNKNOWN, unknown("no inspection entries"),
                              policy("inspection", "equals", "entries naming feature, method and state"), "part.yaml route",
                              "not recorded", "what this inspection measures is unknown", remedy="record the inspection entries",
                              recheck=["process.inspection"]))
        return Result(UNKNOWN, method, "no inspection entries", findings)
    g = ctx.measured("inspected features")
    if isinstance(g, Result):
        return g
    parts: list[tuple[str, str]] = []
    for item in entries:
        name = item.get("feature")
        if name is None:
            parts.append((NOT_APPLICABLE, ""))
        elif g.feature(name) is None:
            findings.append(Draft("process.inspection.feature_unlocated", "minor", UNKNOWN, unknown("no locator"),
                                  unknown("the inspected feature's geometry"), g.method("feature locators"),
                                  "no locator", f"{name} cannot be located for inspection", remedy="add its locator",
                                  recheck=["process.inspection"], feature=name))
            parts.append((UNKNOWN, f"{name} not located"))
        else:
            parts.append((PASS, f"{name} located"))
    return combine(method, parts, findings, uncertainty="probe reach and line of sight of the instrument are not modelled")


def inspection_capability(ctx: Context) -> Result:
    method = "inspection capability: a calibrated measurement for each inspected characteristic"
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    for item in ctx.operation.inspections:
        capability = item.get("capability") or (ctx.operation.capability if isinstance(ctx.operation.capability, str) else None)
        profile = ctx.record.capabilities.get(capability) if capability else None
        if profile is None or profile.process != "inspection":
            findings.append(Draft("process.inspection.capability_unknown", "major", UNKNOWN, text(f"{item.get('id')}: {capability}"),
                                  unknown("an inspection capability profile with a calibrated measurement"),
                                  "inspection entry and capability profiles", "not recorded",
                                  f"the measurement {item.get('id')} relies on is not shown capable", feature=item.get("feature"),
                                  remedy="name an inspection capability profile with a calibrated measurement uncertainty",
                                  recheck=["process.inspection.capability"]))
            parts.append((UNKNOWN, f"{item.get('id')} capability unknown"))
            continue
        cal = ctx.calibrated("measurement_uncertainty", profile=profile)
        if not cal.known:
            findings.append(Draft("process.inspection.uncertainty_unknown", "minor", UNKNOWN, unknown("measurement uncertainty"),
                                  cal.criterion("measurement_uncertainty"), "capability calibrations", "; ".join(cal.reasons),
                                  f"the uncertainty of {item.get('id')} is unknown", remedy="calibrate the instrument for this feature",
                                  recheck=["process.inspection.capability"], feature=item.get("feature")))
            parts.append((UNKNOWN, f"{item.get('id')} uncertainty unknown"))
        else:
            parts.append((PASS, f"{item.get('id')} calibrated ({cal.calibration.id})"))
    if not parts:
        return Result(UNKNOWN, method, "no inspection entries")
    return combine(method, parts, findings)


DRILLING = {"access": gated(access), "feature": gated(feature)}
REAMING = {"access": gated(access), "allowance": gated(allowance)}
TAPPING = {"access": gated(access), "engagement": gated(engagement)}
FINISHING = {"allowance": gated(allowance), "access": gated(access)}
COATING = {"allowance": gated(coating_allowance), "access": gated(coating_access)}
JOINING = {"access": gated(access), "distortion": gated(joining_distortion)}
INSPECTION = {"access": inspection_access, "capability": inspection_capability}
