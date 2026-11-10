#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Migrate a CAD design record from record version 1 to record version 2.

A dry run (the default) prints the exact change to every file and writes nothing; --apply makes them.
The migration adds the blocks record version 2 requires, every one an explicit unknown that names an
open question: the product use case, the requirements' preferences, assumptions and open questions,
each part's manufacturing intent, each interface's functional block, and hw/manufacturing/
capabilities, materials and selection with nothing selected. It moves assembly.yaml
drawing.operations to top-level operations unchanged, and writes a dated migration note under hw/docs/.

It never invents a process, material, capability, criterion or certification, approves nothing, and
edits only mutable record files: never model.py, geometry, hw/versions/, hw/releases/, bundles,
decisions, bom.csv or verification.csv. Edits are textual, so comments and formatting survive; each
edited file must parse to exactly the intended data and validate against its schema, or nothing is
written. A record already at version 2 is left alone; a frozen record needs an open approved change
(flow.change) first; an unknown record_version is refused.

Exit status: 0 nothing to migrate or applied, 1 a dry run with pending changes, 2 refused.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import importlib.util
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SCRIPTS = Path(__file__).resolve().parent


def _load_module(name: str, path: Path) -> Any:
    """A sibling module loaded once by path and registered in sys.modules, where dataclasses look it up."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path.name} not found at {path}")
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


intent = _load_module("cad_record_manufacturing_intent", SCRIPTS / "manufacturing_intent.py")
layout = _load_module("cad_record_layout", SCRIPTS / "layout.py")

SOURCE_VERSION, TARGET_VERSION = intent.LEGACY_RECORD_VERSION, intent.RECORD_VERSION
REASON = "not recorded before record version 2"
MUTABLE = frozenset({"controlled", "free"})
NEW_FILES = {
    "manufacturing/capabilities.yaml": (
        "# Capability profiles of the actual machines, operators and suppliers (schemas/manufacturing-capabilities.schema.json).\n"
        "# None is recorded, so every capability is unknown. Start from cad-record assets/manufacturing/capability-templates.yaml\n"
        "# and record your own machine's values and calibrations; a template is never a capability.\n"
        "profiles: []\n"),
    "manufacturing/materials.yaml": (
        "# Material profiles: family or grade, form, process state and sourced properties with their conditions\n"
        "# (schemas/manufacturing-materials.schema.json). None is selected; part.yaml material strings stay declarations.\n"
        "profiles: []\n"),
    "manufacturing/selection.yaml": (
        "# Material, process and architecture selection (schemas/manufacturing-selection.schema.json): not assessed yet.\n"
        "status: not_assessed\n"
        "candidates: []\n"),
}


class Refused(Exception):
    """The record cannot be migrated as it stands; nothing was written."""


class _Flow(dict[str, Any]):
    """A mapping dumped in flow style, such as a one-line unknown fact."""


class _Dumper(yaml.SafeDumper):  # type: ignore[misc]  # PyYAML ships no stubs here, so SafeDumper is Any
    """Indents a block sequence under its key, as the record files do."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


_Dumper.add_representer(_Flow, lambda dumper, data: dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True))


_FACT_KEYS = frozenset({"status", "value", "sources", "basis", "reason", "question", "decision", "uncertainty", "conditions",
                        "assumption", "derivation", "note"})
_SUBJECT_KEYS = frozenset({"product", "PN", "interface"})


def _flow_facts(value: Any) -> Any:
    """`value` with every fact and subject mapping marked for one-line flow style."""
    if isinstance(value, Mapping):
        items = {key: _flow_facts(item) for key, item in value.items()}
        if ("status" in value and set(value) <= _FACT_KEYS) or set(value) <= _SUBJECT_KEYS:
            return _Flow(items)
        return items
    if isinstance(value, list):
        return [_flow_facts(item) for item in value]
    return value


def dump(block: Mapping[str, Any], indent: int = 0) -> str:
    text = yaml.dump(_flow_facts(dict(block)), Dumper=_Dumper, sort_keys=False, default_flow_style=False,
                     allow_unicode=True, width=4096)
    pad = " " * indent
    return "".join(pad + line if line.strip() else line for line in text.splitlines(keepends=True))


@dataclass
class Change:
    path: str  # project-relative, hw/...
    action: str  # modify or create
    before: str | None
    after: str
    summary: list[str] = field(default_factory=list)
    newline: str = "\n"

    def diff(self) -> str:
        before = (self.before or "").splitlines(keepends=True)
        name = "/dev/null" if self.before is None else f"a/{self.path}"
        lines = difflib.unified_diff(before, self.after.splitlines(keepends=True), name, f"b/{self.path}")
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


@dataclass
class Plan:
    hw: Path
    status: str  # current or pending
    source_version: int
    changes: list[Change]
    questions: list[dict[str, Any]]
    unknown: list[str]
    findings: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "record_version": {"from": self.source_version, "to": TARGET_VERSION},
                "changes": [{"path": c.path, "action": c.action, "summary": c.summary, "diff": c.diff()} for c in self.changes],
                "questions": [{"id": q["id"], "question": q["question"]} for q in self.questions],
                "unknown": self.unknown, "findings": self.findings}


def find_hw(path: Path) -> Path:
    """The hw/ folder of a project directory, or `path` itself when it is hw/."""
    path = Path(path)
    return path / "hw" if (path / "hw").is_dir() else path


def _read(hw: Path, rel: str) -> tuple[str, str] | None:
    """(text with \\n newlines, the file's newline) of hw/<rel>, or None when absent."""
    path = hw / rel
    if not path.is_file():
        return None
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refused(f"hw/{rel} is not UTF-8: {error}") from None
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def _parse(text: str, rel: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise Refused(f"hw/{rel} does not parse: {error}") from None


def _append(text: str, block: str) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def insert_into_items(text: str, list_key: str, payloads: Mapping[str, str]) -> str:
    """`text` with each payload (a YAML mapping text) appended to the block-style item of top-level
    `list_key` whose id it names, at that item's key indentation. Refused for any other layout."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(list_key)}:\s*(#.*)?$", line)), None)
    if start is None:
        raise Refused(f"{list_key} is not a block-style list")
    items: list[tuple[int, int, int]] = []  # (first line, end line, key column)
    dash = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not _content(line):
            continue
        match = re.match(r"^( *)-( +)\S", line)
        if match and (dash is None or len(match.group(1)) == dash):
            dash = len(match.group(1))
            items.append((index, index + 1, dash + 1 + len(match.group(2))))
        elif items and dash is not None and _indent_of(line) > dash:
            items[-1] = (items[-1][0], index + 1, items[-1][2])
        elif _indent_of(line) == 0:
            break
        else:
            raise Refused(f"{list_key} has a line this migration cannot place: {line.strip()!r}")
    inserts: dict[int, str] = {}
    for first, end, column in items:
        item = _parse(_dedent_item("".join(lines[first:end]), column), list_key)
        ident = item.get("id") if isinstance(item, Mapping) else None
        if isinstance(ident, str) and ident in payloads:
            inserts[end] = "".join(" " * column + line if line.strip() else line
                                   for line in payloads[ident].splitlines(keepends=True))
            payloads = {key: value for key, value in payloads.items() if key != ident}
    if payloads:
        raise Refused(f"{list_key}: cannot find the block-style items {sorted(payloads)}")
    out = []
    for position, line in enumerate(lines):
        out.append(line)
        if position + 1 in inserts:
            if not line.endswith("\n"):
                out.append("\n")
            out.append(inserts[position + 1])
    return "".join(out)


def _dedent_item(body: str, column: int) -> str:
    """One `- key: ...` list item as a mapping document: the dash blanked, every line shifted left by `column`."""
    lines = body.splitlines(keepends=True)
    lines[0] = " " * column + lines[0][column:]
    return "".join(line[column:] if _indent_of(line) >= column else line.lstrip(" ") for line in lines)


def move_child_to_top(text: str, parent: str, child: str) -> str:
    """`text` with block `parent.child` moved, verbatim and dedented, to the end as top-level `child`."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(parent)}:\s*(#.*)?$", line)), None)
    if start is None:
        raise Refused(f"{parent} is not a block-style mapping")
    end = next((i for i in range(start + 1, len(lines)) if _content(lines[i]) and _indent_of(lines[i]) == 0), len(lines))
    children = [i for i in range(start + 1, end) if _content(lines[i])]
    if not children:
        raise Refused(f"{parent} has no block-style children")
    column = _indent_of(lines[children[0]])
    first = next((i for i in children if _indent_of(lines[i]) == column and re.match(rf"^ *{re.escape(child)}:", lines[i])), None)
    if first is None:
        raise Refused(f"{parent}.{child} is not a block-style key")
    last = first
    for i in range(first + 1, end):
        if _content(lines[i]):
            if _indent_of(lines[i]) <= column:
                break
            last = i
        elif lines[i].strip() and _indent_of(lines[i]) <= column:
            break
    moved = [line[column:] if line.strip() else line for line in lines[first:last + 1]]
    if moved and not moved[-1].endswith("\n"):
        moved[-1] += "\n"
    remaining = lines[:first] + lines[last + 1:]
    return _append("".join(remaining), "".join(moved))


def _checked(rel: str, after: str, expected: Any) -> str:
    """`after`, refused unless it parses to exactly `expected`."""
    if _parse(after, rel) != expected:
        raise Refused(f"hw/{rel}: a textual edit would not give the intended data; edit the file by hand with the "
                      "blocks the dry run lists, or simplify its layout")
    return after


def _mutable(rel: str) -> None:
    match = layout.classify(f"hw/{rel}")
    if match is None or match.entry.kind != "record" or match.entry.mutability not in MUTABLE:
        raise Refused(f"hw/{rel} is not a mutable record file; the migration never writes it")


def plan(hw: Path, today: date | None = None) -> Plan:
    """The migration of the record at `hw`: every change it would make, nothing written. Raises Refused."""
    hw, today = Path(hw), today or date.today()
    product_text = _read(hw, "product.yaml")
    if product_text is None:
        raise Refused(f"{hw} has no product.yaml; this is not a CAD record")
    product = _parse(product_text[0], "product.yaml")
    if not isinstance(product, Mapping):
        raise Refused("hw/product.yaml is not a mapping")
    try:
        version = intent.record_version(product)
    except intent.UnsupportedRecordVersion as error:
        raise Refused(str(error)) from None
    if version == TARGET_VERSION:
        return Plan(hw, "current", version, [], [], [], [])
    flow_data = product.get("flow")
    flow = flow_data if isinstance(flow_data, Mapping) else {}
    if flow.get("frozen") and not flow.get("change"):
        raise Refused(f"the record is frozen at {flow['frozen'].get('version')}; migrating changes controlled records, so "
                      "open an approved change (product.yaml flow.change with its decision) first")
    texts: dict[str, tuple[str, str]] = {}
    part_rels = sorted(path.relative_to(hw).as_posix() for path in hw.glob("parts/*/part.yaml") if path.is_file())
    for rel in ("requirements.yaml", "interfaces.yaml", "assembly.yaml", *part_rels):
        found = _read(hw, rel)
        if found is None:
            raise Refused(f"hw/{rel} is missing; validate.py lists what a record needs")
        texts[rel] = found
    data = {rel: _parse(text, rel) for rel, (text, _newline) in texts.items()}
    for rel, document in data.items():
        if not isinstance(document, Mapping):
            raise Refused(f"hw/{rel} is not a mapping")

    requirements = data["requirements.yaml"]
    assembly = data["assembly.yaml"]
    drawing = assembly.get("drawing") if isinstance(assembly.get("drawing"), Mapping) else {}
    move_operations = "operations" not in assembly and "operations" in drawing
    interfaces = [item for item in data["interfaces.yaml"].get("interfaces") or () if isinstance(item, Mapping)]

    # Questions: the use case, then assembly operations, then each part and interface, in record order.
    questions, use_case_paths = intent.use_case_questions(1)
    number = len(questions) + 1

    def ask(text: str, subjects: list[dict[str, Any]]) -> str:
        nonlocal number
        ident = f"Q-{number:03d}"
        number += 1
        questions.append({"id": ident, "question": text, "subjects": subjects, "owner": "lead", "material_to_choice": True,
                          "safety_relevant": True, "status": "open"})
        return ident

    operations_question = None
    if "operations" not in assembly and not move_operations:
        operations_question = ask("How is the assembly installed: which operations, in which order, with which tools, "
                                  "access and closure?", [{"product": True}])
    part_questions = {}
    for rel in part_rels:
        part = data[rel]
        part_questions[rel] = ask(
            f"{part.get('PN')} ({part.get('title')}): which functions does it serve, which loads and service conditions does it "
            "see, which material profile (family or exact grade) and which complete route (operations, capability, "
            "orientation, setups, supports, finishing and inspection) apply, and which surfaces are critical? The legacy "
            f"record declares material {part.get('material')} and process {part.get('process')}; they stay declarations, "
            "not evidence.", [{"PN": part.get("PN")}])
    interface_questions: dict[str, str] = {}
    for item in interfaces:
        question = ask(
            f"{item.get('id')} ({item.get('type')}, {item.get('from')} to {item.get('to')}): is it critical, which functions "
            "does it serve, and which acceptance criteria, stack and contact evidence verify it?", [{"interface": item.get("id")}])
        if isinstance(item.get("id"), str):
            interface_questions[item["id"]] = question

    changes: list[Change] = []
    unknown: list[str] = []
    stamp = today.isoformat()

    # requirements.yaml: the three classified-statement lists.
    rel = "requirements.yaml"
    text, newline = texts[rel]
    present = [key for key in ("preferences", "assumptions", "open_questions") if key in requirements]
    if "open_questions" in requirements:
        existing = {q.get("id") for q in requirements.get("open_questions") or () if isinstance(q, Mapping)}
        missing = sorted({q["id"] for q in questions} - existing)
        if missing:
            raise Refused(f"hw/requirements.yaml already has open_questions without {', '.join(missing)}, which the migrated "
                          "blocks name; finish the earlier migration by hand or restore the file from version control")
    block: dict[str, Any] = {key: value for key, value in (("preferences", []), ("assumptions", []), ("open_questions", questions))
             if key not in present}
    if block:
        expected = {**copy.deepcopy(requirements), **block}
        after = _checked(rel, _append(text, dump(block)), expected)
        changes.append(Change(f"hw/{rel}", "modify", text, after,
                              [f"adds {', '.join(block)}" + (f" ({len(questions)} open questions)" if "open_questions" in block else "")],
                              newline))
        data[rel] = expected

    # parts: one intent block each.
    for rel in part_rels:
        part = data[rel]
        if "intent" in part:
            continue
        text, newline = texts[rel]
        question = part_questions[rel]

        def unknown_fact(reason: str = REASON) -> dict[str, Any]:
            return {"status": "unknown", "reason": reason, "question": question}

        block = {"intent": {
            "functions": unknown_fact(),
            "material_profile": unknown_fact(f"{REASON}; the legacy material {part.get('material')} is a declaration, "
                                             "not a sourced grade"),
            "service": unknown_fact(),
            "critical_surfaces": unknown_fact(),
            "route": unknown_fact(f"{REASON}; the legacy process label {part.get('process')} names no operations, capability, "
                                  "orientation, supports or inspection"),
        }}
        expected = {**copy.deepcopy(part), **block}
        after = _checked(rel, _append(text, dump(block)), expected)
        changes.append(Change(f"hw/{rel}", "modify", text, after,
                              [f"adds intent: functions, material_profile, service, critical_surfaces and route unknown ({question})"],
                              newline))
        unknown.append(f"{part.get('PN')}: functions, material profile, service, critical surfaces and route ({question})")
        data[rel] = expected

    # interfaces.yaml: one functional block per interface.
    rel = "interfaces.yaml"
    text, newline = texts[rel]
    payloads: dict[str, str] = {}
    expected = copy.deepcopy(data[rel])
    for item in expected.get("interfaces") or ():
        if isinstance(item, dict) and "functional" not in item:
            ident = item.get("id")
            if not isinstance(ident, str):
                raise Refused("hw/interfaces.yaml has an interface without an id; give it one, then migrate")
            question = interface_questions[ident]
            functional = {"critical": {"status": "unknown", "reason": REASON, "question": question},
                          "functions": {"status": "unknown", "reason": REASON, "question": question}}
            item["functional"] = functional
            payloads[ident] = dump({"functional": functional})
            unknown.append(f"{ident}: criticality and functions ({question})")
    if payloads:
        after = _checked(rel, insert_into_items(text, "interfaces", payloads), expected)
        changes.append(Change(f"hw/{rel}", "modify", text, after,
                              [f"adds functional (critical and functions unknown) to {', '.join(payloads)}"], newline))
        data[rel] = expected

    # assembly.yaml: one authority for installation operations.
    rel = "assembly.yaml"
    text, newline = texts[rel]
    if move_operations:
        expected = copy.deepcopy(assembly)
        expected["operations"] = expected["drawing"].pop("operations")
        after = _checked(rel, move_child_to_top(text, "drawing", "operations"), expected)
        changes.append(Change(f"hw/{rel}", "modify", text, after,
                              [f"moves drawing.operations ({len(expected['operations'] or [])} operations) to top-level "
                               "operations, unchanged"], newline))
        data[rel] = expected
    elif "operations" not in assembly:
        block = {"operations": {"status": "unknown", "reason": REASON, "question": operations_question}}
        expected = {**copy.deepcopy(assembly), **block}
        after = _checked(rel, _append(text, dump(block)), expected)
        changes.append(Change(f"hw/{rel}", "modify", text, after, [f"adds operations unknown ({operations_question})"], newline))
        unknown.append(f"assembly operations ({operations_question})")
        data[rel] = expected

    # hw/manufacturing/: capability, material and selection records with nothing recorded.
    for rel, content in NEW_FILES.items():
        if not (hw / rel).exists():
            changes.append(Change(f"hw/{rel}", "create", None, content, ["creates it with nothing recorded"]))
            data[rel] = yaml.safe_load(content)
    unknown.insert(0, "capabilities, material profiles and selection: none recorded (hw/manufacturing/)")

    # product.yaml last: the version bump and the use case.
    rel = "product.yaml"
    text, newline = product_text
    block = {"record_version": TARGET_VERSION}
    if "use_case" not in product:
        block["use_case"] = intent.blank_use_case(REASON, use_case_paths)
    expected = {**copy.deepcopy(product), **block}
    comment = (f"# Record version {TARGET_VERSION} (cad-record migrate.py, {stamp}): every use-case field stays unknown\n"
               "# until the open questions in requirements.yaml are answered.\n")
    after = _checked(rel, _append(text, comment + dump(block)), expected)
    changes.append(Change(f"hw/{rel}", "modify", text, after,
                          [f"sets record_version {TARGET_VERSION}", "adds use_case: every field unknown (Q-001 to "
                           f"Q-{len(intent.USE_CASE_QUESTIONS):03d})"], newline))
    unknown.insert(0, f"use case: service class, purpose, users, environment, loads, motion, accuracy, consequences, "
                      f"logistics, capability, release scope and evidence policy (Q-001 to Q-{len(intent.USE_CASE_QUESTIONS):03d})")
    data[rel] = expected

    note_rel = f"docs/record-migration-v{TARGET_VERSION}-{stamp}.md"
    existing_note = _read(hw, note_rel)
    changes.insert(len(changes) - 1, Change(f"hw/{note_rel}", "create" if existing_note is None else "modify",
                                            None if existing_note is None else existing_note[0],
                                            _note(stamp, changes, unknown, questions), ["the migration note"]))

    for change in changes:
        _mutable(change.path[len("hw/"):])
    documents, _problems = intent.read_documents(hw)
    documents.update({rel: value for rel, value in data.items() if intent.schema_for(rel)})
    changed = {c.path[len("hw/"):] for c in changes}
    schema = [p for p in intent.schema_problems(documents) if p.path[len("hw/"):] in changed]
    if schema:
        raise Refused("the migrated record would not validate; fix these first: " +
                      "; ".join(f"{p.path}: {p.message}" for p in schema))
    record = intent.normalize_record(documents, hw=hw)
    findings = [p.finding() for p in record.problems if p.code != intent.MIGRATION_REQUIRED]
    return Plan(hw, "pending", version, changes, questions, unknown, findings)


def _note(stamp: str, changes: list[Change], unknown: list[str], questions: list[dict[str, Any]]) -> str:
    lines = [f"# Record migration to record version {TARGET_VERSION}", "",
             f"Date: {stamp}. Command: cad-record `migrate.py --apply`. Source: record version {SOURCE_VERSION} (product.yaml "
             "without record_version).", "",
             "The migration added the blocks record version 2 requires. Every new value is `unknown` and names an open",
             "question in requirements.yaml. Nothing was approved, and no process, material, capability, criterion or",
             "certification was inferred. The legacy `material`, `process`, `build_volume`, `min_wall_mm` and drawing fields",
             "are unchanged: they stay declarations and diagnostic heuristics, never sourced grades, allowables or calibrated",
             "accuracy. Answer the questions, record sources and decisions, then rebuild and recheck.", "",
             "## Files changed", ""]
    lines += [f"- `{c.path}` ({c.action}): {'; '.join(c.summary)}" for c in changes]
    lines += ["", "## Now explicitly unknown", ""]
    lines += [f"- {item}" for item in unknown]
    lines += ["", "## Open questions", "", "| Id | Question |", "|---|---|"]
    lines += [f"| {q['id']} | {q['question'].replace('|', '/')} |" for q in questions]
    lines += ["", "## Not touched", "",
              "`model.py` files, geometry, `hw/versions/`, `hw/releases/`, check, drawing, render, motion and analysis bundles,",
              "decisions, `bom.csv` and `verification.csv`. Sealed versions and releases stay readable by their own format and",
              "are not re-certified; build a new version after migrating.", ""]
    return "\n".join(lines)


def apply(plan: Plan) -> None:
    """Write every change of `plan`: each file staged under .cad-work/migration/ and renamed onto its
    target, product.yaml last, so an interrupted run leaves a version 1 record that migrates again."""
    if plan.status != "pending":
        return
    staging = plan.hw.parent / layout.SCRATCH / "migration" / f"record-v{TARGET_VERSION}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        for index, change in enumerate(plan.changes):
            target = plan.hw / change.path[len("hw/"):]
            staged = staging / f"{index:03d}"
            staged.write_bytes(change.after.replace("\n", change.newline).encode("utf-8"))
            if target.exists():
                shutil.copymode(target, staged)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_dir", type=Path, help="project directory containing hw/, or the hw/ directory itself")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the exact changes and write nothing (the default)")
    mode.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON instead of text and diffs")
    args = parser.parse_args(argv)
    hw = find_hw(args.project_dir)
    try:
        migration = plan(hw)
    except Refused as error:
        if args.json:
            print(json.dumps({"status": "refused", "message": str(error)}, indent=2))
        else:
            print(f"migrate.py: refused: {error}\nNothing was changed.", file=sys.stderr)
        return 2
    if migration.status == "current":
        report = {"status": "current", "record_version": {"from": TARGET_VERSION, "to": TARGET_VERSION}, "changes": []}
        print(json.dumps(report, indent=2) if args.json else f"{hw} is at record version {TARGET_VERSION}; nothing to migrate.")
        return 0
    if args.apply:
        apply(migration)
    report = migration.as_dict()
    report["status"] = "applied" if args.apply else "pending"
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        verb = "Applied" if args.apply else "Dry run; would apply"
        print(f"{verb}: record version {migration.source_version} -> {TARGET_VERSION}, {len(migration.changes)} files.")
        for change in migration.changes:
            print(f"\n== {change.path} ({change.action}): {'; '.join(change.summary)}")
            print(change.diff(), end="")
        print("\nNow explicitly unknown:")
        print("\n".join(f"- {item}" for item in migration.unknown))
        if migration.findings:
            print("\nFindings that remain after migration (validate.py reports them):")
            print("\n".join(f"- {f['path']}: {f['message']}" for f in migration.findings))
        if not args.apply:
            print("\nNothing was changed. Run again with --apply to write these changes.")
    return 0 if args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
