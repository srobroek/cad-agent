# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Shared helpers for corpus families: module loading, verdicts taken at the packet, cases for assembly operations.

Heavy imports (yaml, OCP, the check modules) happen inside the functions, so importing this is free.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from corpus import HERE, Outcome, Rejected, Skip, command_failed

SKILLS = HERE.parents[2]
REPO = SKILLS.parent
MOTION_FIXTURES = SKILLS / "cad-motion" / "tests" / "fixtures"
PNS = ("TST-0101", "TST-0102", "TST-0103", "TST-0104", "TST-0105")
TITLES = {"TST-0101": "Housing", "TST-0102": "Bearing", "TST-0103": "Cover", "TST-0104": "Cover screw",
          "TST-0105": "Bracket"}


def load(name: str, path: Path) -> Any:
    """One module per name, as scripts load their siblings under these names (scripts/modload.py)."""
    if str(REPO / "scripts") not in sys.path:
        sys.path.insert(0, str(REPO / "scripts"))
    from modload import load_module

    return load_module(name, path)


def verdict(outcomes: Iterable[str]) -> Outcome:
    """A check's obligation outcomes as one verdict: any fail gives fail, else any unknown gives unknown, else pass."""
    seen = set(outcomes)
    if not seen:
        raise ValueError("a check that returns no outcome has no verdict")
    if "fail" in seen:
        return "fail"
    if "unknown" in seen:
        return "unknown"
    return "pass"


def require(module: str) -> None:
    """Raise Skip without the module."""
    import importlib.util

    if importlib.util.find_spec(module) is None:
        raise Skip(f"{module} is not installed")


# --- the packet a release sees ---------------------------------------------------------------------------------------

BUILD = "v9000"  # the version label evidence assesses the in-memory packet for. No repository version uses it
MANIFEST_SHA256 = "0" * 64
_STATE_OUTCOME = {"fail": "fail", "pass": "pass", "not_applicable": "pass"}  # any other state is not a pass


def _evidence() -> Any:
    return load("cad_record_evidence", SKILLS / "cad-record" / "scripts" / "evidence.py")


def state_verdict(record: Any, produced: Sequence[Any], version: str, manifest_sha256: str, prefixes: tuple[str, ...],
                  today: Any = None) -> Outcome:
    """What cad-record evidence.assess concludes for the obligations of `record` whose id starts with a prefix, given the
    version's producer packets (ProducerPacket objects of evidence): fail if any failed, pass if all passed, else unknown. Evidence
    refusing a packet (packet.invalid) raises Rejected: a miss with the reason, not a verdict. A record with no such
    obligation also raises it: the release asks nothing of the check."""
    assessment = _evidence().assess(record, version=version, manifest_sha256=manifest_sha256, packets=list(produced),
                                    today=today or TODAY)
    refused = [f.message for f in assessment.findings if f.code == "packet.invalid"]
    if refused:
        raise Rejected("evidence: " + "; ".join(refused))
    states = {i: s.result for i, s in assessment.statuses.items() if i.startswith(prefixes)}
    if not states:
        raise Rejected(f"the record expects no obligation starting {prefixes}")
    return verdict(_STATE_OUTCOME.get(state, "unknown") for state in states.values())


def release_verdict(record: Any, packet: Mapping[str, Any], *prefixes: str, today: Any = None) -> Outcome:
    """`state_verdict` for one producer `packet` as its script would publish it, after the schema check every producer
    runs before it seals one (a packet that breaks it raises Rejected)."""
    packets = load("cad_record_check_packet", SKILLS / "cad-record" / "scripts" / "check_packet.py")
    try:
        packets.validate(packet)
    except packets.PacketError as error:
        raise Rejected(f"schema: {error}") from error
    produced = _evidence().ProducerPacket(f"hw/checks/{BUILD}/corpus", "0" * 64, packet)
    return state_verdict(record, [produced], BUILD, MANIFEST_SHA256, prefixes, today)


# --- assembly operations (cad-record/scripts/assembly_operations.py) ---------------------------------------------

def operations_file(name: str, socket_sha256: str = "0" * 64) -> list[dict[str, Any]]:
    require("yaml")
    import yaml

    text = (MOTION_FIXTURES / name).read_text(encoding="utf-8").replace("{socket_sha256}", socket_sha256)
    data: list[dict[str, Any]] = yaml.safe_load(text)["operations"]
    return data


def reordered(items: list[dict[str, Any]], *ids: str) -> list[dict[str, Any]]:
    import copy

    by_id = {item["id"]: item for item in items}
    return [copy.deepcopy(by_id[ident]) for ident in ids]


# --- additive (FDM) checks on real B-rep (cad-design process_checks) ---------------------------------------------

APPROVED = {"status": "approved", "rotation": {"quaternion": [1, 0, 0, 0]}, "decision": "DR-001"}
PN = "TST-0101"


def solids() -> Any:
    """access_fixture.py's OCP builders (box, cylinder, fuse, cut), reused rather than copied."""
    require("OCP")
    return load("mfg_corpus_access_fixture", MOTION_FIXTURES / "access_fixture.py")


TODAY = date(2026, 6, 1)


def printed_record(supports: Mapping[str, Any] | None = None) -> tuple[Any, Any]:
    """(the record, the process_checks module) of one printed part on the TEST profile, with the support plan the record asks for."""
    import tempfile

    require("yaml")
    require("jsonschema")
    import capability_profile

    for folder in (SKILLS / "cad-design" / "scripts", SKILLS / "cad-model" / "scripts"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    import process_checks as pc

    fx = load("cad_record_mi_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
    route = [fx.operation("print", "fdm", capability_profile.PRINTER, output_state="printed", orientation=dict(APPROVED),
                          supports=dict(supports or {"strategy": "none"}))]
    intent = fx.intent(functions=[{"function": "cosmetic"}], material_profile=capability_profile.MATERIAL, route=route,
                       critical_surfaces=fx.not_applicable("none here"))
    part = fx.part(PN, process="fdm", material=capability_profile.MATERIAL, part_intent=intent)
    documents = fx.record(parts=[part], capabilities=capability_profile.capabilities(),
                          materials=capability_profile.materials())
    with tempfile.TemporaryDirectory() as tmp:
        hw = fx.write_record(Path(tmp) / "project" / "hw", documents)
        record, problems = pc.intent().check_record(hw)
    errors = [f"{p.code} {p.path}: {p.message}" for p in problems if p.severity == "error"]
    if errors:
        raise ValueError(f"the corpus record is invalid: {errors}")
    return record, pc


def shipped_catalog(pc: Any) -> Any:
    """The guideline catalog production hands the process checks for a record with no project guideline sets: the shipped
    ones (form_check passes `guidelines.discover(...)` of the record's folder). Without it only calibrations count."""
    return pc.guidelines().discover(None)


def printed_verdict(shape: Any, prefix: str, supports: Mapping[str, Any] | None = None, frame: str = "bed") -> Outcome:
    """What a release concludes about the obligations whose id starts with `prefix` (e.g. "process.fdm.support@") for one
    printed part. The path is form_check.check_version's: `shape` probed by form_check.probe with the plan the record
    asks for and the build axis a bed placement gives, assessed by the process checks into the packet, validated against
    its schema, then assessed by cad-record evidence."""
    require("OCP")
    record, pc = printed_record(supports)
    catalog = shipped_catalog(pc)
    form_check = load("cad_design_form_check", SKILLS / "cad-design" / "scripts" / "form_check.py")
    build_axis = [0.0, 0.0, 1.0] if frame == "bed" else None  # form_check.version_geometry: R^T z of a bed placement
    facts = form_check.probe(shape, frame=frame, locators={}, plan=pc.probe_plan(record, PN, "main", today=TODAY, catalog=catalog),
                             build_axis=build_axis)
    evidence = ({"path": f"hw/versions/{BUILD}/manufacturing/{PN}/part.step", "sha256": "0" * 64},)
    packet = pc.assess(record, {(PN, "main"): pc.Geometry(PN, "main", facts, evidence)}, today=TODAY,
                       catalog=catalog).packet(
        {"version_manifest": MANIFEST_SHA256, "producer_code": "0" * 64})
    return release_verdict(record, packet, prefix, today=TODAY)


def purchase_verdict(record: Any, *prefixes: str) -> Outcome:
    """What a release concludes about the obligations whose id starts with one of `prefixes` (`purchase.`, `route.`,
    `process.cnc.` and the like) for the parts of `record`. The path is the one the process checks take, without geometry: they assess
    the record into the packet, the schema check validates the packet, then cad-record evidence assesses it. An
    obligation that needs a part's geometry stays unknown, as it does for a build with none."""
    for folder in (SKILLS / "cad-design" / "scripts", SKILLS / "cad-model" / "scripts"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    import process_checks as pc

    packet = pc.assess(record, {}, today=TODAY, catalog=shipped_catalog(pc)).packet(
        {"version_manifest": MANIFEST_SHA256, "producer_code": "0" * 64})
    return release_verdict(record, packet, *prefixes, today=TODAY)


# --- assembly access (cad-motion/scripts/access.py on the access fixture's geometry) -----------------------------

_ACCESS_ROOT: Path | None = None


def _access_geometry() -> tuple[Path, str]:
    """Build access_fixture.py's STEP parts once per process into a temp directory removed at exit."""
    import atexit
    import hashlib
    import shutil
    import subprocess
    import tempfile

    global _ACCESS_ROOT
    if _ACCESS_ROOT is None:
        if shutil.which("uv") is None:
            raise Skip("uv is required to build the access geometry")
        root = Path(tempfile.mkdtemp(prefix="mfg-corpus-access-"))
        atexit.register(shutil.rmtree, root, True)
        build = subprocess.run(["uv", "run", "--quiet", "--script", str(MOTION_FIXTURES / "access_fixture.py"),
                                str(root / "geometry")], capture_output=True, text=True, timeout=900, check=False)
        if build.returncode != 0:
            raise command_failed("access_fixture.py", build.args, build.returncode, build.stdout + build.stderr)
        _ACCESS_ROOT = root
    return _ACCESS_ROOT, hashlib.sha256((_ACCESS_ROOT / "geometry" / "socket.step").read_bytes()).hexdigest()


def socket_operations(name: str) -> list[dict[str, Any]]:
    """An operations file of cad-motion's fixtures with the built socket's sha256 bound."""
    require("yaml")
    return operations_file(name, _access_geometry()[1])


def assembly_verdict(items: list[dict[str, Any]], *prefixes: str) -> Outcome:
    """What a release concludes about the obligations whose id starts with one of `prefixes` for the five parts with
    `items` as the assembly operations. The packet is access.py's own (its order check over the chain's inventory, then
    the path planning), as the access.py command prints it."""
    import shutil
    import tempfile

    require("yaml")
    root, _ = _access_geometry()
    fx = load("cad_record_mi_fixtures", SKILLS / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py")
    parts = [fx.part(pn, TITLES[pn], process="cnc") for pn in PNS]
    interface = fx.interface("IF-001", "TST-0101", "TST-0102", "mounting",
                             limits={"press_fit": {"fit": "22 H7/p6", "pair": ["TST-0102", "TST-0101"]}})
    documents = fx.record(parts=parts, interfaces=[interface], operations=items)
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        hw = fx.write_record(Path(tmp) / "hw", documents)
        source = hw / "manufacturing" / "sources" / "socket.step"
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "geometry" / "socket.step", source)
        return access_packet_verdict(hw, root / "geometry" / "parts", [f"{pn}=TST-0101" for pn in PNS[1:]], *prefixes)


def access_packet_verdict(hw: Path, parts_dir: Path, attach: list[str], *prefixes: str) -> Outcome:
    """What a release concludes about the obligations whose id starts with one of `prefixes` for the record at `hw`
    whose built parts are in `parts_dir`, with `attach` as the `--attach REF=PARENT` pairs. The packet is the one the
    access.py command prints, as in assembly_verdict."""
    import json
    import subprocess

    intent = load("cad_record_manufacturing_intent", SKILLS / "cad-record" / "scripts" / "manufacturing_intent.py")
    pairs = [arg for pair in attach for arg in ("--attach", pair)]
    run = subprocess.run(["uv", "run", "--quiet", "--script", str(SKILLS / "cad-motion" / "scripts" / "access.py"),
                          "--hw", str(hw), "--parts-dir", str(parts_dir), *pairs],
                         capture_output=True, text=True, timeout=1800, check=False)
    if run.returncode not in (0, 1):
        raise command_failed("access.py", run.args, run.returncode, run.stdout + run.stderr)
    printed = json.loads(run.stdout)
    record, problems = intent.check_record(hw)
    printed.pop("detail")
    return release_verdict(record, printed, *prefixes)


# --- kernel validity (cad-model check: the cad.readback packet of a built version) ------------------------------

def readback_verdict(model_source: str, *, manufacture: Mapping[str, Any] | None = None) -> Outcome:
    """What a release concludes about the cad.readback obligation of a one-part project whose model.py is
    `model_source`: cad_model run builds the version, cad_model check publishes its packet to hw/checks/<V>/cad/, and
    evidence.collect_packets reads it back sealed, as the release gate does. `manufacture` is the part.yaml manufacture
    block (None: none, so the part is made as installed). A build that refuses the part leaves no version, so the case
    never reached the check: that is an error naming the command, never a verdict."""
    import hashlib
    import shutil
    import subprocess
    import tempfile

    require("yaml")
    require("jsonschema")
    if shutil.which("uv") is None:
        raise Skip("uv is required to build the version")
    projects = load("cad_model_manufacturing_projects", SKILLS / "cad-model" / "tests" / "fixtures" / "manufacturing_projects.py")
    projects.ENVELOPE_MM = (1000.0, 1000.0, 1000.0)  # the defect models are long: the build volume is not what is under test
    fx = projects.shared()
    intent = load("cad_record_manufacturing_intent", SKILLS / "cad-record" / "scripts" / "manufacturing_intent.py")
    cad_model = str(SKILLS / "cad-model" / "scripts" / "cad_model.py")
    documents = projects.documents()
    if manufacture is not None:
        part = f"parts/{projects.PN}/part.yaml"
        documents[part] = {**documents[part], "manufacture": dict(manufacture)}
    with tempfile.TemporaryDirectory() as tmp:
        hw = fx.write_record(Path(tmp) / "project" / "hw", documents)
        model = hw / "parts" / projects.PN / "model.py"
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_text(model_source, encoding="utf-8")
        record, problems = intent.check_record(hw)
        errors = [f"{p.code} {p.path}: {p.message}" for p in problems if p.severity == "error"]
        if errors:
            raise ValueError(f"the corpus record is invalid: {errors}")
        build = subprocess.run(["uv", "run", "--quiet", "--script", cad_model, "run", str(hw), "--label", "v1"],
                               capture_output=True, text=True, timeout=1800, check=False)
        manifest = hw / "versions" / "v1" / "manifest.json"
        if build.returncode != 0 or not manifest.is_file():  # a refused part has no version, so the check never ran
            raise command_failed("cad_model run", build.args, build.returncode, build.stdout + build.stderr)
        check = subprocess.run(["uv", "run", "--quiet", "--script", cad_model, "check", "v1", "--hw", str(hw)],
                               capture_output=True, text=True, timeout=1800, check=False)
        if check.returncode not in (0, 1):
            raise command_failed("cad_model check", check.args, check.returncode, check.stdout + check.stderr)
        sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
        packet = hw / "checks" / "v1" / "cad" / "result.json"
        if not packet.is_file():  # a version exists, so a check that left no packet is a failed run and not an unevidenced obligation
            raise RuntimeError(f"cad_model check exited {check.returncode} (`{' '.join(check.args)}`) but left no packet at "
                               f"hw/checks/v1/cad/result.json: {(check.stdout + check.stderr)[-1500:]}")
        found, findings = _evidence().collect_packets(hw, "v1", sha)
        if findings:
            raise Rejected("evidence: " + "; ".join(f.message for f in findings))
        return state_verdict(record, found, "v1", sha, ("cad.readback@",))


# --- semantic output oracles (cad-model check: the agreement and declared-dimension outcomes of cad.readback) ------

ORACLE_BUILD = HERE / "fixtures" / "oracles" / "build_case.py"
AGREEMENT = ("cad.glb_step_agreement@",)  # the GLB<->STEP agreement of every made part
DIMENSIONS = ("cad.declared_dimensions@",)  # every declared heat-set hole diameter
_ORACLE_CACHE: dict[str, tuple[Any, list[Any], str, str]] = {}


def oracle_verdict(model: Mapping[str, Any] | None, fault: Mapping[str, Any] | None, prefixes: tuple[str, ...],
                   changed_only: bool = False) -> Outcome:
    """What a release concludes about the obligations whose id starts with one of `prefixes` (cad.glb_step_agreement@,
    cad.declared_dimensions@) for the three-part oracle project of fixtures/oracles/build_case.py with the `model`
    overrides and the one exporter `fault`. The build script builds the version, `cad_model check` (`--changed-only`
    with `changed_only`) publishes its packet to hw/checks/<V>/cad/, and evidence.collect_packets reads it back sealed,
    as the release gate does. A case is built and checked once per process, so pairs that share a clean twin build it once."""
    import json

    key = json.dumps({"model": model, "fault": fault, "changed_only": changed_only}, sort_keys=True)
    if key not in _ORACLE_CACHE:
        _ORACLE_CACHE[key] = _oracle_packets(model, fault, changed_only)
    record, found, label, sha = _ORACLE_CACHE[key]
    return state_verdict(record, found, label, sha, prefixes)


def _oracle_packets(model: Mapping[str, Any] | None, fault: Mapping[str, Any] | None,
                    changed_only: bool) -> tuple[Any, list[Any], str, str]:
    """(the record, the version's sealed producer packets, the version label, its manifest sha256) of one oracle case."""
    import hashlib
    import json
    import shutil
    import subprocess
    import tempfile

    uv = shutil.which("uv")
    if uv is None:
        raise Skip("uv is not on PATH: the cad-model runtime cannot be created")
    with tempfile.TemporaryDirectory(prefix="mfg-oracle-") as work:
        case_file = Path(work) / "case.json"
        case_file.write_text(json.dumps({"model": model, "fault": fault, "changed_only": changed_only}), encoding="utf-8")
        built = subprocess.run([uv, "run", "--quiet", "--script", str(ORACLE_BUILD), str(case_file), work],
                               capture_output=True, text=True, check=False)  # no timeout: cold imports take minutes
        if built.returncode != 0:
            raise command_failed("build_case.py", built.args, built.returncode, built.stderr)
        lines = built.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"build_case.py exited 0 but printed no version: {built.stderr[-1500:]}")
        version = json.loads(lines[-1])
        hw, label = Path(version["hw"]), version["label"]
        options = ["--changed-only"] if changed_only else []
        run = subprocess.run([uv, "run", "--quiet", "--script", str(SKILLS / "cad-model" / "scripts" / "cad_model.py"),
                              "check", label, "--hw", str(hw), *options], capture_output=True, text=True, check=False)
        if run.returncode not in (0, 1):
            raise command_failed("cad_model check", run.args, run.returncode, run.stdout + run.stderr)
        if not (hw / "checks" / label / "cad" / "result.json").is_file():
            raise RuntimeError(f"cad_model check exited {run.returncode} (`{' '.join(run.args)}`) but left no packet at "
                               f"hw/checks/{label}/cad/result.json: {(run.stdout + run.stderr)[-1500:]}")
        intent = load("cad_record_manufacturing_intent", SKILLS / "cad-record" / "scripts" / "manufacturing_intent.py")
        record, problems = intent.check_record(hw)
        errors = [f"{p.code} {p.path}: {p.message}" for p in problems if p.severity == "error"]
        if errors:
            raise ValueError(f"the oracle record is invalid: {errors}")
        sha = hashlib.sha256((hw / "versions" / label / "manifest.json").read_bytes()).hexdigest()
        found, findings = _evidence().collect_packets(hw, label, sha)
        if findings:
            raise Rejected("evidence: " + "; ".join(f.message for f in findings))
        return record, list(found), label, sha


def oracle_case(model: Mapping[str, Any] | None = None, fault: Mapping[str, Any] | None = None, *,
                prefixes: tuple[str, ...], changed_only: bool = False) -> Callable[[bool], Outcome]:
    """The `run` of a Pair: run(True) builds the oracle project with the `model` overrides and the one exporter `fault`,
    run(False) the unmodified project, and each takes oracle_verdict for `prefixes`. With `changed_only`, `model` is
    the change under test (v2 against a clean v1), so the twin keeps it and differs from the mutant by the fault alone."""
    def run(mutant: bool) -> Outcome:
        if mutant:
            return oracle_verdict(model, fault, prefixes, changed_only)
        return oracle_verdict(model, None, prefixes, True) if changed_only else oracle_verdict(None, None, prefixes)
    return run
