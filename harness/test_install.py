#!/usr/bin/env python3
"""Install contract tests using only temporary project and home directories."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CAD = Path(__file__).resolve().parents[1]
INSTALLER = CAD / "harness/install.py"
HARNESS_PATHS = {
    "claude": (".claude/skills", ".claude/agents"),
    "codex": (".agents/skills", ".codex/agents"),
    "opencode": (".opencode/skills", ".opencode/agents"),
    "kiro": (".kiro/skills", ".kiro/agents"),
}
AGENT_FILES = {
    harness: sorted(path.name for path in (CAD / "harness" / harness / "agents").glob("*"))
    for harness in HARNESS_PATHS
}


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-install-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

    def run_installer(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(self.home)}
        return subprocess.run([sys.executable, str(INSTALLER), *args], cwd=cwd or self.project,
                              env=env, capture_output=True, text=True, timeout=60)

    def run_ok(self, *args: str) -> str:
        result = self.run_installer(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_project_install_idempotence_exact_paths_and_uninstall(self) -> None:
        for harness, (skills_relative, agents_relative) in HARNESS_PATHS.items():
            with self.subTest(harness=harness):
                project = self.root / f"project-{harness}"
                project.mkdir()
                command = ["--harness", harness, "--project", str(project)]
                dry = self.run_installer(*command, "--dry-run")
                self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
                self.assertIn(str(project / skills_relative / "cad-flow/SKILL.md"), dry.stdout)
                for agent_file in AGENT_FILES[harness]:
                    self.assertIn(str(project / agents_relative / agent_file), dry.stdout)
                if harness == "opencode":
                    self.assertIn(str(project / ".opencode/commands/cad-flow.md"), dry.stdout)
                self.assertFalse((project / skills_relative).exists())

                installed = self.run_installer(*command)
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                self.assertTrue((project / skills_relative / "cad-flow/SKILL.md").is_file())
                self.assertFalse(list((project / skills_relative).rglob("tests")))
                self.assertFalse(list((project / skills_relative).rglob("__pycache__")))
                for agent_file in AGENT_FILES[harness]:
                    self.assertTrue((project / agents_relative / agent_file).is_file())
                if harness == "opencode":
                    self.assertTrue((project / ".opencode/commands/cad-flow.md").is_file())

                second = self.run_installer(*command)
                self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                self.assertIn("already installed; no files changed", second.stdout)
                removed = self.run_installer(*command, "--uninstall")
                self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
                self.assertFalse((project / skills_relative).exists())
                self.assertFalse((project / agents_relative).exists())
                if harness == "opencode":
                    self.assertFalse((project / ".opencode/commands").exists())
                self.assertFalse((project / ".cad-agent-install.json").exists())

    def test_user_install_and_uninstall_use_contract_roots(self) -> None:
        for harness in HARNESS_PATHS:
            with self.subTest(harness=harness):
                command = ["--harness", harness, "--user"]
                if harness == "claude":
                    root, skills_relative, agents_relative = self.home / ".claude", "skills", "agents"
                elif harness == "codex":
                    root, skills_relative, agents_relative = self.home, ".agents/skills", ".codex/agents"
                elif harness == "opencode":
                    root, skills_relative, agents_relative = self.home / ".config/opencode", "skills", "agents"
                else:
                    root, skills_relative, agents_relative = self.home / ".kiro", "skills", "agents"
                dry = self.run_installer(*command, "--dry-run")
                self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
                self.assertIn(str(root / skills_relative / "cad-flow/SKILL.md"), dry.stdout)
                self.assertIn(str(root / agents_relative / AGENT_FILES[harness][0]), dry.stdout)
                if harness == "opencode":
                    self.assertIn(str(root / "commands/cad-flow.md"), dry.stdout)
                self.assertEqual(self.run_installer(*command).returncode, 0)
                self.assertTrue((root / skills_relative / "cad-flow/SKILL.md").is_file())
                for agent_file in AGENT_FILES[harness]:
                    self.assertTrue((root / agents_relative / agent_file).is_file())
                self.assertEqual(self.run_installer(*command, "--uninstall").returncode, 0)
                self.assertFalse((root / skills_relative).exists())
                self.assertFalse((root / agents_relative).exists())
                if harness == "opencode":
                    self.assertFalse((root / "commands").exists())

    def test_refuses_non_cad_file_and_does_not_partially_install(self) -> None:
        conflict = self.project / ".claude/agents/cad-analyst.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("user agent\n", encoding="utf-8")
        result = self.run_installer("--harness", "claude", "--project", str(self.project))
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to overwrite non-cad file", result.stderr)
        self.assertEqual(conflict.read_text(encoding="utf-8"), "user agent\n")
        self.assertFalse((self.project / ".claude/skills/cad-flow/SKILL.md").exists())

    def test_modified_installed_file_is_not_overwritten_or_uninstalled(self) -> None:
        command = ["--harness", "claude", "--project", str(self.project)]
        self.assertEqual(self.run_installer(*command).returncode, 0)
        target = self.project / ".claude/skills/cad-flow/SKILL.md"
        target.write_text(target.read_text(encoding="utf-8") + "\nlocal change\n", encoding="utf-8")
        rejected_install = self.run_installer(*command)
        self.assertEqual(rejected_install.returncode, 1)
        self.assertIn("installed CAD file was modified", rejected_install.stderr)
        rejected_uninstall = self.run_installer(*command, "--uninstall")
        self.assertEqual(rejected_uninstall.returncode, 1)
        self.assertIn("installed CAD file was modified", rejected_uninstall.stderr)
        self.assertTrue(target.is_file())

    def test_default_current_directory_is_project_but_checkout_is_refused_implicitly(self) -> None:
        command = ["--harness", "claude", "--dry-run"]
        result = self.run_installer(*command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.project / ".claude/skills/cad-flow/SKILL.md"), result.stdout)
        checkout = self.run_installer(*command, cwd=CAD)
        self.assertEqual(checkout.returncode, 1)
        self.assertIn("pass --project explicitly", checkout.stderr)

    def test_refuses_missing_project_scope_and_symlink_destination(self) -> None:
        missing = self.run_installer("--harness", "codex", "--project", str(self.root / "missing"), "--dry-run")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("project directory must exist", missing.stderr)
        outside = self.root / "outside"
        outside.mkdir()
        symlink = self.project / ".kiro"
        symlink.symlink_to(outside, target_is_directory=True)
        unsafe = self.run_installer("--harness", "kiro", "--project", str(self.project), "--dry-run")
        self.assertEqual(unsafe.returncode, 1)
        self.assertIn("refusing symlink destination directory", unsafe.stderr)


if __name__ == "__main__":
    unittest.main()
