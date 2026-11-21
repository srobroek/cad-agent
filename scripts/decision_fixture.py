# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Test helper: write the MADR 4.0.0 decision records that hw/decisions/ holds in a test project.

Tests put this directory on sys.path (see modload.py). A call replaces the record with the same DR id."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import yaml

from modload import load_module

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "cad-record" / "scripts"
FIXTURES = Path(__file__).resolve().parents[1] / "skills" / "cad-record" / "tests" / "fixtures"
records = load_module("cad_record_decision_records", SCRIPTS / "decision_records.py")


def write_test_decision(hw: Path, ident: str, *, status: str = "accepted", title: str | None = None,
                        context: str = "A test decision.", date: datetime.date = datetime.date(2026, 1, 1),
                        superseded_by: tuple[str, ...] = ()) -> Path:
    """`hw`/decisions/`ident`-slug.md with the given MADR status and date, replacing an earlier record of that id.
    A record named in `superseded_by` must exist already."""
    folder = Path(hw) / "decisions"
    for old in folder.glob(f"{ident}-*.md"):
        old.unlink()
    return records.write_decision(
        folder, number=int(ident.removeprefix("DR-")), title=title or f"Test decision {ident}", status=status,
        date=date, context=context, options=("The test option",), chosen="The test option",
        justification="the test needs a record", superseded_by=superseded_by)


def decision_path(hw: Path, ident: str) -> Path:
    """The file of record `ident` under `hw`/decisions/."""
    (found,) = (Path(hw) / "decisions").glob(f"{ident}-*.md")
    return found


def add_flow_profile(hw: Path, *, decision: str | None = None, status: str = "accepted", **changes: Any) -> str:
    """Settle the project-start profile of the test project `hw`: write the decision record (default the id the
    cad-record fixture profile cites, with MADR `status`) and set product.yaml flow.profile to that fixture
    profile (`changes` as in its `flow_profile`). Returns the decision id. product.yaml must exist."""
    fixtures = load_module("cad_record_mi_fixtures", FIXTURES / "manufacturing_intent.py")
    decision = decision or fixtures.PROFILE_DECISION
    write_test_decision(hw, decision, status=status)
    path = Path(hw) / "product.yaml"
    product = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    product.setdefault("flow", {})["profile"] = fixtures.flow_profile(decision, **changes)
    path.write_text(yaml.safe_dump(product, sort_keys=False), encoding="utf-8")
    return str(decision)
