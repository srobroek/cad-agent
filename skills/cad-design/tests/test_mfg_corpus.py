#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Tests for the mutation-corpus harness itself (mfg_corpus/): rate arithmetic, registry, the rates command.

Run: python3 -m unittest discover -s skills/cad-design/tests -p test_mfg_corpus.py
Pure standard library; the families' own checks run through mfg_corpus/rates.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

CORPUS = Path(__file__).resolve().parent / "mfg_corpus"
sys.path.insert(0, str(CORPUS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus  # noqa: E402
from calibration_flag import pin_on  # noqa: E402

_FLAG = pin_on()


def setUpModule() -> None:
    """The corpus baselines were taken with printer calibration on (CAD_CALIBRATION=1); its rates move with the flag."""
    _FLAG.start()


def tearDownModule() -> None:
    _FLAG.stop()


def pair(ident: str, mutant: str, twin: str = "pass") -> corpus.Pair:
    return corpus.Pair(ident, "d", lambda is_mutant: mutant if is_mutant else twin)


def skipping(is_mutant: bool) -> str:
    raise corpus.Skip("no ocp")


def crashing(is_mutant: bool) -> str:
    raise RuntimeError("boom")


class RateTests(unittest.TestCase):
    def rows(self, *pairs: corpus.Pair) -> dict:
        return corpus.evaluate([corpus.Family("f", "chk", tuple(pairs))])[0]

    def test_rates_are_counted_per_variant(self) -> None:
        row = self.rows(pair("a", "fail"), pair("b", "fail"), pair("c", "unknown"), pair("d", "pass", "fail"))
        self.assertEqual((row["detection_rate"], row["abstention_rate"], row["false_positive_rate"]), (0.5, 0.25, 0.25))
        self.assertEqual((row["detected"], row["abstained"], row["missed"], row["false_positives"]), (2, 1, 1, 1))

    def test_skipped_pairs_leave_the_rates_and_are_counted(self) -> None:
        row = self.rows(pair("a", "fail"), corpus.Pair("b", "d", skipping))
        self.assertEqual((row["pairs"], row["ran"], row["skipped"], row["detection_rate"]), (2, 1, 1, 1.0))

    def test_a_crashing_check_is_an_error_not_a_miss_or_a_detection(self) -> None:
        row = self.rows(corpus.Pair("a", "d", crashing))
        self.assertEqual((row["errors"], row["detected"], row["missed"], row["detection_rate"]), (1, 0, 0, None))
        self.assertIn("boom", row["results"][0]["error"])

    def test_a_twin_crash_voids_the_mutant_verdict_too(self) -> None:
        def run(is_mutant: bool) -> str:
            if is_mutant:
                return "fail"
            raise RuntimeError("twin boom")

        row = self.rows(corpus.Pair("a", "d", run))
        self.assertEqual((row["errors"], row["detected"], row["missed"], row["detection_rate"]), (1, 0, 0, None))
        self.assertIn("twin boom", row["results"][0]["error"])
        self.assertIsNone(row["results"][0]["mutant"])

    def test_a_rejected_packet_is_an_error_with_its_reason_not_a_miss_or_a_detection(self) -> None:
        def refused(is_mutant: bool) -> str:
            raise corpus.Rejected("packet.invalid: it gives an obligation two outcomes")

        row = self.rows(corpus.Pair("a", "d", refused), pair("b", "fail"))
        self.assertEqual((row["errors"], row["ran"], row["detected"], row["missed"]), (1, 1, 1, 0))
        self.assertIn("two outcomes", row["results"][0]["error"])

    def test_a_known_gap_is_a_miss_whatever_the_mutant_returns_and_its_twin_still_counts(self) -> None:
        row = self.rows(corpus.Pair("a", "d", lambda m: "fail", known_gap="fails for the wrong reason"),
                        corpus.Pair("b", "d", lambda m: "unknown" if m else "pass", known_gap="abstains for the wrong reason"),
                        pair("c", "fail"))
        self.assertEqual((row["ran"], row["detected"], row["abstained"], row["missed"], row["known_gaps"]), (3, 1, 0, 2, 2))
        self.assertEqual((row["detection_rate"], row["false_positive_rate"]), (0.3333, 0.3333))
        self.assertEqual(row["results"][0]["known_gap"], "fails for the wrong reason")

    def test_a_family_with_nothing_run_has_no_rate(self) -> None:
        row = self.rows(corpus.Pair("a", "d", skipping))
        self.assertIsNone(row["detection_rate"])

    def test_an_unknown_outcome_word_is_an_error(self) -> None:
        self.assertEqual(self.rows(pair("a", "maybe"))["errors"], 1)


class RegistryTests(unittest.TestCase):
    def test_duplicate_family_ids_and_pair_ids_are_rejected(self) -> None:
        one = corpus.Family("f", "c", (pair("a", "fail"),))
        with self.assertRaises(ValueError):
            corpus.validate([one, one])
        with self.assertRaises(ValueError):
            corpus.validate([corpus.Family("g", "c", (pair("a", "fail"), pair("a", "fail")))])

    def test_a_family_module_dropped_in_the_directory_is_registered_and_underscore_files_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mine.py").write_text(
                "from corpus import Family, Pair\nFAMILY = Family('mine', 'c', (Pair('p', 'd', lambda m: 'fail' if m else 'pass'),))\n")
            (Path(tmp) / "_skip.py").write_text("raise SystemExit('must not load')\n")
            self.assertEqual([f.id for f in corpus.load_families(Path(tmp))], ["mine"])

    def test_the_shipped_registry_loads_with_unique_ids(self) -> None:
        corpus.load_families()


def write_family(directory: Path, name: str, mutant: str = "fail", twin: str = "pass", known_gap: str | None = None) -> None:
    (directory / f"{name}.py").write_text(
        "from corpus import Family, Pair\n"
        f"FAMILY = Family('{name}', 'c', (Pair('p', 'd', lambda m: '{mutant}' if m else '{twin}', known_gap={known_gap!r}),))\n")


def write_baseline(directory: Path, name: str, detection: float = 1.0, false_positive: float = 0.0, ran: int = 1) -> None:
    (directory / f"{name}.json").write_text(json.dumps(
        {"detection_rate": detection, "abstention_rate": 0.0, "false_positive_rate": false_positive, "ran": ran}))


class BaselineTests(unittest.TestCase):
    """corpus.check_baseline: the registry and baseline/ match one-to-one and no family regresses."""

    def problems(self, families: list[corpus.Family], baselines: dict[str, dict]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            for name, kwargs in baselines.items():
                write_baseline(Path(tmp), name, **kwargs)
            return corpus.check_baseline(corpus.evaluate(families), Path(tmp))

    def family(self, name: str, *pairs: corpus.Pair) -> corpus.Family:
        return corpus.Family(name, "c", pairs or (pair("a", "fail"),))

    def test_matching_baselines_have_no_problem(self) -> None:
        self.assertEqual(self.problems([self.family("f")], {"f": {}}), [])

    def test_a_family_without_a_baseline_file_is_a_problem(self) -> None:
        self.assertEqual(self.problems([self.family("f"), self.family("g")], {"f": {}}),
                         ["g: no baseline file baseline/g.json"])

    def test_a_restricted_run_still_needs_a_baseline_for_each_family_it_evaluated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_baseline(Path(tmp), "f")
            rows = corpus.evaluate([self.family("g")])
            self.assertEqual(corpus.check_baseline(rows, Path(tmp), whole_registry=False),
                             ["g: no baseline file baseline/g.json"])

    def test_a_restricted_run_ignores_baselines_of_families_it_did_not_evaluate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("f", "other"):
                write_baseline(Path(tmp), name)
            rows = corpus.evaluate([self.family("f")])
            self.assertEqual(corpus.check_baseline(rows, Path(tmp), whole_registry=False), [])

    def test_a_baseline_file_without_a_family_is_a_problem(self) -> None:
        self.assertEqual(self.problems([self.family("f")], {"f": {}, "gone": {}}),
                         ["gone: baseline file without a registered family"])

    def test_fewer_pairs_run_than_recorded_is_a_problem(self) -> None:
        skipped = corpus.Pair("b", "d", skipping)
        [problem] = self.problems([self.family("f", pair("a", "fail"), skipped)], {"f": {"ran": 2}})
        self.assertEqual(problem, "f: ran 1 pairs, baseline 2 (1 skipped)")

    def test_lower_detection_and_higher_false_positives_are_problems(self) -> None:
        problems = self.problems([self.family("f", pair("a", "pass", "fail"))], {"f": {}})
        self.assertEqual(problems, ["f: detection 0.0 below baseline 1.0", "f: false positives 1.0 above baseline 0.0"])

    def test_a_family_that_beats_its_baseline_is_fine(self) -> None:
        self.assertEqual(self.problems([self.family("f")], {"f": {"detection": 0.5, "false_positive": 0.5}}), [])


class RatchetTests(unittest.TestCase):
    """The shipped families against baseline/, run as rates.py --check-baseline in its own uv script environment
    (it carries OCP, which the test interpreter may lack). Under CI=true a missing uv fails instead of skipping.
    MFG_CORPUS_RATCHET_IN_CI_STEP=1 (set on the rest shard) skips it there: the freecad-gated shard's
    "Mutation corpus rates and baseline ratchet" step enforces the same check."""

    def test_no_family_regresses_from_its_baseline(self) -> None:
        if os.environ.get("MFG_CORPUS_RATCHET_IN_CI_STEP") == "1":
            self.skipTest("enforced by the freecad-gated shard's 'Mutation corpus rates and baseline ratchet' CI step")
        if shutil.which("uv") is None:
            if os.environ.get("CI") == "true":
                self.fail("uv is required for the corpus ratchet in CI")
            self.skipTest("uv is required to run rates.py in its own environment")
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(["uv", "run", "--script", str(CORPUS / "rates.py"), "--check-baseline", "--out",
                                   str(Path(tmp) / "r.json")], capture_output=True, text=True, check=False, timeout=1800)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class RatchetGuardTests(unittest.TestCase):
    """RatchetTests with uv unavailable: it fails under CI=true, and skips there only for the dedupe marker."""

    METHOD = "test_no_family_regresses_from_its_baseline"
    MARKER = "MFG_CORPUS_RATCHET_IN_CI_STEP"

    def outcome(self, **env: str) -> unittest.TestResult:
        """Run the ratchet test. Hide uv from PATH, apply `env`, and remove the marker unless `env` has it."""
        result = unittest.TestResult()
        with mock.patch.dict(os.environ, env), mock.patch("shutil.which", return_value=None):
            if self.MARKER not in env:
                os.environ.pop(self.MARKER, None)
            RatchetTests(self.METHOD).run(result)
        return result

    def test_in_ci_without_uv_and_without_the_marker_it_fails_and_does_not_skip(self) -> None:
        result = self.outcome(CI="true")
        self.assertEqual((len(result.failures), result.skipped, result.errors), (1, [], []))
        self.assertIn("uv is required", result.failures[0][1])

    def test_only_the_marker_value_1_skips_it_and_the_reason_names_the_ci_step(self) -> None:
        skipped = self.outcome(CI="true", **{self.MARKER: "1"})
        [(_, reason)] = skipped.skipped
        self.assertIn("Mutation corpus rates and baseline ratchet", reason)
        self.assertEqual((skipped.failures, skipped.errors), ([], []))
        for value in ("", "0", "true", "yes"):
            with self.subTest(marker=value):
                result = self.outcome(CI="true", **{self.MARKER: value})
                self.assertEqual((len(result.failures), result.skipped), (1, []))

    def test_outside_ci_without_uv_it_skips_for_the_missing_tool(self) -> None:
        result = self.outcome(CI="")
        [(_, reason)] = result.skipped
        self.assertIn("uv is required", reason)
        self.assertEqual((result.failures, result.errors), ([], []))


DEPS = importlib.util.find_spec("yaml") is not None and importlib.util.find_spec("jsonschema") is not None
DOC_DEPS = DEPS and importlib.util.find_spec("defusedxml") is not None


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class ProfileTests(unittest.TestCase):
    def test_the_test_profile_is_a_valid_record_with_the_calibrations_the_additive_checks_read(self) -> None:
        import capability_profile

        sys.path.insert(0, str(CORPUS.parents[1] / "scripts"))
        import process_checks

        fx_path = CORPUS.parents[2] / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py"
        spec = importlib.util.spec_from_file_location("mfg_corpus_fx", fx_path)
        assert spec and spec.loader
        fx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fx)
        documents = fx.record(capabilities=capability_profile.capabilities(), materials=capability_profile.materials())
        with tempfile.TemporaryDirectory() as tmp:
            hw = fx.write_record(Path(tmp) / "project" / "hw", documents)
            _, problems = process_checks.intent().check_record(hw)
        self.assertEqual([p.code for p in problems if p.severity == "error"], [])
        found = {c["characteristic"] for c in capability_profile.capabilities()[0]["calibrations"]}
        self.assertEqual(found, {"overhang_angle", "bridge_span", "min_wall", "min_feature", "min_bed_contact_area"})


INNER_CHECKS = frozenset({"check_order", "check_joints", "check_version", "check_record", "assess", "probe",
                          "assess_printed", "access_outcomes", "load"})  # inner check functions, and the old helpers that called them
ALLOWED_IMPORTS = frozenset({"corpus", "support", "doc_support", "OCP"})


def inner_check_uses(directory: Path) -> list[str]:
    """Where a family module imports or calls a check below the packet: a check function by name, subprocess (a check script
    run for its own report), or any module that is neither the standard library nor the corpus helpers. Families take
    their verdicts from support.py / doc_support.py, which run the real path to the packet and evidence."""
    import ast

    found = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
                continue
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
                found += [f"{path.name}:{node.lineno} imports {alias.name}" for alias in node.names if alias.name in INNER_CHECKS]
            found += [f"{path.name}:{node.lineno} imports {root}" for root in roots
                      if root == "subprocess" or (root not in sys.stdlib_module_names and root not in ALLOWED_IMPORTS)]
            if isinstance(node, ast.Call):
                callee = node.func
                name = callee.id if isinstance(callee, ast.Name) else callee.attr if isinstance(callee, ast.Attribute) else ""
                if name in INNER_CHECKS:
                    found.append(f"{path.name}:{node.lineno} calls {name}")
    return found


class PacketVerdictGuardTests(unittest.TestCase):
    """A verdict is what the packet and evidence conclude, so a family may not run an inner check itself."""

    def test_no_shipped_family_imports_or_calls_a_check_below_the_packet(self) -> None:
        self.assertEqual(inner_check_uses(corpus.FAMILIES_DIR), [])

    def test_the_guard_names_a_family_that_calls_the_check_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.py").write_text(
                "import subprocess\nfrom support import check_order, verdict\n\n"
                "def run(mutant):\n    return verdict(check_order([]).values())\n", encoding="utf-8")
            self.assertEqual(inner_check_uses(Path(tmp)), ["bad.py:1 imports subprocess", "bad.py:2 imports check_order",
                                                          "bad.py:5 calls check_order"])


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class GuidelineFallbackTests(unittest.TestCase):
    """With printer calibration off (the release behaviour) the corpus verdict rests on the shipped guideline sets, as the
    verdict form_check gives does. Without the catalog it would read UNKNOWN for a check a guideline backs."""

    def test_a_0_5_mm_plate_fails_the_shipped_0_8_mm_minimum_wall_guideline_with_calibration_off(self) -> None:
        import support

        try:
            _, pc = support.printed_record()
            with mock.patch.object(pc.guidelines(), "calibration_enabled", return_value=False):  # module-level pin is on
                verdict = support.printed_verdict(support.solids().box(0, 0, 0, 30, 20, 0.5), "process.fdm.geometry@")
        except corpus.Skip as skip:
            self.skipTest(str(skip))
        self.assertEqual(verdict, "fail")


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class PacketRefusalTests(unittest.TestCase):
    """A packet that the schema or evidence refuses is a miss with its reason, never a verdict."""

    @classmethod
    def setUpClass(cls) -> None:
        import support

        cls.support = support
        cls.record, pc = support.printed_record()
        # with no geometry the checks give every obligation the outcome unknown, so the packet result reads unknown
        cls.packet = pc.assess(cls.record, {}, today=support.TODAY).packet({"version_manifest": "0" * 64, "producer_code": "0" * 64})

    def pair_result(self, packet: dict) -> corpus.PairResult:
        def run(is_mutant: bool) -> str:
            return self.support.release_verdict(self.record, packet, "process.fdm.", today=self.support.TODAY)

        return corpus.run_pair(corpus.Pair("p", "d", run))

    def test_a_schema_invalid_packet_is_rejected_with_the_schema_reason(self) -> None:
        result = self.pair_result({"schema_version": 1})
        self.assertTrue((result.error or "").startswith("Rejected: schema: packet does not match manufacturing-check-result.schema.json"),
                        result.error)

    def test_a_schema_valid_packet_that_contradicts_its_outcomes_is_rejected_with_the_evidence_reason(self) -> None:
        self.assertEqual(self.packet["result"], "unknown")
        lying = {**self.packet, "result": "pass"}
        result = self.pair_result(lying)
        self.assertIn("evidence: ", result.error or "")
        self.assertIn("its result 'pass' is not what its outcomes show (unknown)", result.error or "")

    def test_a_rejected_packet_is_an_error_in_the_family_row_never_a_miss_or_a_detection(self) -> None:
        def run(is_mutant: bool) -> str:
            return self.support.release_verdict(self.record, {"schema_version": 1}, "process.fdm.")

        row = corpus.evaluate([corpus.Family("f", "c", (corpus.Pair("p", "d", run),))])[0]
        self.assertEqual((row["ran"], row["errors"], row["detected"], row["abstained"], row["missed"]), (0, 1, 0, 0, 0))


class CaseErrorTests(unittest.TestCase):
    """A case whose run errors is an error: never a miss, never an abstention, never in a rate, and it fails rates.py
    whatever the baseline tolerates (issue #187). Packet-level cases fail through their `run`. Oracle cases fail through
    support.oracle_verdict, and these tests replace its build and check commands with fakes."""

    STDERR = "Traceback ...\nRecordError: v1 records are refused"

    def rates(self, *pairs: corpus.Pair, check_baseline: bool = False, tolerance: bool = False,
              matching: dict | None = None) -> tuple[int, str]:
        import contextlib
        import io

        import rates

        with tempfile.TemporaryDirectory() as tmp:
            families = Path(tmp) / "families"
            baseline = Path(tmp) / "baseline"
            families.mkdir()
            baseline.mkdir()
            (families / "fam.py").write_text(
                "import test_mfg_corpus_pairs\nfrom corpus import Family\n"
                "FAMILY = Family('fam', 'chk', test_mfg_corpus_pairs.PAIRS)\n", encoding="utf-8")
            if tolerance:  # a baseline that tolerates anything
                write_baseline(baseline, "fam", detection=0.0, false_positive=1.0, ran=0)
            if matching is not None:  # a baseline equal to the rates of the pairs that ran cleanly
                write_baseline(baseline, "fam", **matching)
            argv = ["--families-dir", str(families), "--baseline-dir", str(baseline), "--out", str(Path(tmp) / "o.json")]
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"test_mfg_corpus_pairs": mock.Mock(PAIRS=pairs)}), contextlib.redirect_stdout(out):
                code = rates.main([*argv, "--check-baseline"] if check_baseline else argv)
        return code, out.getvalue()

    def oracle_pair(self, ident: str, build_returncode: int, build_stdout: str = "", check_returncode: int = 0) -> corpus.Pair:
        """A pair through support.oracle_verdict whose build script and `cad_model check` are fakes with these results."""
        import support

        def fake(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if str(support.ORACLE_BUILD) in argv:
                return subprocess.CompletedProcess(argv, build_returncode, build_stdout, self.STDERR)
            return subprocess.CompletedProcess(argv, check_returncode, "", self.STDERR)

        def run(is_mutant: bool) -> str:
            with mock.patch("shutil.which", return_value="/bin/uv"), mock.patch("subprocess.run", fake):
                support._ORACLE_CACHE.clear()
                return support.oracle_verdict({"id": ident, "mutant": is_mutant}, None, support.AGREEMENT)

        return corpus.Pair(ident, "d", run)

    def test_an_error_beside_clean_pairs_fails_a_baseline_run_that_matches_the_clean_rates_exactly(self) -> None:
        """The partial failure of issue #187: the surviving pairs meet their baseline, so only the error can fail the run."""
        matching = {"detection": 1.0, "false_positive": 0.0, "ran": 1}
        for label, broken in (("packet-level", corpus.Pair("broken", "d", crashing)),
                              ("oracle", self.oracle_pair("broken", 2))):
            with self.subTest(case=label):
                code, text = self.rates(pair("fine", "fail"), broken, check_baseline=True, matching=matching)
                self.assertEqual(code, 1, text)
                self.assertIn("error: fam/broken:", text)
                self.assertNotIn("baseline:", text)

    def test_a_clean_family_that_matches_its_baseline_exactly_exits_zero(self) -> None:
        code, text = self.rates(pair("fine", "fail"), check_baseline=True, matching={"detection": 1.0, "false_positive": 0.0, "ran": 1})
        self.assertEqual(code, 0, text)
        self.assertNotIn("baseline:", text)

    def test_a_mutant_whose_run_errors_fails_rates_and_names_the_case(self) -> None:
        for check_baseline in (False, True):
            with self.subTest(check_baseline=check_baseline):
                code, text = self.rates(corpus.Pair("broken", "d", crashing), check_baseline=check_baseline, tolerance=True)
                self.assertEqual(code, 1, text)
                self.assertRegex(text, r"error: fam/broken: RuntimeError: boom")

    def test_a_twin_whose_run_errors_is_an_error_not_a_missing_false_positive(self) -> None:
        def run(is_mutant: bool) -> str:
            if is_mutant:
                return "fail"
            raise RuntimeError("twin boom")

        row = corpus.evaluate([corpus.Family("f", "c", (corpus.Pair("a", "d", run),))])[0]
        self.assertEqual((row["errors"], row["ran"], row["detected"], row["false_positives"], row["false_positive_rate"]),
                         (1, 0, 0, 0, None))
        code, text = self.rates(corpus.Pair("a", "d", run), tolerance=True)
        self.assertEqual(code, 1, text)
        self.assertIn("error: fam/a: RuntimeError: twin boom", text)

    def test_an_error_leaves_every_rate_of_the_pairs_that_ran_untouched(self) -> None:
        row = corpus.evaluate([corpus.Family("f", "c", (pair("a", "fail"), pair("b", "pass", "fail"), corpus.Pair("c", "d", crashing)))])[0]
        self.assertEqual((row["ran"], row["errors"], row["missed"]), (2, 1, 1))
        self.assertEqual((row["detection_rate"], row["abstention_rate"], row["false_positive_rate"]), (0.5, 0.0, 0.5))

    def test_a_clean_run_that_does_not_detect_is_a_miss_and_not_an_error(self) -> None:
        code, text = self.rates(pair("quiet", "pass"), check_baseline=True, tolerance=True)
        self.assertEqual(code, 0, text)
        self.assertNotIn("error:", text)
        row = corpus.evaluate([corpus.Family("f", "c", (pair("quiet", "pass"),))])[0]
        self.assertEqual((row["errors"], row["missed"], row["ran"]), (0, 1, 1))

    def test_a_setup_exception_before_the_check_is_an_error(self) -> None:
        def run(is_mutant: bool) -> str:
            raise ValueError("the corpus record is invalid: ['record.v1 refused']")

        code, text = self.rates(corpus.Pair("setup", "d", run), tolerance=True)
        self.assertEqual(code, 1, text)
        self.assertIn("error: fam/setup: ValueError: the corpus record is invalid", text)

    def test_an_oracle_case_whose_build_exits_non_zero_is_an_error_with_command_and_stderr(self) -> None:
        for check_baseline in (False, True):
            with self.subTest(check_baseline=check_baseline):
                code, text = self.rates(self.oracle_pair("build", 2), check_baseline=check_baseline, tolerance=True)
                self.assertEqual(code, 1, text)
                self.assertIn("error: fam/build:", text)
                self.assertIn("build_case.py", text)
                self.assertIn("exited 2", text)
                self.assertIn("RecordError: v1 records are refused", text)

    def test_an_oracle_case_whose_build_prints_no_version_is_an_error(self) -> None:
        code, text = self.rates(self.oracle_pair("silent", 0), tolerance=True)
        self.assertEqual(code, 1, text)
        self.assertIn("error: fam/silent:", text)

    def test_an_oracle_case_whose_check_crashes_or_leaves_no_packet_is_an_error_naming_cad_model_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            built = json.dumps({"hw": tmp, "label": "v1"}) + "\n"
            for returncode, says in ((3, "exited 3"), (1, "left no packet at hw/checks/v1/cad/result.json")):
                with self.subTest(returncode=returncode):
                    code, text = self.rates(self.oracle_pair("check", 0, built, returncode), tolerance=True)
                    self.assertEqual(code, 1, text)
                    self.assertIn("error: fam/check:", text)
                    self.assertIn("cad_model check", text)
                    self.assertIn(says, text)

    @unittest.skipUnless(DEPS and shutil.which("uv"), "pyyaml, jsonschema and uv are required")
    def test_a_packet_level_command_failure_names_the_command_and_the_stderr_tail(self) -> None:
        import support

        def fake(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 3, "", self.STDERR)

        def run(is_mutant: bool) -> str:
            return support.readback_verdict("KERNEL = 'cadquery'\nPARAMS = {}\n")

        with mock.patch("subprocess.run", fake):
            code, text = self.rates(corpus.Pair("kernel", "d", run), tolerance=True)
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"(?s)error: fam/kernel: RuntimeError: cad_model run exited 3 \(`uv run --quiet --script \S*cad_model\.py run .*`\):.*RecordError")


@unittest.skipUnless(DEPS and shutil.which("uv"), "pyyaml, jsonschema and uv are required")
class NativeExitCodeTests(unittest.TestCase):
    """cad_model documents exit 0 and 1 (and 2 for usage). A build or check killed by a signal is a harness error, not an abstention."""

    def verdict_row(self, run_code: int, check_code: int, manifest: bool) -> dict:
        import support

        def fake(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            step = argv[5]  # uv run --quiet --script <cad_model.py> <step> ...
            if step == "run":
                if manifest:
                    target = Path(argv[6]) / "versions" / "v1" / "manifest.json"
                    target.parent.mkdir(parents=True)
                    target.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(argv, run_code, "", "")
            return subprocess.CompletedProcess(argv, check_code, "", "")

        def run(is_mutant: bool) -> str:
            return support.readback_verdict("KERNEL = 'cadquery'\nPARAMS = {}\n")

        with mock.patch("subprocess.run", fake):
            return corpus.evaluate([corpus.Family("f", "c", (corpus.Pair("p", "d", run),))])[0]

    def test_a_build_killed_by_a_signal_is_an_error_not_a_miss_or_an_abstention(self) -> None:
        row = self.verdict_row(-9, 0, manifest=False)
        self.assertEqual((row["errors"], row["abstained"], row["missed"]), (1, 0, 0))
        self.assertIn("cad_model run exited -9", row["results"][0]["error"])

    def test_a_check_killed_by_a_signal_is_an_error_not_a_miss_or_an_abstention(self) -> None:
        row = self.verdict_row(0, -9, manifest=True)
        self.assertEqual((row["errors"], row["abstained"], row["missed"]), (1, 0, 0))
        self.assertIn("cad_model check exited -9", row["results"][0]["error"])

    def test_a_check_that_leaves_no_packet_is_an_error_naming_the_command_and_the_file(self) -> None:
        row = self.verdict_row(0, 0, manifest=True)
        self.assertEqual((row["errors"], row["abstained"], row["missed"], row["ran"]), (1, 0, 0, 0))
        error = row["results"][0]["error"]
        self.assertIn("cad_model check exited 0", error)
        self.assertIn("cad_model.py check v1 --hw", error)
        self.assertIn("checks/v1/cad/result.json", error)

    def test_a_build_that_refuses_the_part_with_exit_1_and_no_version_is_an_error_naming_the_command(self) -> None:
        """The refused part never reached cad_model check, so the case has no verdict (issue #192)."""
        row = self.verdict_row(1, 0, manifest=False)
        self.assertEqual((row["errors"], row["abstained"], row["missed"], row["ran"]), (1, 0, 0, 0))
        error = row["results"][0]["error"]
        self.assertIn("cad_model run exited 1", error)
        self.assertIn("cad_model.py run", error)

    def test_an_access_packet_command_that_fails_is_an_error_naming_the_command(self) -> None:
        import support

        def fake(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 3, "", "access.py refused the record")

        def run(is_mutant: bool) -> str:
            return support.access_packet_verdict(Path("hw"), Path("parts"), ["TST-0102=TST-0101"], "assembly.access@")

        with mock.patch("subprocess.run", fake):
            row = corpus.evaluate([corpus.Family("f", "c", (corpus.Pair("p", "d", run),))])[0]
        self.assertEqual((row["errors"], row["missed"], row["ran"]), (1, 0, 0))
        self.assertIn("access.py exited 3", row["results"][0]["error"])
        self.assertIn("access.py --hw hw --parts-dir parts --attach TST-0102=TST-0101", row["results"][0]["error"])
        self.assertIn("access.py refused the record", row["results"][0]["error"])

    def test_a_twin_whose_build_refuses_the_part_is_an_error_not_a_clean_twin(self) -> None:
        def fake(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, "", "build refused the twin")

        twin = family_pair("form_check_kernel_validity", "t_bore_unorientable_face")
        with mock.patch("subprocess.run", fake), self.assertRaises(RuntimeError) as raised:
            twin.run(False)
        self.assertIn("cad_model run exited 1", str(raised.exception))
        self.assertIn("build refused the twin", str(raised.exception))


@unittest.skipUnless(DEPS and shutil.which("uv"), "pyyaml, jsonschema and uv are required")
class ReadbackManufactureStateTests(unittest.TestCase):
    """readback_verdict builds its one-part project with the manufacturing state the caller names (issue #192)."""

    def manufacture_the_build_reads(self, **state: object) -> list[object]:
        import support
        import yaml

        seen: list[object] = []

        def fake(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            part = Path(argv[6]) / "parts" / "TST-0001" / "part.yaml"  # uv run --quiet --script <cad_model.py> run <hw> ...
            seen.append(yaml.safe_load(part.read_text(encoding="utf-8")).get("manufacture"))
            return subprocess.CompletedProcess(argv, 1, "", "refused")

        with mock.patch("subprocess.run", fake), self.assertRaises(RuntimeError):
            support.readback_verdict("KERNEL = 'cadquery'\nPARAMS = {}\n", **state)  # type: ignore[arg-type]
        return seen

    def test_the_part_record_the_build_reads_holds_the_named_manufacturing_state(self) -> None:
        different = {"relationship": "different", "state": "repaired", "reason": "corpus_twin", "decision": "DR-001",
                     "volume_abs_tolerance_mm3": 700.0}
        for label, state, expected in (("different", {"manufacture": different}, [different]), ("unnamed", {}, [None])):
            with self.subTest(state=label):
                self.assertEqual(self.manufacture_the_build_reads(**state), expected)


def family_pair(family_id: str, pair_id: str) -> corpus.Pair:
    family = next(item for item in corpus.load_families() if item.id == family_id)
    return next(item for item in family.pairs if item.id == pair_id)


@unittest.skipUnless(DOC_DEPS, "pyyaml, jsonschema and defusedxml are required")
class DocLeakTests(unittest.TestCase):
    """A doc_* mutant carries its defect and nothing else: the identifier or step it removes is absent from EVERY document the
    generator writes (build guide, okh.toml, DIN checklist), and the twin still has it. The check reads the whole output, so a
    string that survives in another field (an interface display text, say) shows up."""

    NAMES = ("index.md", "okh.toml", "din-spec-3105-1-checklist.md")

    def documents(self, family_id: str, pair_id: str, mutant: bool) -> dict[str, str]:
        """The documents of build_guide.make_guide for the pair's record, as the pair's own run builds it."""
        import doc_support

        module = doc_support.load("cad_record_build_guide", doc_support.RECORD / "scripts" / "build_guide.py")
        found: dict[str, str] = {}

        def capture(hw: Path, root: Path, prefixes: tuple[str, ...]) -> str:
            version = hw / "versions" / doc_support.VERSION
            version.mkdir(parents=True, exist_ok=True)
            (version / "manifest.json").write_text(json.dumps({"label": doc_support.VERSION, "manufacturing": {}}), encoding="utf-8")
            found.update(module.make_guide(hw, doc_support.VERSION, root / "docs"))
            return "pass"

        with mock.patch.object(doc_support, "judge", capture):
            family_pair(family_id, pair_id).run(mutant)
        self.assertEqual(sorted(found), sorted(self.NAMES))
        return found

    def assert_absent_from_mutant_present_in_twin(self, family_id: str, pair_id: str, tokens: tuple[str, ...]) -> None:
        mutant, twin = self.documents(family_id, pair_id, True), self.documents(family_id, pair_id, False)
        for token in tokens:
            with self.subTest(pair=pair_id, token=token):
                self.assertEqual([name for name, text in mutant.items() if token in text], [], "survives in the mutant")
                self.assertTrue([name for name, text in twin.items() if token in text], "missing from the twin")

    def test_each_removed_step_or_field_is_absent_from_every_document(self) -> None:
        cases = {
            ("doc_heatset_insert", "iron_missing"): ("Soldering iron", "230"),
            ("doc_heatset_insert", "iron_temperature"): ("230",),
            ("doc_heatset_insert", "pressing_depth"): ("depth min",),
            ("doc_retaining_ring", "no_pliers"): ("pliers",),
            ("doc_fastener_bom", "hole_without_fastener"): ("SCR-0001", "Fasten item 12"),
        }
        for (family_id, pair_id), tokens in cases.items():
            self.assert_absent_from_mutant_present_in_twin(family_id, pair_id, tokens)

    def test_the_added_screws_of_the_reverse_pair_are_in_the_mutant_only(self) -> None:
        mutant = self.documents("doc_fastener_bom", "fastener_without_hole", True)
        twin = self.documents("doc_fastener_bom", "fastener_without_hole", False)
        for token in ("SCR-0002", "M3"):
            with self.subTest(token=token):
                self.assertTrue([name for name, text in mutant.items() if token in text])
                self.assertEqual([name for name, text in twin.items() if token in text], [])


@unittest.skipUnless(DOC_DEPS, "pyyaml, jsonschema and defusedxml are required")
class DocTreeLeakTests(unittest.TestCase):
    """A doc_purchased_designation mutant carries its defect and nothing else: the identifier it removes is absent from EVERY
    file of its tree (the record, the BOM and the generated build guide), and the twin has it."""

    def tree(self, pair_id: str, mutant: bool) -> dict[str, str]:
        import doc_support

        module = doc_support.load("cad_record_build_guide", doc_support.RECORD / "scripts" / "build_guide.py")
        found: dict[str, str] = {}

        def capture(hw: Path, *prefixes: str) -> str:
            root = hw.parent
            version = hw / "versions" / doc_support.VERSION
            version.mkdir(parents=True, exist_ok=True)
            (version / "manifest.json").write_text(json.dumps({"label": doc_support.VERSION, "manufacturing": {}}), encoding="utf-8")
            documents = module.make_guide(hw, doc_support.VERSION, root / "guide")
            found.update({str(path.relative_to(root)): path.read_text(encoding="utf-8", errors="replace")
                          for path in sorted(root.rglob("*")) if path.is_file()})
            found.update({f"generated/{name}": text for name, text in documents.items()})
            return "pass"

        with mock.patch.object(doc_support, "record_verdict", capture):
            family_pair("doc_purchased_designation", pair_id).run(mutant)
        return found

    def test_the_identifier_of_each_pair_is_in_no_file_of_the_mutant_and_in_the_twin(self) -> None:
        corpus.load_families()
        tokens = sys.modules["mfg_corpus_family_doc_purchased_designation"].TOKENS
        self.assertEqual(sorted(tokens), ["designation_standard", "mpn_non_standard", "title_with_size", "title_without_size"])
        for pair_id, token in tokens.items():
            mutant, twin = self.tree(pair_id, True), self.tree(pair_id, False)
            with self.subTest(pair=pair_id, token=token):
                self.assertTrue([name for name in mutant if name.startswith("generated/")], "the guide is part of the tree")
                for variant in (mutant, twin):
                    self.assertEqual([name for name in variant if "decisions" in name], [], "the record has no decision file")
                    self.assertEqual([name for name, text in variant.items() if re.search(r"DR-\d", text)], [], "nothing cites a decision")
                self.assertEqual([name for name, text in mutant.items() if token in text], [], "survives in the mutant")
                self.assertTrue([name for name, text in twin.items() if token in text], "missing from the twin")


@unittest.skipUnless(DOC_DEPS, "pyyaml, jsonschema and defusedxml are required")
class DocRecordLeakTests(unittest.TestCase):
    """The doc_manufacturing and doc_retaining_ring/no_groove_part mutants generate no document, so the check reads the record:
    the part document of the mutant lacks what the pair removes, and the twin has it."""

    def dump(self, part: dict) -> str:
        return json.dumps(part, sort_keys=True, default=str)

    def test_a_stock_less_printed_part_names_no_material_profile_anywhere(self) -> None:
        import doc_support

        mutant, twin = self.dump(doc_support.fdm_part(material=False)), self.dump(doc_support.fdm_part())
        self.assertNotIn(doc_support.POLYMER, mutant)
        self.assertIn(doc_support.POLYMER, twin)

    def test_the_other_printed_part_mutants_lack_the_key_they_remove(self) -> None:
        import doc_support

        for kwargs, token in (({"orientation": False}, '"orientation"'), ({"supports": False}, '"supports"'),
                              ({"route": False}, '"print"')):
            with self.subTest(removed=token):
                self.assertNotIn(token, self.dump(doc_support.fdm_part(**kwargs)))
                self.assertIn(token, self.dump(doc_support.fdm_part()))

    def test_a_setup_less_milled_part_has_none_of_the_setup_keys(self) -> None:
        import doc_support

        mutant, twin = self.dump(doc_support.cnc_part(setup=False)), self.dump(doc_support.cnc_part())
        for token in ('"setup"', '"datums"', '"access"', '"workholding"', '"dimensions"'):
            with self.subTest(token=token):
                self.assertNotIn(token, mutant)
                self.assertIn(token, twin)

    def test_a_milled_part_without_workholding_keeps_the_rest_of_its_setup(self) -> None:
        import doc_support

        mutant = self.dump(doc_support.cnc_part(workholding=False))
        self.assertNotIn('"workholding"', mutant)
        for token in ('"setup"', '"datums"', '"access"', '"dimensions"'):
            self.assertIn(token, mutant)

    def test_a_pin_without_a_groove_has_no_groove_in_its_drawing(self) -> None:
        import doc_support
        import yaml

        corpus.load_families()
        family = sys.modules["mfg_corpus_family_doc_retaining_ring"]
        for mutant in (True, False):
            with self.subTest(mutant=mutant), doc_support.workspace() as root:
                hw = doc_support.lamp(root)
                family.part_groove(hw, mutant)
                text = (hw / "parts" / "LMP-0005" / "part.yaml").read_text(encoding="utf-8")
                self.assertEqual("groove" in yaml.safe_load(text)["drawing"]["features"], not mutant)
                self.assertEqual("groove" in text, not mutant)


@unittest.skipUnless(DEPS, "pyyaml and jsonschema are required")
class DocVerdictPathTests(unittest.TestCase):
    """The verdicts of the doc_* families come from the packet and evidence, so each helper changes its answer when what it
    reads changes, and it refuses a packet the schema rejects. A constant verdict or a swallowed error cannot pass these."""

    def test_the_four_manufacturing_defects_are_detected_by_the_route_obligations(self) -> None:
        for pair_id in ("fdm_orientation", "fdm_supports", "cnc_setup", "cnc_workholding"):
            with self.subTest(pair=pair_id):
                pair = family_pair("doc_manufacturing", pair_id)
                self.assertIsNone(pair.known_gap)
                self.assertEqual((pair.run(True), pair.run(False)), ("fail", "pass"))

    def test_a_failing_joint_fact_of_the_record_fails_the_joint_verdict(self) -> None:
        import doc_support

        fx = doc_support.fixtures()
        short = {"criterion": fx.criterion("edge_distance", ">=", 4.5, "mm", source=fx.source("standard", "TST-STD-2 insert edge distance")),
                 "distance": fx.known(fx.quantity(1.0, "mm"))}

        def change(hw: Path, mutant: bool) -> None:
            if mutant:
                doc_support.edit(hw, "assembly.yaml", lambda assembly: doc_support.operation(
                    assembly, "heat_set_socket_insert").update(edge_distance=short))

        run = doc_support.lamp_verdict(change, "assembly.joint@op:heat_set_socket_insert")
        self.assertEqual((run(True), run(False)), ("fail", "unknown"))

    def bought(self, **purchase: object) -> dict:
        import doc_support

        fx = doc_support.fixtures()
        facts = {"identity": fx.known("TST-BRG 608"),
                 "specification": fx.known("TST ISO 15 608", "standard", [fx.source("standard", "TST ISO 15")]),
                 "ratings": fx.known("TST C 3.3 kN", "supplier_typical", [fx.source("datasheet", "TST bearing")]),
                 "availability": fx.known("TST stock item")}
        facts.update(purchase)
        part = doc_support.made_part("TST-0003", [fx.operation("buy", "purchased", purchase=facts)], "purchased", None)
        part.update(make_buy="buy", procurement={"mode": "catalog", "manufacturer_part_number": "TST-BRG-608"})
        part["intent"]["material_profile"] = fx.not_applicable("catalogue item")
        return part

    def test_the_purchase_verdict_follows_the_facts_of_the_record(self) -> None:
        import doc_support

        fx = doc_support.fixtures()
        known = doc_support.route_verdict(self.bought(), "purchase.identity@")
        unknown = doc_support.route_verdict(self.bought(identity=fx.unknown("not recorded")), "purchase.identity@")
        self.assertEqual((known, unknown), ("pass", "unknown"))

    def test_a_purchase_packet_that_only_the_schema_rejects_is_refused(self) -> None:
        """The packet is the real one plus a key the schema forbids (additionalProperties false). Evidence reads the packet
        without noticing, so only the schema step can refuse it, and the reason names that step."""
        import types

        import doc_support

        pc = doc_support.process_checks()
        real = pc.assess

        def assess_with_extra_key(*args: object, **kwargs: object) -> object:
            result = real(*args, **kwargs)
            return types.SimpleNamespace(packet=lambda dependencies: {**result.packet(dependencies), "unreviewed": True})

        with mock.patch.object(pc, "assess", assess_with_extra_key), self.assertRaisesRegex(corpus.Rejected, r"^schema:"):
            doc_support.route_verdict(self.bought(), "purchase.identity@")

    def test_purchase_verdict_runs_from_a_process_that_only_imports_support(self) -> None:
        """#106 calls it without doc_support, so it sets up its own import path."""
        script = (
            "import pathlib, sys, tempfile\n"
            f"sys.path.insert(0, {str(CORPUS)!r})\n"
            "import support\n"
            "fx = support.load('cad_record_mi_fixtures', support.SKILLS / 'cad-record' / 'tests' / 'fixtures' / 'manufacturing_intent.py')\n"
            "ev = support.load('cad_record_evidence', support.SKILLS / 'cad-record' / 'scripts' / 'evidence.py')\n"
            "with tempfile.TemporaryDirectory() as tmp:\n"
            "    hw = fx.write_record(pathlib.Path(tmp) / 'hw', fx.record())\n"
            "    record, _ = ev.mi.check_record(hw)\n"
            "print(support.purchase_verdict(record, 'route.consistency@'))\n")
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False, cwd=tempfile.gettempdir(),
                              env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        self.assertEqual(done.returncode, 0, done.stderr[-600:])
        self.assertEqual(done.stdout.strip(), "unknown")


class DependencyGuardTests(unittest.TestCase):
    """A family whose tool is absent skips its pairs: neither an error nor a verdict."""

    DOC = {"doc_fastener_bom", "doc_heatset_insert", "doc_install_step", "doc_instruction_order", "doc_manufacturing",
           "doc_retaining_ring"}
    NEEDS = {"yaml": {"assembly_operations_order", "access_assembly", "additive_min_feature", "additive_stability",
                      "additive_support_overhang_bridge", "stability_contact_hull"} | DOC,
             "jsonschema": DOC - {"doc_install_step", "doc_instruction_order"},
             "OCP": {"additive_min_feature", "additive_stability", "additive_support_overhang_bridge",
                     "stability_contact_hull"}}

    def test_pairs_skip_when_their_module_is_absent(self) -> None:
        """Absent for real: find_spec says so and `import <module>` raises ImportError, with any cached submodule gone."""
        real = importlib.util.find_spec
        families = {f.id: f for f in corpus.load_families()}
        for module, needing in self.NEEDS.items():
            hidden = {name: None for name in list(sys.modules) if name == module or name.startswith(module + ".")}
            with mock.patch("importlib.util.find_spec", lambda name, *a, _m=module: None if name == _m else real(name, *a)), \
                    mock.patch.dict(sys.modules, {**hidden, module: None}):
                for family in sorted(needing):
                    for item in families[family].pairs:
                        with self.subTest(module=module, family=family, pair=item.id):
                            result = corpus.run_pair(item)
                            self.assertEqual((result.skipped is not None, result.error), (True, None))


class CommandTests(unittest.TestCase):
    def run_rates(self, tmp: str, *args: str, families: tuple[str, ...] = ("alpha", "beta")) -> subprocess.CompletedProcess[str]:
        directory = Path(tmp) / "families"
        directory.mkdir(exist_ok=True)
        for name in families:
            write_family(directory, name)
        return subprocess.run([sys.executable, str(CORPUS / "rates.py"), "--families-dir", str(directory), "--out",
                               str(Path(tmp) / "r.json"), *args], capture_output=True, text=True, check=False)

    def test_a_known_gap_pair_is_listed_on_the_console_and_counted_as_a_miss_in_the_report(self) -> None:
        why = "fails because the span is measured along the roof"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "families"
            directory.mkdir()
            write_family(directory, "alpha", known_gap=why)
            write_family(directory, "beta")
            out = Path(tmp) / "r.json"
            done = subprocess.run([sys.executable, str(CORPUS / "rates.py"), "--families-dir", str(directory), "--out", str(out)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual([line for line in done.stdout.splitlines() if line.startswith("known gap:")],
                             [f"known gap: alpha/p: {why}"])
            alpha, beta = json.loads(out.read_text())["families"]
        self.assertEqual((alpha["ran"], alpha["detected"], alpha["missed"], alpha["known_gaps"], alpha["detection_rate"]),
                         (1, 0, 1, 1, 0.0))
        self.assertEqual((alpha["results"][0]["mutant"], alpha["results"][0]["known_gap"]), ("fail", why))
        self.assertEqual((beta["detected"], beta["known_gaps"]), (1, 0))

    def test_the_rate_command_writes_json_and_prints_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            done = self.run_rates(tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(f"report: {out}", done.stdout)
            data = json.loads(out.read_text())
            self.assertEqual(data["format"], 1)
            self.assertEqual([f["family"] for f in data["families"]], ["alpha", "beta"])

    def test_repeated_family_options_restrict_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_rates(tmp, "--family", "beta", "--family", "gamma", families=("alpha", "beta", "gamma"))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual([f["family"] for f in json.loads((Path(tmp) / "r.json").read_text())["families"]],
                             ["beta", "gamma"])

    def test_an_unknown_family_id_exits_non_zero_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_rates(tmp, "--family", "alpha", "--family", "nope")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("unknown family: nope", done.stderr)
            self.assertFalse((Path(tmp) / "r.json").exists())

    def test_check_baseline_exits_one_and_names_the_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baselines = Path(tmp) / "baseline"
            baselines.mkdir()
            write_baseline(baselines, "alpha")
            done = self.run_rates(tmp, "--check-baseline", "--baseline-dir", str(baselines))
            self.assertEqual(done.returncode, 1)
            self.assertIn("baseline: beta: no baseline file", done.stdout)

    def test_check_baseline_on_a_restricted_run_needs_that_familys_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baselines = Path(tmp) / "baseline"
            baselines.mkdir()
            write_baseline(baselines, "alpha")
            done = self.run_rates(tmp, "--family", "beta", "--check-baseline", "--baseline-dir", str(baselines))
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertIn("baseline: beta: no baseline file", done.stdout)

    def test_check_baseline_on_a_restricted_run_ignores_other_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            baselines = Path(tmp) / "baseline"
            baselines.mkdir()
            write_baseline(baselines, "alpha")
            done = self.run_rates(tmp, "--family", "alpha", "--check-baseline", "--baseline-dir", str(baselines))
            self.assertEqual(done.returncode, 0, done.stdout)


if __name__ == "__main__":
    unittest.main()
