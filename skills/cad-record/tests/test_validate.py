#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Regression cases for CAD record validation and example-record provenance."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from modload import result_payload  # noqa: E402
from decision_fixture import decision_path, write_test_decision  # noqa: E402

SKILL = Path(__file__).resolve().parents[1]
CONCEPT = SKILL.parent / "cad-concept"
TEMPLATE = SKILL / "assets" / "project-template"
LAMP_EXAMPLE = SKILL.parent.parent / "examples" / "lamp-arm"
EXAMPLE = LAMP_EXAMPLE / "hw"
VALIDATOR = SKILL / "scripts" / "validate.py"
PLACEHOLDER_INTENT = "".join(f"  {name}: {{status: unknown, reason: not recorded yet}}\n" for name in (
    "functions", "material_profile", "service", "critical_surfaces", "route"))
# The functional block a record version 2 interface needs, all unknown (without questions).
PLACEHOLDER_FUNCTIONAL = ("functional: {critical: {status: unknown, reason: not recorded yet}, "
                          "functions: {status: unknown, reason: not recorded yet}}")


def _fixtures():
    name = "cad_record_mi_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SKILL / "tests" / "fixtures" / "manufacturing_intent.py")
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


fx = _fixtures()


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class ValidateProject(unittest.TestCase):
    """The lamp example (record version 2), edited one fact at a time."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cad-record-test-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.hw = self.project / "hw"
        fx.migrated_copy(EXAMPLE, self.hw)
        self.migrated_interfaces = (self.hw / "interfaces.yaml").read_bytes()
        self.part_path = self.hw / "parts" / "LMP-0001" / "part.yaml"

    def set_part(self, pn: str = "LMP-0001", rev: str = "X1") -> None:
        lines = self.part_path.read_text(encoding="utf-8").splitlines()
        lines = [f"PN: {pn}" if line.startswith("PN:") else line for line in lines]
        lines = [f"rev: {rev}" if line.startswith("rev:") else line for line in lines]
        self.part_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_joint(self, field: str, value: int) -> None:
        path = self.hw / "assembly.yaml"
        lines = path.read_text(encoding="utf-8").splitlines()
        start = lines.index("    min: -10")
        target = {"min": start, "max": start + 1, "nominal": start + 2}[field]
        lines[target] = f"    {field}: {value}"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def validate(self, validator: Path = VALIDATOR) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = subprocess.run(
            [sys.executable, str(validator), str(self.project)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, result_payload(result.stdout)

    def plausibility(self) -> list[dict[str, object]]:
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [item for item in report["findings"] if str(item.get("code", "")).startswith("plausibility.")]

    def test_a_value_beyond_its_band_is_a_warning_that_does_not_block(self) -> None:
        path = self.hw / "manufacturing" / "capabilities.yaml"
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("value: {value: 350, units: deg_C}"), 1)
        path.write_text(text.replace("value: {value: 350, units: deg_C}", "value: {value: 450, units: deg_C}"), encoding="utf-8")
        (found,) = self.plausibility()  # 450 is inside the 500 deg_C hard bound and past the 350 deg_C band
        self.assertEqual((found["code"], found["severity"]), ("plausibility.hotend_max_temperature", "warning"))
        self.assertTrue(str(found["path"]).endswith("manufacturing/capabilities.yaml#/profiles/0/parameters/hotend_max_temperature/value/value"), found)
        self.assertIn("450", str(found["message"]))

    def test_a_layer_height_beyond_its_nozzle_band_is_a_warning_that_does_not_block(self) -> None:
        path = self.hw / "manufacturing" / "capabilities.yaml"
        text = path.read_text(encoding="utf-8")
        old = 'layer_height:\n        status: unknown\n        reason: "Bambu Lab publishes no layer-height range for the H2D (tech specs, accessed 2026-11-18); a calibration print would answer it"'
        self.assertEqual(text.count(old), 1)
        new = ("layer_height:\n        status: known\n        value: {value: 0.9, units: mm}\n        basis: supplier_typical\n"
               "        sources:\n          - {kind: datasheet, reference: TST fixture, url: https://example.com/h2d, accessed: \"2026-11-18\"}")
        path.write_text(text.replace(old, new), encoding="utf-8")
        found = self.plausibility()
        self.assertEqual([item["code"] for item in found], ["plausibility.layer_height"], found)

    def test_an_integer_too_large_for_a_float_is_a_schema_error_and_no_crash(self) -> None:
        path = self.hw / "manufacturing" / "capabilities.yaml"
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("value: {value: 350, units: deg_C}"), 1)
        path.write_text(text.replace("value: {value: 350, units: deg_C}", "value: {value: 1" + "0" * 400 + ", units: deg_C}"), encoding="utf-8")
        result, report = self.validate()
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue([item for item in report["findings"] if item.get("severity") == "error"], report)
        self.assertEqual([item for item in report["findings"] if str(item.get("code", "")).startswith("plausibility.")], [])

    def test_copied_template_with_one_placeholder_part_validates(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-template-")
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name) / "project"
        shutil.copytree(TEMPLATE, project)
        hw = project / "hw"
        part_dir = hw / "parts" / "PRT-0001"
        part_dir.mkdir(parents=True)
        (part_dir / "part.yaml").write_text(
            "PN: PRT-0001\nrev: X1\ntitle: Placeholder part\nmaterial: Placeholder material\n"
            "process: cnc\nfinish: As machined\nmass_target: null\nmake_buy: make\nintent:\n" + PLACEHOLDER_INTENT,
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(VALIDATOR), str(project)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result_payload(result.stdout), {"ok": True, "findings": []})

    def test_empty_template_passes_at_ground_but_requires_parts_at_layout(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-empty-template-")
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name) / "project"
        shutil.copytree(TEMPLATE, project)
        result = subprocess.run([sys.executable, str(VALIDATOR), str(project)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result_payload(result.stdout), {"ok": True, "findings": []})

        product = project / "hw" / "product.yaml"
        product.write_text(product.read_text(encoding="utf-8").replace("stage: ground", "stage: layout"),
                            encoding="utf-8")
        result = subprocess.run([sys.executable, str(VALIDATOR), str(project)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no part records found", result.stdout)

        product.write_text(product.read_text(encoding="utf-8").replace("stage: layout", "stage: design_freeze")
                            .replace("  recheck_from: ground", "  recheck_from: ground\n  frozen:\n    version: v1\n    manifest_sha256: '" + "0" * 64 + "'\n    decision: DR-001"),
                            encoding="utf-8")
        write_test_decision(project / "hw", "DR-001")
        result = subprocess.run([sys.executable, str(VALIDATOR), str(project)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("flow/frozen/version v1 is not a built version", result.stdout)

    def test_the_example_record_validates_with_no_findings_and_no_plausibility_warning(self) -> None:
        """The H2D profile states 350 deg_C, a real figure from its spec page, so no band warns."""
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report, {"ok": True, "findings": []})

    def test_product_license_accepts_spdx_expressions_and_rejects_malformed_values(self) -> None:
        path = self.hw / "product.yaml"
        product = yaml.safe_load(path.read_text(encoding="utf-8"))
        product["license"] = {"hardware": "CERN-OHL-S-2.0", "documentation": "(CC-BY-4.0 OR MIT) AND GPL-2.0+"}
        path.write_text(yaml.safe_dump(product, sort_keys=False), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report, {"ok": True, "findings": []})

        product["license"]["hardware"] = "CC BY 4.0"
        path.write_text(yaml.safe_dump(product, sort_keys=False), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("license/hardware 'CC BY 4.0' is not an SPDX license expression: 'BY' follows a complete "
                      "expression", json.dumps(report["findings"]))

    def test_an_unknown_general_tolerance_validates_but_no_drawing_can_state_it(self) -> None:
        path = self.hw / "product.yaml"
        product = yaml.safe_load(path.read_text(encoding="utf-8"))
        product["general_tolerance"] = {"status": "unknown", "reason": "not recorded yet", "question": "Q-013"}
        path.write_text(yaml.safe_dump(product, sort_keys=False), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        messages = {(Path(item["path"]).name, item["message"]) for item in report["findings"]}
        self.assertTrue(any(name == "product.yaml" and "general_tolerance is unknown (question Q-013)" in message
                            for name, message in messages), messages)
        self.assertTrue(any(name == "part.yaml" and "record it before a drawing states general tolerances" in message
                            for name, message in messages), messages)
        for item in report["findings"]:  # nothing else: the schema admits the unknown form
            self.assertIn("general_tolerance", item["message"], item)

    def test_spdx_expression_shape(self) -> None:
        spec = importlib.util.spec_from_file_location("cad_record_validate_spdx", VALIDATOR)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for expression in ("CERN-OHL-S-2.0", "GPL-2.0+", "GPL-2.0-or-later WITH Classpath-exception-2.0",
                           "(MIT OR Apache-2.0) AND CC-BY-4.0", "LicenseRef-acme-hardware",
                           "DocumentRef-spdx-tool-1.2:LicenseRef-MIT-Style-2", "MIT WITH AdditionRef-x",
                           "CERN-OHL-S"):  # shape only: no SPDX license list is bundled
            with self.subTest(expression=expression):
                self.assertIsNone(module.spdx_expression_problem(expression))
        for expression in ("", "CC BY 4.0", "MIT AND", "(MIT", "MIT)", "MIT and Apache-2.0", "LicenseRef-x+",
                           "(MIT) WITH Classpath-exception-2.0", "MIT WITH", "MIT WITH LicenseRef-x", "AND MIT",
                           "MIT/X11"):
            with self.subTest(expression=expression):
                self.assertIsNotNone(module.spdx_expression_problem(expression))

    def test_product_rejects_nonpositive_build_volume_axis(self) -> None:
        path = self.hw / "product.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("x_mm: 256", "x_mm: 0"), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("x_mm", json.dumps(report["findings"]))

    def test_rejects_bad_part_number(self) -> None:
        self.set_part(pn="BAD-one")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("PN", json.dumps(report["findings"]))

    def test_rejects_each_reserved_revision_letter(self) -> None:
        for revision in "IOQSXZ":
            with self.subTest(revision=revision):
                self.set_part(rev=revision)
                result, report = self.validate()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("rev", json.dumps(report["findings"]))

    def test_rejects_joint_minimum_greater_than_maximum(self) -> None:
        self.set_joint("min", 90)
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("minimum exceeds maximum", json.dumps(report["findings"]))

    def test_rejects_joint_nominal_outside_limits(self) -> None:
        self.set_joint("nominal", 81)
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("nominal is outside its limits", json.dumps(report["findings"]))


    def test_rejects_requirement_without_verification_method(self) -> None:
        path = self.hw / "requirements.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("method: T", "method: X", 1), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("verification/method", json.dumps(report["findings"]))

    def test_rejects_bom_part_number_absent_from_parts(self) -> None:
        path = self.hw / "bom.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
            columns = rows[0].keys()
        rows[0]["PN"] = "LMP-9999"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no matching", json.dumps(report["findings"]))

    def test_rejects_joint_parent_outside_assembly_parts(self) -> None:
        path = self.hw / "assembly.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("parent: LMP-0001", "parent: LMP-9999", 1), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", json.dumps(report["findings"]))

    def test_rejects_joint_type_not_supported_by_pose(self) -> None:
        path = self.hw / "assembly.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("type: revolute", "type: cylindrical", 1), encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("cylindrical", json.dumps(report["findings"]))

    def test_requires_verification_row_for_each_requirement_at_release(self) -> None:
        path = self.hw / "verification.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
            columns = rows[0].keys()
        rows.pop()
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("REQ-007 has no verification row", json.dumps(report["findings"]))

    def make_release_ready(self) -> None:
        for part_file in (self.hw / "parts").glob("*/part.yaml"):
            part_file.write_text(
                part_file.read_text(encoding="utf-8").replace("rev: X1", "rev: A"),
                encoding="utf-8",
            )
        bom_path = self.hw / "bom.csv"
        with bom_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
            columns = rows[0].keys()
        for row in rows:
            row["rev"] = "A"
        with bom_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        verification_path = self.hw / "verification.csv"
        with verification_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
            columns = rows[0].keys()
        for row in rows:
            row["result"] = "pass"
        with verification_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def write_release(self, directory: str, rev: str) -> None:
        """A sealed package of one byte copy (bom.csv) under a schema-valid version 3 manifest."""
        release_dir = self.hw / "releases" / directory
        release_dir.mkdir(parents=True, exist_ok=True)
        content = (self.hw / "bom.csv").read_bytes()
        (release_dir / "bom.csv").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        (release_dir / "manifest.json").write_text(json.dumps({
            "schema_version": 3, "record_version": 2, "product_id": "lamp-arm", "rev": rev,
            "released_at": "2026-10-26T00:00:00Z",
            "frozen": {"version": "v1", "manifest_sha256": "0" * 64, "decision": "DR-001", "evidence_sha256": "0" * 64},
            "approval": "DR-001",
            "files": {"bom.csv": {"sha256": digest, "original": "hw/bom.csv"}},
            "manufacturing": {}, "bundles": [], "checks": {"record-validation": "pass"},
            "acceptance": {"evidence_sha256": "0" * 64, "inventory": {"hw/bom.csv": digest},
                           "scope": {"service_class": "fit_assembly", "purpose": "TST", "operating_limits": [],
                                     "exclusions": [], "trial": False},
                           "claims": {}, "waivers": [], "open_obligations": []},
        }), encoding="utf-8")

    def test_accepts_release_with_existing_files_and_verified_released_parts(self) -> None:
        self.make_release_ready()
        self.write_release("A", "A")
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # A record version 2 verification pass is a summary of ledger evidence, not evidence: without
        # hw/manufacturing/evidence.yaml entries each pass row warns (an error from the design freeze on).
        with (self.hw / "verification.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(item["code"], item["severity"], item["path"].rsplit("/", 1)[-1]) for item in report["findings"]],
                         [("evidence.csv_unsupported", "warning", f"verification.csv:{line}")
                          for line in range(2, len(rows) + 2)])

    def test_rejects_release_with_mismatched_directory_planned_checks_and_prototype(self) -> None:
        self.write_release("B", "A")
        result, report = self.validate()
        findings = json.dumps(report["findings"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("directory name does not match manifest rev", findings)
        self.assertIn("verification result must be pass or waived", findings)
        self.assertIn("release cannot include prototype revision X1", findings)

    def test_rejects_part_with_missing_decision_reference(self) -> None:
        path = self.hw / "parts" / "LMP-0001" / "part.yaml"
        text = path.read_text(encoding="utf-8").replace("decisions: [DR-002, DR-004]", "decisions: [DR-999]")
        path.write_text(text, encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("decision DR-999 has no record", json.dumps(report["findings"]))

    def test_validates_optional_concept_handoff(self) -> None:
        source = CONCEPT / "assets" / "examples" / "lamp-arm.scene.json"
        scene = json.loads(source.read_text(encoding="utf-8"))
        scene["chosen"] = scene["variants"][0]["id"]
        scene_path = self.hw / "concept" / "lamp-arm.scene.json"
        scene_path.parent.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scene_path.parent, ignore_errors=True)
        scene_path.write_text(json.dumps(scene), encoding="utf-8")
        handoff_path = scene_path.parent / "handoff.json"
        handoff = subprocess.run(
            [sys.executable, str(CONCEPT / "scripts" / "concept.py"), "handoff", str(scene_path), "--out", str(handoff_path)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(handoff.returncode, 0, handoff.stdout + handoff.stderr)
        self.assertTrue(json.loads(handoff.stdout)["ok"])
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["findings"], [])

    def test_reports_info_when_concept_handoff_schema_is_missing(self) -> None:
        handoff_path = self.hw / "concept" / "handoff.json"
        handoff_path.parent.mkdir(parents=True)
        handoff_path.write_text("{}", encoding="utf-8")
        validator_copy = self.project / "skill" / "scripts" / "validate.py"
        validator_copy.parent.mkdir(parents=True)
        shutil.copy2(VALIDATOR, validator_copy)
        for sibling in ("assembly_data.py", "assembly_operations.py", "design_rules.py", "layout.py",
                        "manufacturing_intent.py", "evidence.py", "evidence_paths.py", "impact.py", "selection.py",
                        "schema_registry.py", "yaml_loader.py", "emit.py", "decision_records.py", "guidelines.py"):
            shutil.copy2(VALIDATOR.with_name(sibling), validator_copy.with_name(sibling))
        shutil.copytree(SKILL / "schemas", self.project / "skill" / "schemas")
        shutil.copy2(SKILL / "layout.json", self.project / "skill" / "layout.json")
        coupling_copy = self.project / "cad-motion" / "scripts" / "coupling.py"  # the sibling skill's module
        coupling_copy.parent.mkdir(parents=True)
        shutil.copy2(SKILL.parent / "cad-motion" / "scripts" / "coupling.py", coupling_copy)
        result, report = self.validate(validator_copy)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(item["severity"] == "info" and "skipped handoff schema validation" in item["message"] for item in report["findings"]))

    def edit_assembly(self, old: str, new: str) -> None:
        path = self.hw / "assembly.yaml"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def messages(self) -> list[str]:
        result, report = self.validate()
        self.assertEqual(result.returncode, 1, result.stdout)
        return [item["message"] for item in report["findings"]]

    def anchor_on(self, decision: str, status: str | None, change: bool = False) -> list[str]:
        """The flow decision messages for the example frozen at an unbuilt v1 by `decision` (or with an
        open change it approves), its record written with MADR `status` (None: the record stays as it is)."""
        path = self.hw / "product.yaml"
        product = yaml.safe_load(path.read_text(encoding="utf-8"))
        product["flow"].update(stage="design_freeze", frozen={"version": "v1", "manifest_sha256": "0" * 64,
                                                              "decision": "DR-004"})
        if change:
            product["flow"]["change"] = {"decision": decision, "baseline_version": "v1", "affected_parts": ["LMP-0001"]}
        else:
            product["flow"]["frozen"]["decision"] = decision
        path.write_text(yaml.safe_dump(product, sort_keys=False), encoding="utf-8")
        if status is not None:
            write_test_decision(self.hw, decision, status=status)
        return [message for message in self.messages() if message.startswith("flow/") and "/decision " in message]

    def test_only_the_users_approval_freezes_a_version_through_a_provisional_and_void_cycle(self) -> None:
        for ident, status in (("DR-005", "proposed"), ("DR-006", "rejected"), ("DR-007", "deprecated")):
            with self.subTest(status):
                self.assertEqual(self.anchor_on(ident, status),
                                 [f"flow/frozen/decision {ident} has status {status}, not the user's approval "
                                  "(accepted); only the user's approval freezes a version"])
        self.assertEqual(self.anchor_on("DR-008", "accepted"), [])
        self.assertEqual(self.anchor_on("DR-004", None), [], "the example's own record is accepted")

    def test_a_decision_the_reader_refuses_never_approves_and_names_why(self) -> None:
        record = write_test_decision(self.hw, "DR-009")  # complete in every section but the status below
        record.write_text(record.read_text(encoding="utf-8").replace("status: accepted", "status: approved", 1),
                          encoding="utf-8")
        [message] = self.anchor_on("DR-009", None)
        self.assertTrue(message.startswith("flow/frozen/decision DR-009 is not a valid MADR record: "), message)
        self.assertIn(record.name, message)
        self.assertIn("status 'approved'", message)

    def test_conflicting_statuses_in_one_record_never_approve(self) -> None:
        record = write_test_decision(self.hw, "DR-009")
        record.write_text(record.read_text(encoding="utf-8").replace("status: accepted", "status: rejected\nstatus: accepted", 1),
                          encoding="utf-8")
        [message] = self.anchor_on("DR-009", None)
        self.assertTrue(message.startswith("flow/frozen/decision DR-009 is not a valid MADR record: "), message)
        self.assertIn("status", message)
        from modload import load_module  # noqa: PLC0415 - used by this test alone
        validate = load_module("cad_record_validate", SKILL / "scripts" / "validate.py")
        self.assertFalse(validate.decision_approves(self.hw, "DR-009"))

    def test_a_collection_as_a_front_matter_key_is_a_finding_not_a_crash(self) -> None:
        record = write_test_decision(self.hw, "DR-009")
        record.write_text(record.read_text(encoding="utf-8").replace("status: accepted", "status: accepted\n? [a, b]\n: x", 1),
                          encoding="utf-8")
        [message] = self.anchor_on("DR-009", None)
        self.assertTrue(message.startswith("flow/frozen/decision DR-009 is not a valid MADR record: "), message)
        self.assertIn(record.name, message)

    def front_matter_finding(self, front: str) -> str:
        """The one finding for record DR-009 whose status line is replaced by `front`; it must name the file."""
        record = write_test_decision(self.hw, "DR-009")
        record.write_text(record.read_text(encoding="utf-8").replace("status: accepted", front, 1), encoding="utf-8")
        [message] = self.anchor_on("DR-009", None)
        self.assertTrue(message.startswith("flow/frozen/decision DR-009 is not a valid MADR record: "), message)
        self.assertIn(record.name, message)
        self.assertLess(len(message), 1000, "a diagnostic stays bounded")
        return message

    def test_a_malformed_tagged_boolean_or_timestamp_is_a_finding_not_a_crash(self) -> None:
        for front in ("status: !!bool maybe", "status: accepted\nx: !!timestamp not-a-time"):
            with self.subTest(front):
                self.front_matter_finding(front)

    def test_an_empty_tagged_number_is_a_finding_not_a_crash(self) -> None:
        for front in ('status: accepted\nx: !!int ""', 'status: accepted\nx: !!float ""'):
            with self.subTest(front):
                self.front_matter_finding(front)

    def test_a_sequence_tagged_as_a_mapping_is_a_finding_not_a_crash(self) -> None:
        for front in ("status: accepted\nx: !!map [a]", "status: accepted\nx: !!set [a]"):
            with self.subTest(front):
                self.front_matter_finding(front)

    def test_excessive_front_matter_nesting_is_a_finding_not_a_crash(self) -> None:
        self.front_matter_finding("status: accepted\nx: " + "[" * 5000 + "]" * 5000)

    def test_a_very_long_sexagesimal_float_is_a_finding_not_a_crash(self) -> None:
        self.front_matter_finding("status: accepted\nx: !!float " + ":".join(["1"] * 6000))

    def test_oversized_diagnostic_values_stay_bounded_in_the_finding(self) -> None:
        unknown = "".join(f"unknown-key-{index}-{'k' * 100}: x\n" for index in range(200))
        for front in ("status: accepted\nsuperseded-by: [" + "d" * 5000 + "]",
                      "status: !!bool " + "b" * 5000,
                      "status: accepted\n" + unknown.rstrip("\n")):
            with self.subTest(front[:30]):
                self.front_matter_finding(front)

    def test_a_huge_integer_key_or_status_is_a_bounded_finding_not_a_crash(self) -> None:
        huge = "0x" + "f" * 4000
        for front in (f"status: accepted\n? {huge}\n: x", f"status: {huge}"):
            with self.subTest(front[:20]):
                self.front_matter_finding(front)

    def test_an_old_form_decision_file_is_no_record(self) -> None:
        (self.hw / "decisions" / "DR-009.md").write_text("Status: APPROVED\n\n# DR-009 Gate\n", encoding="utf-8")
        self.assertEqual(self.anchor_on("DR-009", None),
                         ["flow/frozen/decision DR-009 has no record: expected hw/decisions/DR-009-<slug>.md"])

    def test_a_provisional_decision_never_opens_an_engineering_change(self) -> None:
        self.assertEqual(self.anchor_on("DR-005", "proposed", change=True),
                         ["flow/change/decision DR-005 has status proposed, not the user's approval (accepted); "
                          "only the user's approval opens an engineering change"])

    def test_a_bare_pn_placed_three_times_is_an_ambiguous_joint_endpoint(self) -> None:
        self.edit_assembly('child: "@ring-elbow"', "child: LMP-0011")
        self.assertTrue(any("ambiguous" in m and "@ring-base, @ring-elbow, @ring-head" in m for m in self.messages()))

    def test_an_unknown_occurrence_reference_is_rejected(self) -> None:
        self.edit_assembly('child: "@ring-head"', 'child: "@ring-tail"')
        self.assertTrue(any("@ring-tail names no occurrence" in m for m in self.messages()))

    def test_a_repeated_part_must_be_part_local(self) -> None:
        path = self.hw / "parts" / "LMP-0011" / "part.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("model_frame: part_local\n", ""), encoding="utf-8")
        self.assertTrue(any("LMP-0011 is placed 3 times but its model_frame is assembly_zero" in m for m in self.messages()))

    def test_bom_quantity_must_match_the_occurrence_count(self) -> None:
        path = self.hw / "bom.csv"
        path.write_text(path.read_text(encoding="utf-8").replace("1,LMP-0011,X1,3,", "1,LMP-0011,X1,2,"), encoding="utf-8")
        self.assertTrue(any("BOM quantity of LMP-0011 is 2" in m and "places it 3 times" in m for m in self.messages()))

    def test_an_explicit_pn_cannot_also_be_a_legacy_member(self) -> None:
        self.edit_assembly("LMP-0009, LMP-0010]", "LMP-0009, LMP-0010, LMP-0011]")
        self.assertTrue(any("lists LMP-0011 in parts, but LMP-0011 has explicit occurrences" in m for m in self.messages()))

    def test_an_occurrence_needs_finite_placement_and_one_group(self) -> None:
        self.edit_assembly("translation_mm: [0, 0, 21.495]", "translation_mm: [0, 0, .nan]")
        self.edit_assembly('occurrences: ["@ring-base", ', "occurrences: [")
        found = self.messages()
        self.assertTrue(any("translation_mm" in m for m in found), found)

    def test_an_occurrence_belonging_to_no_group_is_rejected(self) -> None:
        self.edit_assembly('occurrences: ["@ring-base", ', "occurrences: [")
        self.assertTrue(any("@ring-base belongs to no subassembly" in m for m in self.messages()))

    def test_a_heat_set_pair_naming_an_occurrence_of_another_part_is_rejected(self) -> None:
        path = self.hw / "interfaces.yaml"
        text = path.read_text(encoding="utf-8").replace("pair: [LMP-0004, LMP-0010]", 'pair: [LMP-0004, "@ring-hub"]')
        path.write_text(text, encoding="utf-8")
        self.assertTrue(any("@ring-hub names no occurrence" in m for m in self.messages()))

    def add_clearance(self, pair: str, minimum: object = 1.0, kind: str = "clearance", number: int = 6) -> None:
        """Append a clearance requirement interface IF-00<number> to the lamp example's interfaces.yaml."""
        path = self.hw / "interfaces.yaml"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"  - {{id: IF-00{number}, from: head, to: base_plate, type: {kind}, "
                         f"description: The head clears the base plate., pair: {pair}, min_clearance_mm: {minimum}, "
                         f"{PLACEHOLDER_FUNCTIONAL}}}\n")

    def test_a_clearance_requirement_on_two_occurrences_validates(self) -> None:
        self.add_clearance('[LMP-0004, "@ring-base"]')
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["findings"], [])

    def test_a_clearance_requirement_rejects_an_unresolved_reference(self) -> None:
        self.add_clearance('[LMP-0004, "@ring-tail"]', number=6)
        self.add_clearance("[LMP-0099, LMP-0001]", number=7)
        self.add_clearance("[LMP-0011, LMP-0001]", number=8)
        found = self.messages()
        self.assertTrue(any(m.startswith("interfaces/5/pair:") and "@ring-tail names no occurrence" in m for m in found), found)
        self.assertIn("interfaces/6/pair: part LMP-0099 does not exist", found)
        self.assertTrue(any(m.startswith("interfaces/7/pair:") and "ambiguous" in m for m in found), found)

    def test_a_clearance_requirement_needs_a_positive_minimum_and_its_type(self) -> None:
        for minimum, kind, expected in ((0, "clearance", "less than or equal to the minimum of 0"),
                                        (-1.5, "clearance", "less than or equal to the minimum of 0"),
                                        (2.0, "mounting", "'clearance' was expected")):
            with self.subTest(minimum=minimum, kind=kind):
                (self.hw / "interfaces.yaml").write_bytes(self.migrated_interfaces)
                self.add_clearance("[LMP-0004, LMP-0001]", minimum, kind)
                found = self.messages()
                self.assertTrue(any(m.startswith("interfaces/5") and expected in m for m in found), found)

    def test_a_clearance_pair_without_a_minimum_is_rejected(self) -> None:
        path = self.hw / "interfaces.yaml"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("  - {id: IF-006, from: head, to: base, type: clearance, description: Gap., "
                         f"pair: [LMP-0004, LMP-0001], {PLACEHOLDER_FUNCTIONAL}}}\n")
        self.assertTrue(any("'min_clearance_mm' is a dependency of 'pair'" in m for m in self.messages()))

    def test_a_clearance_requirement_on_one_occurrence_is_rejected(self) -> None:
        self.add_clearance('["@ring-base", "@ring-base"]')
        self.assertTrue(any(m.startswith("interfaces/5/pair:") and "non-unique" in m for m in self.messages()))
        # A PN placed once and its @id name one occurrence: make @ring-head the one, part-local LMP-0010.
        (self.hw / "interfaces.yaml").write_bytes(self.migrated_interfaces)
        self.edit_assembly("  - id: ring-head\n    PN: LMP-0011", "  - id: ring-head\n    PN: LMP-0010")
        self.edit_assembly("LMP-0009, LMP-0010]", "LMP-0009]")
        with (self.hw / "parts" / "LMP-0010" / "part.yaml").open("a", encoding="utf-8") as part:
            part.write("model_frame: part_local\n")
        self.add_clearance('[LMP-0010, "@ring-head"]')
        self.assertIn("interfaces/5/pair: LMP-0010 and @ring-head are the same occurrence", self.messages())

    def test_an_occurrence_pair_takes_one_clearance_requirement(self) -> None:
        self.add_clearance('[LMP-0004, "@ring-base"]', number=6)
        self.add_clearance('["@ring-base", LMP-0004]', 3.0, number=7)
        self.assertIn('interfaces/6/pair: @ring-base / LMP-0004 already has a clearance requirement at interfaces/5',
                      self.messages())

    HEAD = "    min: -45\n    max: 45\n    nominal: 0\n"  # J-003, the head joint
    ELBOW = "    min: -120\n    max: 0\n    nominal: -30\n"  # J-002, the elbow joint

    def couple_head(self, coupling: str) -> None:
        self.edit_assembly(self.HEAD, f"    min: -45\n    max: 45\n    coupling: {coupling}\n")

    def test_a_coupled_joint_validates_without_a_nominal(self) -> None:
        self.couple_head("{type: linear, driver: J-002, ratio: 0.25}")
        result, report = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["findings"], [])

    def test_a_self_coupling_is_rejected(self) -> None:
        self.couple_head("{type: linear, driver: J-003, ratio: 1}")
        self.assertIn("J-003 coupling: J-003 is coupled to itself", self.messages())

    def test_a_coupling_cycle_is_rejected_by_name(self) -> None:
        self.couple_head("{type: linear, driver: J-002, ratio: 1}")
        self.edit_assembly(self.ELBOW, "    min: -120\n    max: 0\n    coupling: {type: linear, driver: J-003, ratio: 1}\n")
        self.assertTrue(any(m.startswith("coupling cycle: J-00") and "(each joint drives the next)" in m
                            for m in self.messages()))

    def test_a_missing_driver_or_profile_is_rejected(self) -> None:
        self.couple_head("{type: linear, driver: J-099, ratio: 1}")
        self.assertIn("J-003 coupling: driver J-099 is not a joint of this assembly", self.messages())
        self.couple_head_again("{type: periodic_table, driver: J-002, profile: tilt}")
        self.assertIn("J-003 coupling: profile 'tilt' is not in `profiles`", self.messages())

    def couple_head_again(self, coupling: str) -> None:
        path = self.hw / "assembly.yaml"
        lines = path.read_text(encoding="utf-8").splitlines()
        index = next(i for i, line in enumerate(lines) if line.startswith("    coupling: "))
        lines[index] = f"    coupling: {coupling}"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_derived_coordinate_beyond_its_limits_is_rejected(self) -> None:
        self.couple_head("{type: linear, driver: J-002, ratio: 1}")  # J-003 would follow J-002 down to -120
        self.assertIn("J-003 follows J-002 (linear ratio 1 offset 0) reaches -120 deg at J-002 = -120 deg, "
                      "outside its limits [-45, 45]", self.messages())

    def test_a_coupled_joint_cannot_also_author_a_nominal(self) -> None:
        self.edit_assembly(self.HEAD, self.HEAD + "    coupling: {type: linear, driver: J-002, ratio: 0.25}\n")
        self.assertTrue(any(m.startswith("joints/2: ") and "is valid under each of" in m for m in self.messages()))

    def test_a_fixed_joint_cannot_be_coupled(self) -> None:
        assembly = yaml.safe_load((self.hw / "assembly.yaml").read_text(encoding="utf-8"))
        fixed = next(joint for joint in assembly["joints"] if joint["type"] == "fixed")
        del fixed["nominal"]
        fixed["coupling"] = {"type": "linear", "driver": "J-001", "ratio": 1}
        (self.hw / "assembly.yaml").write_text(yaml.safe_dump(assembly, sort_keys=False), encoding="utf-8")
        self.assertTrue(any(f"joints/3: 'nominal' is a required property" == m for m in self.messages()))

    def author_motion_case(self, case: str, to: str) -> Path:
        path = self.hw / "motion" / "cases" / case / "plan.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(f"schema: 1\nsegments:\n  - id: reach\n    to: {to}\n    camera: iso\n", encoding="utf-8")
        return path.parent

    def motion_findings(self) -> tuple[int, list[tuple[str, str, str]]]:
        result, report = self.validate()
        return result.returncode, [(item["path"], item["severity"], item["message"]) for item in report["findings"]
                                   if "/motion/" in item["path"]]

    def test_an_authored_motion_case_inside_the_joint_limits_validates(self) -> None:
        self.author_motion_case("reach-out", "{J-001: 80, J-002: -120}")
        self.assertEqual(self.motion_findings(), (0, []))

    def test_motion_cases_are_checked_when_a_member_is_left_for_attach(self) -> None:
        # Without J-010 the base ring has no joint path to the ground, which `--attach` connects at run time.
        self.edit_assembly('  - id: J-010\n    type: fixed\n    parent: LMP-0005\n    child: "@ring-base"\n'
                           "    axis: [0, 0, 1]\n    origin: [0, 0, 40]\n    min: 0\n    max: 0\n    nominal: 0\n", "")
        self.author_motion_case("reach-out", "{J-001: 80, J-002: -120}")
        too_far = str(self.author_motion_case("too-far", "{J-002: 30}"))
        self.assertEqual(self.motion_findings(),
                         (1, [(too_far, "error", "segments[0] (reach): J-002 = 30 deg is outside [-120, 0]")]))

    def test_an_authored_motion_case_names_each_target_the_render_would_refuse(self) -> None:
        self.couple_head("{type: linear, driver: J-002, ratio: 0.25}")
        case = str(self.author_motion_case("reach-out", "{J-002: 30, J-003: 10, J-099: 1}"))
        code, findings = self.motion_findings()
        self.assertEqual(code, 1)
        problems = {message for path, severity, message in findings if path == case and severity == "error"}
        self.assertIn("segments[0] (reach): J-002 = 30 deg is outside [-120, 0]", problems)
        self.assertTrue(any(m.startswith("segments[0] (reach): J-003 follows J-002") and m.endswith("move its driver instead")
                            for m in problems), problems)
        self.assertTrue(any(m.startswith("segments[0] (reach): J-099 is not a joint of this record") for m in problems))

    def test_the_generated_default_motion_case_cannot_be_authored(self) -> None:
        case = self.author_motion_case("default", "{J-001: 0}")
        for contents in ("an authored plan", "no plan"):
            if contents == "no plan":
                (case / "plan.yaml").replace(case / ".gitkeep")
            result, report = self.validate()
            self.assertEqual(result.returncode, 1, f"{contents}: {result.stderr}")
            self.assertTrue(any(item["path"] == str(case) and "case `default` is reserved" in item["message"]
                                for item in report["findings"]), f"{contents}: {report['findings']}")

    def test_a_joint_without_nominal_is_reported_beside_an_authored_motion_case(self) -> None:
        self.edit_assembly(self.ELBOW, "    min: -120\n    max: 0\n")
        self.author_motion_case("reach-out", "{J-002: -60}")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(any(item["message"].startswith("joints/1: ") for item in report["findings"]))


class ApprovalProblem(unittest.TestCase):
    """validate.approval_problem: why a decision cannot approve, None when it does. decision_approves is its boolean."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-approval-")
        self.addCleanup(temporary.cleanup)
        self.hw = Path(temporary.name) / "hw"
        from modload import load_module  # noqa: PLC0415 - used by this class alone
        self.validate = load_module("cad_record_validate", SKILL / "scripts" / "validate.py")

    def test_an_accepted_decision_approves(self) -> None:
        write_test_decision(self.hw, "DR-001", status="accepted")
        self.assertIsNone(self.validate.approval_problem(self.hw, "DR-001"))
        self.assertTrue(self.validate.decision_approves(self.hw, "DR-001"))

    def test_any_other_status_names_itself_as_the_reason(self) -> None:
        for status in ("proposed", "rejected", "deprecated"):
            with self.subTest(status):
                write_test_decision(self.hw, "DR-002", status=status)
                self.assertEqual(self.validate.approval_problem(self.hw, "DR-002"),
                                 f"has status {status}, not the user's approval (accepted)")
                self.assertFalse(self.validate.decision_approves(self.hw, "DR-002"))

    def test_a_missing_or_malformed_decision_never_approves_and_says_why(self) -> None:
        write_test_decision(self.hw, "DR-003")
        self.assertIn("has no record", self.validate.approval_problem(self.hw, "DR-004"))
        self.assertIn("is not a DR-### id", self.validate.approval_problem(self.hw, "DR-3"))
        decision_path(self.hw, "DR-003").write_text("# not a record\n", encoding="utf-8")
        self.assertIn("is not a valid MADR record", self.validate.approval_problem(self.hw, "DR-003"))


class LoadDirection(unittest.TestCase):
    def test_a_load_direction_beyond_float_range_is_a_finding_not_a_crash(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-load-direction-")
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name) / "project"
        documents = fx.tst_record()
        documents["product.yaml"]["use_case"]["loads"][0]["direction"] = [10 ** 400, 0, 0]
        fx.write_record(project / "hw", documents)
        result = subprocess.run([sys.executable, str(VALIDATOR), str(project)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stderr)
        codes = [item.get("code") for item in json.loads(result.stdout)["findings"] if item["severity"] == "error"]
        self.assertIn("load.direction_not_unit", codes)

    def test_a_non_finite_component_beside_an_oversized_one_is_only_a_number_finding(self) -> None:
        for direction in ([10 ** 400, float("nan"), 0], [10 ** 400, float("inf"), 0]):
            with self.subTest(direction):
                temporary = tempfile.TemporaryDirectory(prefix="cad-record-load-direction-")
                self.addCleanup(temporary.cleanup)
                project = Path(temporary.name) / "project"
                documents = fx.tst_record()
                documents["product.yaml"]["use_case"]["loads"][0]["direction"] = direction
                fx.write_record(project / "hw", documents)
                result = subprocess.run([sys.executable, str(VALIDATOR), str(project)], capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 1, result.stderr)
                codes = [item.get("code") for item in json.loads(result.stdout)["findings"] if item["severity"] == "error"]
                self.assertEqual(codes.count("intent.number"), 1, codes)
                self.assertNotIn("load.direction_not_unit", codes)


class RecordVersion(unittest.TestCase):
    """Row 19: a record of an unsupported version stays readable and is never certified; gated actions name the version."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-version-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.hw = self.project / "hw"

    def validate(self) -> tuple[int, list[dict[str, object]]]:
        result = subprocess.run([sys.executable, str(VALIDATOR), str(self.project)], capture_output=True, text=True,
                                timeout=120)
        return result.returncode, result_payload(result.stdout)["findings"]

    def test_an_unsupported_record_version_is_named_not_guessed(self) -> None:
        for version in ("record_version: 1", "record_version: 3", None):
            with self.subTest(version=version):
                shutil.rmtree(self.hw, ignore_errors=True)
                fx.migrated_copy(EXAMPLE, self.hw)
                product = self.hw / "product.yaml"
                text = product.read_text(encoding="utf-8")
                product.write_text(text.replace("record_version: 2\n", f"{version}\n" if version else ""), encoding="utf-8")
                before = tree(self.hw)
                code, findings = self.validate()
                self.assertEqual(code, 1)
                self.assertIn("record.version_unsupported", [item.get("code") for item in findings])
                self.assertEqual(tree(self.hw), before, "reading a record rewrites nothing")

    def test_an_unsupported_record_version_still_reports_its_other_findings(self) -> None:
        fx.migrated_copy(EXAMPLE, self.hw)
        product = self.hw / "product.yaml"
        product.write_text(product.read_text(encoding="utf-8").replace("record_version: 2", "record_version: 1"), encoding="utf-8")
        part = self.hw / "parts" / "LMP-0001" / "part.yaml"
        part.write_text(part.read_text(encoding="utf-8").replace("decisions: [DR-002, DR-004]", "decisions: [DR-999]"),
                        encoding="utf-8")
        code, findings = self.validate()
        messages = json.dumps(findings)
        self.assertIn("record.version_unsupported", messages)
        self.assertIn("decision DR-999 has no record", messages)


class LedgerEvidence(unittest.TestCase):
    """The record's own evidence checks: waivers must stand, and a CSV result is never evidence."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-evidence-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.hw = fx.migrated_copy(EXAMPLE, self.project / "hw")

    def findings(self) -> list[dict[str, object]]:
        result = subprocess.run([sys.executable, str(VALIDATOR), str(self.project)], capture_output=True, text=True,
                                timeout=120)
        return result_payload(result.stdout)["findings"]

    def test_an_unapproved_waiver_is_an_error(self) -> None:
        subject = {"PN": "LMP-0001"}
        entry = {"id": "EV-001", "requirement": "REQ-001", "subject": subject, "level": "physically_validated",
                 "result": "waived", "method": "TST",
                 "waiver": {"decision": "DR-099", "subject": subject, "criterion": "TST", "operating_envelope": "TST",
                            "reason": "TST", "consequence": "TST", "authority": "TST", "until_change": "TST"}}
        (self.hw / "manufacturing" / "evidence.yaml").write_text(yaml.safe_dump({"entries": [entry]}), encoding="utf-8")
        found = [item for item in self.findings() if str(item.get("code", "")).startswith("waiver.")]
        self.assertEqual([(item["code"], item["severity"]) for item in found], [("waiver.unapproved", "error")])
        self.assertTrue(found[0]["path"].endswith("evidence.yaml#/entries/0/waiver"))

    def test_a_ledger_entry_that_cites_scratch_is_refused_by_the_validator(self) -> None:
        subject = {"PN": "LMP-0001"}
        entry = {"id": "EV-001", "requirement": "REQ-001", "subject": subject, "level": "simulation_supported",
                 "result": "planned", "method": "BRepExtrema script .cad-work/layout/regional.py"}
        (self.hw / "manufacturing" / "evidence.yaml").write_text(yaml.safe_dump({"entries": [entry]}), encoding="utf-8")
        found = [item for item in self.findings() if item.get("code") == "evidence.scratch_path"]
        self.assertEqual([item["severity"] for item in found], ["error"])
        self.assertIn(".cad-work/layout/regional.py", found[0]["message"])
        self.assertTrue(found[0]["path"].endswith("evidence.yaml#/entries/EV-001"))

    def test_a_csv_pass_without_ledger_evidence_warns_and_blocks_from_the_freeze(self) -> None:
        path = self.hw / "verification.csv"
        path.write_text(path.read_text(encoding="utf-8").replace(",planned,", ",pass,", 1), encoding="utf-8")
        found = [item for item in self.findings() if item.get("code") == "evidence.csv_unsupported"]
        self.assertEqual([item["severity"] for item in found], ["warning"])
        product = self.hw / "product.yaml"
        data = yaml.safe_load(product.read_text(encoding="utf-8"))
        data["flow"]["stage"] = "design_freeze"
        product.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        found = [item for item in self.findings() if item.get("code") == "evidence.csv_unsupported"]
        self.assertEqual([item["severity"] for item in found], ["error"])



class AssemblyInventory(unittest.TestCase):
    """validate.assembly_inventory called directly: through the CLI the schema refuses a wrong shape first."""

    def inventory_findings(self, assembly: dict[str, object]) -> tuple[object, list[dict[str, str]]]:
        from modload import load_module  # noqa: PLC0415 - used by this class alone

        validate = load_module("cad_record_validate", SKILL / "scripts" / "validate.py")
        findings: list[dict[str, str]] = []
        return validate.assembly_inventory(assembly, "hw/assembly.yaml", {}, findings), findings

    def test_a_non_mapping_subassembly_is_a_named_inventory_finding(self) -> None:
        inventory, findings = self.inventory_findings({"subassemblies": ["main"]})
        self.assertIsNone(inventory)
        self.assertEqual([(item["path"], item["severity"], item["message"]) for item in findings],
                         [("hw/assembly.yaml", "error", "subassemblies/0 must be a mapping")])


class SubjectOccurrences(unittest.TestCase):
    """validate_project reports a subject occurrence that is unknown or belongs to another part."""

    def findings(self, subject: dict[str, object]) -> list[dict[str, object]]:
        from modload import load_module  # noqa: PLC0415 - used by this class alone

        validate = load_module("cad_record_validate", SKILL / "scripts" / "validate.py")
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-occurrence-")
        self.addCleanup(temporary.cleanup)
        hw = Path(temporary.name) / "project" / "hw"
        fx.write_record(hw, fx.with_subject_occurrences(fx.tst_record(), subject))
        return [f for f in validate.validate_project(hw.parent) if f.get("code") == "intent.reference"]

    def test_an_occurrence_of_another_part_is_a_finding(self) -> None:
        [item] = self.findings({"PN": "TST-0001", "occurrence": "@left"})
        self.assertIn("@left is an occurrence of TST-0003, not TST-0001", str(item["message"]))

    def test_an_unknown_occurrence_is_a_finding(self) -> None:
        [item] = self.findings({"PN": "TST-0003", "occurrence": "@ghost"})
        self.assertIn("@ghost names no occurrence", str(item["message"]))

    def test_the_part_own_occurrence_is_clean(self) -> None:
        self.assertEqual(self.findings({"PN": "TST-0003", "occurrence": "@left"}), [])

    def test_an_omitted_occurrence_of_a_single_occurrence_part_is_clean(self) -> None:
        self.assertEqual(self.findings({"PN": "TST-0001"}), [])


class FlowProfileFindings(unittest.TestCase):
    """validate.py reports the project-start profile through flow_profile_gate. The lamp example is clean; each
    test below makes one edit to its product.yaml."""

    validate = ValidateProject.validate

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-record-profile-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.hw = self.project / "hw"
        fx.migrated_copy(EXAMPLE, self.hw)
        self.product_path = self.hw / "product.yaml"

    def edit_product(self, old: str, new: str) -> None:
        text = self.product_path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, old)
        self.product_path.write_text(text.replace(old, new), encoding="utf-8")

    def drop_profile(self) -> None:
        lines = self.product_path.read_text(encoding="utf-8").splitlines(keepends=True)
        start = lines.index("  profile:\n")
        end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("    "))
        self.product_path.write_text("".join(lines[:start] + lines[end:]), encoding="utf-8")

    def flow_findings(self) -> list[dict[str, object]]:
        _result, report = self.validate()
        return [item for item in report["findings"] if str(item.get("code", "")).startswith("flow.")]

    def test_the_example_profile_is_clean(self) -> None:
        self.assertEqual(self.flow_findings(), [])

    def test_a_missing_profile_blocks_from_specify_on(self) -> None:
        self.drop_profile()
        found = self.flow_findings()
        self.assertEqual([(item["code"], item["severity"]) for item in found], [("flow.profile", "error")])
        self.assertIn("flow.profile is missing", str(found[0]["message"]))

    def test_a_bare_word_the_yaml_read_as_a_boolean_is_reported_with_its_fix(self) -> None:
        self.edit_product('critique: "light"', "critique: off")
        found = [item for item in self.flow_findings() if item["code"] == "flow.profile_unquoted"]
        self.assertEqual(len(found), 1, found)
        self.assertIn('critique: "off"', str(found[0]["action"]))

    def test_a_conflict_with_the_record_is_an_error_at_layout_and_a_warning_before(self) -> None:
        self.edit_product("enabled: false", "enabled: true\n      mode: accurate")  # fea on, critique light
        found = self.flow_findings()
        self.assertEqual([(item["code"], item["severity"]) for item in found], [("flow.conflict", "error")])
        self.assertIn("fea is on, but critique is light", str(found[0]["message"]))
        self.edit_product("stage: layout\n  recheck_from: layout", "stage: specify\n  recheck_from: specify")
        self.assertEqual([item["severity"] for item in self.flow_findings() if item["code"] == "flow.conflict"], ["warning"])

    def make_first_interface_critical(self) -> None:
        path = self.hw / "interfaces.yaml"
        text = path.read_text(encoding="utf-8")
        old = "critical: {status: unknown, reason: not recorded before record version 2, question: Q-023}"
        fact = ('critical: {status: known, value: true, basis: user_declared, '
                'sources: [{kind: user, reference: TST fixture, origin: TST fixture user}]}')
        self.assertEqual(text.count(old), 1)
        path.write_text(text.replace(old, fact, 1), encoding="utf-8")

    def test_a_critical_interface_conflicts_with_stack_up_off(self) -> None:
        self.edit_product('stack_up: "auto"', 'stack_up: "off"')
        self.make_first_interface_critical()
        found = self.flow_findings()
        self.assertEqual([(item["code"], item["severity"]) for item in found], [("flow.conflict", "error")])
        self.assertIn("stack_up is off", str(found[0]["message"]))

    def test_a_malformed_interfaces_file_is_a_schema_finding_and_judges_no_conflict(self) -> None:
        self.edit_product('stack_up: "auto"', 'stack_up: "off"')
        self.make_first_interface_critical()
        path = self.hw / "interfaces.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "bogus: 1\n", encoding="utf-8")
        result, report = self.validate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(any(str(item["path"]).endswith("interfaces.yaml") for item in report["findings"]))
        self.assertEqual([item for item in report["findings"] if item.get("code") == "flow.conflict"], [])


if __name__ == "__main__":
    unittest.main()
