"""Selection scenarios for test_selection.py: the plan's regression rows 1-5 and the transparency case.

Each scenario returns a record as {path below hw/: document}, built with the shared manufacturing-intent
fixture builders (tests/fixtures/manufacturing_intent.py), so a test keeps the inputs it asserts on
visible and can edit them before writing. Every material, machine, supplier, number and source is a
synthetic TST value invented for these tests: none is real material, machine or supplier data, and no
example project's assumptions are copied.

Load it by path, as the shared builder is loaded:

    spec = importlib.util.spec_from_file_location("cad_record_selection_scenarios", <tests>/fixtures/selection/scenarios.py)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent.parent


def _shared() -> Any:
    name = "cad_record_mi_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, FIXTURES / "manufacturing_intent.py")
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


fx = _shared()
PLACEHOLDER_DIGEST = "0" * 64  # test_selection.py writes the current digest over it unless a test wants a stale one


# --- use case and statements ------------------------------------------------------------------------------------

def ready_use_case(service_class: str, *, temperature: tuple[float, float] = (10, 40), loads: list[dict[str, Any]] | None = None,
                   equipment: list[str] | None = None) -> dict[str, Any]:
    """A confirmed use case (DR-001) whose every fact is known or not applicable, so assess_use_case allows a
    final choice; motion is not applicable and the default load is a static synthetic one."""
    block = fx.use_case("confirmed")
    block["decision"] = "DR-001"
    block["service_class"] = fx.known(service_class)
    special = {
        "environment.temperature": fx.known(fx.quantity(lower=temperature[0], upper=temperature[1], units="deg_C")),
        "motion.speed": fx.not_applicable("no motion in this fixture"),
        "motion.range": fx.not_applicable("no motion in this fixture"),
        "motion.service_life": fx.known(fx.quantity(2000, "h")),
        "consequences.severity": fx.known("minor"),
        "logistics.quantity": fx.known(fx.quantity(10, "count")),
        "logistics.budget": fx.known({"amount": 500, "currency": "EUR"}),
        "logistics.lead_time": fx.known(fx.quantity(21, "d")),
    }
    lists = {"consequences.forbidden_outcomes", "consequences.standards", "release_scope.operating_limits",
             "release_scope.exclusions"}
    for dotted in fx.intent_module().USE_CASE_FACTS:
        value = special.get(dotted) or fx.known([f"TST fixture {dotted}"] if dotted in lists else f"TST fixture {dotted}")
        group, _, name = dotted.partition(".")
        if name:
            block[group][name] = value
        else:
            block[group] = value
    block["loads"] = loads if loads is not None else [
        {"id": "service_load", "kind": "static", "magnitude": fx.known(fx.quantity(20, "N"))}]
    block["capability"]["equipment"] = equipment if equipment is not None else fx.not_applicable("no equipment list in this fixture")
    block["evidence_policy"] = {"obligations": [{"requirement": "REQ-001", "level": "process_planned"}], "decision": "DR-001"}
    return block


def resolved_questions() -> list[dict[str, Any]]:
    """The standard use-case questions Q-001..Q-011, every one resolved by DR-001."""
    entries = fx.open_questions()
    for entry in entries:
        entry["status"] = "resolved"
        entry["resolution"] = {"answer": "TST fixture answer", "decision": "DR-001"}
    return entries


def preference(ident: str, text: str, weight: float) -> dict[str, Any]:
    return {"id": ident, "text": text, "source": fx.source("user"), "weight": weight}


def must(ident: str, text: str, prop: str, operator: str, value: Any, units: str | None, subjects: list[dict[str, Any]],
         **extra: Any) -> dict[str, Any]:
    """A mandatory requirement with one structured acceptance criterion."""
    return fx.requirement(ident, text, acceptance=[fx.criterion(prop, operator, value, units)], subjects=subjects, **extra)


# --- profiles ---------------------------------------------------------------------------------------------------

def prop(name: str, value: float, units: str, *, orientation: str | None = None, temperature: tuple[float, float] | None = (0, 60),
         conditioning: str = "dry") -> dict[str, Any]:
    """A supplier-typical property from a synthetic datasheet. temperature None: the test temperature does not
    apply (a temperature-type property); orientation None: not stated, for non-printed forms."""
    data = fx.material_property(name, value, units, conditioning=conditioning,
                                orientation=fx.known(orientation) if orientation else None)
    data["value"] = fx.known(fx.quantity(value, units), "supplier_typical", [fx.source("datasheet", f"TST datasheet {name}")])
    data["conditions"]["temperature"] = (fx.known(fx.quantity(lower=temperature[0], upper=temperature[1], units="deg_C"), "measured",
                                                  [fx.source("test")])
                                         if temperature else fx.not_applicable("not a test-temperature property"))
    return data


def printed(name: str, value: float, units: str, **extra: Any) -> list[dict[str, Any]]:
    """A printed property in both stated orientations: XY at `value`, Z at two thirds of it (synthetic)."""
    return [prop(name, value, units, orientation="XY", **extra), prop(name, round(value * 2 / 3, 3), units, orientation="Z", **extra)]


def temperature_prop(name: str, value: float, *, printed_form: bool = False) -> dict[str, Any]:
    data = prop(name, value, "deg_C", temperature=None)
    if printed_form:
        data["conditions"]["orientation"] = fx.not_applicable("not orientation dependent in this fixture")
    return data


def material(ident: str, form: str, properties: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return fx.material_profile(ident, form=form, family=f"TST family {ident.split('_')[1]}", properties=properties, **extra)


def approval(material_id: str, **extra: Any) -> dict[str, Any]:
    data = {"material": material_id, "status": "approved", "source": fx.source("user")}
    data.update(extra)
    return data


def capability(ident: str, process: str, materials: list[str], *, provider: str = "local", **extra: Any) -> dict[str, Any]:
    approvals = [approval(m, extraction=fx.known("TST extraction"), thickness=fx.known(fx.quantity(3)))
                 if process == "laser" else approval(m) for m in materials]
    return fx.capability_profile(ident, process, materials=approvals, provider=provider, **extra)


# --- parts and candidates ---------------------------------------------------------------------------------------

def made_part(pn: str, title: str, process: str, material_id: str, capability_id: str, *, functions: list[str] | None = None,
              **extra: Any) -> dict[str, Any]:
    """A part made by one operation, as the record holds it today."""
    return fx.part(pn, title, process=process, material=f"TST {material_id}", part_intent=fx.intent(
        functions=[{"function": f} for f in functions or ["structural"]], material_profile=material_id,
        critical_surfaces=fx.not_applicable("no critical surface in this fixture"),
        route=[fx.operation("make", process, capability_id)]), **extra)


def step(kind: str, capability_id: str | None = None, provider: str | None = None) -> dict[str, Any]:
    if capability_id is None:
        capability_value: Any = (fx.not_applicable("a purchase has no capability") if kind == "purchased"
                                 else fx.unknown("no capability in this fixture"))
    else:
        capability_value = capability_id
    data = {"kind": kind, "capability": capability_value}
    if provider:
        data["provider"] = provider
    return data


def cpart(pn: str, material_id: Any, *steps: dict[str, Any]) -> dict[str, Any]:
    return {"PN": pn, "material_profile": material_id, "route": list(steps)}


def candidate(ident: str, title: str, architecture: str, parts: list[dict[str, Any]], *, cost: float, scores: dict[str, float] | None = None,
              evaluations: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """A candidate with an estimated cost at 10 pieces and a 5 day estimated lead time (synthetic)."""
    data = {"id": ident, "title": title, "architecture": architecture, "parts": parts, "evaluations": evaluations or [],
            "quantity": fx.known(fx.quantity(10, "count")), "cost": fx.known({"amount": cost, "currency": "EUR"}, "estimate"),
            "lead_time": fx.known(fx.quantity(5, "d"), "estimate"), "maintenance": fx.known(f"TST maintenance of {ident}"),
            "evidence_confidence": f"TST fixture: {ident} rests on synthetic datasheet values",
            "pros": [f"TST pro of {ident}"], "cons": [f"TST con of {ident}"], "tradeoffs": [f"TST tradeoff of {ident}"]}
    if scores:
        data["preference_scores"] = [{"preference": key, "score": value} for key, value in scores.items()]
    data.update(extra)
    return data


def evaluation(requirement: str, outcome: str, prop_name: str, operator: str, value: Any, units: str | None, *,
               measured: dict[str, Any] | None = None, method: str = "TST coupon test", source_kind: str = "test") -> dict[str, Any]:
    data = {"requirement": requirement, "outcome": outcome, "method": method,
            "criterion": fx.criterion(prop_name, operator, value, units), "source": fx.source(source_kind, f"TST {method}")}
    if measured is not None:
        data["value"] = measured
    return data


def selection(candidates: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    data = {"status": "assessed", "assessment": {"input_sha256": PLACEHOLDER_DIGEST, "method": "selection.py", "date": "2026-01-01"},
            "candidates": candidates}
    data.update(extra)
    return data


def build(*, use: dict[str, Any], requirements: list[dict[str, Any]], parts: list[dict[str, Any]], materials: list[dict[str, Any]],
          capabilities: list[dict[str, Any]], candidates: list[dict[str, Any]], questions: list[dict[str, Any]] | None = None,
          preferences: list[dict[str, Any]] | None = None, interfaces: list[dict[str, Any]] | None = None,
          selection_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return fx.record(product_data=fx.product(use), parts=parts, interfaces=interfaces,
                     requirements_data=fx.requirements(requirements, questions=questions if questions is not None else resolved_questions(),
                                                       preferences=preferences),
                     capabilities=capabilities, materials=materials, selection=selection(candidates, **(selection_extra or {})))


# --- scenarios --------------------------------------------------------------------------------------------------

def unassessed_case() -> dict[str, Any]:
    """Row 1: a convenient printed clamp, recorded before the use case is assessed: every use-case fact is
    unknown and Q-001..Q-011 are open, so only a conditional report is possible."""
    return build(
        use=fx.use_case(), questions=fx.open_questions(),
        requirements=[must("REQ-001", "Hold the clamp load", "yield_strength", ">=", 20, "MPa", [{"PN": "TST-0101"}])],
        parts=[made_part("TST-0101", "Clamp", "fdm", "tst_polymer_s_printed", "tst_printer")],
        materials=[material("tst_polymer_s_printed", "printed", printed("yield_strength", 40, "MPa"))],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_s_printed"])],
        candidates=[candidate("convenient_print", "Printed clamp", "one printed part",
                              [cpart("TST-0101", "tst_polymer_s_printed", step("fdm", "tst_printer", "local"))], cost=3)])


def hot_loaded_bracket() -> dict[str, Any]:
    """Row 2: a bracket that prints within the envelope on a calibrated printer from an approved material,
    but whose sourced maximum service temperature fails the governing heat requirement."""
    return build(
        use=ready_use_case("working_prototype", temperature=(20, 85), equipment=["tst_printer"]),
        requirements=[must("REQ-001", "Carry the service load", "yield_strength", ">=", 20, "MPa", [{"PN": "TST-0201"}]),
                      must("REQ-002", "Withstand the heater beside it", "max_service_temperature", ">=", 95, "deg_C",
                           [{"PN": "TST-0201"}])],
        parts=[made_part("TST-0201", "Heater bracket", "fdm", "tst_polymer_a_printed", "tst_printer")],
        materials=[material("tst_polymer_a_printed", "printed", printed("yield_strength", 45, "MPa", temperature=(0, 100))
                            + [temperature_prop("max_service_temperature", 65, printed_form=True)]),
                   material("tst_metal_b_bar", "wrought", [prop("yield_strength", 200, "MPa", temperature=(0, 150)),
                                                           temperature_prop("max_service_temperature", 150)])],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_a_printed"],
                                 calibrations=[fx.calibration("hole_coupon", "hole_diameter_deviation", "tst_polymer_a_printed",
                                                              fx.quantity(lower=-0.2, upper=0.0), orientation="XY")]),
                      capability("tst_mill", "cnc", ["tst_metal_b_bar"], provider="outsourced")],
        preferences=[preference("PRF-001", "Lower cost", 1)],
        candidates=[
            candidate("printed_bracket", "Printed bracket", "one printed part",
                      [dict(cpart("TST-0201", "tst_polymer_a_printed", step("fdm", "tst_printer", "local")),
                            note="fits the printer's build envelope in the approved orientation")], cost=4, scores={"PRF-001": 9}),
            candidate("machined_bracket", "Machined bracket", "one machined part",
                      [cpart("TST-0201", "tst_metal_b_bar", step("cnc", "tst_mill", "outsourced"))], cost=35, scores={"PRF-001": 3})])


def qualified_polymer_cover() -> dict[str, Any]:
    """Row 3: a lightly loaded cover whose printed polymer grade meets every sourced criterion at the operating
    temperature on a calibrated, approved printer: the polymer is accepted, not displaced by metal."""
    return build(
        use=ready_use_case("functional_demo", temperature=(15, 35)),
        requirements=[must("REQ-001", "Carry the cover load", "yield_strength", ">=", 15, "MPa", [{"PN": "TST-0301"}]),
                      must("REQ-002", "Stay rigid in service", "max_service_temperature", ">=", 50, "deg_C", [{"PN": "TST-0301"}])],
        parts=[made_part("TST-0301", "Sensor cover", "fdm", "tst_polymer_c_printed", "tst_printer")],
        materials=[material("tst_polymer_c_printed", "printed", printed("yield_strength", 40, "MPa")
                            + [temperature_prop("max_service_temperature", 60, printed_form=True)]),
                   material("tst_metal_d_bar", "wrought", [prop("yield_strength", 150, "MPa", temperature=(0, 150)),
                                                           temperature_prop("max_service_temperature", 200)])],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_c_printed"],
                                 calibrations=[fx.calibration("hole_coupon", "hole_diameter_deviation", "tst_polymer_c_printed",
                                                              fx.quantity(lower=-0.15, upper=0.05), orientation="XY")]),
                      capability("tst_mill", "cnc", ["tst_metal_d_bar"], provider="outsourced")],
        preferences=[preference("PRF-001", "Lower cost", 2), preference("PRF-002", "Shorter lead time", 1)],
        candidates=[
            candidate("printed_cover", "Printed cover", "one printed part",
                      [cpart("TST-0301", "tst_polymer_c_printed", step("fdm", "tst_printer", "local"))], cost=3,
                      scores={"PRF-001": 9, "PRF-002": 9}),
            candidate("machined_cover", "Machined cover", "one machined part",
                      [cpart("TST-0301", "tst_metal_d_bar", step("cnc", "tst_mill", "outsourced"))], cost=40,
                      scores={"PRF-001": 3, "PRF-002": 4})])


def wear_shaft_options() -> dict[str, Any]:
    """Row 4: a pivot shaft running in a printed hub under a cyclic radial load. A printed shaft fails the
    sourced strength criterion and the wear coupon; machined, purchased and hybrid (purchased pin pressed
    into the hub) alternatives meet both."""
    load = {"id": "radial_load", "kind": "cyclic", "magnitude": fx.known(fx.quantity(50, "N")), "requirements": ["REQ-001"]}
    wear = ("wear_depth", "<=", 0.1, "mm")
    return build(
        use=ready_use_case("working_prototype", loads=[load], equipment=["tst_printer"]),
        requirements=[must("REQ-001", "The running shaft carries the cyclic radial load", "yield_strength", ">=", 120, "MPa",
                           [{"PN": "TST-0401"}, {"PN": "TST-0403"}]),
                      must("REQ-002", "The bearing surface keeps its running clearance over the service life", *wear,
                           [{"interface": "IF-001"}])],
        parts=[made_part("TST-0401", "Pivot shaft", "fdm", "tst_polymer_m_printed", "tst_printer", functions=["rotating"]),
               made_part("TST-0402", "Hub", "fdm", "tst_polymer_m_printed", "tst_printer", functions=["structural"]),
               fx.part("TST-0403", "Pin", process="purchased", make_buy="buy", material="TST steel", procurement={"mode": "catalog"},
                       part_intent=fx.intent(functions=[{"function": "rotating"}], material_profile="tst_steel_n_ground",
                                             critical_surfaces=fx.not_applicable("catalogue item"),
                                             route=[fx.operation("buy", "purchased")]))],
        interfaces=[fx.interface("IF-001", "TST-0401", "TST-0402", "motion",
                                 {"critical": fx.known(True), "functions": [{"function": "rotating"}]})],
        materials=[material("tst_polymer_m_printed", "printed", printed("yield_strength", 45, "MPa")),
                   material("tst_metal_o_bar", "wrought", [prop("yield_strength", 300, "MPa", temperature=(0, 150))]),
                   material("tst_steel_n_ground", "purchased", [prop("yield_strength", 600, "MPa", temperature=(0, 150))])],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_m_printed"]),
                      capability("tst_mill", "cnc", ["tst_metal_o_bar"], provider="outsourced")],
        preferences=[preference("PRF-001", "Lower cost", 2), preference("PRF-002", "Convenient to make and assemble", 1)],
        candidates=[
            candidate("printed_shaft", "Printed shaft in a printed hub", "two printed parts",
                      [cpart("TST-0401", "tst_polymer_m_printed", step("fdm", "tst_printer", "local")),
                       cpart("TST-0402", "tst_polymer_m_printed", step("fdm", "tst_printer", "local"))], cost=2,
                      scores={"PRF-001": 10, "PRF-002": 10}, interfaces=["IF-001"],
                      evaluations=[evaluation("REQ-002", "fail", *wear, measured=fx.quantity(0.4))]),
            candidate("machined_shaft", "Machined metal shaft in a printed hub", "machined shaft, printed hub",
                      [cpart("TST-0401", "tst_metal_o_bar", step("cnc", "tst_mill", "outsourced")),
                       cpart("TST-0402", "tst_polymer_m_printed", step("fdm", "tst_printer", "local"))], cost=30,
                      scores={"PRF-001": 2, "PRF-002": 3}, interfaces=["IF-001"],
                      evaluations=[evaluation("REQ-002", "pass", *wear, measured=fx.quantity(0.05))]),
            candidate("purchased_shaft", "Purchased ground shaft in a printed hub", "purchased shaft, printed hub",
                      [cpart("TST-0401", "tst_steel_n_ground", step("purchased")),
                       cpart("TST-0402", "tst_polymer_m_printed", step("fdm", "tst_printer", "local"))], cost=8,
                      scores={"PRF-001": 7, "PRF-002": 8}, interfaces=["IF-001"],
                      evaluations=[evaluation("REQ-002", "pass", *wear, measured=fx.quantity(0.04))]),
            candidate("hybrid_pin", "Printed hub with a pressed purchased pin", "hybrid: printed hub, purchased pin",
                      [cpart("TST-0402", "tst_polymer_m_printed", step("fdm", "tst_printer", "local")),
                       cpart("TST-0403", "tst_steel_n_ground", step("purchased"))], cost=6,
                      scores={"PRF-001": 8, "PRF-002": 5},
                      changes={"design": ["the hub carries a pressed purchased pin instead of a separate shaft"],
                               "interfaces": ["a new press fit between TST-0402 and TST-0403"]},
                      evaluations=[evaluation("REQ-002", "pass", *wear, measured=fx.quantity(0.06))])])


def plate_frame_options() -> dict[str, Any]:
    """Row 4, sheet alternative: a stiff frame. The printed frame fails the stiffness criterion; a laser-cut and
    formed sheet frame on approved local equipment and a machined frame both meet it."""
    return build(
        use=ready_use_case("working_prototype", equipment=["tst_printer", "tst_laser", "tst_brake"]),
        requirements=[must("REQ-001", "The frame is stiff enough", "youngs_modulus", ">=", 50, "GPa", [{"PN": "TST-0501"}])],
        parts=[made_part("TST-0501", "Frame", "fdm", "tst_polymer_p_printed", "tst_printer")],
        materials=[material("tst_polymer_p_printed", "printed", printed("youngs_modulus", 2, "GPa")),
                   material("tst_metal_q_sheet", "sheet", [prop("youngs_modulus", 190, "GPa", temperature=(0, 150))]),
                   material("tst_metal_r_bar", "wrought", [prop("youngs_modulus", 70, "GPa", temperature=(0, 150))])],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_p_printed"]),
                      capability("tst_laser", "laser", ["tst_metal_q_sheet"]),
                      capability("tst_brake", "sheet_forming", ["tst_metal_q_sheet"]),
                      capability("tst_mill", "cnc", ["tst_metal_r_bar"], provider="outsourced")],
        preferences=[preference("PRF-001", "Lower cost", 1)],
        candidates=[
            candidate("printed_frame", "Printed frame", "one printed part",
                      [cpart("TST-0501", "tst_polymer_p_printed", step("fdm", "tst_printer", "local"))], cost=3, scores={"PRF-001": 10}),
            candidate("sheet_frame", "Laser-cut and formed sheet frame", "one sheet part",
                      [cpart("TST-0501", "tst_metal_q_sheet", step("laser", "tst_laser", "local"),
                             step("sheet_forming", "tst_brake", "local"))], cost=12, scores={"PRF-001": 7}),
            candidate("machined_frame", "Machined frame", "one machined part",
                      [cpart("TST-0501", "tst_metal_r_bar", step("cnc", "tst_mill", "outsourced"))], cost=60, scores={"PRF-001": 2})])


def mandatory_vs_score() -> dict[str, Any]:
    """Row 5: a production lever under a critical-risk strength requirement. The cheapest printed option scores
    100 but fails (even its typical value fails the criterion that needs an allowable), a vendor's authored pass
    notwithstanding; the compliant machined option scores 10; an unproven option scores 1000 but rests on a
    typical value and an authored pass against a weaker criterion. selection.yaml wrongly confirms the cheapest."""
    risk = {"severity": "critical", "consequence": "TST fixture: the lever releases its load"}
    criterion = ("yield_strength", ">=", 100, "MPa")
    documents = build(
        use=ready_use_case("production"),
        requirements=[must("REQ-001", "The lever holds its rated load", *criterion, [{"PN": "TST-0701"}], risk=risk)],
        parts=[made_part("TST-0701", "Lever", "fdm", "tst_polymer_g_printed", "tst_printer")],
        materials=[material("tst_polymer_g_printed", "printed", printed("yield_strength", 40, "MPa")),
                   material("tst_metal_h_bar", "wrought", [fx.material_property(
                       "yield_strength", 150, "MPa", basis="allowable",
                       temperature=fx.quantity(lower=0, upper=150, units="deg_C"))], typical_not_allowable=False),
                   material("tst_metal_i_bar", "wrought", [prop("yield_strength", 300, "MPa", temperature=(0, 150))])],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_g_printed"]),
                      capability("tst_mill", "cnc", ["tst_metal_h_bar", "tst_metal_i_bar"], provider="outsourced")],
        preferences=[preference("PRF-001", "Lower cost", 1)],
        candidates=[
            candidate("cheap_unsafe", "Printed lever", "one printed part",
                      [cpart("TST-0701", "tst_polymer_g_printed", step("fdm", "tst_printer", "local"))], cost=1, scores={"PRF-001": 100},
                      evaluations=[evaluation("REQ-001", "pass", *criterion, method="TST vendor claim", source_kind="supplier")]),
            candidate("compliant", "Machined lever from a grade with an allowable", "one machined part",
                      [cpart("TST-0701", "tst_metal_h_bar", step("cnc", "tst_mill", "outsourced"))], cost=50, scores={"PRF-001": 10}),
            candidate("unproven", "Machined lever from a typical-only grade", "one machined part",
                      [cpart("TST-0701", "tst_metal_i_bar", step("cnc", "tst_mill", "outsourced"))], cost=20, scores={"PRF-001": 1000},
                      evaluations=[evaluation("REQ-001", "pass", "yield_strength", ">=", 30, "MPa", method="TST screening")])],
        selection_extra={"status": "confirmed"})
    selection_doc = documents["manufacturing/selection.yaml"]
    selection_doc["assessment"]["decision"] = "DR-001"
    selection_doc["recommendation"] = {"status": "final", "primary": "cheap_unsafe", "reasons": ["TST: cheapest"],
                                       "limits": ["TST: none recorded"], "what_would_change": ["TST: nothing recorded"]}
    return documents


def transparent_cover() -> dict[str, Any]:
    """Transparency: a window that must pass light, recorded today as a 'clear' FDM print. FDM and resin are
    opaque processes and are eliminated whatever their transmission figure; identified transparent sheet on an
    approved laser is feasible; a purchased window stays conditional until its purchase criteria are recorded."""
    sheet = material("tst_clear_k_sheet", "sheet", [prop("light_transmission", 92, "percent", temperature=None)],
                     manufacturer=fx.known("TST sheet maker"))
    return build(
        use=ready_use_case("functional_demo", equipment=["tst_printer", "tst_resin_printer", "tst_laser"]),
        requirements=[must("REQ-001", "The window passes enough light", "light_transmission", ">=", 85, "percent",
                           [{"PN": "TST-0601"}])],
        parts=[made_part("TST-0601", "Window", "fdm", "tst_polymer_j_printed", "tst_printer", functions=["optical", "cosmetic"],
                         transparency="transparent")],
        materials=[material("tst_polymer_j_printed", "printed", [prop("light_transmission", 80, "percent", orientation="XY", temperature=None)]),
                   material("tst_resin_l_printed", "printed", [prop("light_transmission", 88, "percent", orientation="XY", temperature=None)]),
                   sheet],
        capabilities=[capability("tst_printer", "fdm", ["tst_polymer_j_printed"]),
                      capability("tst_resin_printer", "resin", ["tst_resin_l_printed"]),
                      capability("tst_laser", "laser", ["tst_clear_k_sheet"])],
        preferences=[preference("PRF-001", "Lower cost", 1)],
        candidates=[
            candidate("fdm_window", "Printed 'clear' window", "one printed part",
                      [cpart("TST-0601", "tst_polymer_j_printed", step("fdm", "tst_printer", "local"))], cost=1, scores={"PRF-001": 10}),
            candidate("resin_window", "Resin-printed window", "one printed part",
                      [cpart("TST-0601", "tst_resin_l_printed", step("resin", "tst_resin_printer", "local"))], cost=3,
                      scores={"PRF-001": 9}),
            candidate("laser_window", "Window laser-cut from identified transparent sheet", "one sheet part",
                      [cpart("TST-0601", "tst_clear_k_sheet", step("laser", "tst_laser", "local"))], cost=10, scores={"PRF-001": 6}),
            candidate("purchased_window", "Purchased transparent window", "one purchased part",
                      [cpart("TST-0601", "tst_clear_k_sheet", step("purchased"))], cost=15, scores={"PRF-001": 4})])
