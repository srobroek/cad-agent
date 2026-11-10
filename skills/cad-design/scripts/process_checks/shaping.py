"""Shaping handlers: casting and moulding (draft, walls, tooling against quantity, defects and secondary
machining) and extrusion (constant profile and secondary operations).

The pull direction is the operation's approved orientation: machine +Z is the direction the tool opens,
so the native pull axis is the third row of that rotation. Geometry is the model-frame component.

Calibration characteristics read: min_draft (deg, criterion draft >= upper), min_wall (mm), shrinkage
(ratio or mm, allowance evidence).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .additive import wall_check
from .core import (FAIL, NOT_APPLICABLE, PASS, UNKNOWN, Context, Draft, Result, angle, combine, dot, gated, policy, text,
                   unknown)

MACHINING = frozenset({"cnc", "drilling", "reaming", "finishing", "tapping"})


def pull_axis(ctx: Context) -> tuple[float, float, float] | None:
    """The native pull direction from the approved orientation, None when it is not approved."""
    data = ctx.operation.orientation
    if not isinstance(data, Mapping) or data.get("status") != "approved":
        return None
    rotation = data.get("rotation") or {}
    if "quaternion" in rotation:
        w, x, y, z = (float(v) for v in rotation["quaternion"])
        norm = math.sqrt(w * w + x * x + y * y + z * z)
        w, x, y, z = (v / norm for v in (w, x, y, z))
        return (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y))
    axis, theta = rotation["axis"], math.radians(float(rotation.get("angle_deg", 0.0)))  # the schema requires both
    length = math.sqrt(sum(float(a) ** 2 for a in axis))
    x, y, z = (float(a) / length for a in axis)
    c, s, k = math.cos(theta), math.sin(theta), 1.0 - math.cos(theta)
    return (z * x * k - y * s, z * y * k + x * s, c + z * z * k)


def _pull_unknown(ctx: Context) -> Result:
    kind = ctx.operation.kind
    draft = Draft(f"process.{kind}.pull_unknown", "major", UNKNOWN, unknown("no approved pull direction"),
                  policy("orientation_status", "equals", "approved"), "part.yaml route orientation",
                  "the operation records no approved orientation (machine +Z is the pull direction)",
                  "draft, parting and undercuts cannot be judged", remedy="approve the tool's pull direction as the operation's orientation",
                  recheck=[f"process.{kind}.draft"], earliest_stage="detail")
    return Result(UNKNOWN, f"{kind} pull direction", "pull direction unknown", [draft])


def draft(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} draft and undercuts along the approved pull direction"
    pull = pull_axis(ctx)
    if pull is None:
        return _pull_unknown(ctx)
    g = ctx.measured("draft and undercuts")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    cal = ctx.calibrated("min_draft")
    need = cal.bounds("deg")[1] if cal.known else None
    worst, worst_face = 90.0, None
    for face in g.faces():
        normals = face.get("normals") or ()
        if not normals:
            continue
        # draft = the angle between the face and the pull direction; 0 for a wall parallel to it
        draft_deg = min(math.degrees(math.asin(min(1.0, abs(dot(n, pull))))) for n in normals)
        if draft_deg < worst:
            worst, worst_face = draft_deg, face
    if worst_face is None:
        return Result(UNKNOWN, method, "no face normals probed")
    if need is None:
        findings.append(Draft(f"process.{kind}.draft_uncalibrated", "minor", UNKNOWN, angle(worst),
                              cal.criterion("draft_angle", ">="), "capability calibrations", "; ".join(cal.reasons),
                              "the minimum draft for this material, finish and tool is unknown",
                              remedy="record the tool maker's or foundry's minimum draft", recheck=[f"process.{kind}.draft"]))
        parts.append((UNKNOWN, "draft criterion unknown"))
    elif worst < need - 1e-9:
        feats = sorted(worst_face.get("features") or ())
        findings.append(Draft(f"process.{kind}.draft", "major", FAIL, angle(worst), cal.criterion("draft_angle", ">=", need),
                              g.method("sampled face normals against the pull direction"), "sampled normals",
                              "a face with too little draft drags or locks in the tool",
                              remedy="add draft to the faces along the pull, or move the parting", recheck=[f"process.{kind}.draft"],
                              feature=feats[0] if len(feats) == 1 else None))
        parts.append((FAIL, f"draft {worst:.2f} deg"))
    else:
        parts.append((PASS, f"least draft {worst:.2f} deg"))
    keys = [k for k, v in (("+pull", pull), ("-pull", tuple(-c for c in pull)))]
    probed = True
    hidden: list[Mapping[str, Any]] = []
    for face in g.faces():
        visible = face.get("visible") or {}
        if not all(k in visible for k in keys):
            parts.append((UNKNOWN, "pull visibility not probed"))
            probed = False
            break
        if max(visible[k] for k in keys) <= 0.0:
            hidden.append(face)
    if probed and hidden:
        feats = sorted({f for face in hidden for f in face.get("features") or ()})
        findings.append(Draft(f"process.{kind}.undercut", "major", FAIL, text(f"{len(hidden)} face(s) hidden from both halves"),
                              policy("face_visibility", "equals", "visible from one tool half"),
                              g.method("rays along both pull senses"), "sampled rays",
                              "an undercut locks the part in a two-part tool",
                              remedy="remove the undercut, add a side action or core, or machine it afterwards",
                              recheck=[f"process.{kind}.draft"], feature=feats[0] if len(feats) == 1 else None))
        parts.append((FAIL, "undercuts"))
    elif probed:
        parts.append((PASS, "no undercut along the pull"))
    return combine(method, parts, findings)


def walls(ctx: Context) -> Result:
    method = f"{ctx.operation.kind} walls against the calibrated minimum"
    g = ctx.measured("walls")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    return combine(method, [wall_check(ctx, g, findings)], findings,
                   uncertainty="wall transitions and section ratios are not measured")


def tooling(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} tooling plan against the stated quantity"
    profile = ctx.profile
    tool = profile.parameters.get("tooling") if profile is not None else None
    quantity = ctx.record.use_case.facts.get("logistics.quantity")
    missing = []
    if tool is None or not tool.known:
        missing.append("the tooling (capability parameter tooling)")
    if quantity is None or not quantity.known:
        missing.append("the production quantity (use_case logistics.quantity)")
    if missing:
        draft = Draft(f"process.{kind}.tooling_unplanned", "major", UNKNOWN, text(f"missing {', '.join(missing)}"),
                      policy("tooling_plan", "equals", "tooling and quantity recorded"), "record content", "record content",
                      "tooling cost against quantity cannot be weighed", remedy=f"record {', '.join(missing)}",
                      recheck=[f"process.{kind}.tooling", "selection"], earliest_stage="detail")
        return Result(UNKNOWN, method, f"missing {', '.join(missing)}", [draft])
    return Result(PASS, method, "tooling and quantity recorded; the cost comparison is the selection's, never invented here")


def defects(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} shrinkage allowance and the secondary machining of critical surfaces"
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    shrink = ctx.calibrated("shrinkage")
    allowances = [a for a in ctx.operation.allowances if a.get("purpose") == "shrinkage"]
    if not shrink.known and not allowances:
        findings.append(Draft(f"process.{kind}.shrinkage_unknown", "major", UNKNOWN, unknown("shrinkage"),
                              shrink.criterion("shrinkage"), "capability calibrations and allowances", "; ".join(shrink.reasons),
                              "as-made dimensions are uncertain by the material's shrinkage",
                              remedy="record the shrinkage the tool is compensated for", recheck=[f"process.{kind}.defects"]))
        parts.append((UNKNOWN, "shrinkage unknown"))
    else:
        parts.append((PASS, "shrinkage calibrated or allowed"))
    later = ctx.after()
    for surface in ctx.surfaces():
        if surface.dimension_state == "as_manufactured":
            continue
        finishing = [op for op in later if op.kind in MACHINING and surface.feature in ctx.targets(op)]
        if not finishing:
            findings.append(Draft(f"process.{kind}.secondary_missing", "major", FAIL, text(f"{surface.feature} {surface.dimension_state}"),
                                  policy("secondary_operation", "equals", "a later machining operation on the feature"),
                                  "part.yaml route", "record content",
                                  f"the {surface.role} {surface.feature} is specified finished but nothing machines it",
                                  remedy=f"add the machining operation of {surface.feature} with its stock allowance",
                                  recheck=[f"process.{kind}.defects", "route.operation_plan"], feature=surface.feature))
            parts.append((FAIL, f"{surface.feature} not machined"))
        elif not any(a.get("feature") == surface.feature for a in ctx.operation.allowances):
            findings.append(Draft(f"process.{kind}.machining_allowance", "major", FAIL, unknown("no allowance"),
                                  policy("machining_allowance", "equals", "recorded"), "part.yaml route allowances", "record content",
                                  f"{finishing[0].id} machines {surface.feature} but the {kind} leaves no stock for it",
                                  remedy=f"record the machining allowance on {surface.feature}", feature=surface.feature,
                                  recheck=[f"process.{kind}.defects"]))
            parts.append((FAIL, f"{surface.feature} has no machining allowance"))
        else:
            parts.append((PASS, f"{surface.feature} machined by {finishing[0].id}"))
    findings.append(Draft(f"process.{kind}.defect_evidence", "info", UNKNOWN, unknown("no process defect evidence"),
                          unknown("porosity, sink, voids and fill of the actual process"), "record content",
                          "process defects are not geometric", "defects are unvalidated until first articles are inspected",
                          remedy="inspect first articles (sectioning, X-ray or CT where critical)", recheck=[f"process.{kind}"],
                          earliest_stage="release", link=False))
    return combine(method, parts, findings)


def profile(ctx: Context) -> Result:
    method = "extrusion profile: a constant section along one axis"
    g = ctx.measured("extrusion profile")
    if isinstance(g, Result):
        return g
    axis = g.facts.get("prismatic_axis")
    if axis is None:
        draft = Draft("process.extrusion.not_prismatic", "major", FAIL, text("no constant section axis"),
                      policy("section", "equals", "constant along one axis"), g.method("face normals and cylinder axes"),
                      "every face must be parallel to one axis, apart from two end caps",
                      "the part is not one extruded profile: its other features need secondary operations",
                      remedy="route the non-profile features to secondary operations", recheck=["process.extrusion"])
        return Result(FAIL, method, "not prismatic", [draft])
    return Result(PASS, method, f"constant section along {axis}")


def secondary(ctx: Context) -> Result:
    method = "extrusion secondary operations: cut to length and the non-profile features"
    if ctx.after():
        return Result(PASS, method, f"followed by {', '.join(op.id for op in ctx.after())}")
    g = ctx.measured("secondary features")
    if isinstance(g, Result):
        return g
    holes = g.holes()
    axis = g.facts.get("prismatic_axis")
    cross = [h for h in holes if axis is None or abs(dot(h["axis"], axis)) < 0.9998]
    if cross:
        draft = Draft("process.extrusion.secondary_missing", "major", FAIL, {"value": len(cross), "units": "count"},
                      policy("secondary_operation", "equals", "a later operation for each non-profile feature"),
                      g.method("bore axes against the profile axis"), "exact bores",
                      "holes across the profile cannot be extruded and no later operation makes them",
                      remedy="add the drilling or machining operation", recheck=["process.extrusion.secondary"])
        return Result(FAIL, method, "cross holes without an operation", [draft])
    return Result(NOT_APPLICABLE, method, "no non-profile feature and no later operation")


CASTING = {"draft": gated(draft), "walls": gated(walls), "tooling": gated(tooling), "defects": gated(defects)}
MOULDING = dict(CASTING)
EXTRUSION = {"profile": gated(profile), "secondary": gated(secondary)}
