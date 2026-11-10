"""Sheet handlers: laser, waterjet and plasma cutting, each against its own capability profile (no laser
assumption is inherited by waterjet or plasma), and sheet-metal forming.

Laser material safety is plugin policy: a halogenated polymer (PVC, CPVC, vinyl, PVB, PTFE, FEP, PVDF)
is rejected whatever a profile says; any other stock must be the exact identified grade, approved for
this machine at this thickness, with its coating identified and extraction approved, or it is blocked
pending identification. Kerf, taper and fits are calibrations, never nominal flat geometry.

Calibration characteristics read (scope: material, and thickness where given):
  kerf_deviation        mm, as-cut minus nominal width of a cut feature after kerf compensation
  min_hole, min_web     mm, smallest reliable cut hole and web: criterion >= upper
  heat_affected_zone    mm, the edge zone the heat changes (laser, plasma)
  flatness              mm, cut-part flatness (laser, plasma) for planar datums: upper <= the datum's flatness limit
  taper, edge_finish    waterjet edge taper (mm) and finish (um: upper <= the edge's surface_roughness limit)
  min_bend_radius       mm, inner bend radius: criterion radius >= upper (sheet forming)
  min_hole_to_bend      mm, hole edge to bend: criterion >= upper
  springback, bend_allowance  sheet forming flat-pattern and angle evidence
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from .additive import hole_fits
from .core import (FAIL, NOT_APPLICABLE, PASS, POLICY, UNKNOWN, Calibrated, Context, Draft, Result, combine, gated, length,
                   policy, source_dict, text, unknown)

HALOGENATED = re.compile(r"(?<![a-z])(pvc|cpvc|polyvinyl[\s_-]*chloride|vinyl|pvb|polyvinyl[\s_-]*butyral|ptfe|teflon|"
                         r"fep|pvdf|chlorinated|fluoropolymer)(?![a-z])", re.IGNORECASE)
LASER_POLICY = {"kind": "policy", "reference": "laser cutting material safety: reject PVC and other halogenated polymers; "
                "require identified, approved stock, coating and extraction",
                "origin": "cad-agent plugin", "document": "skills/cad-design/scripts/process_checks/sheet.py"}
SHAPING = frozenset({"cnc", "drilling", "reaming", "finishing"})


def _stock_thickness(ctx: Context, op: Any) -> tuple[Any, dict[str, Any] | None]:
    """The stock thickness Quantity an operation records (setup.stock.thickness or purchase.thickness)."""
    data = ((op.setup or {}).get("stock") or {}).get("thickness")
    if not isinstance(data, Mapping) and op.purchase:
        data = op.purchase.get("thickness")
    if not isinstance(data, Mapping) or data.get("status") != "known":
        return None, None
    sources = data.get("sources") or []
    return ctx.mi.quantity(data["value"]).to("mm"), (source_dict(sources[0]) if sources else None)


def _known(fact: Any) -> bool:
    return isinstance(fact, Mapping) and fact.get("status") == "known"


# --- material ---------------------------------------------------------------------------------------------------

def _rule(laser: bool, prop: str, value: str) -> dict[str, Any]:
    """A categorical criterion of the laser material-safety policy (laser) or the dispatch policy."""
    if not laser:
        return policy(prop, "equals", value)
    return {"property": prop, "operator": "equals", "value": value, "source": dict(LASER_POLICY)}


def material(ctx: Context) -> Result:
    kind = ctx.operation.kind
    laser = kind == "laser"
    method = f"{kind} stock: exact identified material, thickness, coating{' and extraction' if laser else ''} approved for the machine"
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    block = "critical" if laser else "major"
    ident = ctx.stock_material_id()
    profile = ctx.record.materials.get(ident) if ident else None
    if profile is None:
        findings.append(Draft(f"process.{kind}.material_unidentified", block, UNKNOWN, unknown("the stock material is unknown"),
                              _rule(laser, "stock_material", "an identified grade"),
                              "material profile lookup", "no material profile names the stock",
                              "cutting an unidentified material can release toxic or corrosive fumes and damage the machine"
                              if laser else "the cut quality and machine approval of an unidentified material are unknown",
                              remedy="identify the stock: manufacturer, grade and datasheet, then record its material profile",
                              recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        return Result(UNKNOWN, method, "stock not identified", findings)
    words = " ".join(str(v) for v in (profile.id, profile.family, profile.raw.get("title"),
                                      profile.grade.value if profile.grade.known else ""))
    unsafe = HALOGENATED.search(words) if laser else None
    if unsafe is not None:
        found = unsafe.group(1)
        findings.append(Draft("process.laser.material_unsafe", "critical", FAIL, text(f"{profile.id}: {found}"),
                              {"property": "laser_material", "operator": "equals", "value": "not halogenated",
                               "source": dict(LASER_POLICY)}, "material profile identity", "family and grade names",
                              "laser cutting a halogenated polymer releases hydrogen chloride or fluoride: it injures people and "
                              "corrodes the machine; no profile approval overrides this",
                              remedy="choose a laser-safe identified stock (such as a cast acrylic grade approved for the machine) "
                                     "or another process",
                              alternatives=[{"text": "CNC route the part from the same sheet", "tradeoffs": "tool radius in "
                                             "internal corners; machining capability and workholding"},
                                            {"text": "waterjet", "tradeoffs": "its own profile, taper and edge finish"}],
                              recheck=[f"process.{kind}.material", "route.consistency"], earliest_stage="parts"))
        return Result(FAIL, method, "unsafe laser material", findings)
    if profile.level != "grade" or not profile.grade.known:
        findings.append(Draft(f"process.{kind}.material_unidentified", block, UNKNOWN,
                              text(f"{profile.id} is {profile.level} guidance ({profile.family})"),
                              _rule(laser, "stock_material", "an identified grade"),
                              "material profile level", "a family name is not a material identity",
                              f"{profile.family} is not an exact stock: its additives, coating and fumes are unknown"
                              if laser else f"{profile.family} is not an exact stock",
                              remedy="identify the exact grade and supplier and record a grade profile with its datasheet",
                              recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        parts.append((UNKNOWN, "grade unknown"))
    approvals = ctx.capability.materials
    if ctx.mi.is_fact(approvals):
        findings.append(Draft(f"process.{kind}.material_approval", block, UNKNOWN, unknown("approvals unknown"),
                              unknown(approvals.reason or "the capability profile records no material approvals"),
                              "capability profile materials", "not recorded",
                              f"no approval says {ctx.capability.id} may cut {profile.id}",
                              remedy="record the exact materials, thicknesses and coatings the machine is approved for",
                              recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        return combine(method, parts + [(UNKNOWN, "approvals unknown")], findings)
    entry = next((a for a in approvals if a.get("material") == profile.id), None)
    if entry is None:
        findings.append(Draft(f"process.{kind}.material_approval", block, UNKNOWN, text(f"{profile.id} on {ctx.capability.id}"),
                              _rule(laser, "material_approval", "approved for this machine, material and thickness"),
                              "capability profile materials", "no approval entry",
                              f"{ctx.capability.id} is not approved for {profile.id}: do not cut it until it is",
                              remedy=f"approve {profile.id} on {ctx.capability.id} with the supplier's safety data and test cuts",
                              recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        return combine(method, parts + [(UNKNOWN, "not approved")], findings)
    source = source_dict(entry["source"])
    if entry.get("status") == "forbidden":
        findings.append(Draft(f"process.{kind}.material_forbidden", block, FAIL, text(f"{profile.id} forbidden"),
                              {"property": "material_approval", "operator": "equals", "value": "approved", "source": source},
                              "capability profile materials", "recorded approval",
                              f"{ctx.capability.id} forbids {profile.id}: {entry.get('reason')}",
                              remedy="choose an approved material or another capability", recheck=[f"process.{kind}.material"],
                              earliest_stage="parts"))
        return combine(method, parts + [(FAIL, "forbidden")], findings)
    parts.append((PASS, f"{profile.id} approved on {ctx.capability.id}"))
    modes = entry.get("operations")
    if modes and not ctx.before() and "cut" not in modes:
        findings.append(Draft(f"process.{kind}.operation_mode", "major", FAIL, text(f"approved for {', '.join(modes)}"),
                              {"property": "material_operations", "operator": "one_of", "value": ["cut"], "source": source},
                              "capability profile materials", "recorded approval",
                              f"{profile.id} is approved on {ctx.capability.id} only for {', '.join(modes)}, not cutting",
                              remedy="approve cutting of this stock, or score or engrave only", recheck=[f"process.{kind}.material"]))
        parts.append((FAIL, "cutting not approved"))
    parts.append(_thickness(ctx, entry, source, findings))
    coating = entry.get("coating")
    if not _known(coating):
        findings.append(Draft(f"process.{kind}.coating_unidentified", block if laser else "minor", UNKNOWN,
                              unknown("the coating is not identified"),
                              _rule(laser, "coating", "identified"),
                              "capability profile approval", "not recorded",
                              "an unidentified coating or film can release hazardous fumes or ruin the edge",
                              remedy="identify the stock's coating or film (or record none) in the approval",
                              recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        parts.append((UNKNOWN, "coating unidentified"))
    if laser:
        extraction = entry.get("extraction")
        machine = ctx.capability.parameters.get("extraction")
        if not _known(extraction) and not (machine is not None and machine.known):
            findings.append(Draft("process.laser.extraction", "critical", UNKNOWN, unknown("extraction not approved"),
                                  _rule(True, "extraction", "approved for this material"),
                                  "capability profile approval and parameters", "not recorded",
                                  "fume extraction suitable for this material is not established",
                                  remedy="record the extraction and filtration approved for this material",
                                  recheck=["process.laser.material"], earliest_stage="parts"))
            parts.append((UNKNOWN, "extraction unknown"))
    return combine(method, parts, findings)


def _thickness(ctx: Context, entry: Any, source: dict[str, Any], findings: list[Draft]) -> tuple[str, str]:
    kind = ctx.operation.kind
    stock, _ = _stock_thickness(ctx, ctx.operation)
    approved = entry.get("thickness")
    if stock is None:
        findings.append(Draft(f"process.{kind}.thickness_unknown", "major", UNKNOWN, unknown("stock thickness"),
                              policy("stock_thickness", "equals", "known"), "part.yaml route setup stock", "not recorded",
                              "approval, kerf and fits depend on the actual sheet thickness",
                              remedy="record the stock thickness with its tolerance from the supplier",
                              recheck=[f"process.{kind}"], earliest_stage="parts"))
        return UNKNOWN, "stock thickness unknown"
    if not _known(approved):
        findings.append(Draft(f"process.{kind}.thickness_approval", "major", UNKNOWN, {**stock.as_dict()},
                              unknown("the approval states no thickness"), "capability profile approval", "not recorded",
                              "the machine's approval at this thickness is unknown",
                              remedy="record the approved thickness range", recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        return UNKNOWN, "approved thickness unknown"
    window = ctx.mi.quantity(approved["value"]).to("mm")
    if not window.contains(stock):
        findings.append(Draft(f"process.{kind}.thickness", "major", FAIL, {**stock.as_dict()},
                              {"property": "stock_thickness", "operator": "between", "value": list(window.bounds), "units": "mm",
                               "source": source}, "capability profile approval", "recorded values",
                              "the stock thickness lies outside what the machine is approved to cut",
                              remedy="choose approved stock or extend the approval with test cuts",
                              recheck=[f"process.{kind}.material"], earliest_stage="parts"))
        return FAIL, "thickness outside the approval"
    return PASS, f"thickness {stock} approved"


# --- kerf and fits ----------------------------------------------------------------------------------------------

def kerf(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} kerf: calibrated as-cut width deviation, and every tab-and-slot fit over sheet thickness, kerf and coating intervals"
    findings: list[Draft] = []
    cal = ctx.calibrated("kerf_deviation")
    parts: list[tuple[str, str]] = []
    if not cal.known:
        findings.append(Draft(f"process.{kind}.kerf_uncalibrated", "major", UNKNOWN, unknown("as-cut width deviation"),
                              cal.criterion("kerf_deviation"), "capability calibrations", "; ".join(cal.reasons),
                              "as-cut sizes and every fit are unknown: nominal flat geometry is not an accuracy",
                              remedy="cut kerf coupons in this exact stock and thickness and record the deviation",
                              recheck=[f"process.{kind}.kerf"], earliest_stage="detail"))
        parts.append((UNKNOWN, "kerf not calibrated"))
    else:
        parts.append((PASS, f"kerf deviation calibrated ({cal.calibration.id})"))
    fits = _tab_slot_fits(ctx, cal, findings)
    parts += fits
    if fits:
        findings.append(Draft(f"process.{kind}.evidence_required", "info", UNKNOWN, unknown("no fit coupon assessed here"),
                              unknown("a representative fit coupon in the actual stock, kerf and coating"),
                              "interval stack of nominal width, calibrated kerf, stock thickness and coating",
                              "flat-part geometry does not establish assembled fit, rigidity or alignment",
                              "the tab-and-slot fits are unvalidated until a coupon assembles",
                              remedy="cut and assemble a representative fit coupon from the production stock",
                              recheck=[f"process.{kind}.fit_coupon"], earliest_stage="release", link=False))
    return combine(method, parts, findings)


def _tab_slot_fits(ctx: Context, cal: Calibrated, findings: list[Draft]) -> list[tuple[str, str]]:
    """Each interface where a slot on this component receives a tab of another sheet part: the worst-case
    clearance from the slot's nominal width and calibrated kerf, the tab's sheet thickness interval and
    the coating each side carries, against the interface's clearance criterion."""
    kind, g = ctx.operation.kind, ctx.geometry
    parts: list[tuple[str, str]] = []
    for if_id, interface in sorted(ctx.record.interfaces.items()):
        raw = interface.functional.raw
        surfaces = list(raw.get("surfaces") or ())
        mine = [s for s in surfaces if s.get("PN") == ctx.pn and (s.get("component") in (None, ctx.component))]
        others = [s for s in surfaces if s.get("PN") != ctx.pn]
        criterion = next((a["criterion"] for a in raw.get("acceptance") or ()
                          if a.get("criterion", {}).get("property") == "clearance"), None)
        if not mine or not others or criterion is None:
            continue
        slot, tab = mine[0], others[0]
        if g is None:
            parts.append((UNKNOWN, f"{if_id} slot width not measured"))
            continue
        width = (g.feature(slot["feature"]) or {}).get("width_mm")
        if width is None:
            continue  # not a slot of this component: the mating part's handler judges its own side
        thickness, thickness_source = _mating_thickness(ctx, tab["PN"])
        if thickness is None:
            findings.append(Draft(f"process.{kind}.tab_thickness_unknown", "major", UNKNOWN, unknown("tab sheet thickness"),
                                  {**criterion}, "mating part route stock", "not recorded",
                                  f"the {if_id} fit depends on {tab['PN']}'s actual sheet thickness",
                                  remedy=f"record {tab['PN']}'s stock thickness with its supplier tolerance",
                                  recheck=[f"process.{kind}.kerf", "interface.fit"], feature=slot["feature"]))
            parts.append((UNKNOWN, f"{if_id} tab thickness unknown"))
            continue
        if not cal.known:
            parts.append((UNKNOWN, f"{if_id} kerf unknown"))
            continue
        coat_slot = _coating(ctx, ctx.pn, slot["feature"])
        coat_tab = _coating(ctx, tab["PN"], tab["feature"])
        if coat_slot is None or coat_tab is None:
            which = slot["feature"] if coat_slot is None else f"{tab['PN']} {tab['feature']}"
            findings.append(Draft(f"process.{kind}.coating_thickness_unknown", "major", UNKNOWN, unknown("coating thickness"),
                                  {**criterion}, "route coating allowances", "a coating operation records no coating allowance",
                                  f"the coating on {which} changes the {if_id} fit by an unknown amount",
                                  remedy="record the coating allowance (thickness per surface) for the fitted feature",
                                  recheck=[f"process.{kind}.kerf", "interface.fit"], feature=slot["feature"]))
            parts.append((UNKNOWN, f"{if_id} coating unknown"))
            continue
        dev_low, dev_high = cal.bounds("mm")
        t_low, t_high = thickness.bounds
        low = (width + dev_low - 2 * coat_slot[1]) - (t_high + 2 * coat_tab[1])
        high = (width + dev_high - 2 * coat_slot[0]) - (t_low + 2 * coat_tab[0])
        nominal = width - (t_low + t_high) / 2.0
        c_units = criterion.get("units", "mm")
        convert = ctx.mi.convert
        if criterion.get("operator") == "between":
            need_low, need_high = (convert(v, c_units, "mm") for v in criterion["value"])
        elif criterion.get("operator") == ">=":
            need_low, need_high = convert(criterion["value"], c_units, "mm"), math.inf
        else:
            parts.append((UNKNOWN, f"{if_id} clearance criterion is not a range"))
            continue
        measured = {"lower": round(low, 6) + 0.0, "upper": round(high, 6) + 0.0, "units": "mm"}
        why = (f"nominal clearance {nominal:+.3f} mm; slot {width:g} mm with kerf {dev_low:+g}..{dev_high:+g} mm, "
               f"tab sheet {t_low:g}..{t_high:g} mm, coating {coat_slot[0]:g}..{coat_slot[1]:g} mm per slot face and "
               f"{coat_tab[0]:g}..{coat_tab[1]:g} mm per tab face")
        if low < need_low - 1e-9 or high > need_high + 1e-9:
            findings.append(Draft(f"process.{kind}.tab_slot_fit", "major", FAIL, measured, {**criterion},
                                  "worst-case interval of slot width, calibrated kerf, sheet thickness and coating", why,
                                  f"the nominal {if_id} tab-and-slot fit {'binds or does not assemble' if low < need_low else 'is loose'} "
                                  "once actual sheet thickness, kerf and coating vary",
                                  remedy="set the slot width from the measured stock thickness, kerf and coating so the worst "
                                         "case meets the clearance, and confirm with fit coupons",
                                  alternatives=[{"text": "measure each sheet batch and adjust the kerf compensation",
                                                 "tradeoffs": "a per-batch process step and inspection"},
                                                {"text": "mask the coating off the fitted faces", "tradeoffs": "a masking step; "
                                                 "uncoated edges"}],
                                  requirements=list(interface.functional.requirements),
                                  recheck=[f"process.{kind}.kerf", "interface.fit", "interface.stack"], feature=slot["feature"]))
            parts.append((FAIL, f"{if_id} worst-case clearance {low:+.3f}..{high:+.3f} mm"))
        else:
            parts.append((PASS, f"{if_id} worst-case clearance {low:+.3f}..{high:+.3f} mm"))
    return parts


def _mating_thickness(ctx: Context, pn: str) -> tuple[Any, Any]:
    part = ctx.record.parts.get(pn)
    route = part.intent.route if part is not None else None
    if route is None or ctx.mi.is_fact(route):
        return None, None
    for op in route:
        found = _stock_thickness(ctx, op)
        if found[0] is not None:
            return found
    return None, None


def _coating(ctx: Context, pn: str, feature: str) -> tuple[float, float] | None:
    """(lower, upper) coating thickness per surface on `feature` after cutting: (0, 0) without a coating
    operation on it, None when a coating operation covers it but records no coating allowance."""
    part = ctx.record.parts.get(pn)
    route = () if part is None or ctx.mi.is_fact(part.intent.route) else part.intent.route
    total = [0.0, 0.0]
    for op in route:
        if op.kind != "coating":
            continue
        targets = {t.get("feature") for t in op.targets}
        if targets and feature not in targets:
            continue
        amounts = [a for a in op.allowances if a.get("feature") == feature and a.get("purpose") == "coating"]
        if not amounts:
            return None
        lower, upper = ctx.mi.quantity(amounts[0]["amount"]).to("mm").bounds
        total[0] += lower
        total[1] += upper
    return total[0], total[1]


# --- features ---------------------------------------------------------------------------------------------------

def features(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} cut features: minimum holes and webs, controlled-depth features and critical as-cut bores"
    g = ctx.measured("cut features")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    machined = {f for op in ctx.after() if op.kind in SHAPING for f in ctx.targets(op)}
    for hole in g.holes():
        feats = set(hole.get("features") or ())
        if not hole.get("through") and not feats & machined:
            findings.append(Draft(f"process.{kind}.controlled_depth", "major", FAIL,
                                  length(hole["depth_mm"]), policy("cut_depth", "equals", "through the sheet",
                                                                    note="engraving is not a substitute for machining"),
                                  g.method("bore recognition"), "exact bores",
                                  f"a blind Ø{hole['diameter_mm']:g} mm bore cannot be cut; engraving does not make a "
                                  "controlled-depth precision feature",
                                  remedy="machine it in a later operation, or make it a through feature",
                                  recheck=[f"process.{kind}.features"], feature=sorted(feats)[0] if len(feats) == 1 else None))
            parts.append((FAIL, f"blind hole {hole.get('id')}"))
    holes = [h for h in g.holes() if h.get("through")]
    if holes:
        cal = ctx.calibrated("min_hole")
        smallest = min(holes, key=lambda h: h["diameter_mm"])
        if not cal.known:
            findings.append(Draft(f"process.{kind}.min_hole_uncalibrated", "minor", UNKNOWN, length(smallest["diameter_mm"]),
                                  cal.criterion("hole_diameter", ">="), "capability calibrations", "; ".join(cal.reasons),
                                  "the smallest reliable hole in this stock is unknown",
                                  remedy="calibrate the smallest cut hole for this stock and thickness",
                                  recheck=[f"process.{kind}.features"]))
            parts.append((UNKNOWN, "min hole not calibrated"))
        elif smallest["diameter_mm"] < cal.bounds("mm")[1] - 1e-9:
            findings.append(Draft(f"process.{kind}.min_hole", "major", FAIL, length(smallest["diameter_mm"]),
                                  cal.criterion("hole_diameter", ">=", cal.bounds("mm")[1]), g.method("bore recognition"),
                                  "exact bore", "the hole is below the smallest reliable cut hole",
                                  remedy="enlarge it, or drill it in a secondary operation", recheck=[f"process.{kind}.features"],
                                  feature=_one(smallest.get("features"))))
            parts.append((FAIL, "hole below the calibrated minimum"))
        else:
            parts.append((PASS, "holes above the calibrated minimum"))
    walls = g.facts.get("walls") or {}
    if walls.get("thickness_mm") is not None:
        cal = ctx.calibrated("min_web")
        if not cal.known:
            findings.append(Draft(f"process.{kind}.min_web_uncalibrated", "minor", UNKNOWN, length(walls["thickness_mm"]),
                                  cal.criterion("web_width", ">="), "capability calibrations", "; ".join(cal.reasons),
                                  "the narrowest reliable web is unknown", remedy="calibrate the minimum web",
                                  recheck=[f"process.{kind}.features"]))
            parts.append((UNKNOWN, "min web not calibrated"))
        elif walls["thickness_mm"] < cal.bounds("mm")[1] - 1e-9:
            findings.append(Draft(f"process.{kind}.min_web", "major", FAIL, length(walls["thickness_mm"]),
                                  cal.criterion("web_width", ">=", cal.bounds("mm")[1]), g.method("minimum-wall scan"),
                                  "sampled scan", "a web narrower than the calibrated minimum burns through or distorts",
                                  remedy="widen the web", recheck=[f"process.{kind}.features"]))
            parts.append((FAIL, "web below the calibrated minimum"))
        else:
            parts.append((PASS, "webs above the calibrated minimum"))
    parts += hole_fits(ctx, g, findings, "kerf_deviation", oriented=False)
    return combine(method, parts, findings)


def thermal(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} heat effects: heat-affected zone and flatness against calibrations"
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    cut_critical = [s for s in ctx.surfaces() if s.dimension_state == "as_manufactured"
                    and not any(s.feature in ctx.targets(op) for op in ctx.after())]
    haz = ctx.calibrated("heat_affected_zone")
    if cut_critical or kind == "plasma":
        if haz.known:
            parts.append((PASS, f"heat-affected zone calibrated ({haz.calibration.id})"))
        else:
            findings.append(Draft(f"process.{kind}.haz_uncalibrated", "minor", UNKNOWN, unknown("heat-affected zone"),
                                  haz.criterion("heat_affected_zone"), "capability calibrations", "; ".join(haz.reasons),
                                  "the edge zone changed by heat (hardness, melt, burn, dross) on critical as-cut edges is unknown",
                                  remedy="calibrate the heat-affected zone, or finish the critical edges",
                                  recheck=[f"process.{kind}.thermal"]))
            parts.append((UNKNOWN, "heat-affected zone unknown"))
    datums = [s for s in cut_critical if s.role == "datum"]
    if datums:
        flat = ctx.calibrated("flatness")
        if flat.known:
            parts += [_within(ctx, s, flat, "flatness", "mm", f"process.{kind}.flatness",
                              f"flatten or finish {s.feature} after cutting, or relax the limit with the design owner",
                              f"process.{kind}.thermal", findings) for s in datums]
        else:
            findings.append(Draft(f"process.{kind}.flatness_uncalibrated", "minor", UNKNOWN, unknown("flatness"),
                                  flat.criterion("flatness"), "capability calibrations", "; ".join(flat.reasons),
                                  "heat distortion and sheet flatness of the datum are unknown",
                                  remedy="calibrate flatness for this stock and size", recheck=[f"process.{kind}.thermal"],
                                  feature=datums[0].feature))
            parts.append((UNKNOWN, "flatness unknown"))
    findings.append(Draft(f"process.{kind}.edge_evidence", "info", UNKNOWN, unknown("edge quality not assessed"),
                          unknown("edge quality, dross, burning and distortion of the actual cut"), "record content",
                          "thermal edge effects are not geometric", "edge quality is unvalidated until cut",
                          remedy="inspect first-article edges", recheck=[f"process.{kind}"], earliest_stage="release", link=False))
    if not parts:
        return Result(NOT_APPLICABLE, method, "no critical as-cut edge", findings)
    return combine(method, parts, findings)


def _within(ctx: Context, surface: Any, cal: Calibrated, prop: str, units: str, code: str, remedy: str, recheck: str,
            findings: list[Draft]) -> tuple[str, str]:
    """The calibrated upper bound of `cal` against every `prop <=` criterion of `surface`: FAIL when the
    calibration can exceed one; a surface stating no such limit has nothing stricter for the calibration to meet."""
    limits = [item for item in ctx.surface_raw(surface).get("criteria") or ()
              if item.get("property") == prop and item.get("operator") == "<="]
    for item in limits:
        limit_units = item.get("units") or units
        worst = cal.bounds(limit_units)[1]
        if worst > float(item["value"]) + 1e-9:
            findings.append(Draft(code, "major", FAIL, {"value": round(worst, 6) + 0.0, "units": limit_units},
                                  {**item, "units": limit_units}, f"calibrated {cal.characteristic} against the criterion",
                                  f"calibration {cal.calibration.id}",
                                  f"the as-cut {surface.feature} can exceed its {prop.replace('_', ' ')} limit",
                                  remedy=remedy, recheck=[recheck], feature=surface.feature))
            return FAIL, f"{surface.feature} {prop}"
    if not limits:
        return PASS, f"{cal.characteristic} calibrated ({cal.calibration.id}); {surface.feature} states no {prop} limit"
    return PASS, f"{surface.feature} {prop} within calibration {cal.calibration.id}"


def nesting(ctx: Context) -> Result:
    kind = ctx.operation.kind
    method = f"{kind} nesting plan: stock size, part retention and post-processing"
    stock = (ctx.operation.setup or {}).get("stock") or {}
    missing = []
    if not _known(stock.get("dimensions")):
        missing.append("the stock sheet dimensions")
    post = " ".join(ctx.operation.raw.get("post_processing") or ()).lower()
    if not post:
        missing.append("post-processing (deburring, cleaning, micro-tab removal)")
    if missing:
        draft = Draft(f"process.{kind}.nesting_unplanned", "minor", UNKNOWN, text(f"missing {', '.join(missing)}"),
                      policy("nesting_plan", "equals", "stock size and post-processing recorded"), "part.yaml route",
                      "record content", "nesting, part retention and finishing of micro-tabs and loose cutouts are unplanned",
                      remedy=f"record {', '.join(missing)}", recheck=[f"process.{kind}.nesting"])
        return Result(UNKNOWN, method, f"missing {', '.join(missing)}", [draft])
    return Result(PASS, method, "stock size and post-processing recorded; grain, nesting layout and retention tabs are "
                  "the cutting plan's, not measured")


def edge(ctx: Context) -> Result:
    method = "waterjet edge: calibrated taper and finish on critical as-cut edges"
    findings: list[Draft] = []
    cut_critical = [s for s in ctx.surfaces() if s.dimension_state == "as_manufactured"
                    and not any(s.feature in ctx.targets(op) for op in ctx.after())]
    if not cut_critical:
        return Result(NOT_APPLICABLE, method, "no critical as-cut edge")
    parts: list[tuple[str, str]] = []
    for name in ("taper", "edge_finish"):
        cal = ctx.calibrated(name)
        if cal.known and name == "edge_finish":
            parts += [_within(ctx, s, cal, "surface_roughness", "um", "process.waterjet.edge_finish",
                              f"finish {s.feature} after cutting, or relax the limit with the design owner",
                              "process.waterjet.edge", findings) for s in cut_critical]
        elif cal.known:
            parts.append((PASS, f"{name} calibrated"))
        else:
            findings.append(Draft(f"process.waterjet.{name}_uncalibrated", "minor", UNKNOWN, unknown(name),
                                  cal.criterion(name), "capability calibrations", "; ".join(cal.reasons),
                                  f"the {name.replace('_', ' ')} of critical as-cut edges is unknown",
                                  remedy=f"calibrate {name} for this stock and thickness, or finish the edges",
                                  recheck=["process.waterjet.edge"], feature=cut_critical[0].feature))
            parts.append((UNKNOWN, f"{name} unknown"))
    return combine(method, parts, findings)


# --- sheet forming ----------------------------------------------------------------------------------------------

def bend(ctx: Context) -> Result:
    method = "sheet forming bends: inner radius and hole-to-bend distance against calibrations"
    g = ctx.measured("bends")
    if isinstance(g, Result):
        return g
    findings: list[Draft] = []
    parts: list[tuple[str, str]] = []
    bends = g.bends()
    if not bends:
        return Result(NOT_APPLICABLE, method, "no bend recognised (coaxial inner and outer cylinders one sheet apart)")
    radius = ctx.calibrated("min_bend_radius")
    if not radius.known:
        findings.append(Draft("process.sheet_forming.bend_uncalibrated", "major", UNKNOWN,
                              length(min(b["inner_radius_mm"] for b in bends)), radius.criterion("inner_bend_radius", ">="),
                              "capability calibrations", "; ".join(radius.reasons), "cracking at the bends is not assessed",
                              remedy="record the minimum bend radius for this material, temper, thickness and grain direction",
                              recheck=["process.sheet_forming.bend"]))
        parts.append((UNKNOWN, "bend radius not calibrated"))
    else:
        need = radius.bounds("mm")[1]
        tight = [b for b in bends if b["inner_radius_mm"] < need - 1e-9]
        for item in tight:
            findings.append(Draft("process.sheet_forming.bend_radius", "major", FAIL, length(item["inner_radius_mm"]),
                                  radius.criterion("inner_bend_radius", ">=", need), g.method("bend recognition"),
                                  "exact cylinders", "the bend is tighter than the calibrated minimum: it can crack",
                                  remedy="enlarge the inner radius or change material or temper",
                                  recheck=["process.sheet_forming.bend"], feature=_one(item.get("features"))))
        parts.append((FAIL if tight else PASS, "bend radius"))
    distance = ctx.calibrated("min_hole_to_bend")
    near = [(h, b, d) for b in bends for h in g.holes()
            for d in [_hole_to_bend(h, b)] if d is not None]
    if near:
        if not distance.known:
            parts.append((UNKNOWN, "hole-to-bend not calibrated"))
            findings.append(Draft("process.sheet_forming.hole_to_bend_uncalibrated", "minor", UNKNOWN,
                                  length(min(d for _, _, d in near)), distance.criterion("hole_to_bend", ">="),
                                  "capability calibrations", "; ".join(distance.reasons),
                                  "hole distortion near bends is not assessed", remedy="calibrate the minimum hole-to-bend distance",
                                  recheck=["process.sheet_forming.bend"]))
        else:
            need = distance.bounds("mm")[1]
            close = [(h, b, d) for h, b, d in near if d < need - 1e-9]
            for hole, _, d in close:
                findings.append(Draft("process.sheet_forming.hole_to_bend", "major", FAIL, length(d),
                                      distance.criterion("hole_to_bend", ">=", need), g.method("hole axis to bend axis distance"),
                                      "distance from the hole edge to the bend's inner tangent, approximated from the axes",
                                      "the hole distorts in the bend", remedy="move the hole or pierce it after bending",
                                      recheck=["process.sheet_forming.bend"], feature=_one(hole.get("features"))))
            parts.append((FAIL if close else PASS, "hole-to-bend distance"))
    return combine(method, parts, findings, uncertainty="reliefs and grain direction are not measured")


def _hole_to_bend(hole: Any, item: Any) -> float | None:
    point, axis = item.get("axis_point"), item.get("axis")
    if not point or not axis:
        return None
    offset = [hole["position"][i] - point[i] for i in range(3)]
    along = sum(offset[i] * axis[i] for i in range(3))
    radial = math.sqrt(max(0.0, sum(c * c for c in offset) - along * along))
    clearance: float = radial - hole["diameter_mm"] / 2.0 - item["inner_radius_mm"] - item.get("thickness_mm", 0.0)
    return clearance


def sequence(ctx: Context) -> Result:
    method = "sheet forming plan: bend order, press-brake tooling and access"
    op, profile = ctx.operation, ctx.profile
    missing = []
    if not op.targets:
        missing.append("the bends in order (targets)")
    if not ((op.setup or {}).get("access") or {}).get("tools"):
        missing.append("the brake tooling (setup access tools)")
    tooling = profile.parameters.get("tooling") if profile is not None else None
    if tooling is None or not tooling.known:
        missing.append("the capability's tooling")
    if missing:
        draft = Draft("process.sheet_forming.sequence_unplanned", "minor", UNKNOWN, text(f"missing {', '.join(missing)}"),
                      policy("bend_plan", "equals", "order, tooling and access recorded"), "part.yaml route", "record content",
                      "a later bend can collide with an earlier flange or the tooling", remedy=f"record {', '.join(missing)}",
                      recheck=["process.sheet_forming.sequence"])
        return Result(UNKNOWN, method, f"missing {', '.join(missing)}", [draft])
    return Result(PASS, method, "bend order and tooling recorded; flange-to-tool collisions are not simulated")


def distortion(ctx: Context) -> Result:
    method = "sheet forming springback and flat-pattern evidence"
    findings, parts = [], []
    for name in ("springback", "bend_allowance"):
        cal = ctx.calibrated(name)
        if cal.known:
            parts.append((PASS, f"{name} calibrated"))
            continue
        findings.append(Draft(f"process.sheet_forming.{name}_uncalibrated", "minor", UNKNOWN, unknown(name),
                              cal.criterion(name), "capability calibrations", "; ".join(cal.reasons),
                              f"{name.replace('_', ' ')} of this material and thickness is unknown: bent dimensions are uncertain",
                              remedy=f"calibrate {name} with test bends", recheck=["process.sheet_forming"]))
        parts.append((UNKNOWN, f"{name} unknown"))
    return combine(method, parts, findings)


def _one(feats: Any) -> str | None:
    feats = sorted(set(feats or ()))
    return feats[0] if len(feats) == 1 else None


LASER = {"material": gated(material), "kerf": gated(kerf), "features": gated(features), "thermal": gated(thermal),
         "nesting": nesting}
WATERJET = {"material": gated(material), "kerf": gated(kerf), "features": gated(features), "edge": gated(edge)}
PLASMA = {"material": gated(material), "kerf": gated(kerf), "features": gated(features), "thermal": gated(thermal)}
SHEET_FORMING = {"bend": gated(bend), "sequence": gated(sequence), "distortion": gated(distortion)}
POLICY_SOURCES = (POLICY, LASER_POLICY)
