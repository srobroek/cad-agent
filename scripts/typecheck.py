#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mypy==2.4.1"]
# ///
"""Run mypy over changed feature modules and strict-check new pure modules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mypy.ini"
CHANGED_FILES = (
    "skills/cad-analysis/scripts/fea_conditions.py",
    "skills/cad-analysis/scripts/fea_static.py",
    "skills/cad-design/scripts/form_check.py",
    "skills/cad-design/scripts/process_checks/__init__.py",
    "skills/cad-design/scripts/process_checks/additive.py",
    "skills/cad-design/scripts/process_checks/core.py",
    "skills/cad-design/scripts/process_checks/dispatch.py",
    "skills/cad-design/scripts/process_checks/machining.py",
    "skills/cad-design/scripts/process_checks/purchased.py",
    "skills/cad-design/scripts/process_checks/route.py",
    "skills/cad-design/scripts/process_checks/secondary.py",
    "skills/cad-design/scripts/process_checks/shaping.py",
    "skills/cad-design/scripts/process_checks/sheet.py",
    "skills/cad-drawings/scripts/assembly_check.py",
    "skills/cad-drawings/scripts/assembly_drawing.py",
    "skills/cad-drawings/scripts/assembly_sheet.py",
    "skills/cad-record/scripts/check_packet.py",
    "skills/cad-fits/scripts/stackup.py",
    "skills/cad-flow/scripts/scratch.py",
    "skills/cad-model/scripts/cad_model.py",
    "skills/cad-model/scripts/cadmodel/build.py",
    "skills/cad-model/scripts/cadmodel/check.py",
    "skills/cad-model/scripts/cadmodel/components.py",
    "skills/cad-model/scripts/cadmodel/diff.py",
    "skills/cad-model/scripts/cadmodel/manufacture.py",
    "skills/cad-model/scripts/cadmodel/occ.py",
    "skills/cad-model/scripts/cadmodel/orientation.py",
    "skills/cad-model/scripts/cadmodel/record.py",
    "skills/cad-model/scripts/cadmodel/runner.py",
    "skills/cad-motion/scripts/access.py",
    "skills/cad-motion/scripts/incremental.py",
    "skills/cad-motion/scripts/pair_policy.py",
    "skills/cad-motion/scripts/play.py",
    "skills/cad-motion/scripts/sweep.py",
    "skills/cad-recheck/scripts/install-hook.py",
    "skills/cad-recheck/scripts/stamp.py",
    "skills/cad-record/scripts/assembly_operations.py",
    "skills/cad-record/scripts/build_guide.py",
    "skills/cad-record/scripts/design_rules.py",
    "skills/cad-record/scripts/evidence.py",
    "skills/cad-record/scripts/impact.py",
    "skills/cad-record/scripts/layout.py",
    "skills/cad-record/scripts/manufacturing_intent.py",
    "skills/cad-record/scripts/migrate.py",
    "skills/cad-record/scripts/prune.py",
    "skills/cad-record/scripts/release.py",
    "skills/cad-record/scripts/selection.py",
    "skills/cad-record/scripts/validate.py",
    "skills/cad-render/scripts/render.py",
    "scripts/typecheck.py",
)
STRICT_FILES = (
    "skills/cad-analysis/scripts/fea_conditions.py",
    "skills/cad-design/scripts/process_checks/__init__.py",
    "skills/cad-design/scripts/process_checks/additive.py",
    "skills/cad-design/scripts/process_checks/core.py",
    "skills/cad-design/scripts/process_checks/dispatch.py",
    "skills/cad-design/scripts/process_checks/machining.py",
    "skills/cad-design/scripts/process_checks/purchased.py",
    "skills/cad-design/scripts/process_checks/route.py",
    "skills/cad-design/scripts/process_checks/secondary.py",
    "skills/cad-design/scripts/process_checks/shaping.py",
    "skills/cad-design/scripts/process_checks/sheet.py",
    "skills/cad-model/scripts/cadmodel/components.py",
    "skills/cad-record/scripts/assembly_operations.py",
    "skills/cad-record/scripts/evidence.py",
    "skills/cad-record/scripts/impact.py",
    "skills/cad-record/scripts/selection.py",
    "skills/cad-record/scripts/check_packet.py",
)


def main() -> int:
    print("Checking all changed/new production modules with configured incremental baseline.")
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(CONFIG), *CHANGED_FILES],
        cwd=ROOT,
        check=False,
    )
    print("Strictly checking new pure feature modules.")
    strict = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(CONFIG), "--strict", *STRICT_FILES],
        cwd=ROOT,
        check=False,
    )
    return 1 if result.returncode or strict.returncode else 0


if __name__ == "__main__":
    raise SystemExit(main())
