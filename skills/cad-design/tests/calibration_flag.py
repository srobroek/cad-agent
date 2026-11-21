# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Turn the calibration flag on for a test module that pins the behaviour of printer calibration.

The flag is off by default (release behaviour). A module that checks calibrated grades, or the guideline-only grade of a
service class, starts `pin_on()` in setUpModule and stops it in tearDownModule. The flag name is read from cad-record's
guidelines.py, the one place that holds it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest import mock

GUIDELINES = Path(__file__).resolve().parents[2] / "cad-record" / "scripts" / "guidelines.py"
MODULE = "cad_record_guidelines"  # the shared name the skills load it under


def pin_on() -> Any:
    """A patch that sets the calibration flag to "1"; `start()` it in setUpModule and `stop()` it in tearDownModule."""
    module = sys.modules.get(MODULE)
    if module is None:
        spec = importlib.util.spec_from_file_location(MODULE, GUIDELINES)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[MODULE] = module
        spec.loader.exec_module(module)
    return mock.patch.dict("os.environ", {module.CALIBRATION_FLAG: "1"})
