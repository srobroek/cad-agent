#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Change impact over the manufacturing-intent dependency domains of a record version 2 design record.

    uv run --script impact.py --hw HW --since-version vM [--version vN] [--out .cad-work/impact.json]

Each subject (a part, an interface, an assembly operation, a requirement or the product) has one
sha256 fingerprint per dependency domain (`DOMAINS`, the evidence ledger's `fingerprints` keys):
use case; loads and environment; material and its condition; process, route, capability and setup;
orientation, supports and finish; geometry, manufacturing state and components; interfaces, datums
and assembly order; criteria and sources. Comparing the record of vM with vN (or with the current
hw/ record) lists the domains that changed per subject, the check obligations and accepted evidence
they make stale, the decisions they reopen, the earliest stage that accepted a changed fact and the
flow it proposes, and the evidence whose recorded fingerprints still match: carry-forward candidates
only, which count again only once the lead accepts them as new current-version ledger entries with an
approving decision. Nothing is ever carried forward, rewritten or re-sealed automatically.

The selection is compared by selection.py's own digest of its inputs (selection.input_digest over
DIGEST_SCOPE): changed inputs, or a selection.yaml assessment that records another digest, reopen the
selection's assessment decision; the recorded choice counts again only once it is re-assessed and
confirmed through the approval gate.

The module is standard library only; load it by path as `cad_record_impact` (dataclasses look it up
in sys.modules). The CLI reads YAML with PyYAML. Prints one JSON report: exit 0 no impact, 1 impact
found (report only), 2 usage or infrastructure error.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


mi = _load_module("cad_record_manufacturing_intent", SCRIPTS / "manufacturing_intent.py")
# The one definition of the selection inputs and their digest (selection.input_digest over DIGEST_SCOPE).
selection = _load_module("cad_record_selection", SCRIPTS / "selection.py")

# The evidence ledger's fingerprint domains (manufacturing-evidence.schema.json#/$defs/fingerprint_domain).
DOMAINS = ("use_case", "load", "environment", "material", "condition", "lot", "process", "route", "capability", "setup",
           "orientation", "finish", "geometry", "interface", "criterion", "source")
# The dependency domains a change is reported under, each grouping fingerprint domains.
GROUPS: Mapping[str, tuple[str, ...]] = {
    "use_case": ("use_case",),
    "loads_environment_duty_life": ("load", "environment"),
    "material_condition": ("material", "condition", "lot"),
    "route_process_capability_setup_stock": ("process", "route", "capability", "setup"),
    "orientation_support_finish": ("orientation", "finish"),
    "geometry_state_components": ("geometry",),
    "interface_datum_tolerance_assembly_order": ("interface",),
    "criterion_source_calibration_policy": ("criterion", "source"),
}
# The earliest workflow stage that accepts a fact of each domain: a change reopens it.
DOMAIN_STAGE: Mapping[str, str] = {
    "use_case": "intent", "load": "intent", "environment": "intent", "criterion": "specify", "interface": "specify",
    "geometry": "layout", "material": "parts", "condition": "parts", "process": "parts", "route": "parts",
    "capability": "parts", "source": "parts", "setup": "detail", "orientation": "detail", "finish": "detail",
    "lot": "release",
}
_EVERYTHING = frozenset(DOMAINS)
_JUDGED = frozenset({"criterion", "source"})  # every check is judged against criteria and sourced data
# The domains each check family depends on, by family prefix (the longest matching prefix decides).
FAMILY_DOMAINS: tuple[tuple[str, frozenset[str]], ...] = (
    ("cad.", frozenset({"geometry"})),
    ("route.", _JUDGED | {"process", "route", "capability", "setup", "orientation", "finish", "material", "condition"}),
    ("process.", _JUDGED | {"geometry", "process", "route", "capability", "setup", "orientation", "finish", "material",
                           "condition"}),
    ("purchase.", _JUDGED | {"process", "route", "load", "environment", "geometry"}),
    ("optical.", _JUDGED | {"material", "process", "route", "finish"}),
    ("engineering.", _JUDGED | {"use_case", "geometry", "material", "condition", "load", "environment", "orientation",
                                "process", "route", "finish"}),
    ("feature.", _JUDGED | {"geometry", "process", "route", "capability", "setup", "orientation", "finish", "material",
                            "condition", "interface"}),
    ("interface.", _JUDGED | {"geometry", "interface", "material", "condition", "process", "route", "capability", "finish",
                              "load", "environment"}),
    ("contact.", _JUDGED | {"geometry", "interface", "material", "condition", "finish", "load", "environment"}),
    ("motion.", _JUDGED | {"geometry", "interface", "material", "finish", "load", "process"}),
    ("assembly.", _JUDGED | {"geometry", "interface"}),
    ("function.", _JUDGED | {"geometry", "material", "condition", "load", "environment", "interface", "process", "finish"}),
    ("requirement.", _EVERYTHING),
    ("policy.", _EVERYTHING),
)
# Obligations derived from the record itself, recomputed on every read and never stale: intent.* (unknown
# inputs) and assembly.joint (evidence.py judges the declared joint facts with assembly_operations.check_joints).
RECORD_FAMILIES = ("intent.", "assembly.joint")


def family_domains(family: str) -> frozenset[str]:
    """The fingerprint domains a check family depends on; an unlisted family depends on every domain."""
    best = max((prefix for prefix, _ in FAMILY_DOMAINS if family.startswith(prefix)), key=len, default=None)
    return dict(FAMILY_DOMAINS)[best] if best is not None else _EVERYTHING


def subject_key(subject: Mapping[str, Any]) -> str:
    """The fingerprinted subject a statement, obligation or evidence is about: a PN, an IF, op:<id>, a REQ
    or `product`. Components, operations and features of a part share the part's fingerprints."""
    if subject.get("PN"):
        return str(subject["PN"])
    if subject.get("interface"):
        return str(subject["interface"])
    if subject.get("assembly_operation"):
        return f"op:{subject['assembly_operation']}"
    if subject.get("requirements"):
        return str(tuple(subject["requirements"])[0])
    return "product"


# --- normalized values -------------------------------------------------------------------------------------------

def plain(value: Any) -> Any:
    """`value` as plain JSON data: dataclasses as dicts of their fields, mappings as dicts, sequences as lists."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value) if f.name != "raw"}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def digest(value: Any) -> str:
    """sha256 of `value` as canonical JSON: sorted keys, no whitespace."""
    text = json.dumps(plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _without(data: Any, *keys: str) -> Any:
    if isinstance(data, Mapping):
        return {key: value for key, value in data.items() if key not in keys}
    return data


def _get(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _artifacts(value: Any) -> dict[str, Any]:
    """Every `artifact` path a structure names, with the sha256 beside it."""
    found: dict[str, Any] = {}

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            if isinstance(item.get("artifact"), str):
                found[item["artifact"]] = item.get("sha256")
            for child in item.values():
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(plain(value))
    return found


class _Record:
    """The raw (plain) views of one normalized Record that fingerprints read."""

    def __init__(self, record: Any, geometry: Mapping[str, str | None]) -> None:
        self.record = record
        self.geometry = geometry
        self.product = plain(record.product)
        self.use_case = self.product.get("use_case") if isinstance(self.product.get("use_case"), Mapping) else None
        self.parts = {pn: plain(part.raw) for pn, part in record.parts.items()}
        self.interfaces = {if_id: plain(interface.raw) for if_id, interface in record.interfaces.items()}
        operations = record.assembly_operations
        self.operations = [] if mi.is_fact(operations) else [plain(op) for op in operations]

    # -- part-level values --
    def intent(self, pn: str) -> Mapping[str, Any]:
        intent = (self.parts.get(pn) or {}).get("intent")
        return intent if isinstance(intent, Mapping) else {}

    def route(self, pn: str) -> list[Mapping[str, Any]]:
        route = self.intent(pn).get("route")
        return [op for op in route if isinstance(op, Mapping)] if isinstance(route, list) else []

    def profiles(self, pn: str) -> list[str]:
        intent = self.intent(pn)
        names = [intent.get("material_profile")] + [c.get("material_profile") for c in intent.get("components") or ()
                                                    if isinstance(c, Mapping)]
        return [name for name in names if isinstance(name, str)]

    def part_values(self, pn: str) -> dict[str, Any]:
        part, intent, route = self.parts.get(pn), self.intent(pn), self.route(pn)
        if part is None:
            return {}
        service = intent.get("service")
        materials = [plain(self.record.materials[name].raw) if name in self.record.materials else {"missing": name}
                     for name in self.profiles(pn)]
        capabilities = [plain(self.record.capabilities[op["capability"]].raw)
                        if op["capability"] in self.record.capabilities else {"missing": op["capability"]}
                        for op in route if isinstance(op.get("capability"), str)]
        capability_facts = [op.get("capability") for op in route if isinstance(op.get("capability"), Mapping)]
        wanted = service.get("loads") if isinstance(service, Mapping) else None
        loads = self.use_case.get("loads") if self.use_case else None
        if isinstance(loads, list) and wanted:
            loads = [load for load in loads if isinstance(load, Mapping) and load.get("id") in wanted]
        criteria = self.requirements_for(pn)
        values: dict[str, Any] = {
            "use_case": _without(self.use_case, "status", "decision"),
            "load": {"loads": loads, "motion": _get(self.use_case, "motion"), "service": _without(service, "environment")},
            "environment": {"product": _get(self.use_case, "environment"), "part": _get(service, "environment")},
            "material": {"declared": part.get("material"), "profile": intent.get("material_profile"), "profiles": materials},
            "condition": {"profiles": [m.get("process_state") for m in materials],
                          "route": [{k: op.get(k) for k in ("id", "material_state_in", "material_state_out", "input_state",
                                                             "output_state")} for op in route]},
            "process": {"process": part.get("process"), "make_buy": part.get("make_buy"),
                        "procurement": _get(part, "procurement", "mode"), "kinds": [op.get("kind") for op in route],
                        "route": None if route else intent.get("route")},
            "route": [_without(op, "setup", "orientation", "supports", "post_processing", "allowances") for op in route],
            "capability": {"profiles": capabilities, "facts": capability_facts},
            "setup": [{"id": op.get("id"), "setup": op.get("setup")} for op in route],
            "orientation": [{"id": op.get("id"), "orientation": op.get("orientation"), "supports": op.get("supports")}
                            for op in route],
            "finish": {"finish": part.get("finish"), "operations": [
                {"id": op.get("id"), "post_processing": op.get("post_processing"), "allowances": op.get("allowances")}
                for op in route]},
            "geometry": {"model": self.geometry.get(pn), "manufacture": part.get("manufacture"),
                         "components": intent.get("components"), "critical_surfaces": intent.get("critical_surfaces")},
            "interface": {"interfaces": [self.interfaces[i] for i in sorted(self.interfaces_of(pn))],
                          "operations": [op for op in self.operations if pn in (op.get("part"), op.get("into"))],
                          "datums": _get(part, "drawing", "datum_features")},
            "criterion": {"requirements": criteria, "policy": _get(self.use_case, "evidence_policy")},
        }
        sources = _artifacts([values["material"], values["capability"], part.get("procurement"), criteria])
        values["source"] = {path: self.record.artifacts.get(path, sha) for path, sha in sorted(sources.items())}
        return values

    def interfaces_of(self, pn: str) -> set[str]:
        found = set()
        for if_id, interface in self.interfaces.items():
            members = {interface.get("from"), interface.get("to"), *(interface.get("pair") or ())}
            members |= {s.get("PN") for s in _get(interface, "functional", "surfaces") or () if isinstance(s, Mapping)}
            if pn in members:
                found.add(if_id)
        return found

    def requirements_for(self, pn: str) -> list[Any]:
        intent = self.intent(pn)
        ids: set[str] = set()
        functions, surfaces = intent.get("functions"), intent.get("critical_surfaces")
        for entry in functions if isinstance(functions, list) else []:
            ids.update(entry.get("requirements") or ())
        ids.update(_get(intent, "service", "requirements") or ())
        for surface in surfaces if isinstance(surfaces, list) else []:
            ids.update(surface.get("requirements") or ())
        for op in self.route(pn):
            ids.update(op.get("requirements") or ())
        for req_id, requirement in self.record.requirements.items():
            if any(subject.get("PN") == pn for subject in requirement.subjects):
                ids.add(req_id)
        return [plain(self.record.requirements[i]) for i in sorted(ids) if i in self.record.requirements]

    def members(self, key: str) -> list[str]:
        """The parts whose fingerprints an interface, operation, requirement or the product depends on."""
        if key in self.interfaces:
            interface = self.interfaces[key]
            return sorted({p for p in (interface.get("from"), interface.get("to"), *(interface.get("pair") or ()))
                           if isinstance(p, str) and p in self.parts})
        if key.startswith("op:"):
            op: Mapping[str, Any] = next((o for o in self.operations if o.get("id") == key[3:]), {})
            return sorted({p for p in (op.get("part"), op.get("into")) if isinstance(p, str) and p in self.parts})
        if key in self.record.requirements:
            linked = sorted(pn for pn in self.parts if any(r.get("id") == key for r in self.requirements_for(pn)))
            linked += sorted(i for i, f in self.interfaces.items() if key in (_get(f, "functional", "requirements") or ()))
            return [m for m in linked if m in self.parts] or sorted(self.parts)
        return sorted(self.parts)

    def values(self, key: str) -> dict[str, Any]:
        if key in self.parts:
            return self.part_values(key)
        members = {pn: self.part_values(pn) for pn in self.members(key)}
        combined = {domain: {pn: values.get(domain) for pn, values in members.items()} for domain in DOMAINS}
        combined["use_case"] = _without(self.use_case, "status", "decision")
        if key in self.interfaces:
            interface = self.interfaces[key]
            ops = [op for op in self.operations if op.get("interface") == key]
            combined["interface"] = {"interface": interface, "operations": ops}
            combined["criterion"] = {"acceptance": _get(interface, "functional", "acceptance"),
                                     "requirements": [plain(self.record.requirements[r])
                                                      for r in _get(interface, "functional", "requirements") or ()
                                                      if r in self.record.requirements],
                                     "policy": _get(self.use_case, "evidence_policy")}
        elif key.startswith("op:"):
            combined["interface"] = {"operation": next((o for o in self.operations if o.get("id") == key[3:]), None),
                                     "parts": combined["interface"]}
        elif key in self.record.requirements:
            combined["criterion"] = {"requirement": plain(self.record.requirements[key]),
                                     "policy": _get(self.use_case, "evidence_policy")}
        else:
            combined["criterion"] = {"requirements": [plain(r) for _, r in sorted(self.record.requirements.items())],
                                     "policy": _get(self.use_case, "evidence_policy")}
        combined.pop("lot", None)
        return combined


def fingerprints(record: Any, subject: Mapping[str, Any], geometry: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """sha256 per dependency domain of the subject `subject` in the normalized record `record`. `geometry`
    maps a PN to the sha256 of the model source a version built (its manifest `parts[PN].source_sha256`,
    or the current hw/parts/<PN>/model.py): without it the geometry is fingerprinted as unknown. The lot
    is a property of physical evidence, never of the record, so it is never fingerprinted here."""
    values = _Record(record, geometry or {}).values(subject_key(subject))
    return {domain: digest(values[domain]) for domain in DOMAINS if domain in values}


def subject_fingerprints(record: Any, geometry: Mapping[str, str | None] | None = None) -> dict[str, dict[str, str]]:
    """The fingerprints of every subject of `record`: the product, each part, interface, assembly
    operation and requirement."""
    view = _Record(record, geometry or {})
    keys = ["product", *sorted(view.parts), *sorted(view.interfaces),
            *(f"op:{op['id']}" for op in view.operations if isinstance(op.get("id"), str)), *sorted(record.requirements)]
    result = {}
    for key in keys:
        values = view.values(key)
        result[key] = {domain: digest(values[domain]) for domain in DOMAINS if domain in values}
    return result


# --- comparison --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Impact:
    """What changed between two records and what it makes stale; a proposal, never applied."""

    since: str | None
    version: str | None
    changed: Mapping[str, tuple[str, ...]]  # subject key -> changed fingerprint domains
    stale_obligations: Mapping[str, tuple[str, ...]]  # obligation id -> the changed domains it depends on
    stale_evidence: Mapping[str, tuple[str, ...]]  # evidence id -> the domains whose fingerprint differs or is absent
    reopened_decisions: Mapping[str, tuple[str, ...]]  # decision id -> why
    earliest_stage: str | None
    proposed_flow: Mapping[str, str] | None
    carry_forward: tuple[Mapping[str, Any], ...]
    # The selection input digests (selection.input_digest) before and after, the one selection.yaml's
    # assessment records, whether the inputs changed and whether that assessment is stale; None when the
    # caller supplied no digests.
    selection: Mapping[str, Any] | None = None

    @property
    def any(self) -> bool:
        return bool(self.changed or self.stale_evidence or (self.selection or {}).get("changed"))

    def groups(self) -> dict[str, list[str]]:
        """The changed dependency domains (GROUPS) per subject."""
        return {key: [group for group, members in GROUPS.items() if set(members) & set(domains)]
                for key, domains in self.changed.items()}

    def as_dict(self) -> dict[str, Any]:
        return {"since": self.since, "version": self.version, "changed": {k: list(v) for k, v in self.changed.items()},
                "groups": self.groups(), "stale_obligations": {k: list(v) for k, v in self.stale_obligations.items()},
                "stale_evidence": {k: list(v) for k, v in self.stale_evidence.items()},
                "reopened_decisions": {k: list(v) for k, v in self.reopened_decisions.items()},
                "earliest_stage": self.earliest_stage, "proposed_flow": dict(self.proposed_flow) if self.proposed_flow else None,
                "carry_forward": [dict(item) for item in self.carry_forward],
                "selection": dict(self.selection) if self.selection is not None else None}


def changed_domains(before: Mapping[str, Mapping[str, str]], after: Mapping[str, Mapping[str, str]]) -> dict[str, tuple[str, ...]]:
    """Per subject key, the fingerprint domains that differ, appear or disappear."""
    changed = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, {}), after.get(key, {})
        domains = tuple(d for d in DOMAINS if old.get(d) != new.get(d))
        if domains:
            changed[key] = domains
    return changed


def stale_obligations(obligations: Iterable[Any], changed: Mapping[str, Iterable[str]]) -> dict[str, tuple[str, ...]]:
    """The obligations (manufacturing_intent.Obligation) whose subject changed in a domain their family
    depends on, with those domains. Record-derived intent.* obligations are recomputed, never stale."""
    stale = {}
    for obligation in obligations:
        if obligation.family.startswith(RECORD_FAMILIES):
            continue
        hit = tuple(d for d in DOMAINS if d in set(changed.get(subject_key(obligation.subject), ()))
                    and d in family_domains(obligation.family))
        if hit:
            stale[obligation.id] = hit
    return stale


def evidence_staleness(entry: Mapping[str, Any], current: Mapping[str, str]) -> tuple[str, ...]:
    """The fingerprint domains in which accepted evidence `entry` no longer matches the current subject
    `current`: each recorded domain that differs, or ("unrecorded",) when it records none."""
    recorded = entry.get("fingerprints")
    if not isinstance(recorded, Mapping) or not recorded:
        return ("unrecorded",)
    return tuple(d for d in DOMAINS if d in recorded and recorded[d] != current.get(d))


def _stage_min(stages: Iterable[str]) -> str | None:
    known = [s for s in stages if s in mi.STAGES]
    return min(known, key=mi.STAGES.index) if known else None


def _earlier(recorded: object, earliest: str) -> str:
    """The earlier of a recorded stage and `earliest`; `earliest` when `recorded` is not a stage."""
    if isinstance(recorded, str) and recorded in mi.STAGES:
        return min((recorded, earliest), key=mi.STAGES.index)
    return earliest


def compare(before: Any, after: Any, *, before_geometry: Mapping[str, str | None] | None = None,
            after_geometry: Mapping[str, str | None] | None = None, since: str | None = None,
            version: str | None = None, before_selection_sha256: str | None = None,
            after_selection_sha256: str | None = None) -> Impact:
    """The impact of changing record `before` (the record of version `since`) into `after` (the record of
    `version`, or the current record when `version` is None). Ledger evidence comes from `after`. The
    selection input digests of the two records' documents (selection.input_digest) decide whether the
    recorded selection assessment is reopened: when the inputs changed, or when `after`'s recorded
    input_sha256 is not its digest."""
    old = subject_fingerprints(before, before_geometry)
    new = subject_fingerprints(after, after_geometry)
    changed = changed_domains(old, new)
    stale = stale_obligations(mi.required_checks(after), changed)

    stale_evidence: dict[str, tuple[str, ...]] = {}
    carry: list[dict[str, Any]] = []
    reopened: dict[str, list[str]] = {}

    def reopen(decision: Any, why: str) -> None:
        if isinstance(decision, str) and why not in reopened.setdefault(decision, []):
            reopened[decision].append(why)

    for entry in after.evidence:
        key = subject_key(entry.get("subject") or {})
        domains = evidence_staleness(entry, new.get(key, {}))
        if domains:
            stale_evidence[str(entry.get("id"))] = domains
            for decision in entry.get("decisions") or ():
                reopen(decision, f"it accepted {entry.get('id')}, now stale in {', '.join(domains)}")
        elif since is not None and _get(entry, "configuration", "version") == since and version != since:
            carry.append({"evidence": entry.get("id"), "from": since, "to": version, "subject": plain(entry.get("subject")),
                          "level": entry.get("level"), "fingerprints": dict(new.get(key, {})),
                          "note": "candidate only: counts for the new version once a new ledger entry for it cites the "
                                  "same evidence and an approving impact decision"})

    if "use_case" in {d for domains in changed.values() for d in domains}:
        reopen(after.use_case.decision, "the use case changed")
    selection_state = _selection_state(after, before_selection_sha256, after_selection_sha256)
    if selection_state is not None and (selection_state["changed"] or selection_state["stale"]):
        reopen(selection_state["decision"], "the selection inputs changed (selection.input_digest)"
               if selection_state["changed"] else "the recorded selection assessment is not of the current inputs")
    for pn, part in after.parts.items():
        part_domains = set(changed.get(pn, ()))
        route = part.intent.route
        if part_domains & {"geometry", "orientation", "load", "material"} and not mi.is_fact(route):
            for op in route:
                if isinstance(op.orientation, Mapping):
                    reopen(op.orientation.get("decision"), f"{pn} {op.id}: its approved orientation depends on "
                                                           f"{', '.join(sorted(part_domains & {'geometry', 'orientation', 'load', 'material'}))}")
    if changed:
        reopen(_get(plain(after.product), "flow", "frozen", "decision"), "the frozen record changed")

    earliest = _stage_min(DOMAIN_STAGE[d] for domains in changed.values() for d in domains)
    flow = _get(plain(after.product), "flow") or {}
    proposed: dict[str, str] | None = None
    if earliest is not None:
        # never raised: a reopened stage keeps every check at or after it
        proposed = {"stage": _earlier(flow.get("stage"), earliest), "recheck_from": _earlier(flow.get("recheck_from"), earliest)}
    return Impact(since, version, changed, stale, stale_evidence,
                  {k: tuple(v) for k, v in sorted(reopened.items())}, earliest, proposed, tuple(carry),
                  {key: value for key, value in selection_state.items() if key != "decision"} if selection_state else None)


def _selection_state(after: Any, before_sha256: str | None, after_sha256: str | None) -> dict[str, Any] | None:
    """The selection inputs' digests, the one `after`'s selection.yaml assessment records, whether they
    changed (None without `before_sha256`) and whether that assessment is stale; None without `after_sha256`."""
    if after_sha256 is None:
        return None
    assessment = _get(plain(after.selection.raw), "assessment")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    recorded = assessment.get("input_sha256")
    return {"before": before_sha256, "after": after_sha256, "recorded": recorded,
            "changed": None if before_sha256 is None else before_sha256 != after_sha256,
            "stale": recorded is not None and recorded != after_sha256, "decision": assessment.get("decision")}


# --- reading records ---------------------------------------------------------------------------------------------

LEDGERS = ("manufacturing/evidence.yaml", "manufacturing/findings.yaml")


def read_inputs(directory: Path, ledgers_from: Path | None = None) -> tuple[dict[str, Any], Any]:
    """The parsed record documents below `directory` (hw/ or a version's record/ snapshot) and their
    normalized record. A snapshot holds no ledgers, so `ledgers_from` (hw/) supplies them."""
    documents, _ = mi.read_documents(directory)
    if ledgers_from is not None:
        current, _ = mi.read_documents(ledgers_from)
        documents = {rel: data for rel, data in documents.items() if rel not in LEDGERS}
        documents.update({rel: current[rel] for rel in LEDGERS if rel in current})
    return documents, mi.normalize_record(documents)


def read_record(directory: Path, ledgers_from: Path | None = None) -> Any:
    """The normalized record below `directory` (see read_inputs)."""
    return read_inputs(directory, ledgers_from)[1]


def version_geometry(manifest: Mapping[str, Any]) -> dict[str, str | None]:
    """PN -> the sha256 of the model source a version built, from its manifest `parts`."""
    parts = manifest.get("parts")
    if not isinstance(parts, Mapping):
        return {}
    return {pn: entry.get("source_sha256") if isinstance(entry, Mapping) else None for pn, entry in parts.items()}


def current_geometry(hw: Path) -> dict[str, str | None]:
    """PN -> the sha256 of each current hw/parts/<PN>/model.py."""
    return {path.parent.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(hw).glob("parts/*/model.py")) if path.is_file()}


def _version(hw: Path, label: str) -> tuple[dict[str, Any], Any, dict[str, str | None]]:
    layout = _load_module("cad_record_layout", SCRIPTS / "layout.py")
    _, folder, snapshot = layout.built_version(hw, label)
    manifest = json.loads((folder / layout.MANIFEST).read_text(encoding="utf-8"))
    return (*read_inputs(snapshot, ledgers_from=hw), version_geometry(manifest))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hw", type=Path, required=True, help="the record's hw/ directory")
    parser.add_argument("--since-version", required=True, help="the built version the evidence was accepted for")
    parser.add_argument("--version", help="the built version to compare with; the current hw/ record when omitted")
    parser.add_argument("--out", type=Path, help="also write the report here, under .cad-work/")
    args = parser.parse_args(argv)
    try:
        hw = args.hw.resolve()
        before_documents, before, before_geometry = _version(hw, args.since_version)
        if args.version:
            after_documents, after, after_geometry = _version(hw, args.version)
        else:
            (after_documents, after), after_geometry = read_inputs(hw), current_geometry(hw)
        if after.version != mi.RECORD_VERSION or before.version != mi.RECORD_VERSION:
            problem = mi.migration_problem(after.product) or mi.migration_problem(before.product)
            message = problem.message if problem is not None else "the record is not record version 2"
            print(json.dumps({"status": "error", "code": mi.MIGRATION_REQUIRED, "message": message,
                              "action": problem.action if problem is not None else None}, indent=2))
            return 2
        impact = compare(before, after, before_geometry=before_geometry, after_geometry=after_geometry,
                         since=args.since_version, version=args.version,
                         before_selection_sha256=selection.input_digest(before_documents),
                         after_selection_sha256=selection.input_digest(after_documents))
        report = {**impact.as_dict(), "status": "impact" if impact.any else "unchanged"}
        text = json.dumps(report, indent=2, ensure_ascii=False)
        if args.out is not None:
            layout = _load_module("cad_record_layout", SCRIPTS / "layout.py")
            target = layout.scratch_path(hw, args.out, "--out")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 1 if impact.any else 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
