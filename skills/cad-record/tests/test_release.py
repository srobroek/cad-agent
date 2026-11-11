#!/usr/bin/env python3
"""release.py: a frozen record releases one complete package; any gap refuses and writes nothing in hw/."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import yaml

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
RELEASE = SCRIPTS / "release.py"
BUILD_GUIDE = SCRIPTS / "build_guide.py"
STACKUP = SKILL.parent / "cad-fits" / "scripts" / "stackup.py"
LAMP = SKILL.parent.parent / "examples" / "lamp-arm" / "hw"


def load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


layout = load("cad_record_layout", SCRIPTS / "layout.py")
validate = load("cad_record_validate", SCRIPTS / "validate.py")
release = load("cad_record_release", RELEASE)
prune = load("cad_record_prune", SCRIPTS / "prune.py")
acceptance = load("cad_record_acceptance_fixtures", SKILL / "tests" / "fixtures" / "acceptance.py")
fx, impact = acceptance.fx, acceptance.impact


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def tree(root: Path) -> dict[str, str]:
    """Every file and directory below `root`: a file by its sha256, a directory as `dir`."""
    return {p.relative_to(root).as_posix(): ("dir" if p.is_dir() else sha(p)) for p in sorted(root.rglob("*"))}


def edit_yaml(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def edit_csv(path: Path, change: Callable[[list[dict[str, str]]], None]) -> None:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        columns, rows = reader.fieldnames, list(reader)
    change(rows)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(out.getvalue(), encoding="utf-8")


def publish(hw: Path, kind: str, subject: str, files: dict[str, str], status: str = "pass", version: str = "v1",
            recipe: dict[str, Any] | None = None) -> Path:
    staged = layout.staging(hw, f"bundle.{kind}")
    for name, text in files.items():
        write(staged / name, text)
    return layout.publish(hw, f"bundle.{kind}", {"V": version, "S": subject}, staged,
                          recipe=recipe or {"subject": subject}, producer="test", status=status).path


DRAWING = {"sheet-1.svg": "svg\n", "sheet-1.pdf": "pdf\n", "sheet-1.png": "png\n", "coverage.json": "{}\n",
           "package.json": "{}\n"}


def build_version(hw: Path, omit: tuple[tuple[str, str], ...] = (), failed: dict[str, str] | None = None,
                  attach: tuple[str, ...] = (), label: str = "v1") -> None:
    """hw/versions/<label> as cad-model run seals it: the record snapshot the layout's version.record set
    names, installed STEP per modelled part and the manufacturing outputs of every custom-made part. `omit`
    leaves (PN, file) out; `failed` marks a part's named manufacturing check failed; `attach` lists its
    `--attach REF=PARENT_REF` options."""
    folder = hw / "versions" / label
    pns = [p.parent.name for p in sorted((hw / "parts").glob("*/part.yaml"))]
    for name in layout.snapshot_names(hw, pns):
        target = folder / "record" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((hw / name).read_bytes())
    for name in ("assembly.step", "assembly.glb", "part-map.json", "metrics.json"):
        write(folder / name, f"{name}\n")
    parts, made = {}, {}
    for model in sorted((hw / "parts").glob("*/model.py")):
        pn = model.parent.name
        write(folder / "parts" / f"{pn}.step", f"installed {pn}\n")
        parts[pn] = {"source_sha256": sha(model), "step": f"parts/{pn}.step"}
        record = yaml.safe_load((model.parent / "part.yaml").read_text(encoding="utf-8"))
        if not layout.custom_made(record):
            continue
        outputs = layout.manufacture_policy(record).outputs
        directory = folder / "manufacturing" / pn
        files = {name: sha(write(directory / name, f"{name} of {pn}\n"))
                 for name in outputs if name != "manufacturing.json" and (pn, name) not in omit}
        check = (failed or {}).get(pn)
        row = {"status": "fail" if check else "pass", "complete": not check, "blocking_failures": [check] if check else [],
               "checks": {"step": {"status": "pass"}, check or "placement": {"status": "fail" if check else "pass"}}}
        sidecar = {"schema_version": 1, "PN": pn, "process": record["process"], "outputs": list(outputs),
                   "path": f"manufacturing/{pn}", "files": files, **row}
        files["manufacturing.json"] = sha(write(directory / "manufacturing.json", json.dumps(sidecar, indent=2)))
        made[pn] = {"PN": pn, "process": record["process"], "path": f"manufacturing/{pn}", "files": files,
                    **{key: row[key] for key in ("status", "complete", "blocking_failures")}}
    inventory = {p.relative_to(folder).as_posix(): sha(p) for p in sorted(folder.rglob("*")) if p.is_file()}
    manifest = {"schema_version": 3, "label": label, "product_id": "lamp-arm", "attach": list(attach), "parts": parts,
                "record": {k: v for k, v in inventory.items() if k.startswith("record/")}, "manufacturing": made,
                "files": inventory}
    write(folder / "manifest.json", json.dumps(manifest, indent=2))
    write(hw / "versions" / "latest.json", json.dumps({"label": label, "manifest": f"versions/{label}/manifest.json"}))


def frozen_lamp(hw: Path, *, before_build: Callable[[Path], None] | None = None, omit: tuple[tuple[str, str], ...] = (),
                failed: dict[str, str] | None = None, skip_bundles: tuple[str, ...] = (), attach: tuple[str, ...] = (),
                label: str = "v1", older: tuple[str, ...] = (), service_class: str = "appearance_concept",
                ledger: Callable[[Path, Any, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
                anchor: bool = True) -> None:
    """The lamp example migrated to record version 2 and reconciled for release (released revisions, a
    confirmed `service_class` use case and explicit routes, every software check of `label` passed by a
    sealed producer packet, verification passed with accepted ledger evidence), built as `label`, frozen at
    it with its acceptance digest (unless not `anchor`), every required bundle published, and DR-005
    approving it. Each `older` version is built first, with a check and a render bundle of its own;
    `ledger` may rewrite the accepted entries before the freeze."""
    fx.migrated_copy(LAMP, hw)
    for part in sorted((hw / "parts").glob("*/part.yaml")):
        edit_yaml(part, lambda data: data.update(rev="A"))
    edit_csv(hw / "bom.csv", lambda rows: [row.update(rev="A") for row in rows])
    write(hw / "analysis" / "cases" / "arm-load" / "case.yaml", "id: arm-load\n")
    write(hw / "docs" / "inspection.md", "# Inspection record\n")
    if before_build:
        before_build(hw)
    acceptance.release_ready(hw, service_class)
    if not yaml.safe_load((hw / "manufacturing" / "selection.yaml").read_text(encoding="utf-8")).get("candidates"):
        acceptance.confirm_selection(hw)  # a freeze and a release need the confirmed material and process selection
    for old in older:
        build_version(hw, label=old)
        publish(hw, "checks", "static", {"result.json": f'{{"status": "pass", "label": "{old}"}}\n'}, version=old)
        publish(hw, "renders", "iso", {"iso.png": f"{old} render\n"}, version=old)
    build_version(hw, omit, failed, attach, label)
    record = impact.read_record(hw)
    acceptance.publish_packet(hw, label, acceptance.producer_packet(record, label))
    evidence = {"REQ-002": f"sweep hw/motion/{label}/elbow-sweep", "REQ-004": f"hw/analysis/{label}/arm-load/results.json"}
    edit_csv(hw / "verification.csv", lambda rows: [row.update(result="pass", evidence=evidence.get(
        row["requirement_id"], "hw/docs/inspection.md")) for row in rows])
    version_sha = sha(hw / "versions" / label / "manifest.json")
    entries = [acceptance.entry(record, f"EV-{number:03d}", {"product": True, "requirements": [req]}, "cad_valid",
                                requirement=req, version=label, manifest_sha256=version_sha,
                                geometry=impact.current_geometry(hw), artifacts=["hw/docs/inspection.md"], hw=hw)
               for number, req in enumerate(sorted(record.requirements), start=1)]
    acceptance.write_ledger(hw, ledger(hw, record, entries) if ledger else entries)
    frozen = {"version": label, "manifest_sha256": version_sha, "decision": "DR-004"}
    if anchor:
        frozen["evidence_sha256"] = acceptance.freeze_anchor(hw, label)
    edit_yaml(hw / "product.yaml", lambda data: data["flow"].update(stage="release", frozen=frozen))
    write(hw / "decisions" / "DR-005.md", f"# DR-005 Release A\n\nApproved: release {label} as revision A.\n")
    for part in sorted((hw / "versions" / label / "record" / "parts").glob("*/part.yaml")):
        pn = part.parent.name
        if "drawing" in yaml.safe_load(part.read_text(encoding="utf-8")) and pn not in skip_bundles:
            publish(hw, "drawings", pn, DRAWING, version=label)
    publish(hw, "drawings", "assembly", DRAWING, version=label)
    publish(hw, "analysis", "arm-load", {"results.json": '{"status": "pass"}\n'}, version=label)
    publish(hw, "motion", "elbow-sweep", {"result.json": '{"status": "pass"}\n', "worst.step": "worst\n"}, version=label)


class ReleaseCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cad-release-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.count = 0

    def project(self, **options: Any) -> Path:
        self.count += 1
        hw = self.root / f"project-{self.count}" / "hw"
        frozen_lamp(hw, **options)
        return hw

    def release(self, hw: Path, rev: str = "A", version: str = "v1", decision: str = "DR-005",
                now: datetime | None = None) -> tuple[int, dict[str, Any]]:
        return release.run(hw, rev, version, decision, now)


class Release(ReleaseCase):
    def test_the_fixture_is_a_clean_frozen_record(self) -> None:
        hw = self.project()
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [])

    def test_a_complete_frozen_record_releases_a_verified_package(self) -> None:
        hw = self.project()
        result = subprocess.run([sys.executable, str(RELEASE), str(hw), "--rev", "A", "--version", "v1",
                                 "--decision", "DR-005"], capture_output=True, text=True, timeout=120, check=False)
        report = json.loads(result.stdout)
        self.assertEqual((result.returncode, report["status"]), (0, "released"), report.get("findings"))
        package = hw / "releases" / "A"
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        findings: list[dict[str, Any]] = []
        self.assertTrue(validate.validate_document(manifest, "release-manifest.schema.json", "manifest", findings), findings)
        self.assertEqual((manifest["schema_version"], manifest["record_version"], manifest["rev"], manifest["approval"],
                          manifest["frozen"]["version"]), (3, 2, "A", "DR-005", "v1"))
        self.assertEqual(manifest["frozen"]["manifest_sha256"], sha(hw / "versions" / "v1" / "manifest.json"))
        self.assertEqual(layout.verify(package), [])
        for name, item in manifest["files"].items():
            self.assertEqual(sha(layout.locate(hw, item["original"])), item["sha256"], name)
        self.assertEqual(report["manifest_sha256"], sha(package / "manifest.json"))
        self.assertEqual(manifest["manufacturing"]["LMP-0001"],
                         {"process": "fdm", "quantity": 1, "route": ["fdm"],
                          "files": ["manufacturing/LMP-0001/part.step", "manufacturing/LMP-0001/part.stl",
                                    "manufacturing/LMP-0001/manufacturing.json"]})
        accepted = manifest["acceptance"]
        self.assertEqual(accepted["evidence_sha256"], manifest["frozen"]["evidence_sha256"])
        self.assertEqual(accepted["scope"]["service_class"], "appearance_concept")
        self.assertFalse(accepted["scope"]["trial"])
        self.assertEqual((accepted["claims"]["REQ-001"], accepted["waivers"]), (["cad_valid"], []))
        self.assertIn("process.fdm.trial@LMP-0001/make", accepted["open_obligations"], "untested, unclaimed, open")
        for path, digest in accepted["inventory"].items():
            self.assertEqual(manifest["files"][f"acceptance/{path[3:]}"], {"sha256": digest, "original": path})
        self.assertIn("hw/manufacturing/evidence.yaml", accepted["inventory"])
        self.assertEqual(manifest["checks"]["acceptance"], "pass")
        self.assertEqual(manifest["manufacturing"]["LMP-0002"]["files"],
                         ["manufacturing/LMP-0002/part.step", "manufacturing/LMP-0002/manufacturing.json"])
        selected = {item["path"]: item["manifest_sha256"] for item in manifest["bundles"]}
        self.assertEqual(set(selected), {f"hw/drawings/v1/LMP-00{n:02d}" for n in range(1, 11)}
                         | {"hw/drawings/v1/assembly", "hw/analysis/v1/arm-load", "hw/motion/v1/elbow-sweep"})
        for path, manifest_sha in selected.items():
            self.assertEqual(sha(layout.locate(hw, path) / "manifest.json"), manifest_sha)
        for name in ("definition/assembly.step", "definition/record/assembly.yaml", "drawings/assembly/sheet-1.pdf",
                     "analysis/arm-load/results.json", "motion/elbow-sweep/worst.step", "bom.csv", "verification.csv",
                     "docs/inspection.md"):
            self.assertTrue((package / name).is_file(), name)
        self.assertEqual(manifest["checks"]["verification/REQ-002"], "pass")
        self.assertEqual(list((hw.parent / ".cad-work" / "release-A").iterdir()), [], "the stage became the package")
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [], "the released record still validates")

    def test_a_catalogue_buy_is_exempt_and_a_made_to_drawing_part_is_included(self) -> None:
        hw = self.project()
        code, report = self.release(hw)
        self.assertEqual(code, 0, report.get("findings"))
        package = hw / "releases" / "A"
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["manufacturing"]), [f"LMP-000{n}" for n in range(1, 8)])
        self.assertEqual(manifest["manufacturing"]["LMP-0005"],
                         {"process": "cnc", "quantity": 1, "route": ["cnc"],
                          "files": ["manufacturing/LMP-0005/part.step", "manufacturing/LMP-0005/manufacturing.json"]})
        catalogue = ("LMP-0008", "LMP-0009", "LMP-0010", "LMP-0011")
        self.assertEqual([name for name in manifest["files"]
                          if name.startswith(tuple(f"manufacturing/{pn}/" for pn in catalogue))], [])
        self.assertTrue((package / "definition" / "record" / "parts" / "LMP-0008" / "part.yaml").is_file(),
                        "a catalogue buy carries its procurement definition")
        self.assertTrue((package / "drawings" / "LMP-0008" / "sheet-1.pdf").is_file(), "its vendor specification sheet")

    def test_a_repeated_part_gives_one_output_with_its_occurrence_count(self) -> None:
        def make_ring(hw: Path) -> None:
            def custom(data: dict[str, Any]) -> None:
                data.update(make_buy="make", process="cnc")
                del data["procurement"]
            edit_yaml(hw / "parts" / "LMP-0011" / "part.yaml", custom)
            edit_csv(hw / "bom.csv", lambda rows: [row.update(
                make_buy="make", process="cnc", **dict.fromkeys(validate.BOM_PROCUREMENT_COLUMNS[1:], ""))
                for row in rows if row["PN"] == "LMP-0011"])

        hw = self.project(before_build=make_ring)
        code, report = self.release(hw)
        self.assertEqual(code, 0, report.get("findings"))
        manifest = json.loads((hw / "releases" / "A" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manufacturing"]["LMP-0011"],
                         {"process": "cnc", "quantity": 3, "route": ["cnc"],
                          "files": ["manufacturing/LMP-0011/part.step", "manufacturing/LMP-0011/manufacturing.json"]})
        self.assertEqual(sorted(n for n in manifest["files"] if n.startswith("manufacturing/LMP-0011/")),
                         ["manufacturing/LMP-0011/manufacturing.json", "manufacturing/LMP-0011/part.step"])

    def test_a_version_built_with_attach_releases(self) -> None:
        def unjoin(hw: Path) -> None:  # members cad-model attaches at run time instead of by a joint
            edit_yaml(hw / "assembly.yaml", lambda data: data.update(
                joints=[joint for joint in data["joints"] if joint["id"] not in ("J-009", "J-012")]))

        hw = self.project(before_build=unjoin, attach=("LMP-0010=LMP-0004", "@ring-head=LMP-0007"))
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [])
        code, report = self.release(hw)
        self.assertEqual(code, 0, report.get("findings"))
        manifest = json.loads((hw / "releases" / "A" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manufacturing"]["LMP-0007"]["quantity"], 1)

    def test_a_passing_stack_case_releases_with_its_case_bundle(self) -> None:
        """stackup.py publishes the stack at hw/analysis/cases/<case>/case.yaml as hw/analysis/<V>/<case>/, the
        bundle release.py requires for that case, whatever the stack's own (uppercase) id."""
        hw = self.project()
        stack = {"id": "STK-001", "title": "Bearing end float", "units": "mm",
                 "requirement": {"min": 0.0, "max": 0.40, "method": "worst_case"},
                 "contributors": [{"id": "A", "nominal": 50.0, "tol": 0.05, "direction": 1},
                                  {"id": "B", "nominal": 49.8, "tol": 0.05, "direction": -1}]}
        case = write(hw / "analysis" / "cases" / "end-float" / "case.yaml", yaml.safe_dump(stack, sort_keys=False))
        code, report = self.release(hw)
        self.assertEqual(code, 1)
        self.assertIn("analysis case end-float has no result for v1", json.dumps(report["findings"]))
        run = subprocess.run([sys.executable, str(STACKUP), str(case), "--hw", str(hw), "--version", "v1"],
                             capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["bundle"], str(hw / "analysis" / "v1" / "end-float"))
        code, report = self.release(hw)
        self.assertEqual(code, 0, report.get("findings"))
        manifest = json.loads((hw / "releases" / "A" / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("hw/analysis/v1/end-float", {item["path"] for item in manifest["bundles"]})

    def test_a_released_original_may_go_but_its_package_copy_may_not(self) -> None:
        hw = self.project()
        write(hw / "docs" / "notes.md", "# Notes\n")
        self.assertEqual(self.release(hw)[0], 0)
        (hw / "docs" / "notes.md").unlink()
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [], "release A holds its own copy")
        (hw / "releases" / "A" / "docs" / "notes.md").unlink()
        self.assertIn("releases/A/docs/notes.md", json.dumps(validate.blocking(validate.validate_project(hw))))


# what pruning after release A of v3 removes from Pruning.released(), in order: each version's bundle
# roots in layout bundle-kind order, then the version itself
SUPERSEDED = ["hw/checks/v1", "hw/renders/v1", "hw/versions/v1", "hw/checks/v2", "hw/renders/v2", "hw/versions/v2"]


class Pruning(ReleaseCase):
    """--prune after release A of v3: v1 and v2 are superseded, v4 was built after v3 but latest.json names v3."""

    def released(self, *, seal: bool = True) -> Path:
        hw = self.project(label="v3", older=("v1", "v2"))
        build_version(hw, label="v4")
        write(hw / "versions" / "latest.json", json.dumps({"label": "v3", "manifest": "versions/v3/manifest.json"}))
        if seal:
            code, report = self.release(hw, version="v3")
            self.assertEqual(code, 0, report.get("findings"))
        return hw

    def cli(self, hw: Path, *extra: str, decision: str = "DR-005") -> tuple[int, dict[str, Any]]:
        result = subprocess.run([sys.executable, str(RELEASE), str(hw), "--rev", "A", "--version", "v3",
                                 "--decision", decision, "--prune", *extra], capture_output=True, text=True,
                                timeout=300, check=False)
        return result.returncode, json.loads(result.stdout)

    def journal(self, hw: Path, digest: str) -> Path:
        return hw.parent / ".cad-work" / f"prune-A-{digest[:16]}" / "journal.json"

    def assertGone(self, hw: Path, paths: list[str], gone: bool = True) -> None:
        for rel in paths:
            self.assertEqual(os.path.lexists(layout.locate(hw, rel)), not gone, rel)

    def test_plan_then_yes_through_the_cli_removes_only_superseded_versions(self) -> None:
        hw = self.released(seal=False)
        code, report = self.cli(hw)  # seals A, then plans
        self.assertEqual((code, report["status"], report["pruning"]["status"]), (0, "released", "plan"), report)
        plan = report["pruning"]["plan"]
        self.assertEqual((plan["prune"], plan["remove"]), (["v1", "v2"], SUPERSEDED))
        self.assertEqual(plan["keep"]["v4"], ["not older than the released v3"], "a newer build stays without latest.json")
        self.assertIn("released as A", plan["keep"]["v3"])
        before = tree(hw)
        code, again = self.cli(hw)  # A is sealed: the documented rerun plans without sealing again
        self.assertEqual((code, again["already_sealed"], again["pruning"]["plan"]["digest"]), (0, True, plan["digest"]))
        self.assertEqual(tree(hw), before, "a plan changes nothing")
        code, report = self.cli(hw, "--yes", plan["digest"])
        self.assertEqual((code, report["status"], report["pruning"]["status"]), (0, "released", "pruned"), report)
        self.assertEqual(list(report)[-1], "status")
        self.assertEqual(report["pruning"]["removed"], SUPERSEDED)
        self.assertEqual(report["pruning"]["store_gc"]["status"], "pass")
        self.assertGone(hw, SUPERSEDED)
        self.assertEqual(layout.versions(hw), ["v3", "v4"])
        self.assertEqual(layout.verify(hw / "releases" / "A"), [])
        self.assertEqual(list((hw.parent / ".cad-work").glob("prune-*")), [])
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [], "no surviving reference broke")

    def test_yes_refuses_a_digest_that_a_fresh_plan_no_longer_has(self) -> None:
        hw = self.released()
        digest = prune.plan(hw, "A", "v3")["digest"]
        write(hw / "decisions" / "DR-006.md", "# DR-006 Cost-down baseline\n\nKeep v2 as the baseline.\n")
        before = tree(hw)
        code, report = self.cli(hw, "--yes", digest)
        self.assertEqual((code, report["status"], report["pruning"]["status"]), (1, "prune_refused", "refused"))
        self.assertEqual(report["pruning"]["plan"]["prune"], ["v1"])
        self.assertEqual(report["pruning"]["plan"]["keep"]["v2"], ["named by DR-006"])
        self.assertEqual(tree(hw), before)
        result = subprocess.run([sys.executable, str(RELEASE), str(hw), "--rev", "A", "--version", "v3", "--decision",
                                 "DR-005", "--prune", "--yes", "latest"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("64-character digest", result.stderr)

    def test_an_existing_package_of_another_approval_is_refused_not_pruned_against(self) -> None:
        hw = self.released()
        before = tree(hw)
        code, report = self.cli(hw, decision="DR-004")
        self.assertEqual((code, report["status"], report["findings"][0]["code"]), (1, "refused", "release.exists"))
        self.assertNotIn("pruning", report)
        self.assertEqual(tree(hw), before)

    def test_gc_failure_is_prune_incomplete_and_a_rerun_finishes(self) -> None:
        hw = self.released()
        digest = prune.plan(hw, "A", "v3")["digest"]
        staging = hw / "versions" / ".v5.partial-1"  # a cad-model run in progress: store gc refuses
        staging.mkdir()
        code, report = self.cli(hw, "--yes", digest)
        self.assertEqual((code, report["status"], report["pruning"]["status"]), (1, "prune_incomplete", "incomplete"))
        self.assertIn("store gc failed", report["pruning"]["message"])
        self.assertEqual(report["pruning"]["removed"], SUPERSEDED)
        self.assertGone(hw, SUPERSEDED)
        self.assertEqual(json.loads(self.journal(hw, digest).read_text(encoding="utf-8"))["phase"], "gc")
        staging.rmdir()
        code, report = self.cli(hw)  # a plan request names the pending prune instead of planning another
        self.assertEqual((code, report["pruning"]["status"], report["pruning"]["digest"]), (1, "incomplete", digest))
        code, report = self.cli(hw, "--yes", digest)
        self.assertEqual((code, report["status"], report["pruning"]["status"]), (0, "released", "pruned"), report)
        self.assertFalse(self.journal(hw, digest).parent.exists())

    def interrupt_after(self, count: int, hw: Path, digest: str) -> dict[str, Any]:
        rename, moved = os.rename, []

        def interrupted(source: Any, target: Any) -> None:
            if len(moved) == count:
                raise OSError("interrupted")
            moved.append(layout.relative(hw.resolve(), Path(source)))
            rename(source, target)

        with mock.patch.object(prune.os, "rename", interrupted):
            result = prune.after_release(hw, "A", "v3", digest)
        self.assertEqual(moved, SUPERSEDED[:count])
        return result

    def test_an_interrupted_prune_moves_whole_folders_and_a_rerun_finishes(self) -> None:
        hw = self.released()
        digest = prune.plan(hw, "A", "v3")["digest"]
        result = self.interrupt_after(2, hw, digest)
        self.assertEqual((result["status"], result["digest"]), ("incomplete", digest))
        self.assertEqual(layout.verify(hw / "versions" / "v1"), [], "v1 is untouched, never half deleted")
        self.assertGone(hw, SUPERSEDED[:2])
        self.assertGone(hw, SUPERSEDED[2:], gone=False)
        result = prune.after_release(hw, "A", "v3", digest)
        self.assertEqual((result["status"], result["removed"]), ("pruned", SUPERSEDED))
        self.assertGone(hw, SUPERSEDED)
        self.assertFalse(self.journal(hw, digest).parent.exists())

    def test_an_interrupted_prune_moves_back_a_version_that_became_a_milestone(self) -> None:
        hw = self.released()
        digest = prune.plan(hw, "A", "v3")["digest"]
        self.interrupt_after(3, hw, digest)  # every v1 folder has left hw/
        self.assertGone(hw, SUPERSEDED[:3])
        write(hw / "decisions" / "DR-006.md", "# DR-006 Gate\n\nThe PARTS gate approved v1.\n")
        result = prune.after_release(hw, "A", "v3", digest)
        self.assertEqual((result["status"], sorted(result["restored"])), ("refused", sorted(SUPERSEDED[:3])))
        self.assertEqual(result["plan"]["keep"]["v1"], ["named by DR-006"])
        self.assertGone(hw, SUPERSEDED, gone=False)
        for folder in ("versions/v1", "checks/v1/static", "renders/v1/iso"):
            self.assertEqual(layout.verify(hw / folder), [], folder)
        self.assertFalse(self.journal(hw, digest).parent.exists())

    def test_a_real_kept_manifest_that_references_a_superseded_version_keeps_it(self) -> None:
        hw = self.released()
        # a changed-only sweep of v3 names its baseline label in its recipe, as sweep.py records it
        publish(hw, "motion", "elbow-changed", {"result.json": '{"status": "pass"}\n'}, version="v3",
                recipe={"subject": "elbow-changed", "options": {"changed_only": {"baseline": "v2"}}})
        # a v3 check that reuses a v1 result names that bundle's manifest by its sha256
        reused = sha(hw / "checks" / "v1" / "static" / "manifest.json")
        publish(hw, "checks", "recheck", {"result.json": '{"status": "pass"}\n'}, version="v3",
                recipe={"subject": "recheck", "reused": {"manifest_sha256": reused}})
        plan = prune.plan(hw, "A", "v3")
        self.assertEqual(plan["prune"], [])
        self.assertEqual(plan["keep"]["v2"], ["referenced by hw/motion/v3/elbow-changed/manifest.json"])
        self.assertEqual(plan["keep"]["v1"], ["referenced by hw/checks/v3/recheck/manifest.json"])

    def test_a_symlinked_bundle_root_is_refused_in_the_plan(self) -> None:
        hw = self.released()
        elsewhere = self.root / "renders-v2"
        shutil.move(hw / "renders" / "v2", elsewhere)
        (hw / "renders" / "v2").symlink_to(elsewhere, target_is_directory=True)
        plan = prune.plan(hw, "A", "v3")
        self.assertEqual(plan["prune"], ["v1"])
        self.assertEqual(plan["keep"]["v2"], ["refused: hw/renders/v2 is a symbolic link; resolve it by hand to prune v2"])

    def test_plan_keeps_frozen_change_baseline_and_decision_milestones(self) -> None:
        hw = self.released(seal=False)
        write(hw / "decisions" / "DR-006.md", "# DR-006 Gate\n\nThe PARTS gate approved v1.\n")
        edit_yaml(hw / "product.yaml", lambda data: data["flow"].update(
            change={"decision": "DR-006", "baseline_version": "v2", "affected_parts": ["LMP-0001"]}))
        plan = prune.plan(hw, "A", "v3")
        self.assertEqual(plan["prune"], [])
        self.assertEqual(plan["keep"]["v1"], ["named by DR-006"])
        self.assertEqual(plan["keep"]["v2"], ["baseline of product.yaml flow.change"])
        for reason in ("frozen in product.yaml flow.frozen", "named by DR-005", "latest: versions/latest.json names it"):
            self.assertIn(reason, plan["keep"]["v3"])

    def test_evidence_only_the_ledger_cites_keeps_its_version(self) -> None:
        hw = self.released()
        ledger = hw / "manufacturing" / "evidence.yaml"
        data = yaml.safe_load(ledger.read_text(encoding="utf-8"))
        data["entries"].append({"id": "EV-099", "requirement": "REQ-001", "subject": {"product": True}, "level": "cad_valid",
                                "result": "planned", "method": "TST re-run of the v2 sweep",
                                "note": "compare with hw/motion/v2/elbow-sweep"})
        ledger.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        plan = prune.plan(hw, "A", "v3")
        self.assertEqual(plan["prune"], ["v1"])
        self.assertEqual(plan["keep"]["v2"], ["referenced by hw/manufacturing/evidence.yaml"])

    def test_a_release_manifest_version_it_cannot_read_refuses_the_plan(self) -> None:
        hw = self.released()
        write(hw / "releases" / "B" / "manifest.json", json.dumps({"schema_version": 9, "product_id": "lamp-arm", "rev": "B"}))
        with self.assertRaisesRegex(layout.LayoutError, "manifest version 9"):
            prune.plan(hw, "A", "v3")


class Documents(ReleaseCase):
    def guide(self, hw: Path, *extra: str) -> None:
        result = subprocess.run([sys.executable, str(BUILD_GUIDE), str(hw), "--version", "v1", "--out", str(hw / "docs"),
                                 *extra], capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_package_okh_toml_describes_its_release(self) -> None:
        sealing = datetime(2026, 11, 2, 23, 59, 59, tzinfo=timezone.utc)  # a fixed clock: no UTC-midnight race
        hw = self.project()
        self.guide(hw)
        code, report = self.release(hw, now=sealing)
        self.assertEqual(code, 1)
        self.assertIn("release.docs", [item.get("code") for item in report["findings"]])
        self.assertFalse((hw / "releases").exists())
        self.guide(hw, "--rev", "A", "--date", "2026-11-02")
        code, report = self.release(hw, now=sealing + timedelta(seconds=2))  # the next UTC day
        self.assertEqual(code, 1)
        self.assertIn("dated 2026-11-03", json.dumps(report["findings"]))
        code, report = self.release(hw, now=sealing)
        self.assertEqual(code, 0, report.get("findings"))
        okh = tomllib.loads((hw / "releases" / "A" / "docs" / "okh.toml").read_text(encoding="utf-8"))["okhv"]
        manifest = json.loads((hw / "releases" / "A" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["released_at"], "2026-11-02T23:59:59Z")
        self.assertEqual({key: okh[key] for key in ("version", "release", "release_date")},
                         {"version": "v1", "release": "A", "release_date": "2026-11-02"})


class Refusal(ReleaseCase):
    """Each gap refuses with a finding that names it, and hw/ is byte-identical afterwards."""

    def refused(self, hw: Path, path: str, code: str, text: str, **arguments: str) -> None:
        before = tree(hw)
        result, report = self.release(hw, **arguments)
        self.assertEqual((result, report["status"]), (1, "refused"))
        self.assertTrue(any(item["path"] == path and item.get("code") == f"release.{code}" and text in item["message"]
                            for item in report["findings"]), json.dumps(report["findings"], indent=1))
        self.assertEqual(tree(hw), before, "a refused release writes nothing in hw/")

    def test_a_missing_fdm_stl_refuses(self) -> None:
        hw = self.project(omit=(("LMP-0001", "part.stl"),))
        self.refused(hw, "hw/versions/v1/manufacturing/LMP-0001/part.stl", "missing", "a fdm part needs part.step, part.stl")
        self.assertFalse((hw / "releases").exists())

    def test_a_failed_manufacturing_check_refuses(self) -> None:
        hw = self.project(failed={"LMP-0004": "bed_contact"})
        self.refused(hw, "hw/versions/v1/manufacturing/LMP-0004/manufacturing.json", "failed", "bed_contact failed")

    def test_a_tampered_bundle_refuses(self) -> None:
        hw = self.project()
        write(hw / "drawings" / "v1" / "LMP-0002" / "sheet-1.svg", "edited\n")
        self.refused(hw, "hw/drawings/v1/LMP-0002", "tampered", "sheet-1.svg changed after it was sealed")

    def test_a_missing_drawing_refuses(self) -> None:
        hw = self.project(skip_bundles=("LMP-0006",))
        self.refused(hw, "hw/drawings/v1/LMP-0006", "missing", "LMP-0006 declares drawing intent")

    def test_a_changed_current_record_refuses(self) -> None:
        hw = self.project()
        edit_yaml(hw / "parts" / "LMP-0003" / "part.yaml", lambda data: data.update(finish="Anodized"))
        self.refused(hw, "hw/parts/LMP-0003/part.yaml", "changed", "differs from v1/record/parts/LMP-0003/part.yaml")
        hw = self.project()
        model = hw / "parts" / "LMP-0002" / "model.py"
        model.write_text(model.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        self.refused(hw, "hw/parts/LMP-0002/model.py", "changed", "is not the model v1 built")

    def test_a_wrong_version_refuses(self) -> None:
        hw = self.project()
        self.refused(hw, "hw/product.yaml", "frozen", "flow.frozen names v1, not v2", version="v2")

    def test_an_existing_revision_is_never_overwritten(self) -> None:
        hw = self.project()
        self.assertEqual(self.release(hw)[0], 0)
        self.refused(hw, "hw/releases/A", "exists", "a used revision is never overwritten")

    def test_a_missing_decision_refuses(self) -> None:
        hw = self.project()
        self.refused(hw, "hw/decisions/DR-099.md", "decision", "DR-099 does not exist", decision="DR-099")

    def test_a_provisional_or_void_decision_never_approves_a_release(self) -> None:
        hw = self.project()
        for status in ("PROVISIONAL", "VOID"):
            with self.subTest(status=status):
                write(hw / "decisions" / "DR-006.md", f"Status: {status}\n\n# DR-006 Release A\n")
                self.refused(hw, "hw/decisions/DR-006.md", "decision", f"DR-006 is {status}, not the user's approval",
                             decision="DR-006")
        write(hw / "decisions" / "DR-006.md", "Status: approved\n\n# DR-006 Release A\n")
        self.refused(hw, "hw/decisions/DR-006.md", "decision", "unknown gate status 'approved'", decision="DR-006")
        write(hw / "decisions" / "DR-006.md", "Status: APPROVED\n\n# DR-006 Release A\n\nThe user approved v1 as A.\n")
        result, report = self.release(hw, decision="DR-006")
        self.assertEqual((result, report["status"]), (0, "released"), report.get("findings"))

    def test_a_freeze_on_a_provisional_decision_refuses_the_release(self) -> None:
        hw = self.project()
        record = hw / "decisions" / "DR-004.md"
        write(record, "Status: PROVISIONAL\n\n" + record.read_text(encoding="utf-8"))
        before = tree(hw)
        result, report = self.release(hw)
        self.assertEqual((result, report["status"]), (1, "refused"))
        self.assertTrue(any("flow/frozen/decision DR-004 is PROVISIONAL" in item["message"]
                            for item in report["findings"]), json.dumps(report["findings"], indent=1))
        self.assertEqual(tree(hw), before)

    def test_unverified_requirements_and_missing_evidence_refuse(self) -> None:
        hw = self.project()
        edit_csv(hw / "verification.csv", lambda rows: rows[0].update(result="planned"))
        before = tree(hw)
        code, report = self.release(hw)
        self.assertEqual(code, 1)
        self.assertIn("release verification result must be pass or waived", json.dumps(report["findings"]))
        self.assertEqual(tree(hw), before)
        hw = self.project()
        edit_csv(hw / "verification.csv", lambda rows: rows[1].update(evidence="hw/motion/v1/elbow-limits"))
        self.refused(hw, "hw/motion/v1/elbow-limits", "missing", "REQ-002 cites it")

    def test_cited_evidence_inside_the_frozen_version_or_a_bundle_must_exist(self) -> None:
        for evidence in ("hw/analysis/v1/arm-load/result.json", "hw/versions/v1/x.step"):
            with self.subTest(evidence=evidence):
                hw = self.project()
                edit_csv(hw / "verification.csv", lambda rows: [row.update(evidence=evidence) for row in rows
                                                                if row["requirement_id"] == "REQ-004"])
                with (hw / "verification.csv").open(newline="", encoding="utf-8") as stream:
                    line = next(n for n, row in enumerate(csv.DictReader(stream), start=2) if row["requirement_id"] == "REQ-004")
                self.refused(hw, f"hw/verification.csv:{line}", "missing", f"REQ-004 evidence {evidence} does not exist")

    def test_a_bad_argument_is_a_usage_error(self) -> None:
        hw = self.project()
        result = subprocess.run([sys.executable, str(RELEASE), str(hw), "--rev", "I", "--version", "v1",
                                 "--decision", "DR-005"], capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("REV 'I' does not match", json.loads(result.stdout)["message"])


def waive_fdm_trials(hw: Path, record: Any, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The accepted entries plus an approved, scoped trial waiver (DR-006) for every FDM part's print trial, and
    accepted joint evidence (a TST retention test) for every assembly.joint obligation: a fit-assembly release
    resolves the joints, which assembly_operations judges unknown from this record's unsourced heat-set facts."""
    acceptance.decision(hw, "DR-006")
    configuration = entries[0]["configuration"]
    for obligation in acceptance.mi.required_checks(record):
        subject = dict(obligation.subject)
        if obligation.family == "process.fdm.trial":
            entries.append(acceptance.entry(record, f"EV-{len(entries) + 1:03d}", subject, "physically_validated", "waived",
                                            obligation=obligation.id,
                                            waiver=acceptance.waiver(subject, "DR-006", obligation=obligation.id)))
        elif obligation.family == "assembly.joint":
            entries.append(acceptance.entry(record, f"EV-{len(entries) + 1:03d}", subject, obligation.level,
                                            obligation=obligation.id, version=configuration["version"],
                                            manifest_sha256=configuration["manifest_sha256"],
                                            geometry=impact.current_geometry(hw), artifacts=["hw/docs/inspection.md"], hw=hw))
    return entries


def open_selection(hw: Path) -> None:
    """selection.yaml assessing one candidate for LMP-0001 (its recorded FDM route, no material profile, so
    the capability's material approval stays unknown: conditional), on a digest of other inputs (stale)."""
    candidate = {"id": "lamp_as_drawn", "title": "TST: the base as drawn", "architecture": "TST: as recorded",
                 "parts": [{"PN": "LMP-0001", "material_profile": fx.not_applicable(acceptance.NA),
                            "route": [{"kind": "fdm", "capability": "tst_fdm"}]}],
                 "evaluations": [], "pros": ["TST: as drawn"], "cons": ["TST: none recorded"], "tradeoffs": ["TST: none"]}
    write(hw / "manufacturing" / "selection.yaml", yaml.safe_dump({
        "status": "assessed", "assessment": {"input_sha256": "0" * 64, "method": "selection.py", "date": "2026-01-01"},
        "candidates": [candidate]}, sort_keys=False))


class Acceptance(ReleaseCase):
    """The intent acceptance of a record version 2 release: anchor, evidence and service policy."""

    def test_a_version_1_record_refuses_until_it_is_migrated(self) -> None:
        hw = self.project()
        edit_yaml(hw / "product.yaml", lambda data: data.pop("record_version"))
        before = tree(hw)
        code, report = self.release(hw)
        self.assertEqual((code, report["status"]), (1, "refused"))
        self.assertIn("record.migration_required", [item.get("code") for item in report["findings"]])
        self.assertEqual(tree(hw), before)

    def test_a_freeze_without_an_acceptance_anchor_refuses(self) -> None:
        hw = self.project(anchor=False)
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [], "readable: a legacy-freeze warning only")
        self.refused_with(hw, "release.frozen", "anchors no evidence_sha256")

    def test_evidence_accepted_after_the_freeze_needs_a_new_anchor(self) -> None:
        hw = self.project()
        ledger = hw / "manufacturing" / "evidence.yaml"
        data = yaml.safe_load(ledger.read_text(encoding="utf-8"))
        data["entries"].append({"id": "EV-099", "requirement": "REQ-001", "subject": {"product": True}, "level": "cad_valid",
                                "result": "planned", "method": "TST late plan"})
        ledger.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self.assertIn("record.evidence_anchor", [item.get("code") for item in validate.validate_project(hw)])
        self.refused_with(hw, "release.acceptance", "not the acceptance digest")
        anchor = acceptance.freeze_anchor(hw, "v1")
        edit_yaml(hw / "product.yaml", lambda product: product["flow"]["frozen"].update(evidence_sha256=anchor))
        code, report = self.release(hw)
        self.assertEqual(code, 0, report.get("findings"))

    def test_a_verification_pass_without_ledger_evidence_refuses(self) -> None:
        hw = self.project(ledger=lambda hw, record, entries: [])
        self.refused_with(hw, "evidence.csv_unsupported", "a CSV result is a summary of accepted evidence")

    def test_an_untested_fit_prototype_refuses_and_approved_waivers_make_it_an_unvalidated_trial(self) -> None:
        hw = self.project(service_class="fit_assembly")
        self.refused_with(hw, "release.acceptance", "process.fdm.trial@LMP-0001/make is unknown")
        hw = self.project(service_class="fit_assembly", ledger=waive_fdm_trials)
        code, report = self.release(hw)
        self.assertEqual(code, 0, report.get("findings"))
        manifest = json.loads((hw / "releases" / "A" / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["acceptance"]["scope"]["trial"])
        self.assertEqual(manifest["checks"]["acceptance"], "waived")
        self.assertEqual({waiver["decision"] for waiver in manifest["acceptance"]["waivers"]}, {"DR-006"})
        self.assertIn("process.fdm.trial@LMP-0001/make", manifest["acceptance"]["open_obligations"])
        self.assertNotIn("physically_validated", manifest["acceptance"]["claims"]["REQ-001"])

    def test_a_production_release_is_never_a_waived_trial(self) -> None:
        hw = self.project(service_class="production", ledger=waive_fdm_trials)
        self.refused_with(hw, "release.acceptance", "release.trial_not_permitted")

    def test_a_sealed_package_is_read_by_its_own_manifest_version(self) -> None:
        hw = self.project()
        self.assertEqual(self.release(hw)[0], 0)
        code, report = release.sealed(hw, "A", "v1", "DR-005")
        self.assertEqual((code, report["manifest_version"]), (0, 3))
        path = hw / "releases" / "A" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        forged = {**manifest, "acceptance": {**manifest["acceptance"], "evidence_sha256": "0" * 64}}
        path.write_text(json.dumps(forged), encoding="utf-8")
        code, report = release.sealed(hw, "A", "v1", "DR-005")
        self.assertEqual(code, 1)
        self.assertIn("not the digest of its sealed inventory", report["findings"][0]["message"])
        # the same package as a version 2 seal: inspectable as sealed, never re-certified as version 3
        legacy = {key: value for key, value in manifest.items() if key not in ("acceptance", "record_version")}
        legacy.update(schema_version=2, files={k: v for k, v in manifest["files"].items() if not k.startswith("acceptance/")},
                      manufacturing={pn: {k: v for k, v in entry.items() if k != "route"}
                                     for pn, entry in manifest["manufacturing"].items()})
        shutil.rmtree(hw / "releases" / "A" / "acceptance")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        code, report = release.sealed(hw, "A", "v1", "DR-005")
        self.assertEqual((code, report["manifest_version"]), (0, 2))

    def test_an_open_or_stale_selection_refuses_the_release(self) -> None:
        hw = self.project(before_build=open_selection)
        self.assertEqual(validate.blocking(validate.validate_project(hw)), [], "the record itself is valid")
        before = tree(hw)
        result, report = self.release(hw)
        self.assertEqual((result, report["status"]), (1, "refused"))
        messages = [item["message"] for item in report["findings"] if item.get("code") == "release.acceptance"]
        for text in ("gate.selection: the recorded assessed assessment is stale",
                     "gate.selection: the selection is conditional"):
            self.assertTrue(any(text in message for message in messages), messages)
        self.assertEqual(tree(hw), before, "a refused release writes nothing in hw/")

    def refused_with(self, hw: Path, code: str, text: str) -> None:
        before = tree(hw)
        result, report = self.release(hw)
        self.assertEqual((result, report["status"]), (1, "refused"))
        self.assertTrue(any((item.get("code") == code or code in item["message"]) and text in item["message"]
                            for item in report["findings"]), json.dumps(report["findings"], indent=1))
        self.assertEqual(tree(hw), before, "a refused release writes nothing in hw/")


if __name__ == "__main__":
    unittest.main()
