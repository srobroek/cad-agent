# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Assembly operations: the one normalizer of a record's installation, closure, fastening, adjustment,
inspection and service operations, and their pure order, closure and joint checks. Stdlib only, no
file reads and no CAD kernel: callers pass the parsed assembly.yaml mapping or a normalized record.

Authority. Record version 2 keeps operations in assembly.yaml top-level `operations` (a list, or an
unknown fact); record version 1 kept installation operations in `drawing.operations`. `normalize`
reads exactly one of them; a record holding both is refused (`intent.dual_authority`), and no
drawing, guide or check reads either list directly. The migration moves the legacy list verbatim,
so both shapes normalize to the same operations.

Order model. Record order is the assembly sequence; `predecessors` are constraints that sequence must
satisfy. A part an installing operation (press_fit, heat_set, retaining_ring, insert, fasten, join,
close) puts in place is absent before it and present after it until a disassemble operation removes
it; every other occurrence is present from the start (the base). A close operation encloses its
`closes` parts: no later operation reaches them. `check_order` judges exactly this declared sequence:
an operation whose receiving part is absent, whose part is enclosed or installed twice, or whose
predecessor comes later or is missing fails `assembly.order`. Whether the part and the tools can
physically travel to their positions is `assembly.access`, a geometric check (cad-motion access.py)
that this module only plans: `access_plan` lists, per operation, the moving part, its receiver-local
direction or declared insertion path, the tools and the parts present at that state.

Paths. An installing operation may declare `path`: the part's offsets from its installed pose (mm,
receiving part's frame, installed orientation kept), from `start` through `via` to the installed
pose, whose last leg must arrive along `direction`. Without one the part travels the default straight
line along `direction`, and access reports that default.

Tools. A legacy `tools` entry is a display name: its reach and envelope stay unknown. A
`tool_access` entry may name a capability profile, an approach (receiver-local, like `direction`),
a reach and an envelope; an envelope fact whose source carries an artifact (a STEP solid of the tool
and holder in the operated part's model frame at the engaged pose) is what a geometric check uses.

Joints. `check_joints` judges the joint evidence of fastening, insert, press-fit, heat-set,
retaining-ring and joining operations, one `assembly.joint@op:<id>` outcome each: retention needs a
sourced fact, a fastener its engagement and torque, a fastener, insert or heat-set its edge distance
against its sourced criterion (any joint that declares one is judged), and the host of an insert,
heat-set or press fit its sourced service temperature limit against the use-case temperature (a host
part or material profile that does not resolve keeps the joint unknown, and a limit that is an estimate
or has no qualifying source can fail it but never pass it). A limit exceeded or an edge
distance short of its criterion is a failure; retention evidence is what supports a joint.
"""

from __future__ import annotations

import math
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeGuard

Vec = tuple[float, float, float]

KINDS = ("press_fit", "heat_set", "retaining_ring", "insert", "fasten", "join", "close", "adjust", "inspect", "disassemble")
LEGACY_KINDS = ("press_fit", "heat_set", "retaining_ring")  # the kinds drawing.operations held
INSTALLING = frozenset(("press_fit", "heat_set", "retaining_ring", "insert", "fasten", "join", "close"))
JOINING = frozenset(("press_fit", "heat_set", "retaining_ring", "insert", "fasten", "join"))
INTERFACE_LIMIT_KINDS = frozenset(LEGACY_KINDS)  # the fit, insert or ring comes from the interface limit of this kind
ACCEPTANCE = {"seated": 2, "standoff": 2, "flush": 2, "engaged": 1, "torqued": 1, "adjusted": 1, "inspected": 1}
GEOMETRIC_ACCEPTANCE = frozenset(("seated", "standoff", "flush", "engaged"))  # measurable on the assembled model
REQUIRED = {  # schema: assembly.schema.json#/$defs/assembly_operation allOf
    "press_fit": ("into", "interface", "direction", "acceptance"), "heat_set": ("into", "interface", "direction", "acceptance"),
    "retaining_ring": ("into", "interface", "direction", "acceptance"), "insert": ("into", "direction", "acceptance"),
    "fasten": ("into", "direction", "acceptance"), "join": ("into", "direction", "acceptance"), "close": ("into", "closes"),
    "adjust": ("acceptance",), "inspect": ("acceptance",), "disassemble": ("into", "direction"),
}
QUALIFYING_SOURCES = frozenset(("test", "supplier", "datasheet", "standard", "calibration"))
HOST_TEMPERATURE_KINDS = frozenset(("heat_set", "insert", "press_fit"))
TEMPERATURE_LIMITS = ("max_service_temperature", "heat_deflection_temperature")
EDGE_DISTANCE_KINDS = frozenset(("fasten", "insert", "heat_set"))  # hole-based joints: an edge distance is required
LENGTH_MM = {"mm": 1.0, "um": 1e-3, "m": 1e3}  # manufacturing_intent.UNITS' lengths, in mm
PATH_ALIGNMENT = 1e-6  # sine of the largest angle between a path's last leg and `direction`
JOINT_METHOD = ("declared joint evidence: sourced retention, fastener engagement and torque, edge distance against its "
                "criterion, host service temperature limit (cad-record assembly_operations)")
JOINT_COVERAGE = ("the declared facts and material profiles only: no joint strength, pull-out, creep, fatigue, preload "
                  "or cure is computed; retention, engagement and torque count only with a test, supplier, datasheet, "
                  "standard or calibration source")
RULE_SOURCE = types.MappingProxyType({
    "kind": "policy", "reference": "cad-record assembly_operations order and closure rules",
    "origin": "cad-agent", "document": "skills/cad-record/scripts/assembly_operations.py"})
ORDER_METHOD = "declared assembly sequence: predecessors, parts present, closure state (cad-record assembly_operations)"
ORDER_COVERAGE = ("the declared sequence only: the receiving part present, the part not enclosed or installed twice, "
                  "predecessors earlier; part paths and tool reach are assembly.access, a geometric check, not covered")


class OperationsError(ValueError):
    """Operations that cannot be normalized; `code` is intent.dual_authority or intent.schema."""

    def __init__(self, message: str, code: str = "intent.schema") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Tool:
    """A tool that must reach the operation: a display name (`declared` False: legacy `tools`) or a
    `tool_access` entry with its optional capability, approach, reach and envelope facts."""

    name: str
    declared: bool
    capability: str | None = None
    approach: Vec | None = None
    reach: Mapping[str, Any] | None = None
    envelope: Mapping[str, Any] | None = None

    @property
    def reach_known(self) -> bool:
        return isinstance(self.reach, Mapping) and self.reach.get("status") == "known"

    @property
    def envelope_artifact(self) -> tuple[str, str] | None:
        """(path below hw/, sha256) of the solid a known envelope cites, else None: a name has no geometry."""
        if not isinstance(self.envelope, Mapping) or self.envelope.get("status") != "known":
            return None
        for source in self.envelope.get("sources") or ():
            if isinstance(source, Mapping) and source.get("artifact") and source.get("sha256"):
                return source["artifact"], source["sha256"]
        return None


@dataclass(frozen=True)
class AssemblyOperation:
    id: str
    index: int  # position in record order
    kind: str
    part: str  # a part reference: PN or @occurrence
    into: str | None
    interface: str | None
    direction: Vec | None  # unit vector, in the receiving part's model frame
    acceptance: str | None
    features: tuple[str, ...]
    predecessors: tuple[str, ...]
    closes: tuple[str, ...]
    tools: tuple[Tool, ...]
    consumables: tuple[str, ...]
    verify_joint: str | None
    engagement: Mapping[str, Any] | None
    torque: Mapping[str, Any] | None
    retention: Mapping[str, Any] | None
    service_access: Mapping[str, Any] | None
    inspection: str | None
    raw: Mapping[str, Any]
    edge_distance: Mapping[str, Any] | None = None  # {criterion, distance} as recorded
    path: tuple[Vec, ...] | None = None  # declared insertion path: start, then via, mm offsets (receiver frame)

    @property
    def installs(self) -> bool:
        return self.kind in INSTALLING

    @property
    def removes(self) -> bool:
        return self.kind == "disassemble"

    @property
    def geometric_acceptance(self) -> bool:
        return self.acceptance in GEOMETRIC_ACCEPTANCE

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)


@dataclass(frozen=True)
class Operations:
    """The normalized operations of one record. `authority` names the list they came from; `unknown`
    holds the reason when the record states them unknown (or holds none)."""

    authority: str  # operations, drawing.operations or none
    items: tuple[AssemblyOperation, ...]
    unknown: str | None = None

    def get(self, ident: str) -> AssemblyOperation:
        for item in self.items:
            if item.id == ident:
                return item
        raise KeyError(ident)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)


# --------------------------------------------------------------------------- normalization


def normalize(assembly: Mapping[str, Any]) -> Operations:
    """The operations of a parsed assembly.yaml: top-level `operations`, else legacy `drawing.operations`;
    OperationsError when both are present or an entry is malformed."""
    if not isinstance(assembly, Mapping):
        raise OperationsError("assembly.yaml must be a mapping")
    drawing = assembly.get("drawing")
    legacy = drawing.get("operations") if isinstance(drawing, Mapping) else None
    if "operations" in assembly and legacy is not None:
        raise OperationsError("assembly.yaml holds installation operations in both top-level operations and "
                              "drawing.operations: keep them in top-level operations only", "intent.dual_authority")
    if "operations" in assembly:
        data = assembly["operations"]
        if isinstance(data, Mapping) and data.get("status") in ("unknown", "not_applicable"):
            return Operations("operations", (), str(data.get("reason") or f"operations are {data['status']}"))
        return Operations("operations", _items(data, "operations"))
    if legacy is not None:
        return Operations("drawing.operations", _items(legacy, "drawing/operations"))
    return Operations("none", (), "assembly.yaml records no operations")


def from_record(record: Any) -> Operations:
    """The operations of a normalized manufacturing_intent Record (its assembly_operations)."""
    data = record.assembly_operations
    if not isinstance(data, (list, tuple)):  # an unknown or not_applicable Fact
        return Operations("operations", (), data.reason or f"operations are {data.status}")
    authority = "operations" if getattr(record, "version", 2) >= 2 else "drawing.operations"
    return Operations(authority, _items(_thaw(data), "operations"))


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _items(data: Any, where: str) -> tuple[AssemblyOperation, ...]:
    if not isinstance(data, list):
        raise OperationsError(f"{where} must be a list, or an unknown fact with its reason")
    items = tuple(_operation(raw, index, f"{where}/{index}") for index, raw in enumerate(data))
    ids = [item.id for item in items]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise OperationsError(f"{where}: operation ids repeat: {', '.join(duplicates)}")
    return items


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationsError(f"{where} must be a non-empty string")
    return value


def _point(value: Any, where: str) -> Vec:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value)):
        raise OperationsError(f"{where} must be three finite numbers")
    return (float(value[0]) + 0.0, float(value[1]) + 0.0, float(value[2]) + 0.0)


def _vector(value: Any, where: str) -> Vec:
    point = _point(value, where)
    length = math.hypot(*point)
    if length < 1e-9:
        raise OperationsError(f"{where} must not be zero")
    return (point[0] / length + 0.0, point[1] / length + 0.0, point[2] / length + 0.0)


def _path(raw: Mapping[str, Any], kind: str, direction: Vec | None, where: str) -> tuple[Vec, ...] | None:
    """The declared insertion path's points, start then via (the installed pose, the origin, is not
    listed): legs of non-zero length, the last one arriving along `direction`."""
    data = raw.get("path")
    if data is None:
        return None
    at = f"{where}/path"
    if kind not in INSTALLING or direction is None:
        raise OperationsError(f"{at}: only an installing operation with a direction has an insertion path")
    if not isinstance(data, Mapping) or "start" not in data or not isinstance(data.get("via") or [], list):
        raise OperationsError(f"{at} needs a start and, optionally, a list of via points")
    points = (_point(data["start"], f"{at}/start"),
              *(_point(p, f"{at}/via/{i}") for i, p in enumerate(data.get("via") or [])))
    for number, (a, b) in enumerate(zip(points, (*points[1:], (0.0, 0.0, 0.0)))):
        if math.dist(a, b) < 1e-9:
            raise OperationsError(f"{at}: leg {number + 1} has zero length")
    last = points[-1]
    length = math.hypot(*last)
    cross = (last[1] * direction[2] - last[2] * direction[1], last[2] * direction[0] - last[0] * direction[2],
             last[0] * direction[1] - last[1] * direction[0])
    if -sum(a * b for a, b in zip(last, direction)) <= 0.0 or math.hypot(*cross) > PATH_ALIGNMENT * length:
        raise OperationsError(f"{at}: the last leg must arrive along direction")
    return points


def _names(raw: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    values = raw.get(key) or []
    if not isinstance(values, list):
        raise OperationsError(f"{where}/{key} must be a list")
    return tuple(_text(value, f"{where}/{key}") for value in values)


def _fact(raw: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = raw.get(key)
    return value if isinstance(value, Mapping) else None


def _operation(raw: Any, index: int, where: str) -> AssemblyOperation:
    if not isinstance(raw, Mapping):
        raise OperationsError(f"{where} must be a mapping")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise OperationsError(f"{where}/kind must be one of {', '.join(KINDS)}")
    missing = [key for key in REQUIRED[kind] if key not in raw]
    if missing:
        raise OperationsError(f"{where}: a {kind} operation needs {', '.join(missing)}")
    acceptance = raw.get("acceptance")
    features: tuple[str, ...] = ()
    if acceptance is not None:
        if not isinstance(acceptance, Mapping) or acceptance.get("kind") not in ACCEPTANCE:
            raise OperationsError(f"{where}/acceptance needs kind {'|'.join(ACCEPTANCE)}")
        names = acceptance.get("features")
        if not isinstance(names, list) or not 1 <= len(names) <= 2 or not all(isinstance(f, str) and f for f in names):
            raise OperationsError(f"{where}/acceptance names one or two features")
        wanted = ACCEPTANCE[acceptance["kind"]]
        if acceptance["kind"] in GEOMETRIC_ACCEPTANCE and len(names) != wanted:
            raise OperationsError(f"{where}/acceptance {acceptance['kind']} names {wanted} feature(s)")
        features = tuple(names)
    tools = [Tool(name, declared=False) for name in _names(raw, "tools", where)]
    for number, entry in enumerate(raw.get("tool_access") or []):
        at = f"{where}/tool_access/{number}"
        if not isinstance(entry, Mapping):
            raise OperationsError(f"{at} must be a mapping")
        approach = entry.get("approach")
        tools.append(Tool(_text(entry.get("tool"), f"{at}/tool"), declared=True, capability=entry.get("capability"),
                          approach=None if approach is None else _vector(approach, f"{at}/approach"),
                          reach=_fact(entry, "reach"), envelope=_fact(entry, "envelope")))
    joint = raw.get("verify_joint")
    if "edge_distance" in raw and kind not in JOINING:
        raise OperationsError(f"{where}/edge_distance: a {kind} operation makes no joint")
    direction = None if raw.get("direction") is None else _vector(raw["direction"], f"{where}/direction")
    return AssemblyOperation(
        id=_text(raw.get("id"), f"{where}/id"), index=index, kind=kind, part=_text(raw.get("part"), f"{where}/part"),
        into=None if raw.get("into") is None else _text(raw["into"], f"{where}/into"),
        interface=None if raw.get("interface") is None else _text(raw["interface"], f"{where}/interface"),
        direction=direction,
        acceptance=None if acceptance is None else acceptance["kind"], features=features,
        predecessors=_names(raw, "predecessors", where), closes=_names(raw, "closes", where), tools=tuple(tools),
        consumables=_names(raw, "consumables", where),
        verify_joint=None if joint is None else _text(joint, f"{where}/verify_joint"),
        engagement=_fact(raw, "engagement"), torque=_fact(raw, "torque"), retention=_fact(raw, "retention"),
        service_access=_fact(raw, "service_access"),
        inspection=None if raw.get("inspection") is None else _text(raw["inspection"], f"{where}/inspection"),
        raw=types.MappingProxyType(dict(raw)), edge_distance=_fact(raw, "edge_distance"),
        path=_path(raw, kind, direction, where))


# --------------------------------------------------------------------------- sequence and presence


@dataclass(frozen=True)
class Step:
    """One operation in sequence with the assembly state just before it: the occurrences present and
    the enclosed ones (occurrence -> the close operation that enclosed it). `part` and `into` are
    resolved occurrence ids (the raw references without an inventory)."""

    operation: AssemblyOperation
    part: str
    into: str | None
    closes: tuple[str, ...]
    present: frozenset[str]
    closed: Mapping[str, str]


@dataclass(frozen=True)
class AssemblyState:
    steps: tuple[Step, ...]
    base: frozenset[str]  # present from the start: every occurrence no operation installs
    unresolved: Mapping[str, str] = field(default_factory=dict)  # operation id -> why its references do not resolve


def sequence(operations: Operations, inventory: Any = None) -> AssemblyState:
    """The assembly state before each operation, in record order. With an assembly_data Inventory,
    references resolve to occurrence ids and every occurrence counts; without one, the raw references
    stand for themselves and the base is every referenced part no operation installs."""
    unresolved: dict[str, str] = {}

    def resolve(ref: str, op: AssemblyOperation) -> str:
        if inventory is None:
            return ref
        try:
            resolved: str = inventory.resolve(ref).id
            return resolved
        except ValueError as error:
            unresolved.setdefault(op.id, str(error))
            return ref

    resolved = [(op, resolve(op.part, op), None if op.into is None else resolve(op.into, op),
                 tuple(resolve(ref, op) for ref in op.closes)) for op in operations.items]
    installed = {part for op, part, _into, _closes in resolved if op.installs}
    if inventory is not None:
        everyone = set(inventory.ids)
    else:
        everyone = {ref for _op, part, into, closes in resolved for ref in (part, into, *closes) if ref is not None}
    base = frozenset(everyone - installed)
    present = set(base)
    closed: dict[str, str] = {}
    steps = []
    for op, part, into, closes in resolved:
        steps.append(Step(op, part, into, closes, frozenset(present), types.MappingProxyType(dict(closed))))
        if op.installs:
            present.add(part)
        if op.removes:
            present.discard(part)
        if op.kind == "close":
            for ref in closes:
                closed.setdefault(ref, op.id)
    return AssemblyState(tuple(steps), base, types.MappingProxyType(unresolved))


# --------------------------------------------------------------------------- findings


def _finding(ident: str, code: str, op: AssemblyOperation, *, severity: str, outcome: str, measured: str,
             consequence: str, remedy: str, family: str, obligation: str | None, criterion: Mapping[str, Any] | None = None,
             uncertainty: str = "none: the declared record decides it", alternatives: list[dict[str, str]] | None = None,
             coverage: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": ident, "code": code, "subject": {"assembly_operation": op.id}, "severity": severity, "outcome": outcome,
        "measured": {"text": measured} if outcome != "unknown" else {"status": "unknown", "reason": measured},
        "criterion": dict(criterion) if criterion is not None else {
            "property": "assembly_order", "operator": "equals",
            "value": "receiving part present, part reachable (not enclosed), installed once, predecessors earlier",
            "source": dict(RULE_SOURCE)},
        "method": {"name": ORDER_METHOD if family == "assembly.order" else "declared joint evidence (cad-record assembly_operations)",
                   "tool": "cad-record assembly_operations.py"},
        "uncertainty": uncertainty, "consequence": consequence, "remedy": remedy,
        "recheck": {"families": [family], "scope": [{"assembly_operation": op.id}], "earliest_stage": "detail"},
    }
    if obligation is not None:
        data["obligation"] = obligation
    if alternatives:
        data["alternatives"] = alternatives
    if coverage:
        data["coverage"] = coverage
    return data


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return out if out and out[0].isalpha() else f"op_{out}"


@dataclass(frozen=True)
class Report:
    """Per-obligation outcomes (manufacturing.schema.json#/$defs/obligation_outcome) and findings."""

    outcomes: tuple[Mapping[str, Any], ...]
    findings: tuple[Mapping[str, Any], ...]

    def outcome(self, obligation: str) -> Mapping[str, Any]:
        for row in self.outcomes:
            if row["obligation"] == obligation:
                return row
        raise KeyError(obligation)


def check_order(operations: Operations, inventory: Any = None) -> Report:
    """`assembly.order@op:<id>` for every operation (or `intent.assembly_operations@product` unknown when the
    operations are unknown): pass when the declared sequence is possible, else fail with its findings."""
    if operations.unknown is not None:
        return Report(({"obligation": "intent.assembly_operations@product", "outcome": "unknown", "level": "process_planned",
                        "method": ORDER_METHOD, "coverage": "no operations are recorded",
                        "uncertainty": operations.unknown},), ())
    state = sequence(operations, inventory)
    position = {op.id: op.index for op in operations.items}
    installer: dict[str, list[AssemblyOperation]] = {}
    for step in state.steps:
        if step.operation.installs:
            installer.setdefault(step.part, []).append(step.operation)
    outcomes, findings = [], []
    for step in state.steps:
        op = step.operation
        obligation = f"assembly.order@op:{op.id}"
        mine: list[dict[str, Any]] = []

        def fail(code: str, measured: str, consequence: str, remedy: str, *, severity: str = "critical",
                 alternatives: list[dict[str, str]] | None = None) -> None:
            ident = f"{_slug(op.id)}_{code.split('.')[-1]}"
            ident = ident if all(f["id"] != ident for f in mine) else f"{ident}_{len(mine) + 1}"
            mine.append(_finding(ident, code, op, severity=severity, outcome="fail", measured=measured,
                                 consequence=consequence, remedy=remedy, family="assembly.order", obligation=obligation,
                                 alternatives=alternatives))

        if op.id in state.unresolved:
            outcomes.append({"obligation": obligation, "outcome": "unknown", "level": "process_planned",
                             "method": ORDER_METHOD, "coverage": ORDER_COVERAGE,
                             "uncertainty": f"{op.id}: {state.unresolved[op.id]}"})
            continue
        for predecessor in op.predecessors:
            if predecessor not in position:
                fail("assembly.predecessor_unknown", f"predecessor {predecessor} is not an operation",
                     "the declared order cannot be followed", f"name an existing operation, or remove {predecessor}",
                     severity="major")
            elif position[predecessor] >= op.index:
                fail("assembly.order_impossible", f"{op.id} comes before its predecessor {predecessor} in the sequence",
                     f"{op.id} cannot be carried out in the declared sequence",
                     f"move {op.id} after {predecessor}, or correct its predecessors")
        enclosed = [ref for ref in (step.part, step.into) if ref is not None and ref in step.closed
                    and not (op.kind == "close" and ref == step.into)]
        for ref in enclosed:
            closer = step.closed[ref]
            fail("assembly.closed_before_install",
                 f"{op.id} ({op.kind}) reaches {ref}, which {closer} encloses earlier in the sequence",
                 f"{ref} cannot be reached once {closer} has closed it: the {op.kind} is impossible",
                 f"install {ref} before {closer}, or give {ref} an access path that {closer} does not close",
                 alternatives=[{"text": f"reorder: {op.id} before {closer}",
                                "tradeoffs": "check that nothing else needs the open state after it"},
                               {"text": f"split the closing part so {ref} stays reachable",
                                "tradeoffs": "adds a joint, fasteners and a tolerance stack"}])
        if step.into is not None and step.into not in step.present and not enclosed:
            later = [other.id for other in installer.get(step.into, []) if other.index > op.index]
            fail("assembly.into_absent",
                 f"{op.id} puts {op.part} into {op.into}, which is not in place yet" +
                 (f" (installed later by {', '.join(later)})" if later else " (removed earlier)"),
                 "the receiving part is missing at this step: the declared order is impossible",
                 f"install {op.into} before {op.id}")
        if op.installs and step.part in step.present:
            fail("assembly.installed_twice", f"{op.id} installs {op.part}, which is already in place",
                 "the sequence installs the same occurrence twice", "remove the duplicate step, or disassemble first",
                 severity="major")
        if not op.installs and step.part not in step.present and not enclosed:
            fail("assembly.part_absent", f"{op.id} ({op.kind}) needs {op.part}, which is not in place",
                 f"the {op.kind} cannot be done at this step", f"move {op.id} after {op.part} is installed")
        if op.kind == "close":
            for ref in step.closes:
                if ref not in step.present and ref != step.part:
                    later = [other.id for other in installer.get(ref, []) if other.index > op.index]
                    if not later:  # an enclosed part installed later fails at that later step instead
                        fail("assembly.part_absent", f"{op.id} encloses {ref}, which is not in place", f"{ref} is missing "
                             "inside the closed assembly", f"install {ref} before {op.id}")
        findings.extend(mine)
        row: dict[str, Any] = {"obligation": obligation, "outcome": "fail" if mine else "pass", "level": "process_planned",
                               "method": ORDER_METHOD, "coverage": ORDER_COVERAGE}
        if mine:
            row["findings"] = [f["id"] for f in mine]
        outcomes.append(row)
    return Report(tuple(outcomes), tuple(findings))


# --------------------------------------------------------------------------- access plan


@dataclass(frozen=True)
class AccessRequest:
    """What a geometric access check of one operation needs: the operated occurrence (`part`), the one
    that travels (`moving`, None when the operation moves no part) and its direction in the `frame`
    occurrence's model frame, the tools, and the occurrences present at that state as obstacles (the
    moving part and its receiver excluded for the part path; the operated part excluded for the tools).
    `path` is the declared insertion path (receiver frame, see `_path`), None for the default straight
    line along `direction`. `unknown` lists what keeps reach unknown."""

    operation: AssemblyOperation
    part: str
    moving: str | None
    frame: str  # the occurrence whose model frame `direction` and tool approaches are in
    direction: Vec | None
    removal: bool  # a disassemble path runs away from the receiver (along direction)
    path_obstacles: frozenset[str]
    tool_obstacles: frozenset[str]
    tools: tuple[Tool, ...]
    unknown: tuple[str, ...]
    path: tuple[Vec, ...] | None = None


def access_plan(operations: Operations, inventory: Any = None) -> tuple[AccessRequest, ...]:
    """One AccessRequest per operation, from the declared sequence (see `sequence`)."""
    state = sequence(operations, inventory)
    requests = []
    for step in state.steps:
        op = step.operation
        unknown: list[str] = []
        travels = op.kind in JOINING or op.kind in ("close", "disassemble")
        if travels and op.direction is None:
            unknown.append(f"{op.id} has no direction: its part path is unknown")
        if op.id in state.unresolved:
            unknown.append(state.unresolved[op.id])
        for tool in op.tools:
            if not tool.declared:
                unknown.append(f"tool {tool.name!r} is a name only: its reach and envelope are unknown")
                continue
            if tool.envelope_artifact is None:
                unknown.append(f"tool {tool.name!r} has no sourced envelope solid: its reach is unknown")
            if tool.approach is None:
                unknown.append(f"tool {tool.name!r} has no approach direction")
        after = set(step.present) | ({step.part} if op.installs else set())
        path_obstacles = frozenset(after - {step.part} - ({step.into} if step.into else set()))
        tool_obstacles = frozenset(after - {step.part})
        requests.append(AccessRequest(op, step.part, step.part if travels else None, step.into or step.part,
                                      op.direction, op.removes, path_obstacles, tool_obstacles, op.tools, tuple(unknown),
                                      op.path))
    return tuple(requests)


# --------------------------------------------------------------------------- joints


def check_joints(operations: Operations, record: Any, resolve_property: Callable[[Any, str], Any], *,
                 part_of: Callable[[str], str | None] | None = None) -> Report:
    """`assembly.joint@op:<id>` for every joining operation, with the findings on its joint evidence
    (see the module): fail when a finding fails, unknown when one is unknown, else pass. `record` is a
    manufacturing_intent Record, `resolve_property` its resolve_property, and `part_of` maps a part
    reference to its PN (default: a bare PN is itself, an @occurrence unresolved)."""
    part_of = part_of or (lambda ref: None if ref.startswith("@") else ref)
    outcomes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    temperature = record.use_case.facts.get("environment.temperature")
    for op in operations.items:
        if op.kind not in JOINING:
            continue
        obligation = f"assembly.joint@op:{op.id}"
        mine: list[dict[str, Any]] = []

        def add(code: str, outcome: str, measured: str, consequence: str, remedy: str, *, severity: str = "major",
                criterion: Mapping[str, Any] | None = None) -> None:
            unknown = outcome == "unknown"
            mine.append(_finding(f"{_slug(op.id)}_{code.split('.')[-1]}", code, op, severity=severity, outcome=outcome,
                                 measured=measured, consequence=consequence, remedy=remedy, family="assembly.joint",
                                 obligation=obligation,
                                 criterion={"status": "unknown", "reason": measured} if unknown else criterion,
                                 uncertainty=measured if unknown else "the declared facts and profiles"))

        if not _sourced(op.retention):
            add("assembly.retention_unknown", "unknown", f"{op.id}: retention of the {op.kind} is not a sourced fact",
                "the joint may loosen, creep or pull out over the service temperature and life",
                "record retention evidence: a joint test, supplier data or a sourced calculation for this host "
                "material, temperature and life")
        if op.kind == "fasten":
            for name, value in (("engagement", op.engagement), ("torque", op.torque)):
                if not _sourced(value):
                    add(f"assembly.{name}_unknown", "unknown", f"{op.id}: thread {name} is not a sourced fact",
                        "the fastener may strip or loosen", f"record the {name} with its source")
        _edge_distance(op, add)
        _host_temperature(op, record, resolve_property, part_of, temperature, add)
        findings.extend(mine)
        states = {f["outcome"] for f in mine}
        row: dict[str, Any] = {"obligation": obligation,
                               "outcome": "fail" if "fail" in states else "unknown" if "unknown" in states else "pass",
                               "level": "process_planned", "method": JOINT_METHOD, "coverage": JOINT_COVERAGE}
        if mine:
            row["findings"] = [f["id"] for f in mine]
        if row["outcome"] == "unknown":
            row["uncertainty"] = "; ".join(f["measured"]["reason"] for f in mine if f["outcome"] == "unknown")
        outcomes.append(row)
    return Report(tuple(outcomes), tuple(findings))


def _edge_distance(op: AssemblyOperation, add: Callable[..., None]) -> None:
    """A hole-based joint's edge distance against its criterion: required for EDGE_DISTANCE_KINDS,
    judged wherever declared. The distance's lower end must meet the criterion's minimum, its upper
    end a `between` maximum."""
    edge = op.edge_distance
    consequence = "the hole may break out or the host split at its edge under load or installation"
    if edge is None:
        if op.kind in EDGE_DISTANCE_KINDS:
            add("assembly.edge_distance_unknown", "unknown", f"{op.id}: the {op.kind} records no edge distance", consequence,
                "record edge_distance: the sourced criterion (supplier or standard minimum) and the distance the design gives")
        return
    criterion = _mapping(edge.get("criterion"))
    bounds = _criterion_mm(criterion)
    distance = _mapping(edge.get("distance"))
    measured = _quantity_mm(distance.get("value")) if distance.get("status") == "known" else None
    if bounds is None:
        add("assembly.edge_distance_unknown", "unknown", f"{op.id}: the edge distance criterion is not a length bound",
            consequence, "state the criterion as >= or between, in mm, um or m, with its source")
        return
    if measured is None:
        reason = distance.get("reason") or "not a known length"
        add("assembly.edge_distance_unknown", "unknown",
            f"{op.id}: the edge distance is {distance.get('status', 'unknown')}: {reason}", consequence,
            "record the distance from the hole to the nearest edge, as designed or measured")
        return
    (minimum, maximum), (lower, upper) = bounds, measured
    if lower < minimum or (maximum is not None and upper > maximum):
        limit = f">= {minimum:g} mm" if maximum is None else f"between {minimum:g} and {maximum:g} mm"
        add("assembly.edge_distance", "fail", f"{op.id}: the edge distance is {lower:g}..{upper:g} mm, the criterion {limit}",
            consequence, "move the hole, enlarge the boss or flange, or choose a joint whose sourced criterion this "
            "distance meets", severity="critical", criterion=dict(criterion))


def _criterion_mm(criterion: Mapping[str, Any]) -> tuple[float, float | None] | None:
    """(minimum, maximum or None) in mm of an edge distance criterion, None when it is not a length bound."""
    units = criterion.get("units")
    scale = LENGTH_MM.get(units) if isinstance(units, str) else None
    value = criterion.get("value")
    if scale is None:
        return None
    if criterion.get("operator") == ">=" and _number(value):
        return value * scale, None
    if (criterion.get("operator") == "between" and isinstance(value, (list, tuple)) and len(value) == 2
            and all(_number(v) for v in value) and value[0] <= value[1]):
        return value[0] * scale, value[1] * scale
    return None


def _quantity_mm(value: Any) -> tuple[float, float] | None:
    """(lower, upper) in mm of a length quantity, None when it is not one."""
    if not isinstance(value, Mapping) or value.get("units") not in LENGTH_MM:
        return None
    scale = LENGTH_MM[value["units"]]
    if _number(value.get("value")):
        return value["value"] * scale, value["value"] * scale
    if _number(value.get("lower")) and _number(value.get("upper")):
        return value["lower"] * scale, value["upper"] * scale
    return None


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mapping(value: object) -> Mapping[str, Any]:
    """`value` when it is a mapping, else an empty one (an absent or malformed block reads as empty)."""
    return value if isinstance(value, Mapping) else {}


def _host_temperature(op: AssemblyOperation, record: Any, resolve_property: Callable[[Any, str], Any],
                      part_of: Callable[[str], str | None], temperature: Any, add: Callable[..., None]) -> None:
    """The host of an insert, heat-set or press fit against the use-case temperature. A qualified limit (see
    `_qualified`) judges it; an estimated or unsourced one can only fail it: within such a limit the joint stays
    unknown, never a pass."""
    if op.kind not in HOST_TEMPERATURE_KINDS or op.into is None:
        return
    pn = part_of(op.into)
    part = record.parts.get(pn) if pn else None
    profile_id = part.intent.material_profile if part is not None else None
    profile = record.materials.get(profile_id) if isinstance(profile_id, str) else None
    if profile is None:
        what = f"{op.into} resolves to no part" if part is None else f"{pn} has no resolved material profile"
        add("assembly.joint_temperature_unknown", "unknown", f"{op.id}: the host is unknown: {what}",
            f"a {op.kind} in an unidentified host may relax or release when the host softens",
            "name the receiving part and record its material profile with a sourced service temperature limit")
        return
    resolved = [(name, r) for name in TEMPERATURE_LIMITS for r in (resolve_property(profile, name),) if r.status == "resolved"]
    upper = _upper_deg_c(temperature)
    if not resolved or upper is None:
        add("assembly.joint_temperature_unknown", "unknown",
            f"{op.id}: {'the use-case temperature' if upper is None else profile.id + ' temperature limit'} is unknown",
            f"a {op.kind} in {profile.id} may relax or release when the host softens",
            "record the use-case temperature range and the host's sourced service temperature limit")
        return
    qualified = next(((name, r) for name, r in resolved if _qualified(r)), None)
    name, resolution = qualified or resolved[0]
    value = resolution.value
    limit_c = _to_deg_c(value.lower if value.value is None else value.value, value.units)
    if upper > limit_c:
        source = {key: item for key, item in vars(resolution.sources[0]).items() if item is not None}
        add("assembly.joint_temperature", "fail",
            f"{op.id}: the use-case temperature reaches {upper:g} deg_C, above {profile.id} {name} {limit_c:g} deg_C "
            f"({resolution.basis})", f"the {op.kind} host softens in service: the joint loses retention",
            "choose a host material qualified above the service temperature, or a joint that does not rely on the "
            "host's stiffness (a through bolt with a metal sleeve)", severity="critical",
            criterion={"property": "service_temperature", "operator": "<=", "value": limit_c, "units": "deg_C",
                       "source": source})
    elif qualified is None:
        add("assembly.joint_temperature_unknown", "unknown",
            f"{op.id}: {profile.id} {name} {limit_c:g} deg_C ({resolution.basis}) is not a qualified limit (an estimate, or "
            f"no {', '.join(sorted(QUALIFYING_SOURCES))} source): it cannot show the host holds up to {upper:g} deg_C",
            f"a {op.kind} in {profile.id} may relax or release when the host softens",
            "record the host's service temperature limit with a qualifying source (datasheet, supplier, standard, test "
            "or calibration)")


def _qualified(resolution: Any) -> bool:
    """Whether a resolved temperature limit may judge a joint: a sourced value (a QUALIFYING_SOURCES source),
    never an estimate."""
    return bool(resolution.basis != "estimate" and any(source.kind in QUALIFYING_SOURCES for source in resolution.sources))


def _sourced(fact: Mapping[str, Any] | None) -> bool:
    if not isinstance(fact, Mapping) or fact.get("status") != "known":
        return False
    return any(isinstance(s, Mapping) and s.get("kind") in QUALIFYING_SOURCES for s in fact.get("sources") or ())


def _to_deg_c(value: float, units: str) -> float:
    return value - 273.15 if units == "K" else value


def _upper_deg_c(fact: Any) -> float | None:
    if fact is None or getattr(fact, "status", None) != "known":
        return None
    quantity = fact.value
    upper = quantity.value if getattr(quantity, "upper", None) is None else quantity.upper
    return None if upper is None else _to_deg_c(float(upper), quantity.units)


def summary(operations: Operations) -> list[dict[str, Any]]:
    """The normalized semantics as JSON, for drawing recipes and guide provenance."""
    rows = []
    for op in operations.items:
        row: dict[str, Any] = {"id": op.id, "kind": op.kind, "part": op.part}
        for key in ("into", "interface", "acceptance", "verify_joint", "inspection"):
            if getattr(op, key) is not None:
                row[key] = getattr(op, key)
        if op.direction is not None:
            row["direction"] = list(op.direction)
        if op.path is not None:
            row["path"] = [list(point) for point in op.path]
        for key in ("features", "predecessors", "closes", "consumables"):
            if getattr(op, key):
                row[key] = list(getattr(op, key))
        if op.tools:
            row["tools"] = [{"name": t.name, "declared": t.declared, "reach_known": t.reach_known,
                             "envelope": None if t.envelope_artifact is None else t.envelope_artifact[0]} for t in op.tools]
        rows.append(row)
    return rows
