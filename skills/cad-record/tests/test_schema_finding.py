#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""The shared finding schema: what a finding may carry, the honest mistakes it rejects, and which of today's
producers already emit it. Every validation goes through the schema registry."""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parents[1]
SCHEMAS = SKILL / "schemas"
SCRIPTS = SKILL / "scripts"
SKILLS = SKILL.parent
FORM_CHECK = SKILLS / "cad-design" / "scripts" / "form_check.py"
PRODUCER_FINDINGS = Path(__file__).resolve().parent / "fixtures" / "producer_findings.py"

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from modload import load_module as load  # noqa: E402

registry = load("cad_record_schema_registry", SCRIPTS / "schema_registry.py")
validate = load("cad_record_validate", SCRIPTS / "validate.py")
evidence = load("cad_record_evidence", SCRIPTS / "evidence.py")
assembly_operations = load("cad_record_assembly_operations", SCRIPTS / "assembly_operations.py")
SCHEMA_ID = "https://srobroek.com/cad/schemas/finding.schema.json"

GOOD = {
    "code": "process.fdm.overhang",
    "severity": "major",
    "outcome": "fail",
    "subject": {"PN": "TST-0001", "feature": "mount_slot"},
    "location": {"file": "hw/parts/TST-0001/part.yaml", "line": 12},
    "message": "the overhang exceeds the printable angle",
    "remedy": "add a chamfer or split the part",
    "pointer": "drawing/features/mount_slot",
    "value": {"angle_deg": 62.5, "max_deg": 45},
    "source": "step",
}


def errors(document: Any) -> list[str]:
    """The registry validator's messages for `document` against the finding schema."""
    validator = registry.document_validator(SCHEMAS, "finding.schema.json")
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(document)]


def mutated(**changes: Any) -> dict[str, Any]:
    item = copy.deepcopy(GOOD)
    for key, value in changes.items():
        if value is None:
            item.pop(key)
        else:
            item[key] = value
    return item


class FindingSchema(unittest.TestCase):
    def test_the_schema_is_a_registered_draft_2020_12_document_on_the_shared_base(self) -> None:
        contents = registry.schema(SCHEMAS, "finding.schema.json")
        self.assertEqual(contents["$id"], SCHEMA_ID)
        self.assertEqual(contents["$schema"], "https://json-schema.org/draft/2020-12/schema")
        registry.check_schema(contents)

    def test_a_finding_with_every_field_validates(self) -> None:
        self.assertEqual(errors(GOOD), [])

    def test_only_the_code_and_message_are_required(self) -> None:
        self.assertEqual(errors({"code": "fit_unverified", "message": "no inspection verifies the fit"}), [])

    def test_every_canonical_severity_word_is_accepted(self) -> None:
        # the check-packet vocabulary (manufacturing.schema.json severity)
        for word in ("critical", "major", "minor", "info"):
            with self.subTest(severity=word):
                self.assertEqual(errors(mutated(severity=word)), [])

    def test_every_outcome_word_of_a_check_is_accepted(self) -> None:
        for word in ("pass", "fail", "unknown", "not_applicable"):
            with self.subTest(outcome=word):
                self.assertEqual(errors(mutated(outcome=word)), [])

    def test_a_not_assessed_finding_is_accepted_with_either_reason(self) -> None:
        for reason in ("calibration_off", "no_capability_profile"):
            with self.subTest(reason=reason):
                self.assertEqual(errors(mutated(outcome="not_assessed", reason=reason)), [])

    def test_dotted_and_single_segment_codes_are_accepted(self) -> None:
        for code in ("layout.stray", "intent.schema", "motion.play_collision", "section_hatch", "invalid_drawing_intent"):
            with self.subTest(code=code):
                self.assertEqual(errors(mutated(code=code)), [])

    def test_value_takes_a_value_of_any_type(self) -> None:
        for value in (1.5, 0, "62.5 deg", True, [1, "a", None], {"angle_deg": 62.5}, None):
            with self.subTest(value=value):
                self.assertEqual(errors({**GOOD, "value": value}), [])

    def test_the_schema_examples_include_a_minimal_and_a_full_finding(self) -> None:
        examples = registry.schema(SCHEMAS, "finding.schema.json")["examples"]
        self.assertGreaterEqual(len(examples), 2)
        self.assertTrue(any(set(example) == {"code", "message"} for example in examples))


class HonestMistakes(unittest.TestCase):
    """What an agent gets wrong by hand. Each case names the mistake and the message that points at it."""

    def rejects(self, document: Any, fragment: str) -> None:
        found = errors(document)
        self.assertTrue(found, "accepted")
        self.assertTrue(any(fragment in line for line in found), found)

    def test_a_misspelled_severity(self) -> None:
        for word in ("critcal", "Major", "warn", "fatal", ""):
            with self.subTest(severity=word):
                self.rejects(mutated(severity=word), "severity")

    def test_a_legacy_severity_word_of_validate_or_layout(self) -> None:
        for word in ("error", "warning", "stage"):
            with self.subTest(severity=word):
                self.rejects(mutated(severity=word), "severity")

    def test_a_misspelled_outcome(self) -> None:
        self.rejects(mutated(outcome="failed"), "outcome")

    def test_a_not_assessed_finding_without_a_reason(self) -> None:
        self.rejects(mutated(outcome="not_assessed"), "reason")

    def test_a_not_assessed_finding_with_an_unknown_reason(self) -> None:
        self.rejects(mutated(outcome="not_assessed", reason="vibes"), "reason")

    def test_a_reason_on_any_other_outcome(self) -> None:
        for word in ("pass", "fail", "unknown", "not_applicable"):
            with self.subTest(outcome=word):
                self.rejects(mutated(outcome=word, reason="calibration_off"), "reason")

    def test_a_missing_code(self) -> None:
        self.rejects(mutated(code=None), "'code' is a required property")

    def test_a_missing_message(self) -> None:
        self.rejects(mutated(message=None), "'message' is a required property")

    def test_a_code_outside_the_pattern(self) -> None:
        for code in ("", "Process.Overhang", "process-overhang", "process..overhang", ".leading", "trailing.", "1stcheck", "two words"):
            with self.subTest(code=code):
                self.rejects(mutated(code=code), "code")

    def test_a_line_given_as_text(self) -> None:
        self.rejects(mutated(location={"file": "hw/product.yaml", "line": "12"}), "location/line")

    def test_a_line_below_one(self) -> None:
        self.rejects(mutated(location={"file": "hw/product.yaml", "line": 0}), "location/line")

    def test_a_location_without_a_file(self) -> None:
        self.rejects(mutated(location={"line": 12}), "'file' is a required property")

    def test_a_location_with_an_empty_file(self) -> None:
        self.rejects(mutated(location={"file": ""}), "location/file")

    def test_a_location_key_misread_from_pointer_and_path(self) -> None:
        self.rejects(mutated(location={"file": "hw/product.yaml", "pointer": "id"}), "location")

    def test_a_subject_with_a_malformed_part_number(self) -> None:
        for pn in ("tst-0001", "TST0001", "TST-1"):
            with self.subTest(PN=pn):
                self.rejects(mutated(subject={"PN": pn}), "subject/PN")

    def test_an_empty_subject(self) -> None:
        self.rejects(mutated(subject={}), "subject")

    def test_a_message_that_is_not_text(self) -> None:
        self.rejects(mutated(message=["a", "b"]), "message")

    def test_an_empty_feature_remedy_source_or_message(self) -> None:
        cases = {
            "subject/feature": mutated(subject={"PN": "TST-0001", "feature": ""}),
            "remedy": mutated(remedy=""),
            "source": mutated(source=""),
            "message": mutated(message=""),
        }
        for where, document in cases.items():
            with self.subTest(field=where):
                self.rejects(document, where)

    def test_a_pointer_that_is_not_text(self) -> None:
        for pointer in (3, ["parts", 0], {"path": "parts/0"}, True):
            with self.subTest(pointer=pointer):
                self.rejects(mutated(pointer=pointer), "pointer")

    def test_a_value_of_the_wrong_type_for_a_text_or_object_field(self) -> None:
        cases = {
            "code": [7, ["fit_unverified"], None],
            "subject": ["TST-0001", ["TST-0001"], None],
            "location": ["hw/product.yaml", ["hw/product.yaml", 12], None],
            "remedy": [5, ["chamfer it"], None],
            "source": [6, ["step"], None],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.rejects({**GOOD, field: value}, field)

    def test_a_nested_value_of_the_wrong_type(self) -> None:
        cases = {
            "subject/PN": {**GOOD, "subject": {"PN": 12}},
            "subject/feature": {**GOOD, "subject": {"PN": "TST-0001", "feature": 3}},
            "location/file": {**GOOD, "location": {"file": 4}},
            "location/line": {**GOOD, "location": {"file": "hw/product.yaml", "line": 1.5}},
        }
        for where, document in cases.items():
            with self.subTest(field=where):
                self.rejects(document, where)

    def test_a_subject_key_that_is_not_pn_or_feature(self) -> None:
        self.rejects(mutated(subject={"PN": "TST-0001", "part": "TST-0001"}), "part")

    def test_an_unknown_top_level_field(self) -> None:
        self.rejects(mutated(severty="major"), "severty")

    def test_a_finding_that_is_not_an_object(self) -> None:
        self.rejects("process.fdm.overhang: too steep", "object")


# --- producers -----------------------------------------------------------------------------------------------
# One entry per finding shape a producer emits. Every shape comes from the producer's own code, except form_check's,
# which needs FreeCAD to run: those are literals tied to form_check.py by `FormCheckDrift`. The shapes built outside
# this process come from fixtures/producer_findings.py, run in a subprocess, so the producer modules and their sys.path
# entries stay out of the test run.


def emitted_findings() -> dict[str, dict[str, Any]]:
    run = subprocess.run([sys.executable, "-B", str(PRODUCER_FINDINGS)], capture_output=True, text=True, check=False)
    if run.returncode != 0:
        raise RuntimeError(f"{PRODUCER_FINDINGS.name} exited {run.returncode}: {run.stderr[-2000:]}")
    return json.loads(run.stdout)  # type: ignore[no-any-return]


EMITTED = emitted_findings()
OPERATIONS = {"id": "fit-bearing", "kind": "press_fit", "part": "TST-0003", "into": "TST-0001", "interface": "IF-001",
              "direction": [0, 0, -2], "acceptance": {"kind": "seated", "features": ["bearing_face", "seat_floor"]},
              "tools": ["arbor press"], "predecessors": ["ghost"]}
[ORDER_FINDING] = assembly_operations.check_order(assembly_operations.normalize({"operations": [OPERATIONS]})).findings

# form_check.py:1105 min_wall is not in BLOCKING_CHECKS (:102), so :1143 sets blocking False. brep_invalid (:1082) is in
# it, so blocking is True, and it is the only finding with a `source` key.
FORM_CHECK_FINDINGS: dict[str, dict[str, Any]] = {
    "form_check.min_wall": {"check": "min_wall", "part": "TST-0001", "thickness_mm": 0.4, "location_mm": [1.0, 2.0, 3.0],
                            "min_mm": 0.8, "blocking": False},
    "form_check.brep_invalid": {"check": "brep_invalid", "part": "TST-0001", "source": "step", "blocking": True},
}

PRODUCERS: dict[str, dict[str, Any]] = {
    # cad-record validate.py:71 finding(): path, message, severity (error|warning|info)
    "validate.finding": validate.finding("hw/product.yaml", "id is required"),
    # validate.py:71 with a code: a layout or design-rule finding adds code, canonical_location and action
    "validate.coded": validate.finding("hw/stray.txt", "a path no layout entry accepts", severity="warning",
                                       code="layout.stray", canonical_location=None, action="move it"),
    # manufacturing_intent.py:235 Problem.finding(): path#/pointer folded into one string
    "manufacturing_intent.problem": validate.intent.Problem(
        "intent.schema", "hw/manufacturing/intent.yaml", "parts/0", "parts/0: 'rev' is a required property").finding(),
    # evidence.py:157 Finding.as_dict()
    "evidence.finding": evidence.Finding("evidence.stale", "hw/checks/v1", "the check is older than the model",
                                         obligation="OBL-1").as_dict(),
    # cad-drawings drawing_intent.py:72 finding() at 1480 (bare), 106 via finished_state_findings and 1210 (dimension=)
    "drawing_intent.bare": EMITTED["drawing_intent.bare"],
    "drawing_intent.with_feature": EMITTED["drawing_intent.with_feature"],
    "drawing_intent.with_dimension": EMITTED["drawing_intent.with_dimension"],
    # cad-drawings assembly_check.py:78 finding() at assembly_drawing.py:443 (bare) and :240 (sheet=)
    "assembly_check.bare": EMITTED["assembly_check.bare"],
    "assembly_check.with_sheet": EMITTED["assembly_check.with_sheet"],
    # cad-model cadmodel/build.py:82 finding(pn, check, message, kind, where): no code, no severity, `pn` not `PN`
    "build.finding": EMITTED["build.finding"],
    **FORM_CHECK_FINDINGS,
    # cad-design process_checks/dispatch.py:174-209 _findings(), called through process_checks.assess on a printed part
    "packet.process_overhang": EMITTED["packet.process_overhang"],
    # cad-record assembly_operations.py:404-426 _finding() via check_order() (cad-motion access.py:339-384 emits the same
    # keys, with severity critical or major): a packet finding about an assembly operation
    "packet.assembly_operation": ORDER_FINDING,
}

# Producers whose emitted shape validates as it is, sorted. A producer that changes shape moves between lists.
CONFORMING = [
    "assembly_check.bare",
    "drawing_intent.bare",
]


def reasons(document: Any) -> list[str]:
    """Stable reasons a document fails: `missing <key>` and `unexpected <key>`, each prefixed by its location."""
    validator = registry.document_validator(SCHEMAS, "finding.schema.json")
    out: list[str] = []
    for error in validator.iter_errors(document):
        where = "/".join(map(str, error.absolute_path))
        prefix = f"{where}: " if where else ""
        if error.validator == "required":
            out.append(f"{prefix}missing {error.message.split(chr(39))[1]}")
        elif error.validator == "additionalProperties":
            extra = sorted(set(error.instance) - set(error.schema["properties"]))
            out.extend(f"{prefix}unexpected {key}" for key in extra)
        else:
            out.append(f"{prefix}{error.validator}")
    return sorted(out)


EXPECTED_REASONS: dict[str, list[str]] = {
    "validate.finding": ["missing code", "severity: enum", "unexpected path"],
    "validate.coded": ["severity: enum", "unexpected action", "unexpected canonical_location", "unexpected path"],
    "manufacturing_intent.problem": ["severity: enum", "unexpected action", "unexpected canonical_location",
                                     "unexpected path"],
    "evidence.finding": ["severity: enum", "unexpected action", "unexpected canonical_location",
                         "unexpected obligation", "unexpected path"],
    "drawing_intent.with_feature": ["unexpected feature"],
    "drawing_intent.with_dimension": ["unexpected dimension"],
    "assembly_check.with_sheet": ["unexpected sheet"],
    "build.finding": ["missing code", "unexpected check", "unexpected pn", "unexpected type", "unexpected where"],
    "form_check.min_wall": ["missing code", "missing message", "unexpected blocking", "unexpected check",
                            "unexpected location_mm", "unexpected min_mm", "unexpected part",
                            "unexpected thickness_mm"],
    "form_check.brep_invalid": ["missing code", "missing message", "unexpected blocking", "unexpected check",
                                "unexpected part"],
    "packet.process_overhang": ["missing message", "subject: unexpected operation", "unexpected alternatives",
                                "unexpected consequence", "unexpected criterion", "unexpected id", "unexpected measured",
                                "unexpected method", "unexpected obligation", "unexpected recheck",
                                "unexpected uncertainty"],
    "packet.assembly_operation": ["missing message", "subject: unexpected assembly_operation", "unexpected consequence",
                                  "unexpected criterion", "unexpected id", "unexpected measured", "unexpected method",
                                  "unexpected obligation", "unexpected recheck", "unexpected uncertainty"],
}


def form_check_dicts() -> list[ast.Dict]:
    """Every dict literal in form_check.py that names a `check`: the findings it builds."""
    tree = ast.parse(FORM_CHECK.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "check" for key in node.keys)]


def form_check_blocking_checks() -> set[str]:
    """The names in form_check.py's BLOCKING_CHECKS constant, read without importing the module."""
    tree = ast.parse(FORM_CHECK.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "BLOCKING_CHECKS" for t in node.targets):
            call = node.value
            assert isinstance(call, ast.Call)
            return set(ast.literal_eval(call.args[0]))
    raise AssertionError("form_check.py defines no BLOCKING_CHECKS")


class FormCheckDrift(unittest.TestCase):
    """The literal form_check findings keep the keys and blocking flag form_check.py builds, so they cannot drift."""

    def test_each_literal_has_the_keys_of_a_dict_form_check_builds(self) -> None:
        built = [frozenset(key.value for key in node.keys if isinstance(key, ast.Constant)) for node in form_check_dicts()]
        for name, finding in FORM_CHECK_FINDINGS.items():
            with self.subTest(finding=name):
                self.assertIn(frozenset(finding) - {"blocking"}, built)

    def test_each_literal_blocks_exactly_when_form_check_says(self) -> None:
        blocking = form_check_blocking_checks()
        for name, finding in FORM_CHECK_FINDINGS.items():
            with self.subTest(finding=name):
                self.assertEqual(finding["blocking"], finding["check"] in blocking)


class ProducerConformance(unittest.TestCase):
    """Which producers already emit the schema, and what each other one needs the migration PR to change."""

    def test_the_conforming_producers_are_exactly_these(self) -> None:
        passing = sorted(name for name, finding in PRODUCERS.items() if not errors(finding))
        self.assertEqual(passing, CONFORMING)

    def test_every_other_producer_fails_for_exactly_the_recorded_reasons(self) -> None:
        for name, finding in PRODUCERS.items():
            if name in CONFORMING:
                continue
            with self.subTest(producer=name):
                self.assertEqual(reasons(finding), sorted(EXPECTED_REASONS[name]))

    def test_every_surveyed_producer_is_either_conforming_or_has_recorded_reasons(self) -> None:
        self.assertEqual(sorted(set(CONFORMING) | set(EXPECTED_REASONS)), sorted(PRODUCERS))
        self.assertEqual(set(CONFORMING) & set(EXPECTED_REASONS), set())


if __name__ == "__main__":
    unittest.main()
