#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""The FDM overhang verdict at its limit, and on the corpus twins that must not fail it.

Run: python3 -m unittest discover -s skills/cad-design/tests -p test_overhang_boundary.py
Needs uv: the geometry is probed in fixtures/overhang_boundary_probe.py, which brings its own OCP.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from calibration_flag import pin_on

PROBE = Path(__file__).resolve().parent / "fixtures" / "overhang_boundary_probe.py"


_FLAG = pin_on()


def setUpModule() -> None:
    """These tests pin the behaviour of printer calibration, which the calibration flag turns on."""
    _FLAG.start()


def tearDownModule() -> None:
    _FLAG.stop()


@unittest.skipUnless(shutil.which("uv"), "uv is required to probe real geometry")
class OverhangBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run(["uv", "run", "--quiet", "--script", str(PROBE)], capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise AssertionError(f"overhang_boundary_probe.py failed: {done.stderr[-3000:]}")
        cls.report = json.loads(done.stdout)

    def test_the_45_degree_limit_is_judged_on_whole_degrees(self) -> None:
        """The probe rounds each sampled angle to a whole degree and the check fails only an angle above the limit. A
        45.4 degree undercut rounds to 45 and passes. A 45.6 degree one rounds to 46 and fails. A teardrop whose flanks
        sit at the limit relies on this."""
        self.assertEqual(self.report["wedges"], {"45.0": "pass", "45.4": "pass", "45.6": "fail"})

    def test_no_corpus_twin_fails_the_overhang_check(self) -> None:
        """Five valid parts once failed it: a round bore roof, a bridge measured along its length, and three feet with
        a flat ceiling under the body. Each is self-supporting now."""
        self.assertEqual(sorted(self.report["twins"]), [
            "additive_support_overhang_bridge/bore_roof", "fdm_horizontal_hole/square_roof_bridge",
            "stability_contact_hull/diagonal_pads", "stability_contact_hull/l_footprint", "stability_contact_hull/tiny_contact"])
        failing = {name: found for name, found in self.report["twins"].items() if any(outcome == "fail" for _, outcome in found)}
        self.assertEqual(failing, {})


if __name__ == "__main__":
    unittest.main()
