#!/usr/bin/env python3
"""Regression tests for neutral agent sources and generated harness profiles."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

CAD = Path(__file__).resolve().parents[1]
AGENT_NAMES = sorted(path.stem for path in (CAD / "agents").glob("*.md"))
CAPABILITIES = {"read", "write-scratch", "shell-uv", "web"}


class HarnessGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-harness-test-")
        self.addCleanup(temporary.cleanup)
        self.cad = Path(temporary.name) / "cad-agent"
        shutil.copytree(CAD, self.cad, copy_function=shutil.copyfile)
        self.generator = self.cad / "harness" / "generate.py"

    def run_generator(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.generator), *args],
            cwd=self.cad,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def generate(self) -> None:
        result = self.run_generator()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_round_trip_check_and_drift_detection(self) -> None:
        self.generate()
        current = self.run_generator("--check")
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        self.assertIn(f"PASS: {4 * len(AGENT_NAMES) + 1} harness files current", current.stdout)
        output = self.cad / "harness" / "codex" / "agents" / f"{AGENT_NAMES[0]}.toml"
        output.write_text(output.read_text(encoding="utf-8") + "# manual drift\n", encoding="utf-8")
        stale = self.run_generator("--check")
        self.assertEqual(stale.returncode, 1)
        self.assertIn(f"stale harness/codex/agents/{AGENT_NAMES[0]}.toml", stale.stderr)
        self.generate()
        unexpected = self.cad / "harness" / "kiro" / "agents" / "cad-retired-agent.json"
        unexpected.write_text("{}\n", encoding="utf-8")
        extra = self.run_generator("--check")
        self.assertEqual(extra.returncode, 1)
        self.assertIn("unexpected harness/kiro/agents/cad-retired-agent.json", extra.stderr)
        self.generate()
        self.assertFalse(unexpected.exists())


    def test_neutral_sources_have_only_contract_metadata(self) -> None:
        for path in sorted((self.cad / "agents").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            source = yaml.safe_load(text.split("---", 2)[1])
            self.assertEqual(set(source), {"name", "description", "capabilities", "skills"})
            self.assertEqual(source["name"], path.stem)
            self.assertIsInstance(source["capabilities"], list)
            self.assertIsInstance(source["skills"], list)
            self.assertTrue(set(source["capabilities"]) <= CAPABILITIES)
            self.assertNotRegex(text, r"(?i)\bOMP\b|skill://|xd://|\bBeads\b|autoloadSkills|@task|@reviewer|\.omp-plugin|PI_SESSION_ID")
            self.assertNotRegex(text, r"(?i)\b(?:task|eval)\s+(?:tool|agent|function)\b")

    def test_claude_plugin_marketplace_targets_repo_root(self) -> None:
        plugin = json.loads((self.cad / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((self.cad / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "cad-agent")
        self.assertEqual(plugin["agents"], [f"./harness/claude/agents/{name}.md" for name in AGENT_NAMES])
        self.assertEqual(marketplace["name"], "cad-agent")
        self.assertEqual(marketplace["plugins"][0]["name"], "cad-agent")
        self.assertEqual(marketplace["plugins"][0]["source"], "./")
        self.assertNotIn("omp", json.dumps(plugin).lower())
        self.assertNotIn("omp", json.dumps(marketplace).lower())

    def test_claude_fields_follow_subagent_frontmatter_schema(self) -> None:
        self.generate()
        for name in AGENT_NAMES:
            text = (self.cad / "harness/claude/agents" / f"{name}.md").read_text(encoding="utf-8")
            front = yaml.safe_load(text.split("---", 2)[1])
            self.assertEqual(front["name"], name)
            self.assertTrue(front["description"])
            self.assertIn("Read", front["tools"])
            self.assertIsInstance(front["skills"], list)
            self.assertEqual(front["model"], "inherit")

    def test_codex_toml_fields_and_policy_limits(self) -> None:
        self.generate()
        for path in sorted((self.cad / "harness/codex/agents").glob("*.toml")):
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["name"], path.stem)
            self.assertTrue(config["description"])
            self.assertEqual(set(config), {"name", "description", "developer_instructions"})
            self.assertIn(".cad-work-only sandbox", config["developer_instructions"])
            sourcing = tomllib.loads((self.cad / "harness/codex/agents/cad-sourcing-researcher.toml").read_text(encoding="utf-8"))
            self.assertIn("inherit web access from the parent session", sourcing["developer_instructions"])
    def test_opencode_v2_frontmatter_has_ordered_rules_and_yaml_shape(self) -> None:
        self.generate()
        for path in sorted((self.cad / "harness/opencode/agents").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            front = yaml.safe_load(text.split("---", 2)[1])
            self.assertEqual(front["mode"], "subagent")
            self.assertIsInstance(front["permissions"], list)
            self.assertTrue(all(set(rule) == {"action", "resource", "effect"} for rule in front["permissions"]))
            self.assertIn({"action": "*", "resource": "*", "effect": "deny"}, front["permissions"])
            self.assertIn("<!-- Generated by cad-agent; do not edit. -->", text)

        writer = (self.cad / "harness/opencode/agents/cad-part-designer.md").read_text(encoding="utf-8")
        rules = yaml.safe_load(writer.split("---", 2)[1])["permissions"]
        edit_rules = [rule for rule in rules if rule["action"] == "edit"]
        shell_rules = [rule for rule in rules if rule["action"] == "shell"]
        self.assertEqual(edit_rules[-2:], [
            {"action": "edit", "resource": "*", "effect": "deny"},
            {"action": "edit", "resource": ".cad-work/**", "effect": "allow"},
        ])
        self.assertEqual(shell_rules[-2:], [
            {"action": "shell", "resource": "*", "effect": "deny"},
            {"action": "shell", "resource": "uv *", "effect": "allow"},
        ])
        self.assertEqual([rule for rule in rules if rule["action"] == "webfetch"], [{"action": "webfetch", "resource": "*", "effect": "deny"}])
        for path in sorted((self.cad / "harness/opencode/agents").glob("*.md")):
            parsed = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
            if path.stem != "cad-sourcing-researcher":
                self.assertTrue(any(rule == {"action": "websearch", "resource": "*", "effect": "deny"} for rule in parsed["permissions"]))

    def test_kiro_v3_json_uses_installed_skill_resources(self) -> None:
        self.generate()
        for path in sorted((self.cad / "harness/kiro/agents").glob("*.json")):
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["name"], path.stem)
            self.assertTrue(config["description"])
            self.assertIn("read", config["tools"])
            self.assertIsInstance(config["permissions"]["rules"], list)
            self.assertTrue(config["resources"])
            self.assertTrue(all(resource.startswith("skill://.kiro/skills/cad-") for resource in config["resources"]))
            self.assertIn("<!-- Generated by cad-agent; do not edit. -->", config["prompt"])

        writer = json.loads((self.cad / "harness/kiro/agents/cad-part-designer.json").read_text(encoding="utf-8"))
        self.assertIn({"capability": "fs_write", "match": [".cad-work/**"], "effect": "allow"}, writer["permissions"]["rules"])
        self.assertIn({"capability": "shell", "match": ["uv *"], "effect": "allow"}, writer["permissions"]["rules"])
        reader = json.loads((self.cad / "harness/kiro/agents/cad-mech-reviewer.json").read_text(encoding="utf-8"))
        self.assertNotIn("write", reader["tools"])
        self.assertNotIn("shell", reader["tools"])

    def test_command_is_a_generated_opencode_skill_launcher(self) -> None:
        self.generate()
        command = (self.cad / "harness/opencode/commands/cad-flow.md").read_text(encoding="utf-8")
        front, body = command.split("---", 2)[1:]
        self.assertEqual(yaml.safe_load(front)["description"], "Prototype-to-CAD flow")
        self.assertIn("Read the `cad-flow` skill", body)
        self.assertIn("$ARGUMENTS", body)

    def test_rejects_unknown_capabilities_skills_and_names(self) -> None:
        source = self.cad / "agents/cad-analyst.md"
        original = source.read_text(encoding="utf-8")
        cases = (
            ("capabilities: [read, write-scratch, shell-uv]", "capabilities: [telepathy, read]", "capabilities must be distinct"),
            ("skills: [cad-motion, cad-analysis]", "skills: [cad-missing]", "skills name files CAD does not ship"),
            ("name: cad-analyst", "name: cad-renamed", "must be a cad-* kebab-case id"),
            ("capabilities: [read, write-scratch, shell-uv]", "capabilities: [read, read]", "capabilities must be distinct"),
        )
        try:
            for old, new, message in cases:
                with self.subTest(field=old):
                    source.write_text(original.replace(old, new, 1), encoding="utf-8")
                    result = self.run_generator("--check")
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(message, result.stderr)
        finally:
            source.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
