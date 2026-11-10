#!/usr/bin/env python3
"""Integration tests for the staged CAD recheck stamp and git hook.

The project is a small record version 2 record: a confirmed use case and one CNC part. A stamp needs the
evidence packet of the staged record (cad-record evidence.py); the tests build it from a synthetic
producer packet, so they need no CAD kernel. Regression matrix row 13 (stamp refuses stale or missing
expected checks) and the stamp boundary cases."""

from __future__ import annotations

import importlib.util
import os
import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


SKILL = Path(__file__).resolve().parents[1]
STAMP = SKILL / "scripts" / "stamp.py"
INSTALLER = SKILL / "scripts" / "install-hook.py"
ACCEPTANCE = SKILL.parent / "cad-record" / "tests" / "fixtures" / "acceptance.py"


def load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


stamp_module = load("cad_recheck_stamp", STAMP)
acceptance = load("cad_record_acceptance_fixtures", ACCEPTANCE)
evidence, fx = acceptance.evidence, acceptance.fx
LEGACY_PRODUCT = "id: test-project\nflow:\n  stage: {stage}\n  recheck_from: {threshold}\n"


class RecheckHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        # The tests' global git config, so a test can set a global core.hooksPath as Git Defender does.
        self.global_config = Path(self.temporary.name) / "global.gitconfig"
        self.global_config.write_text("", encoding="utf-8")
        self.git("-c", "init.templateDir=", "init", "-q")
        self.git("config", "user.name", "CAD Recheck Test")
        self.git("config", "user.email", "cad-recheck@example.invalid")
        self.write_product("specify", "layout")
        self.add_part()
        self.git("add", "hw")
        self.git("commit", "-qm", "initial design")
        hooks_dir = self.root / ".git" / "hooks"
        if hooks_dir.is_dir():
            for hook in hooks_dir.iterdir():
                if not hook.name.endswith(".sample"):
                    hook.unlink()
        installed = self.install_hook()
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(self.global_config)
        return env

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, check=check, env=self.git_env()
        )

    def run_python(self, script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env=self.git_env(),
        )

    def add_part(self, pn: str = "PRT-0001", title: str = "Fixture") -> Path:
        part = self.root / "hw" / "parts" / pn
        part.mkdir(parents=True, exist_ok=True)
        intent = fx.intent([{"function": "cosmetic"}], fx.not_applicable("TST"), [fx.operation("mill", "cnc", "tst_cnc")],
                           service=fx.not_applicable("TST"), critical_surfaces=fx.not_applicable("TST"))
        record = {"PN": pn, "rev": "X1", "title": title, "material": "Steel", "process": "cnc", "finish": "As machined",
                  "mass_target": None, "make_buy": "make", "intent": intent}
        (part / "part.yaml").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        return part

    def write_product(self, stage: str, threshold: str | None, *, legacy: bool = False) -> None:
        product = self.root / "hw" / "product.yaml"
        product.parent.mkdir(parents=True, exist_ok=True)
        if legacy:
            product.write_text(LEGACY_PRODUCT.format(stage=stage, threshold=threshold), encoding="utf-8")
            return
        flow = {"stage": stage, **({"recheck_from": threshold} if threshold else {})}
        data = {"record_version": 2, "id": "test-project", "flow": flow,
                "use_case": acceptance.confirmed_use_case("appearance_concept")}
        product.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        assembly = self.root / "hw" / "assembly.yaml"
        if not assembly.exists():
            assembly.write_text("operations: []\n", encoding="utf-8")

    def stage_product(self, stage: str, threshold: str = "layout") -> None:
        self.write_product(stage, threshold)
        self.git("add", "hw/product.yaml")

    def install_hook(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_python(INSTALLER, "--project", str(self.root), *args)

    def commit(self) -> subprocess.CompletedProcess[str]:
        return self.git("commit", "-m", "design batch", check=False)

    def evidence_packet(self, stage: str, *, outcome: str = "pass") -> Path:
        """The evidence packet of the staged record at `stage`: every software check `outcome` in a
        synthetic producer packet of a built v1."""
        staged = stamp_module.staged_record(self.root)
        producer = evidence.ProducerPacket("hw/checks/v1/tst", "0" * 64,
                                           acceptance.producer_packet(staged.record, "v1", outcome=outcome))
        packet = evidence.assess(staged.record, version="v1", manifest_sha256="0" * 64, packets=[producer],
                                 selection_sha256=staged.selection_sha256).packet(stage, staged.record_sha256)
        checks = self.root / ".checks.json"
        checks.write_text(json.dumps(packet), encoding="utf-8")
        return checks

    def stamp(self, *, passed: bool, stage: str, outcome: str = "pass") -> dict[str, object]:
        args = ["--stage", stage, "--checks", str(self.evidence_packet(stage, outcome=outcome))]
        if passed:
            args.append("--passed")
        result = self.run_python(STAMP, *args, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git("add", "hw/.cad/recheck.json")
        return json.loads(result.stdout)

    def test_staged_release_requires_stamp_even_when_worktree_is_below_threshold(self) -> None:
        self.write_product("release", "layout")
        self.git("add", "hw/product.yaml")
        self.write_product("specify", "layout")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no staged hw/.cad/recheck.json", result.stderr)

    def test_staged_pre_threshold_does_not_require_stamp_even_when_worktree_is_at_threshold(self) -> None:
        self.write_product("specify", "layout")
        self.git("add", "hw/product.yaml")
        self.write_product("release", "layout")
        self.add_part(title="Staged edit")
        self.git("add", "hw/parts/PRT-0001/part.yaml")
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_recheck_threshold_is_refused(self) -> None:
        product = self.root / "hw" / "product.yaml"
        product.write_text("id: test-project\nflow:\n  stage: release\n", encoding="utf-8")
        self.git("add", "hw/product.yaml")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or unknown flow.recheck_from", result.stderr)

    def test_unparsable_staged_product_is_refused(self) -> None:
        product = self.root / "hw" / "product.yaml"
        product.write_text("id: test-project\nflow: [\n", encoding="utf-8")
        self.git("add", "hw/product.yaml")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot parse staged hw/product.yaml", result.stderr)

    def test_existing_hooks_path_is_refused_without_force(self) -> None:
        self.git("config", "core.hooksPath", ".husky")
        before = self.git("config", "--local", "--get", "core.hooksPath").stdout
        result = self.install_hook()
        self.assertEqual(result.returncode, 2)
        self.assertIn("core.hooksPath is set elsewhere", result.stderr)
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath").stdout, before)

    def test_existing_hooks_path_is_replaced_with_force(self) -> None:
        self.git("config", "core.hooksPath", ".husky")
        result = self.install_hook("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")

    def test_a_global_hooks_path_is_refused_even_with_force_and_names_the_verify_command(self) -> None:
        managed = Path(self.temporary.name) / "managed-hooks"
        managed.mkdir()
        (managed / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (managed / "pre-commit").chmod(0o755)
        self.git("config", "--local", "--unset", "core.hooksPath")
        shutil.rmtree(self.root / ".githooks")
        self.git("config", "--global", "core.hooksPath", str(managed))
        global_before = self.global_config.read_bytes()
        for args in ((), ("--force",)):
            with self.subTest(args=args):
                result = self.install_hook(*args)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(f"{managed.resolve()} (global scope)", result.stderr)
                self.assertIn("--force does not override this", result.stderr)
                self.assertIn(f"uv run --script {STAMP} --verify", result.stderr)
                self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath", check=False).stdout, "")
                self.assertFalse((self.root / ".githooks").exists())
                self.assertEqual(self.global_config.read_bytes(), global_before)

        # The named alternative is the hook's own check: pass below the threshold, fail at it unstamped.
        self.assertEqual(self.run_python(STAMP, "--verify", cwd=self.root).returncode, 0)
        self.stage_product("release")
        refused = self.run_python(STAMP, "--verify", cwd=self.root)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no staged hw/.cad/recheck.json", refused.stderr)

    def test_existing_git_hook_is_refused_without_force(self) -> None:
        hooks_path = self.git("rev-parse", "--git-path", "hooks").stdout.strip()
        hooks_dir = self.root / hooks_path
        hook = hooks_dir / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        result = self.install_hook()
        self.assertEqual(result.returncode, 2)
        self.assertIn("existing non-sample hooks", result.stderr)
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath").stdout.strip(), ".githooks")

    def test_existing_git_hook_is_ignored_with_force(self) -> None:
        hooks_path = self.git("rev-parse", "--git-path", "hooks").stdout.strip()
        hooks_dir = self.root / hooks_path
        hook = hooks_dir / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        result = self.install_hook("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")


    def test_at_or_after_threshold_refuses_missing_stamp(self) -> None:
        self.stage_product("layout")
        self.git("add", "hw/product.yaml")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no staged hw/.cad/recheck.json", result.stderr)
        self.assertIn("uv run --script", result.stderr)

    def test_matching_passing_stamp_is_accepted(self) -> None:
        self.stage_product("detail")
        stamp = self.stamp(passed=True, stage="detail")
        self.assertTrue(stamp["passed"])
        self.assertEqual(stamp["tree_hash"], self.staged_hash())
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_untracked_stray_is_refused_even_below_recheck_threshold(self) -> None:
        stray = self.root / "hw" / "parts" / "PRT-0001" / ".cad-work" / "generated.step"
        stray.parent.mkdir()
        stray.write_bytes(b"hidden generated payload")
        self.stage_product("design_freeze", "layout")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CAD layout has", result.stderr)

    def test_frozen_pre_freeze_stage_refuses_a_stray_file(self) -> None:
        product_path = self.root / "hw" / "product.yaml"
        version = self.root / "hw" / "versions" / "v1"
        version.mkdir(parents=True)
        version_files = {
            "assembly.step": "step\n",
            "assembly.glb": "glb\n",
            "part-map.json": "{}\n",
            "metrics.json": "{}\n",
            "parts/PRT-0001.step": "part\n",
        }
        for relative, content in version_files.items():
            target = version / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        manifest = {"label": "v1", "files": {key: hashlib.sha256((version / key).read_bytes()).hexdigest()
                                                  for key in version_files}}
        manifest_path = version / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        product_path.parent.mkdir(parents=True, exist_ok=True)
        frozen_product = {
            "id": "test-project", "rigor": "pro", "units": "mm", "regime": "iso",
            "general_tolerance": "ISO 2768-m",
            "flow": {"stage": "specify", "recheck_from": "release", "frozen": {
                "version": "v1", "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "decision": "DR-001",
            }},
        }
        product_path.write_text(json.dumps(frozen_product), encoding="utf-8")
        stray = self.root / "hw" / "stray.md"
        stray.write_text("stray\n", encoding="utf-8")
        self.git("add", "hw/product.yaml", "hw/versions", "hw/stray.md")

        result = self.commit()

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stray.md", result.stderr)

    def test_stamp_refuses_staged_stray_before_recording(self) -> None:
        self.stage_product("design_freeze", "layout")
        stray = self.root / "hw" / "parts" / "PRT-0001" / ".cad-work" / "generated.step"
        stray.parent.mkdir()
        stray.write_bytes(b"hidden generated payload")
        self.git("add", "hw/parts/PRT-0001/.cad-work/generated.step")
        checks = self.root / ".checks.json"
        checks.write_text(json.dumps({"all_passed": True, "checks": []}), encoding="utf-8")
        result = self.run_python(STAMP, "--stage", "design_freeze", "--checks", str(checks), "--passed", cwd=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("CAD layout has", result.stderr)
        self.assertFalse((self.root / "hw" / ".cad" / "recheck.json").exists())

    def test_stamp_refuses_a_partial_changed_only_check(self) -> None:
        self.stage_product("detail")
        checks = self.root / ".checks.json"
        checks.write_text(json.dumps({"status": "pass", "scope": {"kind": "changed_pairs", "complete": False,
                                                                  "baseline": "v1"}, "checks": {}}), encoding="utf-8")
        result = self.run_python(STAMP, "--stage", "detail", "--checks", str(checks), "--passed", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("is a partial check (changed_pairs scope, complete: false); run the full check", result.stderr)
        self.assertFalse((self.root / "hw" / ".cad" / "recheck.json").exists())
        checks.write_text(json.dumps({"status": "pass", "scope": {"kind": "all_pairs", "complete": True}, "checks": {}}),
                          encoding="utf-8")
        result = self.run_python(STAMP, "--stage", "detail", "--checks", str(checks), "--passed", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("it is not an evidence packet", result.stderr, "a full cad-model check is not the evidence packet")
        self.assertTrue(self.stamp_with(self.evidence_packet("detail"))["passed"])

    def stamp_with(self, checks: Path, stage: str = "detail") -> dict[str, object]:
        result = self.run_python(STAMP, "--stage", stage, "--checks", str(checks), "--passed", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def stamp_refused(self, checks: Path, text: str, stage: str = "detail") -> None:
        result = self.run_python(STAMP, "--stage", stage, "--checks", str(checks), "--passed", cwd=self.root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(text, result.stderr)
        self.assertFalse((self.root / "hw" / ".cad" / "recheck.json").exists())

    def test_a_version_1_record_is_neither_stamped_nor_committed_at_the_threshold(self) -> None:
        checks = self.evidence_packet("detail")
        self.write_product("detail", "layout", legacy=True)
        self.git("add", "hw/product.yaml")
        self.stamp_refused(checks, "record.migration_required")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("record.migration_required", result.stderr)
        self.assertIn("migrate.py", result.stderr)

    def test_a_due_unknown_check_cannot_be_stamped_passed(self) -> None:
        self.stage_product("detail")
        self.stamp_refused(self.evidence_packet("detail", outcome="unknown"), "--passed contradicts the evidence packet")

    def test_a_packet_of_another_record_or_stage_cannot_stamp(self) -> None:
        self.stage_product("detail")
        checks = self.evidence_packet("detail")
        self.add_part("PRT-0002")
        self.git("add", "hw/parts/PRT-0002/part.yaml")
        self.stamp_refused(checks, "computed from another record")
        self.stamp_refused(self.evidence_packet("parts"), "assessed for stage parts, not detail")

    def test_a_stamp_whose_evidence_blocks_is_refused_at_commit_even_if_edited_to_passed(self) -> None:
        self.stage_product("detail")
        self.stamp(passed=False, stage="detail", outcome="unknown")
        path = self.root / "hw" / ".cad" / "recheck.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["passed"] = True
        data["checks"]["gate"] = {"stage": "detail", "passed": True, "findings": []}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.git("add", "hw/.cad/recheck.json")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocking finding", result.stderr)

    def test_the_recheck_threshold_may_move_earlier_but_never_later(self) -> None:
        self.stage_product("detail", "release")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("later than HEAD's layout", result.stderr)
        self.stage_product("specify", "intent")
        self.stage_product("ground", "intent")  # an earlier threshold is always allowed
        self.assertEqual(self.commit().returncode, 0, self.commit().stderr)

    def test_editing_staged_file_makes_stamp_stale(self) -> None:
        self.stage_product("detail")
        self.stamp(passed=True, stage="detail")
        self.add_part(title="Edited")
        self.git("add", "hw/parts/PRT-0001/part.yaml")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stamp is stale", result.stderr)

    def test_passed_false_is_refused(self) -> None:
        self.stage_product("detail")
        stamp = self.stamp(passed=False, stage="detail")
        self.assertFalse(stamp["passed"])
        self.assertEqual(stamp["tree_hash"], self.staged_hash())
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not say passed: true", result.stderr)

    def staged_hash(self) -> str:
        entries = subprocess.run(
            ["git", "ls-files", "-s", "--", "hw/"],
            cwd=self.root,
            capture_output=True,
            check=True,
        ).stdout.splitlines(keepends=True)
        digest = hashlib.sha256()
        for entry in entries:
            _, separator, path = entry.partition(b"\t")
            if separator and not path.startswith(b"hw/.cad/"):
                digest.update(entry)
        return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
