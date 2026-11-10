#!/usr/bin/env python3
"""Record version 2 schema contracts: shared facts, sources, quantities and subjects, the use case,
classified statements, part intent, interface functions, assembly operations, capability, material,
selection, finding, evidence and check-result documents, release manifest v3, and the shipped template
and capability templates. Every case names the rule it proves."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SKILL = Path(__file__).resolve().parents[1]
SCHEMAS = SKILL / "schemas"
TEMPLATE = SKILL / "assets" / "project-template" / "hw"
ASSETS = SKILL / "assets" / "manufacturing"


def load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


fx = load("cad_record_mi_fixtures", Path(__file__).resolve().parent / "fixtures" / "manufacturing_intent.py")
intent = fx.intent_module()
REGISTRY = Registry().with_resources(
    (s["$id"], Resource.from_contents(s)) for s in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMAS.glob("*.schema.json"))))


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=REGISTRY)


def errors(name: str, document: Any) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(name).iter_errors(document)]


def shared(definition: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"https://srobroek.com/cad/schemas/manufacturing.schema.json#/$defs/{definition}"},
                                registry=REGISTRY)


class SchemaCase(unittest.TestCase):
    def assert_valid(self, name: str, document: Any) -> None:
        self.assertEqual(errors(name, document), [])

    def assert_cases(self, name: str, base: Any, cases: dict[str, tuple[Callable[[Any], None], bool]]) -> None:
        """Each case edits a copy of `base`; True expects it valid, False rejected."""
        self.assert_valid(name, base)
        for label, (edit, valid) in cases.items():
            with self.subTest(label):
                document = copy.deepcopy(base)
                edit(document)
                self.assertEqual(not errors(name, document), valid, errors(name, document))

    def assert_shared(self, definition: str, accepted: dict[str, Any], rejected: dict[str, Any]) -> None:
        check = shared(definition)
        for label, value in accepted.items():
            with self.subTest(accepted=label):
                self.assertEqual([e.message for e in check.iter_errors(value)], [])
        for label, value in rejected.items():
            with self.subTest(rejected=label):
                self.assertTrue(list(check.iter_errors(value)), f"{label} should be rejected")


class SharedDefinitions(SchemaCase):
    def test_every_schema_is_draft_2020_12_and_resolves_offline(self) -> None:
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            with self.subTest(path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                list(Draft202012Validator(schema, registry=REGISTRY).iter_errors({}))

    def test_a_fact_is_known_with_sources_unknown_with_a_reason_or_not_applicable(self) -> None:
        self.assert_shared("fact", {
            "known": fx.known("TST"),
            "unknown with its question": fx.unknown("not measured", "Q-001"),
            "not applicable": fx.not_applicable(),
            "estimate with uncertainty": fx.known(fx.quantity(1), "estimate"),
            "assumed with its assumption": fx.known("TST", "assumed", assumption="ASM-001"),
            "allowable with conditions and derivation": fx.known(fx.quantity(1, "MPa"), "allowable", conditions="23 deg_C dry",
                                                                 derivation="TST derivation"),
        }, {
            "known without sources": {"status": "known", "value": 1, "sources": [], "basis": "measured"},
            "known without basis": {"status": "known", "value": 1, "sources": [fx.source()]},
            "unknown without a reason": {"status": "unknown"},
            "unknown with a value": {"status": "unknown", "reason": "x", "value": 0},
            "missing as zero": 0,
            "estimate without uncertainty": {"status": "known", "value": 1, "sources": [fx.source()], "basis": "estimate"},
            "assumed without its assumption": {"status": "known", "value": 1, "sources": [fx.source()], "basis": "assumed"},
            "allowable without derivation": {"status": "known", "value": 1, "sources": [fx.source()], "basis": "allowable",
                                             "conditions": "x"},
            "typical relabelled pass": {"status": "pass", "reason": "x"},
            "bad question id": fx.unknown("x", "Q-1"),
        })

    def test_typed_facts_constrain_their_known_value(self) -> None:
        self.assert_shared("quantity_fact", {"value": fx.known(fx.quantity(2, "N")), "interval": fx.known(fx.quantity(lower=1, upper=2))},
                           {"text": fx.known("two newtons"), "boolean": fx.known(True)})
        self.assert_shared("text_fact", {"text": fx.known("TST")}, {"empty": fx.known(""), "number": fx.known(3)})
        self.assert_shared("boolean_fact", {"true": fx.known(True)}, {"string": fx.known("yes")})
        self.assert_shared("money_fact", {"money": fx.known({"amount": 10, "currency": "EUR"})},
                           {"no currency": fx.known({"amount": 10}), "negative": fx.known({"amount": -1, "currency": "EUR"})})

    def test_a_quantity_is_a_value_or_an_interval_in_a_listed_unit(self) -> None:
        self.assert_shared("quantity", {"value": fx.quantity(1.5, "mm"), "interval": fx.quantity(lower=-40, upper=60, units="deg_C"),
                                        "dimensionless": fx.quantity(0.3, "dimensionless")},
                           {"no units": {"value": 1}, "unlisted unit": {"value": 1, "units": "inch"},
                            "both": {"value": 1, "lower": 0, "upper": 2, "units": "mm"}, "half interval": {"lower": 0, "units": "mm"},
                            "boolean value": {"value": True, "units": "mm"}, "string value": {"value": "1", "units": "mm"}})

    def test_a_source_states_where_it_comes_from(self) -> None:
        self.assert_shared("source", {
            "datasheet url": fx.source("datasheet", url="https://example.invalid/d.pdf", accessed="2026-02-03", conditions="ISO 527, 23 deg_C"),
            "frozen artifact": fx.source("test", artifact="manufacturing/sources/coupon.csv", sha256="a" * 64),
            "part source": fx.source("supplier", artifact="parts/TST-0001/sources/quote.pdf", sha256="b" * 64),
            "user": fx.source("user"), "estimate": fx.source("estimate"),
            "decision": {"kind": "decision", "reference": "DR-001", "decision": "DR-001"},
            "policy in a document": fx.source("policy"),
            "policy by decision": {"kind": "policy", "reference": "x", "origin": "lead", "decision": "DR-001"},
        }, {
            "datasheet without url or artifact": {"kind": "datasheet", "reference": "x"},
            "url without retrieval date": {"kind": "datasheet", "reference": "x", "url": "https://example.invalid"},
            "plain http": {"kind": "datasheet", "reference": "x", "url": "http://example.invalid", "accessed": "2026-01-01"},
            "artifact without sha256": {"kind": "test", "reference": "x", "artifact": "manufacturing/sources/a.csv"},
            "artifact outside sources": {"kind": "test", "reference": "x", "artifact": "docs/a.csv", "sha256": "a" * 64},
            "artifact escaping": {"kind": "test", "reference": "x", "artifact": "manufacturing/sources/../a", "sha256": "a" * 64},
            "user without origin": {"kind": "user", "reference": "x"},
            "estimate without reason": {"kind": "estimate", "reference": "x"},
            "policy without origin": {"kind": "policy", "reference": "x", "document": "TST policy"},
            "policy without decision or document": {"kind": "policy", "reference": "x", "origin": "lead"},
            "bad date": fx.source("datasheet", url="https://example.invalid", accessed="2026-13-01"),
            "unknown kind": {"kind": "rumour", "reference": "x"},
        })

    def test_subjects_criteria_findings_and_obligations(self) -> None:
        self.assert_shared("subject", {"product": {"product": True}, "part feature": {"PN": "TST-0001", "component": "left", "feature": "bore"},
                                       "interface": {"interface": "IF-001"}, "operation": {"assembly_operation": "press-pin"}},
                           {"empty": {}, "component without PN": {"component": "left"}, "face index": {"PN": "TST-0001", "face": 3},
                            "region without feature": {"PN": "TST-0001", "region": "top"}})
        self.assert_shared("criterion", {
            "at least": fx.criterion("service_temperature", ">=", 60, "deg_C"),
            "between": fx.criterion("clearance", "between", [0.01, 0.05], "mm"),
            "categorical": fx.criterion("material_identity", "equals", "TST grade"),
            "one of": fx.criterion("finish", "one_of", ["anodized", "passivated"]),
        }, {
            "no source": {"property": "x", "operator": ">=", "value": 1, "units": "mm"},
            "number without units": fx.criterion("x", ">=", 1),
            "between one number": fx.criterion("x", "between", [1], "mm"),
            "unknown operator": fx.criterion("x", "~", 1, "mm"),
        })
        finding = {"id": "overhang_1", "code": "process.fdm.overhang", "subject": {"PN": "TST-0001", "operation": "print"},
                   "severity": "major", "outcome": "fail", "measured": fx.quantity(62, "deg"),
                   "criterion": fx.criterion("overhang_angle", "<=", 45, "deg"), "method": {"name": "TST method"},
                   "uncertainty": "mesh 0.1 mm", "consequence": "sagging surface", "remedy": "reorient",
                   "alternatives": [{"text": "add supports", "tradeoffs": "removal marks"}],
                   "recheck": {"families": ["process.fdm.support"], "earliest_stage": "detail"}}
        no_remedy = {k: v for k, v in finding.items() if k != "remedy"}
        self.assert_shared("finding", {"fail with remedy": finding,
                                       "unknown measurement": {**finding, "measured": fx.unknown("no calibrated profile")},
                                       "unknown criterion": {**finding, "criterion": fx.unknown("no calibration")},
                                       "pass needs no remedy": {**no_remedy, "outcome": "pass"}},
                           {"fail without remedy": no_remedy, "no units": {**finding, "measured": {"value": 62}},
                            "no recheck": {k: v for k, v in finding.items() if k != "recheck"},
                            "bad severity": {**finding, "severity": "blocker"}, "bad code": {**finding, "code": "Overhang"}})
        self.assert_shared("obligation_id", {"part": "process.fdm.support@TST-0001/print", "component": "process.cnc.access@TST-0001.left/mill",
                                             "feature": "feature.fit@TST-0001#bore", "interface": "interface.stack@IF-001",
                                             "requirement": "requirement.physically_validated@REQ-004", "product": "intent.use_case@product",
                                             "assembly": "assembly.access@op:press-pin"},
                           {"no anchor": "process.fdm.support", "bad anchor": "process.fdm.support@part", "upper family": "Process@product"})
        self.assert_shared("evidence_level", {level: level for level in intent.EVIDENCE_LEVELS}, {"badge": "certified"})

    def test_units_enum_and_module_units_are_one_list(self) -> None:
        schema = json.loads((SCHEMAS / "manufacturing.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["$defs"]["units"]["enum"]), set(intent.UNITS))
        self.assertEqual(tuple(schema["$defs"]["primary_process"]["enum"]), intent.PRIMARY_PROCESSES)
        self.assertEqual(tuple(schema["$defs"]["evidence_level"]["enum"]), intent.EVIDENCE_LEVELS)
        self.assertEqual(tuple(schema["$defs"]["service_class"]["enum"]), intent.SERVICE_CLASSES)
        self.assertEqual(tuple(schema["$defs"]["function"]["enum"]), intent.FUNCTIONS)


class RecordDocuments(SchemaCase):
    def test_product_version_and_use_case_belong_together(self) -> None:
        def drop(key: str) -> Callable[[Any], None]:
            return lambda d: d.pop(key)

        def frozen(evidence: bool) -> Callable[[Any], None]:
            def edit(d: Any) -> None:
                d["flow"]["frozen"] = {"version": "v1", "manifest_sha256": "a" * 64, "decision": "DR-001"}
                if evidence:
                    d["flow"]["frozen"]["evidence_sha256"] = "b" * 64
            return edit

        self.assert_cases("product.schema.json", fx.product(), {
            "version 2 needs a use case": (drop("use_case"), False),
            "legacy (no version) has no use case": (drop("record_version"), False),
            "version 1 legacy without either": (lambda d: (d.pop("record_version"), d.pop("use_case")), True),
            "explicit version 1 is not a version": (lambda d: d.update(record_version=1), False),
            "version 3 is unsupported": (lambda d: d.update(record_version=3), False),
            "a version 2 freeze anchors evidence": (frozen(True), True),
            "a freeze from before migration stays readable": (frozen(False), True),
            "use case missing a question area": (lambda d: d["use_case"].pop("loads"), False),
            "confirmed needs a decision": (lambda d: d["use_case"].update(status="confirmed"), False),
            "confirmed with its decision": (lambda d: d["use_case"].update(status="confirmed", decision="DR-001"), True),
            "known service class": (lambda d: d["use_case"].update(service_class=fx.known("functional_demo")), True),
            "invented service class": (lambda d: d["use_case"].update(service_class=fx.known("toy")), False),
            "loads listed": (lambda d: d["use_case"].update(loads=[{"id": "load", "kind": "cyclic", "magnitude": fx.known(fx.quantity(5, "N"))}]), True),
            "a load of an unknown kind": (lambda d: d["use_case"].update(loads=[{"id": "load", "kind": "big", "magnitude": fx.unknown()}]), False),
            "empty loads list": (lambda d: d["use_case"].update(loads=[]), False),
            "evidence policy with its decision": (lambda d: d["use_case"].update(evidence_policy={
                "obligations": [{"requirement": "REQ-001", "level": "physically_validated"}], "decision": "DR-001"}), True),
            "legacy process plan still readable": (lambda d: d.update(manufacturing={"processes": ["fdm", "laser"], "min_wall_mm": {"fdm": 1.2}}), True),
            "unknown primary process": (lambda d: d.update(manufacturing={"processes": ["sls"]}), False),
        })

    def test_requirements_keep_preferences_assumptions_and_questions_apart(self) -> None:
        base = fx.requirements([fx.requirement(acceptance=[fx.criterion("load", ">=", 10, "N")], evidence_required=["physically_validated"],
                                               waiver_policy="forbidden", risk={"severity": "major", "consequence": "drop"},
                                               subjects=[{"PN": "TST-0001"}])],
                               preferences=[{"id": "PRF-001", "text": "cheap", "source": fx.source("user"), "weight": 2}],
                               assumptions=[{"id": "ASM-001", "text": "indoor", "origin": "brief", "reason": "stated", "owner": "lead",
                                             "validation": "ask user", "status": "open", "bounds": fx.quantity(lower=0, upper=40, units="deg_C")}])
        self.assert_cases("requirements.schema.json", base, {
            "legacy requirement only": (lambda d: [d.pop(k) for k in ("preferences", "assumptions", "open_questions")], True),
            "preference weight zero": (lambda d: d["preferences"][0].update(weight=0), False),
            "validated assumption needs its decision": (lambda d: d["assumptions"][0].update(status="validated"), False),
            "resolved question needs its resolution": (lambda d: d["open_questions"][0].update(status="resolved"), False),
            "resolved with an answer and decision": (lambda d: d["open_questions"][0].update(
                status="resolved", resolution={"answer": "fit prototype", "decision": "DR-001"}), True),
            "open question with a resolution": (lambda d: d["open_questions"][0].update(resolution={"answer": "x", "decision": "DR-001"}), False),
            "resolution without source or decision": (lambda d: d["open_questions"][0].update(status="resolved", resolution={"answer": "x"}), False),
            "unknown evidence level": (lambda d: d["requirements"][0].update(evidence_required=["certified"]), False),
            "waiver policy invented": (lambda d: d["requirements"][0].update(waiver_policy="always"), False),
        })

    def test_part_intent_records_the_complete_route(self) -> None:
        base = fx.tst_record()["parts/TST-0001/part.yaml"]
        route = lambda d: d["intent"]["route"]  # noqa: E731
        self.assert_cases("part.schema.json", base, {
            "legacy part without intent": (lambda d: d.pop("intent"), True),
            "intent missing its route": (lambda d: d["intent"].pop("route"), False),
            "unknown route": (lambda d: d["intent"].update(route=fx.unknown("not planned", "Q-012")), True),
            "route not applicable is not allowed": (lambda d: d["intent"].update(route=fx.not_applicable()), False),
            "empty route": (lambda d: d["intent"].update(route=[]), False),
            "unknown route kind parses": (lambda d: route(d).append(fx.operation("sinter", "sintering")), True),
            "made operation without capability": (lambda d: route(d)[0].pop("capability"), False),
            "purchase with a capability": (lambda d: route(d).append({**fx.operation("buy", "purchased"), "capability": "x"}), False),
            "purchase without criteria": (lambda d: route(d).append({"id": "buy", "kind": "purchased"}), False),
            "approved orientation without decision": (lambda d: route(d)[0]["orientation"].pop("decision"), False),
            "axis-angle orientation": (lambda d: route(d)[0].update(orientation={"status": "proposed", "rotation": {"axis": [0, 0, 1], "angle_deg": 90}}), True),
            "supports unknown": (lambda d: route(d)[0].update(supports=fx.unknown("not planned")), True),
            "supports on noncritical need removal": (lambda d: route(d)[0]["supports"].pop("removal"), False),
            "finished critical supports need allowance and inspection": (lambda d: route(d)[0]["supports"].update(strategy="finished_critical"), False),
            "allowance without source": (lambda d: route(d)[1]["allowances"][0].pop("source"), False),
            "inspection without state": (lambda d: route(d)[1]["inspection"][0].pop("state"), False),
            "critical surface role invented": (lambda d: d["intent"]["critical_surfaces"][0].update(role="pretty"), False),
            "two components by witness and locator": (lambda d: d["intent"].update(components=[
                {"id": "left", "witness": [0, 0, 1]}, {"id": "right", "locator": {"feature": "right_lug"}}]), True),
            "component with neither": (lambda d: d["intent"].update(components=[{"id": "left"}]), False),
            "function other without description": (lambda d: d["intent"].update(functions=[{"function": "other"}]), False),
            "function invented": (lambda d: d["intent"].update(functions=[{"function": "decorative"}]), False),
            "scalar resin process": (lambda d: d.update(process="resin"), True),
            "scalar other process": (lambda d: d.update(process="other"), True),
            "scalar process invented": (lambda d: d.update(process="sls"), False),
        })

    def test_interface_functional_block(self) -> None:
        base = fx.tst_record()["interfaces.yaml"]
        functional = lambda d: d["interfaces"][0]["functional"]  # noqa: E731
        self.assert_cases("interfaces.schema.json", base, {
            "legacy interface": (lambda d: d["interfaces"][0].pop("functional"), True),
            "criticality required": (lambda d: functional(d).pop("critical"), False),
            "rss needs its decision": (lambda d: functional(d).update(variation={"stack_case": "stk-1", "contributors": ["manufacture"],
                                                                                 "method": "justified_rss"}), False),
            "worst case stack": (lambda d: functional(d).update(variation={"stack_case": "stk-1", "contributors": ["manufacture", "thermal"],
                                                                            "method": "worst_case"}), True),
            "contact needs both materials": (lambda d: functional(d).update(contact={"materials": ["a"]}), False),
            "play bounds": (lambda d: functional(d).update(play={"radial": fx.known(fx.quantity(lower=0, upper=0.05)),
                                                                  "scenario_cases": ["play-1"]}), True),
            "play with compliance under load": (lambda d: functional(d).update(play={
                "axial": fx.known(fx.quantity(0.1)), "compliance": {"axial": fx.known(fx.quantity(0.02)), "angular": fx.unknown("not computed")}}), True),
            "compliance of the backlash": (lambda d: functional(d).update(play={"compliance": {"backlash": fx.known(fx.quantity(0.02))}}), False),
            "empty compliance": (lambda d: functional(d).update(play={"compliance": {}}), False),
            "acceptance without evidence levels": (lambda d: functional(d)["acceptance"][0]["verification"].pop("evidence_required"), False),
            "surface by face index": (lambda d: functional(d).update(surfaces=[{"PN": "TST-0001", "face": 3, "role": "bore",
                                                                                 "dimension_state": "assembled"}]), False),
        })

    def test_assembly_operations_have_one_authority(self) -> None:
        base = fx.tst_record()["assembly.yaml"]
        operation = base["operations"][0]
        drawing = {"number": "TST-ASM", "rev": "X1", "title": "TST", "views": [{"id": "iso", "kind": "assembled", "direction": "iso"}],
                   "items": [{"item": 1, "PN": "TST-0001"}]}
        self.assert_cases("assembly.schema.json", base, {
            "operations unknown": (lambda d: d.update(operations=fx.unknown("not recorded", "Q-012")), True),
            "legacy drawing operations alone": (lambda d: (d.pop("operations"), d.update(drawing={**drawing, "operations": [operation]})), True),
            "both authorities": (lambda d: d.update(drawing={**drawing, "operations": [operation]}), False),
            "a close operation names what it encloses": (lambda d: d["operations"].append({"id": "close", "kind": "close", "part": "TST-0002",
                                                                                           "into": "TST-0001"}), False),
            "fasten with torque and tool access": (lambda d: d["operations"].append({
                "id": "fasten", "kind": "fasten", "part": "TST-0002", "into": "TST-0001", "direction": [0, 0, 1],
                "acceptance": {"kind": "torqued", "features": ["head"]}, "predecessors": ["fit_bearing"],
                "torque": fx.known(fx.quantity(2, "N_m")), "tool_access": [{"tool": "hex key", "reach": fx.unknown("not measured")}]}), True),
            "an insert with its edge distance and insertion path": (lambda d: d["operations"].append({
                "id": "insert", "kind": "insert", "part": "TST-0002", "into": "TST-0001", "direction": [0, 0, -1],
                "acceptance": {"kind": "flush", "features": ["top", "face"]},
                "edge_distance": {"criterion": fx.criterion("edge_distance", ">=", 4, "mm"), "distance": fx.known(fx.quantity(lower=4.5, upper=5))},
                "path": {"start": [10, 0, 20], "via": [[0, 0, 20]]}}), True),
            "an unknown edge distance is recorded as such": (lambda d: d["operations"][0].update(edge_distance={
                "criterion": fx.criterion("edge_distance", "between", [4, 9], "mm"), "distance": fx.unknown("not measured")}), True),
            "edge distance needs its distance": (lambda d: d["operations"][0].update(edge_distance={
                "criterion": fx.criterion("edge_distance", ">=", 4, "mm")}), False),
            "edge distance criterion of another property": (lambda d: d["operations"][0].update(edge_distance={
                "criterion": fx.criterion("pull_out", ">=", 4, "mm"), "distance": fx.known(fx.quantity(5))}), False),
            "edge distance criterion without units": (lambda d: d["operations"][0].update(edge_distance={
                "criterion": fx.criterion("edge_distance", ">=", 4), "distance": fx.known(fx.quantity(5))}), False),
            "edge distance as an upper bound": (lambda d: d["operations"][0].update(edge_distance={
                "criterion": fx.criterion("edge_distance", "<=", 4, "mm"), "distance": fx.known(fx.quantity(5))}), False),
            "edge distance on an inspection": (lambda d: d["operations"].append({
                "id": "check", "kind": "inspect", "part": "TST-0002", "acceptance": {"kind": "inspected", "features": ["bore"]},
                "edge_distance": {"criterion": fx.criterion("edge_distance", ">=", 4, "mm"), "distance": fx.known(fx.quantity(5))}}), False),
            "a path without its start": (lambda d: d["operations"][0].update(path={"via": [[0, 0, 5]]}), False),
            "a removal path": (lambda d: d["operations"].append({"id": "remove", "kind": "disassemble", "part": "TST-0003",
                                                                 "into": "TST-0001", "direction": [0, 0, 1], "path": {"start": [0, 0, 0]}}), False),
            "legacy kind still needs its interface": (lambda d: d["operations"][0].pop("interface"), False),
        })

    def test_capability_profiles_and_templates(self) -> None:
        base = fx.tst_record()["manufacturing/capabilities.yaml"]
        self.assert_cases("manufacturing-capabilities.schema.json", base, {
            "materials unknown": (lambda d: d["profiles"][1].update(materials=fx.unknown()), True),
            "forbidden material needs a reason": (lambda d: d["profiles"][1]["materials"][0].update(status="forbidden"), False),
            "a calibration needs its result samples": (lambda d: d["profiles"][0]["calibrations"][0]["result"].pop("samples"), False),
            "a calibration is scoped to a material": (lambda d: d["profiles"][0]["calibrations"][0]["scope"].pop("material"), False),
            "a template has no calibrations": (lambda d: d["profiles"][0].update(template=True, guidance=["x"]), False),
            "guidance only on templates": (lambda d: d["profiles"][1].update(guidance=["x"]), False),
            "parameter as a bare number": (lambda d: d["profiles"][0]["parameters"].update(nozzle_diameter=0.4), False),
        })

    def test_material_profiles_keep_family_grade_and_conditions_apart(self) -> None:
        base = fx.tst_record()["manufacturing/materials.yaml"]
        prop = lambda d: d["profiles"][1]["properties"][0]  # noqa: E731
        self.assert_cases("manufacturing-materials.schema.json", base, {
            "family with a grade": (lambda d: d["profiles"][0].update(grade=fx.known("TST grade")), False),
            "grade without its grade": (lambda d: d["profiles"][1].update(grade=fx.unknown()), False),
            "property without conditions": (lambda d: prop(d).pop("conditions"), False),
            "property without temperature": (lambda d: prop(d)["conditions"].pop("temperature"), False),
            "conditioning invented": (lambda d: prop(d)["conditions"].update(conditioning="wet"), False),
            "a bare number": (lambda d: prop(d).update(value=111), False),
            "unknown value": (lambda d: prop(d).update(value=fx.unknown("not published")), True),
            "form invented": (lambda d: d["profiles"][1].update(form="powder"), False),
            "typical flag required": (lambda d: d["profiles"][1].pop("typical_not_allowable"), False),
        })

    def test_selection_candidates_propose_parts_and_purchases(self) -> None:
        purchase = {"identity": fx.known("TST pin 6x20"), "specification": fx.known("TST steel"), "ratings": fx.known("TST rating"),
                    "supplier_evidence": [fx.source("supplier")]}
        bought = {"kind": "purchased", "capability": fx.not_applicable("a purchase"), "purchase": purchase}
        candidate = {"id": "pinned", "title": "Bought shaft and a new pin", "architecture": "printed bracket, bought shaft, new pin",
                     "parts": [{"PN": "TST-0002", "material_profile": fx.not_applicable("catalogue item"), "route": [copy.deepcopy(bought)]},
                               {"PN": "TST-0901", "material_profile": fx.not_applicable("catalogue item"), "route": [copy.deepcopy(bought)],
                                "proposed": {"title": "TST pin", "sourcing": "catalogue", "reason": "carries the pivot",
                                             "functions": [{"function": "rotating"}], "transparency": "opaque"}}],
                     "evaluations": [], "pros": ["p"], "cons": ["c"], "tradeoffs": ["t"]}
        selection = {"status": "assessed", "assessment": {"input_sha256": "a" * 64, "method": "selection.py", "date": "2026-01-01"},
                     "candidates": [candidate]}
        existing = lambda d: d["candidates"][0]["parts"][0]  # noqa: E731
        new = lambda d: d["candidates"][0]["parts"][1]  # noqa: E731
        made = [{"kind": "cnc", "capability": fx.unknown("not chosen")}]
        self.assert_cases("manufacturing-selection.schema.json", selection, {
            "a purchased step without criteria": (lambda d: existing(d)["route"][0].pop("purchase"), True),
            "purchase criteria on a made step": (lambda d: existing(d).update(route=[{**made[0], "purchase": purchase}]), False),
            "purchase criteria without ratings": (lambda d: existing(d)["route"][0]["purchase"].pop("ratings"), False),
            "a purchase with an unidentified item": (lambda d: existing(d)["route"][0]["purchase"].update(identity=fx.known("")), False),
            "a proposal without its reason": (lambda d: new(d)["proposed"].pop("reason"), False),
            "a proposal with an unknown sourcing": (lambda d: new(d)["proposed"].update(sourcing="borrowed"), False),
            "a catalogue proposal without purchase criteria": (lambda d: new(d)["route"][0].pop("purchase"), False),
            "a catalogue proposal that is machined": (lambda d: new(d)["route"].append(made[0]), False),
            "a catalogue proposal inspected on receipt": (lambda d: new(d)["route"].append({"kind": "inspection", "capability": fx.unknown("x")}), True),
            "a made proposal": (lambda d: (new(d)["proposed"].update(sourcing="make"), new(d).update(route=made)), True),
            "a made proposal bought as a step": (lambda d: (new(d)["proposed"].update(sourcing="make"), new(d)["route"].append(made[0])), False),
            "a made-to-drawing proposal": (lambda d: (new(d)["proposed"].update(sourcing="made_to_drawing"), new(d).update(route=made)), True),
        })

    def test_selection_findings_evidence_and_check_results(self) -> None:
        candidate = {"id": "printed", "title": "Printed bracket", "architecture": "one printed part",
                     "parts": [{"PN": "TST-0001", "material_profile": "tst_polymer_printed",
                                "route": [{"kind": "fdm", "capability": "tst_printer"}]}],
                     "evaluations": [{"requirement": "REQ-001", "outcome": "unknown", "method": "no creep data"}],
                     "pros": ["cheap"], "cons": ["creep unknown"], "tradeoffs": ["speed against evidence"]}
        selection = {"status": "assessed", "assessment": {"input_sha256": "a" * 64, "method": "selection.py 1", "date": "2026-01-01"},
                     "candidates": [candidate],
                     "recommendation": {"status": "conditional", "primary": "printed", "reasons": ["only candidate"],
                                        "limits": ["no creep evidence"], "what_would_change": ["higher temperature"]}}
        self.assert_cases("manufacturing-selection.schema.json", selection, {
            "not assessed has no candidates": (lambda d: d.update(status="not_assessed"), False),
            "confirmed needs a decision and a final recommendation": (lambda d: d.update(status="confirmed"), False),
            "confirmed": (lambda d: (d.update(status="confirmed"), d["assessment"].update(decision="DR-001"),
                                     d["recommendation"].update(status="final")), True),
            "pass needs criterion and source": (lambda d: d["candidates"][0]["evaluations"][0].update(outcome="pass"), False),
            "a candidate without tradeoffs": (lambda d: d["candidates"][0].pop("tradeoffs"), False),
        })
        self.assert_valid("manufacturing-selection.schema.json", {"status": "not_assessed", "candidates": []})
        waiver = {"decision": "DR-001", "subject": {"PN": "TST-0001"}, "criterion": "creep", "operating_envelope": "below 30 deg_C",
                  "until_change": "any load change", "reason": "trial", "consequence": "sag", "authority": "lead"}
        disposition = {"finding": "overhang_1", "producer": {"bundle": "hw/checks/v1/TST-0001", "manifest_sha256": "a" * 64},
                       "status": "accepted_waiver", "reason": "trial print", "decision": "DR-001", "waiver": waiver}

        def declined_without_decision(d: Any) -> None:
            d["dispositions"][0] = {k: v for k, v in d["dispositions"][0].items() if k not in ("waiver", "decision")}
            d["dispositions"][0]["status"] = "declined"

        self.assert_cases("manufacturing-findings.schema.json", {"dispositions": [disposition]}, {
            "a waiver needs its expiry or change trigger": (lambda d: d["dispositions"][0]["waiver"].pop("until_change"), False),
            "declined needs its decision": (declined_without_decision, False),
            "declined with its decision": (lambda d: (declined_without_decision(d), d["dispositions"][0].update(decision="DR-002")), True),
            "open has no waiver": (lambda d: d["dispositions"][0].update(status="open"), False),
            "producer manifest required": (lambda d: d["dispositions"][0]["producer"].pop("manifest_sha256"), False),
        })
        entry = {"id": "EV-001", "requirement": "REQ-001", "subject": {"PN": "TST-0001"}, "level": "physically_validated",
                 "result": "pass", "method": "coupon load test", "criterion": fx.criterion("load", ">=", 11, "N"),
                 "configuration": {"version": "v1", "manifest_sha256": "a" * 64},
                 "artifacts": [{"path": "hw/manufacturing/sources/test.csv", "sha256": "b" * 64}],
                 "physical": {"specimen": "coupon 1", "configuration": "v1", "material": "TST lot 1", "process": "print",
                              "protocol": "TST protocol", "instrument": "TST scale, calibrated", "results": "12 N",
                              "date": "2026-01-02", "approver": "lead", "scope": "coupon only"}}
        self.assert_cases("manufacturing-evidence.schema.json", {"entries": [entry]}, {
            "physical pass without its record": (lambda d: d["entries"][0].pop("physical"), False),
            "pass without artifacts or bundle": (lambda d: d["entries"][0].pop("artifacts"), False),
            "planned needs no artifacts": (lambda d: (d["entries"][0].update(result="planned"), d["entries"][0].pop("artifacts"),
                                                     d["entries"][0].pop("physical")), True),
            "waived needs its waiver": (lambda d: d["entries"][0].update(result="waived"), False),
            "fingerprint domain invented": (lambda d: d["entries"][0].update(fingerprints={"vibes": "c" * 64}), False),
            "neither requirement nor obligation": (lambda d: d["entries"][0].pop("requirement"), False),
            "level as a badge": (lambda d: d["entries"][0].update(level="certified"), False),
        })
        obligation = {"id": "process.fdm.support@TST-0001/print", "family": "process.fdm.support",
                      "subject": {"PN": "TST-0001", "operation": "print"}, "level": "process_planned", "stage": "detail", "basis": "fdm"}
        result = {"schema_version": 1, "record_version": 2, "producer": "TST 1", "version": "v1", "scope": [{"PN": "TST-0001"}],
                  "expected": [obligation], "outcomes": [{"obligation": obligation["id"], "outcome": "unknown", "method": "none",
                                                          "coverage": "no calibrated profile"}],
                  "findings": [], "dependencies": {"part": "a" * 64}, "complete": True, "result": "unknown"}
        self.assert_cases("manufacturing-check-result.schema.json", result, {
            "completeness required": (lambda d: d.pop("complete"), False),
            "outcome invented": (lambda d: d["outcomes"][0].update(outcome="ok"), False),
            "record version 1": (lambda d: d.update(record_version=1), False),
        })

    def test_release_manifest_v3_and_older_versions(self) -> None:
        frozen = {"version": "v1", "manifest_sha256": "a" * 64, "decision": "DR-001", "evidence_sha256": "b" * 64}
        v3 = {"schema_version": 3, "record_version": 2, "product_id": "tst", "rev": "A", "released_at": "2026-01-01T00:00:00Z",
              "frozen": frozen, "approval": "DR-002", "files": {"bom.csv": {"sha256": "c" * 64, "original": "hw/bom.csv"}},
              "manufacturing": {"TST-0001": {"process": "fdm", "quantity": 1, "files": ["manufacturing/TST-0001/part.step"],
                                             "route": ["fdm", "reaming"],
                                             "components": {"left": ["manufacturing/TST-0001/components/left/part.step"]}}},
              "bundles": [], "checks": {},
              "acceptance": {"evidence_sha256": "b" * 64, "inventory": {"hw/verification.csv": "d" * 64},
                             "scope": {"service_class": "fit_assembly", "purpose": "fit trial", "operating_limits": [], "exclusions": [],
                                       "trial": True},
                             "claims": {"REQ-001": ["process_planned"]}, "waivers": [], "open_obligations": []}}
        self.assert_cases("release-manifest.schema.json", v3, {
            "v3 freeze needs its evidence digest": (lambda d: d["frozen"].pop("evidence_sha256"), False),
            "v3 needs its acceptance": (lambda d: d.pop("acceptance"), False),
            "unknown service class cannot release": (lambda d: d["acceptance"]["scope"].update(service_class="unknown"), False),
            "a route is required": (lambda d: d["manufacturing"]["TST-0001"].pop("route"), False),
            "waiver without authority": (lambda d: d["acceptance"].update(waivers=[{"decision": "DR-001", "subject": {"PN": "TST-0001"},
                                                                                    "criterion": "x", "operating_envelope": "x",
                                                                                    "expires": "2026-02-01", "reason": "x",
                                                                                    "consequence": "x"}]), False),
            "claim of an invented level": (lambda d: d["acceptance"].update(claims={"REQ-001": ["certified"]}), False),
        })
        v2 = {k: v for k, v in v3.items() if k not in ("record_version", "acceptance")}
        v2.update(schema_version=2, frozen={k: v for k, v in frozen.items() if k != "evidence_sha256"},
                  manufacturing={"TST-0001": {"process": "fdm", "quantity": 1, "files": ["manufacturing/TST-0001/part.step"]}})
        self.assert_valid("release-manifest.schema.json", v2)
        self.assert_valid("release-manifest.schema.json", {"product_id": "tst", "rev": "A", "released_at": "2026-01-01T00:00:00Z",
                                                           "files": ["hw/bom.csv"], "checks": {}})
        self.assertTrue(errors("release-manifest.schema.json", {**v2, "acceptance": v3["acceptance"]}), "v2 has no acceptance")


class ShippedAssets(SchemaCase):
    def test_the_template_is_record_version_2_with_nothing_chosen(self) -> None:
        documents, problems = intent.read_documents(TEMPLATE)
        self.assertEqual(problems, [])
        self.assertEqual(intent.schema_problems(documents), [])
        product = documents["product.yaml"]
        self.assertEqual(product["record_version"], 2)
        self.assertNotIn("build_volume", product)
        self.assertNotIn("manufacturing", product)
        # never a defaulted tolerance: unknown until the user answers the GROUND question it names
        self.assertEqual(product["general_tolerance"], {"status": "unknown", "reason": "not recorded yet", "question": "Q-013"})
        questions = {q["id"]: q for q in documents["requirements.yaml"]["open_questions"]}
        self.assertEqual(questions["Q-013"]["status"], "open")
        self.assertIn("general tolerance", questions["Q-013"]["question"])
        record = intent.normalize_record(documents, hw=TEMPLATE)
        self.assertEqual([p.finding() for p in record.problems], [])
        self.assertEqual(record.use_case.status, "unknown")
        self.assertTrue(record.use_case.service_class.unknown)
        self.assertTrue(all(fact.unknown for fact in record.use_case.facts.values()))
        self.assertEqual((dict(record.capabilities), dict(record.materials), record.selection.status), ({}, {}, "not_assessed"))
        assessment = intent.assess_use_case(record)
        self.assertFalse(assessment.final_ready)
        self.assertIn("Q-001", assessment.open_questions)

    def test_capability_templates_hold_no_machine_numbers(self) -> None:
        data = yaml.safe_load((ASSETS / "capability-templates.yaml").read_text(encoding="utf-8"))
        self.assert_valid("manufacturing-capabilities.schema.json", data)
        profiles, problems = intent.profile_document(data, "capabilities", "assets/manufacturing/capability-templates.yaml")
        self.assertEqual([p.finding() for p in problems], [])
        self.assertEqual({p.process for p in profiles}, set(intent.CAPABILITY_PARAMETERS))
        for profile in profiles:
            with self.subTest(profile.id):
                self.assertTrue(profile.template)
                self.assertEqual(profile.calibrations, ())
                self.assertTrue(all(fact.unknown for fact in profile.parameters.values()))
                self.assertEqual(set(profile.parameters), set(intent.CAPABILITY_PARAMETERS[profile.process]))

    def test_starter_material_profiles_are_sourced_typical_values(self) -> None:
        """Every starter profile file (assets/manufacturing/material-profiles/*.yaml, when shipped) validates, is
        typical_not_allowable, and cites a url with its retrieval date and test conditions for every known value."""
        for path in sorted((ASSETS / "material-profiles").glob("*.yaml")):
            with self.subTest(path.name):
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assert_valid("manufacturing-materials.schema.json", data)
                profiles, problems = intent.profile_document(data, "materials", path.name)
                self.assertEqual([p.finding() for p in problems], [])
                for profile in profiles:
                    self.assertTrue(profile.typical_not_allowable, profile.id)
                    for prop in profile.properties:
                        if prop.value.known:
                            self.assertNotEqual(prop.value.basis, "allowable")
                            self.assertTrue(all(s.url and s.accessed and s.conditions for s in prop.value.sources), (profile.id, prop.name))


if __name__ == "__main__":
    unittest.main()
