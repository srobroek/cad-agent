#!/usr/bin/env python3
"""Print, as JSON, findings built by the producers' own code: one object of name -> finding.

test_schema_finding.py runs this in a subprocess, so the producers' modules (and the sys.path entries they need) stay out
of the test process. No FreeCAD is needed. It needs the CI test pins (PyYAML and jsonschema for the cad-design fixtures,
defusedxml for cad-drawings)."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from typing import Any

SKILLS = Path(__file__).resolve().parents[3]
for entry in (SKILLS.parent / "scripts", SKILLS / "cad-drawings" / "scripts", SKILLS / "cad-model" / "scripts",
              SKILLS / "cad-design" / "scripts", SKILLS / "cad-design" / "tests"):
    sys.path.insert(0, str(entry))

import drawing_intent  # noqa: E402
import test_process_checks as design_tests  # noqa: E402
from assembly_check import finding as assembly_check_finding  # noqa: E402
from cadmodel import build  # noqa: E402
from drawing_document import CompositionError  # noqa: E402


def overhang() -> dict[str, Any]:
    """The `process.fdm.overhang` packet finding of a printed part with an overhang and no support plan."""
    documents = design_tests.printed("TST-0101", {"strategy": "none"})
    record = design_tests.load(unittest.TestCase(), documents)
    geometry = design_tests.geometry("TST-0101", design_tests.SHELF)
    result = design_tests.pc.assess(record, geometry, today=date(2026, 6, 1), calibration=True)  # the calibrated overhang limit is what fails here
    [found, *_] = result.by_code("process.fdm.overhang")
    return dict(found)


def fit_unverified() -> dict[str, Any]:
    """A drawn fit whose route inspects nothing: `finished_state_findings` is a public, pure call."""
    part = {"intent": {"route": [{"id": "print", "targets": [{"feature": "pivot_bore"}]}]}}
    [found] = drawing_intent.finished_state_findings("TST-0001", part, "pivot_bore", "H7")
    return dict(found)


def dimension_error() -> dict[str, Any]:
    """drawing_intent.py:1210's call with an IntentError raised while measuring a dimension request."""
    error = drawing_intent.IntentError("datum_invalid", "datum A bound no geometry")
    return drawing_intent.finding(error.code, f"dimension d1: {error}", dimension="d1")


def sheet_no_fit() -> dict[str, Any]:
    """assembly_drawing.py:240's call with the CompositionError that assembly_sheet.py:170 raises."""
    error = CompositionError("sheet_no_fit", "no allowed sheet fits the assembly views legibly: A3: too wide")
    return assembly_check_finding(error.code, f"sheet 2: {error}", sheet=2)


def main() -> None:
    findings = {
        "drawing_intent.bare": drawing_intent.finding("datum_invalid", "geometric tolerance GT-1 uses undeclared datums ['Z']"),
        "drawing_intent.with_feature": fit_unverified(),
        "drawing_intent.with_dimension": dimension_error(),
        "assembly_check.bare": assembly_check_finding("invalid_input", "the assembly names no drawing"),
        "assembly_check.with_sheet": sheet_no_fit(),
        "build.finding": build.finding("TST-0001", "build", "no solid"),
        "packet.process_overhang": overhang(),
    }
    print(json.dumps(findings, sort_keys=True))


if __name__ == "__main__":
    main()
