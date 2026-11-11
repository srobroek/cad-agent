#!/usr/bin/env python3
"""Accepted-evidence fixtures for the evidence, impact, release and recheck tests: a confirmed use case,
resolved questions, a release-ready intent for a migrated record, synthetic producer packets and ledger
entries. Every value is synthetic test data: nothing here is a material, machine, supplier or test result.

Load it by path (it loads the U1 fixture builder and cad-record's evidence module the same way):
    acceptance = load("cad_record_acceptance_fixtures", HERE / "fixtures" / "acceptance.py")
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"


def _load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


fx = _load("cad_record_mi_fixtures", HERE / "manufacturing_intent.py")
mi = fx.intent_module()
evidence = _load("cad_record_evidence", SCRIPTS / "evidence.py")
impact = sys.modules["cad_record_impact"]
layout = sys.modules["cad_record_layout"]

NA = "not applicable to this synthetic test fixture"
DECISION = "DR-001"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def edit_yaml(path: Path, change: Any) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _not_applicable(value: Any) -> Any:
    """Every fact below `value` not applicable."""
    if isinstance(value, dict) and "status" in value:
        return fx.not_applicable(NA)
    if isinstance(value, dict):
        return {key: _not_applicable(item) for key, item in value.items()}
    return value


def confirmed_use_case(service_class: str = "appearance_concept", *, decision: str = DECISION,
                       purpose: str = "TST synthetic release fixture", operating_limits: list[str] | None = None,
                       exclusions: list[str] | None = None, policy_requirement: str = "REQ-001",
                       **overrides: Any) -> dict[str, Any]:
    """A confirmed use case of `service_class`: negligible consequences and every other fact not
    applicable, a known release scope and an approved evidence policy asking CAD-valid evidence of
    `policy_requirement`, so a final choice is possible. `overrides` replace top-level fields."""
    block = {key: _not_applicable(value) for key, value in mi.blank_use_case(NA, {}).items()}
    block.update(status="confirmed", decision=decision, service_class=fx.known(service_class), purpose=fx.known(purpose))
    block["consequences"]["severity"] = fx.known("negligible")
    block["release_scope"] = {"purpose": fx.known(purpose),
                              "operating_limits": fx.known(operating_limits or ["TST bench handling only"]),
                              "exclusions": fx.known(exclusions or ["TST: no load-bearing or end use"])}
    block["evidence_policy"] = {"obligations": [{"requirement": policy_requirement, "level": "cad_valid"}],
                                "decision": decision}
    block.update(copy.deepcopy(overrides))
    return {"status": block.pop("status"), "decision": block.pop("decision"), **block}


def resolve_questions(requirements: dict[str, Any], decision: str = DECISION) -> None:
    """Resolve every open question of a requirements.yaml document in place."""
    for question in requirements.get("open_questions") or []:
        question["status"] = "resolved"
        question["resolution"] = {"answer": "TST fixture: answered for the synthetic release fixture", "decision": decision}


def release_ready(hw: Path, service_class: str = "appearance_concept", **use: Any) -> None:
    """Make a migrated record's manufacturing intent explicit and final-ready: a confirmed use case,
    resolved questions, each made part's functions, synthetic material profile (printed for an additive route,
    bulk stock otherwise) and route on a capability profile per process that approves it (service and critical
    surfaces not applicable), catalogue buys purchased, every interface non-critical."""
    hw = Path(hw)
    edit_yaml(hw / "product.yaml", lambda data: data.update(use_case=confirmed_use_case(service_class, **use)))
    edit_yaml(hw / "requirements.yaml", resolve_questions)
    materials: dict[str, str] = {}  # process kind -> its material profile id
    for path in sorted((hw / "parts").glob("*/part.yaml")):
        part = yaml.safe_load(path.read_text(encoding="utf-8"))
        mode = (part.get("procurement") or {}).get("mode")
        catalogue = part.get("make_buy") == "buy" and mode != "made_to_drawing"
        if catalogue:
            material: Any = fx.not_applicable(NA)
            route = [fx.operation("buy", "purchased", purchase={"identity": fx.known(f"TST catalogue item {part['PN']}"),
                                                                "specification": fx.known("TST specification"),
                                                                "ratings": fx.not_applicable(NA)})]
        else:
            kind = part["process"]
            material = materials.setdefault(kind, "tst_printed_stock" if kind in mi.ADDITIVE_KINDS else "tst_bulk_stock")
            extra = {"provider": "outsourced"} if mode == "made_to_drawing" else {}
            if kind in mi.ADDITIVE_KINDS:
                extra.update(orientation=fx.not_applicable(NA), supports=fx.not_applicable(NA))
            route = [fx.operation("make", kind, f"tst_{kind}", **extra)]
        part["intent"] = fx.intent([{"function": "cosmetic"}], material, route, service=fx.not_applicable(NA),
                                   critical_surfaces=fx.not_applicable(NA))
        path.write_text(yaml.safe_dump(part, sort_keys=False, allow_unicode=True), encoding="utf-8")
    edit_yaml(hw / "manufacturing" / "materials.yaml", lambda data: data.update(profiles=[
        fx.material_profile(ident, form="printed" if ident == "tst_printed_stock" else "bulk")
        for ident in sorted(set(materials.values()))]))
    edit_yaml(hw / "manufacturing" / "capabilities.yaml", lambda data: data.update(profiles=[
        fx.capability_profile(f"tst_{kind}", kind, materials=[{"material": ident, "status": "approved", "source": fx.source("user")}])
        for kind, ident in sorted(materials.items())]))

    def non_critical(data: dict[str, Any]) -> None:
        for interface in data.get("interfaces") or []:
            interface["functional"] = {"critical": fx.known(False), "functions": [{"function": "locating"}]}
    edit_yaml(hw / "interfaces.yaml", non_critical)


def confirm_selection(hw: Path, decision: str = DECISION) -> str:
    """Record in a release-ready record (release_ready) a confirmed, supported material and process selection:
    one candidate covering every part as recorded (each made part's material profile and route, each catalogue
    item bought), with an authored, sourced evaluation passing every mandatory requirement, confirmed final by
    `decision` on the current selection input digest. A design freeze and a release need a confirmed selection
    covering every made part (an unrecorded one blocks like any unknown). Returns the digest. Every value is
    synthetic."""
    scenarios = selection_scenarios()
    documents, problems = mi.read_documents(Path(hw))
    assert not problems, problems
    must = [r["id"] for r in documents["requirements.yaml"]["requirements"] if r.get("priority") == "must"]
    parts = []
    for rel, part in sorted(documents.items()):
        if not rel.startswith("parts/"):
            continue
        if part.get("make_buy") == "buy" and (part.get("procurement") or {}).get("mode") != "made_to_drawing":
            parts.append({"PN": part["PN"], "material_profile": fx.not_applicable("catalogue item: its rating governs"),
                          "route": [{"kind": "purchased", "capability": fx.not_applicable("a catalogue item")}]})
        else:
            intent = part["intent"]
            parts.append({"PN": part["PN"], "material_profile": intent["material_profile"],
                          "route": [{key: op[key] for key in ("kind", "capability", "provider") if key in op}
                                    for op in intent["route"]]})
    evaluations = [scenarios.evaluation(req, "pass", "tst_property", ">=", 1, "mm", measured={"value": 1, "units": "mm"})
                   for req in must]
    documents["manufacturing/selection.yaml"] = scenarios.selection([scenarios.candidate(
        "as_recorded", "TST product as recorded", "TST: the recorded architecture", parts, cost=1.0,
        evaluations=evaluations)])
    documents, digest = recorded_selection(documents, primary="as_recorded", decision=decision)
    (Path(hw) / "manufacturing" / "selection.yaml").write_text(
        yaml.safe_dump(documents["manufacturing/selection.yaml"], sort_keys=False, allow_unicode=True), encoding="utf-8")
    return digest


def producer_packet(record: Any, version: str, *, outcome: str = "pass", obligations: Any = None,
                    findings: tuple[dict[str, Any], ...] = (), complete: bool = True) -> dict[str, Any]:
    """A synthetic producer check-result packet giving `outcome` to every obligation a software producer
    can show: not the record-derived ones (intent.*, assembly.joint), never a physical validation."""
    chosen = [o for o in (obligations if obligations is not None else mi.required_checks(record))
              if not o.family.startswith(impact.RECORD_FAMILIES) and o.level != "physically_validated"]
    outcomes = [{"obligation": o.id, "outcome": outcome, "level": o.level, "method": "TST synthetic check",
                 "coverage": "TST fixture: every expected obligation"} for o in chosen]
    if not complete and outcomes:
        outcomes = outcomes[:-1]
    shown = "fail" if outcome == "fail" and outcomes else ("unknown" if outcome == "unknown" or not complete else "pass")
    return {"schema_version": 1, "record_version": 2, "producer": "TST synthetic producer", "version": version,
            "scope": [{"product": True}], "expected": [o.as_dict() for o in chosen], "outcomes": outcomes,
            "findings": list(findings), "dependencies": {}, "complete": complete, "result": shown}


def publish_packet(hw: Path, version: str, data: dict[str, Any], subject: str = "manufacturing-intent") -> Path:
    """Seal a producer packet as hw/checks/<version>/<subject>/result.json."""
    staged = layout.staging(hw, "bundle.checks")
    (staged / "result.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return layout.publish(hw, "bundle.checks", {"V": version, "S": subject}, staged,
                          recipe={"subject": subject, "producer": "tst"}, producer="tst", status=data["result"]).path


def waiver(subject: dict[str, Any], decision: str = "DR-002", **extra: Any) -> dict[str, Any]:
    data = {"decision": decision, "subject": subject, "criterion": "TST criterion", "operating_envelope": "TST bench only",
            "reason": "TST synthetic waiver", "consequence": "TST: the item stays unvalidated", "authority": "TST lead",
            "until_change": "the next design change"}
    data.update(extra)
    return data


PHYSICAL = {"specimen": "TST coupon 1", "configuration": "TST as built", "material": "TST lot 1", "process": "TST route",
            "protocol": "TST protocol", "instrument": "TST gauge, calibrated", "results": "TST within limits",
            "date": "2026-01-01", "approver": "TST lead", "scope": "TST coupon scope only"}


def entry(record: Any, ident: str, subject: dict[str, Any], level: str, result: str = "pass", *,
          version: str | None = None, manifest_sha256: str | None = None, geometry: dict[str, Any] | None = None,
          requirement: str | None = None, obligation: str | None = None, artifacts: list[str] | None = None,
          bundle: str | None = None, hw: Path | None = None, domains: tuple[str, ...] | None = None,
          **extra: Any) -> dict[str, Any]:
    """An accepted evidence ledger entry; a pass or fail is tied to `version`, cites `artifacts` (hw/ paths;
    hw/verification.csv by default) or the sealed `bundle`, hashed as they stand below `hw` (a placeholder
    hash without it), and records the subject's current fingerprints (every domain, or `domains`)."""
    def digest(path: str) -> str:
        return sha((hw.parent / path).read_bytes()) if hw is not None else "0" * 64

    data: dict[str, Any] = {"id": ident, "subject": subject, "level": level, "result": result,
                            "method": "TST synthetic method"}
    if requirement:
        data["requirement"] = requirement
    if obligation:
        data["obligation"] = obligation
    if result in ("pass", "fail"):
        data["configuration"] = {"version": version, "manifest_sha256": manifest_sha256}
        data["criterion"] = fx.criterion("tst_property", ">=", 1, "mm")
        if bundle is not None:
            data["bundle"] = {"path": bundle, "manifest_sha256": digest(f"{bundle}/{layout.MANIFEST}")}
        else:
            data["artifacts"] = [{"path": path, "sha256": digest(path)} for path in (artifacts or ["hw/verification.csv"])]
        prints = impact.fingerprints(record, subject, geometry or {})
        data["fingerprints"] = {d: prints[d] for d in (domains or tuple(prints)) if d in prints}
        if level == "physically_validated":
            data["physical"] = dict(PHYSICAL)
    data.update(extra)
    return data


def write_ledger(hw: Path, entries: list[dict[str, Any]], dispositions: list[dict[str, Any]] | None = None) -> None:
    (hw / "manufacturing").mkdir(parents=True, exist_ok=True)
    (hw / "manufacturing" / "evidence.yaml").write_text(yaml.safe_dump({"entries": entries}, sort_keys=False), encoding="utf-8")
    if dispositions is not None:
        (hw / "manufacturing" / "findings.yaml").write_text(yaml.safe_dump({"dispositions": dispositions}, sort_keys=False),
                                                           encoding="utf-8")


def decision(hw: Path, ident: str, status: str | None = None) -> None:
    path = hw / "decisions" / f"{ident}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = f"Status: {status}\n\n" if status else ""
    path.write_text(f"{head}# {ident} TST fixture decision\n\nApproved by the TST user.\n", encoding="utf-8")


def freeze_anchor(hw: Path, version: str) -> str:
    """The acceptance digest of `version` as hw/ stands (what a record version 2 freeze records)."""
    folder = layout.version_dir(hw, version)
    packets, problems = evidence.collect_packets(hw, version, sha((folder / layout.MANIFEST).read_bytes()))
    assert not problems, problems
    return evidence.acceptance_digest(evidence.acceptance_inventory(hw, impact.read_record(hw), packets))


FINAL_RECOMMENDATION = {"status": "final", "reasons": ["TST: confirmed for the fixture"], "limits": ["TST: none recorded"],
                        "what_would_change": ["TST: nothing recorded"]}


def selection_scenarios() -> Any:
    """U2's selection scenario builders (tests/fixtures/selection/scenarios.py), each a valid TST record."""
    return _load("cad_record_selection_scenarios", HERE / "selection" / "scenarios.py")


def recorded_selection(docs: dict[str, Any], *, primary: str | None = None, decision: str = DECISION,
                       digest: str | None = None) -> tuple[dict[str, Any], str]:
    """A copy of record documents whose selection.yaml assessment records `digest` (default the current
    selection input digest, selection.input_digest) and, with `primary`, that candidate confirmed final by
    `decision`; and the current digest. Status, assessment and recommendation are not selection inputs."""
    docs = copy.deepcopy(docs)
    current = evidence.selection.input_digest(docs)
    document = docs["manufacturing/selection.yaml"]
    document["assessment"]["input_sha256"] = digest or current
    if primary is not None:
        document["status"] = "confirmed"
        document["assessment"]["decision"] = decision
        document["recommendation"] = {**FINAL_RECOMMENDATION, "primary": primary}
    assert evidence.selection.input_digest(docs) == current, "recording the selection changed its own inputs"
    return docs, current
