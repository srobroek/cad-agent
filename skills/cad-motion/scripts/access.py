# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Assembly and service access on built geometry: the `assembly.access` check of every recorded
assembly operation, beside the pure `assembly.order` check, as one required-check packet.

Usage: uv run --script access.py --hw hw (--version vN | --parts-dir DIR | --step STEP) [--attach REF=PARENT]...
       [--step-mm 1.0] [--min-step-mm 0.05] [--replace] [--out .cad-work/access.json]

Operations come from cad-record assembly_operations (top-level `operations`, or legacy
`drawing.operations`; both is refused). Order and closure are the pure check of that module. Access
is geometric, at the nominal joint pose, in the assembly state the declared sequence gives each
operation (the parts present then):
  - the part path: the operation's declared insertion `path` (assembly_operations: legs from its start
    through its via points to the installed pose, receiving occurrence's model frame, orientation
    kept; the start is taken as reachable), else the default, reported as such: a straight line along
    its `direction` (rotated from the receiving occurrence's model frame into the assembly frame) from
    beyond the separating plane of every present part to its installed pose; a disassemble operation
    moves it the other way. The receiving part and the parts interfaces.yaml declares fitted or
    inserted with the moving part are in contact by design and are not obstacles.
  - each tool: a `tool_access` entry whose envelope fact cites an artifact, a STEP solid of the tool
    and holder in the operated part's model frame at the engaged pose, moves along its `approach`
    (receiving occurrence's frame) to that pose, past every present part except the operated one.
A path is measured with cad-motion's pair evaluator at sampled positions, leg by leg. A translation moves
every point by the same distance, so a distance d_i at s_i and d_j at s_j prove the segment clear when
d_i + d_j > |s_j - s_i|; segments not proven are halved down to --min-step-mm. Exact-box gaps prove
most samples without the kernel; ambiguous ones are kernel-measured. A shared volume above pose's
interference threshold is a collision. A segment still unproven, a tool known by name only, an
envelope without a solid, a missing direction or geometry keeps the operation unknown, never a
pass. Not covered: curved motion between declared points, part or tool rotation and posture, hands,
forces, deflection, and any joint pose but the nominal. No path planner: the path is the declared one.

With --version the packet is published to hw/checks/<V>/assembly-access/ (bundle.checks): result.json
(schemas/manufacturing-check-result.schema.json) and evidence/access.json (every path and sample).
Exit 0 = every obligation passes, 1 = a failure or an unknown, 2 = usage or input error.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} not found")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
pose = _load("cad_motion_pose", HERE / "pose.py")
pair_policy = _load("cad_motion_pair_policy", HERE / "pair_policy.py")
pair_measure = _load("cad_motion_pair_measure", HERE / "pair_measure.py")
operations_module = _load("cad_record_assembly_operations", SKILLS / "cad-record" / "scripts" / "assembly_operations.py")
intent_module = _load("cad_record_manufacturing_intent", SKILLS / "cad-record" / "scripts" / "manufacturing_intent.py")
check_packet = _load("cad_record_check_packet", SKILLS / "cad-record" / "scripts" / "check_packet.py")
LAYOUT = pose.LAYOUT

PRODUCER = "cad-motion access.py"
SUBJECT = "assembly-access"
FAMILIES = ("assembly.order", "assembly.access", "intent.assembly_operations")
SEPARATION_MARGIN_MM = 1.0
ACCESS_METHOD = ("part paths (the declared insertion legs, else the default straight line along the direction) and tool "
                 "approaches at the nominal pose, against the parts present in the declared sequence, sampled with a "
                 "translation (1-Lipschitz) bound between samples (cad-motion access.py)")
ACCESS_COVERAGE = ("the part's declared insertion path, else the default straight line along its direction from the "
                   "separating plane, and each tool envelope's approach, in the declared assembly state at the nominal joint "
                   "pose; contact with the receiving part, fitted partners and the operated part is by design and not "
                   "checked; a declared path's start is taken as reachable; curved paths, tool posture, hands, forces and "
                   "other poses are not covered")
ROUTES = {"straight_default": "the default straight path along the direction", "declared_path": "the declared insertion path",
          "approach": "the declared tool approach"}
RULE_SOURCE = {"kind": "policy", "reference": "cad-motion access path clearance rule", "origin": "cad-agent",
               "document": "skills/cad-motion/scripts/access.py"}


class AccessError(ValueError):
    """Inputs that cannot be used: a usage error (exit 2)."""


# --------------------------------------------------------------------------- vectors


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _rotate(rigid: Any, vector: Sequence[float]) -> tuple[float, float, float]:
    rot = rigid.rot
    x, y, z = (sum(rot[i][k] * vector[k] for k in range(3)) for i in range(3))
    return (x, y, z)


def _finite(value: float) -> float | None:
    return round(value, 6) + 0.0 if math.isfinite(value) else None


def _shifted(rigid: Any, direction: Sequence[float], s: float) -> Any:
    """`rigid` moved back by s along `direction` (the pose s before arriving)."""
    return pose.Rigid.translation(tuple(direction), -s) @ rigid


# --------------------------------------------------------------------------- one path


@dataclass
class Travel:
    """One straight path: `moving` (a definition key) placed `world` at arrival, arriving along unit
    `direction` (assembly frame), past `obstacles` [(occurrence, definition key, world placement)].
    `leg_mm` is a declared leg's length; None travels from beyond the separating plane. `route` is a
    ROUTES key: what the path is."""

    label: str
    moving: str
    world: Any
    direction: tuple[float, float, float]
    obstacles: list[tuple[str, str, Any]]
    leg_mm: float | None = None
    route: str = "straight_default"
    travel_mm: float = 0.0
    status: str = "unknown"  # clear, collision or unknown
    samples: int = 0
    collisions: list[dict[str, Any]] = field(default_factory=list)
    unproven: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {"label": self.label, "route": self.route, "moving": self.moving,
                "direction": [round(v, 9) + 0.0 for v in self.direction],
                "travel_mm": round(self.travel_mm, 6), "obstacles": [o for o, _, _ in self.obstacles],
                "status": self.status, "samples": self.samples, "collisions": self.collisions,
                "unproven": self.unproven, "failures": self.failures}


def separation_travel(measurer: Any, path: Travel) -> float:
    """How far back along the direction the moving shape's box lies wholly behind every obstacle's box
    (a separating plane normal to the direction), plus SEPARATION_MARGIN_MM; 0 with no obstacle in reach."""
    if not path.obstacles:
        return 0.0
    ahead = max(_dot(path.world.apply(c), path.direction) for c in measurer.corners(path.moving))
    behind = min(_dot(world.apply(c), path.direction) for _, key, world in path.obstacles for c in measurer.corners(key))
    return max(0.0, ahead - behind) + SEPARATION_MARGIN_MM


def run_path(session: Any, measurer: Any, path: Travel, step_mm: float, min_step_mm: float) -> None:
    """Sample and refine `path`; sets its status (see the module docstring)."""
    path.travel_mm = separation_travel(measurer, path) if path.leg_mm is None else path.leg_mm
    if path.travel_mm == 0.0:
        path.status = "clear"
        return
    count = max(1, math.ceil(path.travel_mm / step_mm))
    positions = sorted({path.travel_mm * i / count for i in range(count + 1)})
    lower: dict[tuple[int, float], float] = {}  # (obstacle index, s) -> distance lower bound
    pending = [(index, s) for index in range(len(path.obstacles)) for s in positions]
    open_segments: dict[int, list[tuple[float, float]]] = {
        index: list(zip(positions, positions[1:])) or [(0.0, 0.0)] for index in range(len(path.obstacles))}
    while pending:
        requests = []
        for index, s in pending:
            occurrence, key, world = path.obstacles[index]
            placement = world.inverse() @ _shifted(path.world, path.direction, s)
            requests.append(pair_measure.Request(key, path.moving, placement, group=(path.label, occurrence),
                                                 sample_id=f"{path.label}@{s:.6f}"))
        outcomes = session.measure(requests)
        path.samples += len(requests)
        for (index, s), outcome in zip(pending, outcomes):
            occurrence = path.obstacles[index][0]
            if isinstance(outcome, pair_measure.Measured):
                if outcome.overlap_mm3 > pose.INTERFERENCE_MM3:
                    path.collisions.append({"obstacle": occurrence, "at_mm": round(s, 6),
                                            "overlap_mm3": round(outcome.overlap_mm3, 6)})
                lower[(index, s)] = outcome.distance_mm
            elif isinstance(outcome, (pair_measure.Cleared, pair_measure.Pruned)):
                lower[(index, s)] = outcome.lower_mm
            else:
                path.failures.append(f"{occurrence} at {s:.6f} mm: "
                                     f"{outcome.message if outcome is not None else 'not measured before the deadline'}")
                lower[(index, s)] = -math.inf
        if path.collisions:
            path.status = "collision"
            return
        pending = []
        for index, segments in open_segments.items():
            still = []
            for start, end in segments:
                width = end - start
                if lower[(index, start)] + lower[(index, end)] > width:
                    continue
                if width / 2 < min_step_mm or not math.isfinite(lower[(index, start)] + lower[(index, end)]):
                    path.unproven.append({"obstacle": path.obstacles[index][0], "from_mm": round(start, 6),
                                          "to_mm": round(end, 6),
                                          "lower_mm": [_finite(lower[(index, start)]), _finite(lower[(index, end)])]})
                    continue
                middle = (start + end) / 2
                pending.append((index, middle))
                still.extend([(start, middle), (middle, end)])
            open_segments[index] = still
    path.status = "unknown" if path.unproven or path.failures else "clear"


# --------------------------------------------------------------------------- operations


@dataclass
class OperationAccess:
    operation: Any
    paths: list[Travel] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if any(p.status == "collision" for p in self.paths):
            return "fail"
        if self.unknown or any(p.status != "clear" for p in self.paths) or not self.paths:
            return "unknown"
        return "pass"


def plan_paths(requests: Iterable[Any], chain: Any, world: Mapping[str, Any], definitions: Mapping[str, Any],
               fitted: set[tuple[str, str]], tools: Mapping[tuple[str, int], tuple[str, Any]]) -> list[OperationAccess]:
    """The paths of each operation's AccessRequest; `tools` maps (operation id, tool index) to the
    envelope definition key and its shape, for the tools whose solid was read."""
    plans = []
    for request in requests:
        op = request.operation
        entry = OperationAccess(op, unknown=list(request.unknown))
        frame = request.frame
        if frame not in world:
            entry.unknown.append(f"{op.id}: {frame} has no geometry or placement")
            plans.append(entry)
            continue
        rotation = world[frame]

        def obstacles(names: Iterable[str], moving: str | None) -> list[tuple[str, str, Any]]:
            found = []
            for name in sorted(names):
                if moving is not None and tuple(sorted((name, moving))) in fitted:
                    continue
                if name not in world or chain.pn(name) not in definitions:
                    entry.unknown.append(f"{op.id}: obstacle {name} has no geometry")
                    continue
                found.append((name, chain.pn(name), world[name]))
            return found

        if request.moving is not None and request.direction is not None:
            moving = request.moving
            if moving not in world or chain.pn(moving) not in definitions:
                entry.unknown.append(f"{op.id}: {moving} has no geometry")
            elif request.path is not None:
                entry.paths.extend(declared_legs(op.id, chain.pn(moving), world[moving], rotation, request.path,
                                                 obstacles(request.path_obstacles, moving)))
            else:
                direction = _rotate(rotation, request.direction)
                if request.removal:
                    direction = (-direction[0], -direction[1], -direction[2])
                entry.paths.append(Travel(f"{op.id}/part", chain.pn(moving), world[moving], direction,
                                         obstacles(request.path_obstacles, moving)))
        operated = request.part
        for number, tool in enumerate(request.tools):
            if not tool.declared or tool.approach is None or (op.id, number) not in tools:
                continue  # its reason is in request.unknown, or the envelope solid could not be read
            if operated not in world:
                entry.unknown.append(f"{op.id}: the operated part {op.part} has no placement for tool {tool.name!r}")
                continue
            key, _shape = tools[(op.id, number)]
            entry.paths.append(Travel(f"{op.id}/tool/{number}", key, world[operated], _rotate(rotation, tool.approach),
                                     obstacles(request.tool_obstacles, None), route="approach"))
        plans.append(entry)
    return plans


def declared_legs(ident: str, moving: str, installed: Any, frame: Any, points: Sequence[Sequence[float]],
                  obstacles: list[tuple[str, str, Any]]) -> list[Travel]:
    """The straight legs of a declared insertion path: `points` (start, then via) are the moving part's
    offsets from its `installed` placement in the receiving occurrence's frame (`frame`'s rotation);
    the last leg ends at the installed pose. Each leg is a Travel of its own length, arriving at its end."""
    offsets = [_rotate(frame, p) for p in points] + [(0.0, 0.0, 0.0)]
    legs = []
    for number, (start, end) in enumerate(zip(offsets, offsets[1:]), start=1):
        length = math.dist(start, end)
        dx, dy, dz = ((e - s) / length for s, e in zip(start, end))
        arrival = pose.Rigid(pose.Rigid.identity().rot, end) @ installed
        legs.append(Travel(f"{ident}/part/leg{number}", moving, arrival, (dx, dy, dz), list(obstacles),
                           leg_mm=length, route="declared_path"))
    return legs


def read_tools(requests: Iterable[Any], record_hw: Path, hw: Path) -> tuple[dict[tuple[str, int], tuple[str, Any]],
                                                                          dict[tuple[str, int], str], dict[str, str]]:
    """(envelope key and shape per (operation id, tool index), the reason a cited solid could not be used,
    the sha256 of each envelope artifact read). The artifact resolves in the record snapshot, else hw/."""
    shapes: dict[tuple[str, int], tuple[str, Any]] = {}
    problems: dict[tuple[str, int], str] = {}
    digests: dict[str, str] = {}
    for request in requests:
        for number, tool in enumerate(request.tools):
            cited = tool.envelope_artifact if tool.declared else None
            if cited is None:
                continue
            artifact, sha = cited
            candidates = [root / artifact for root in (record_hw, hw)]
            target = next((p for p in candidates if p.is_file() and not p.is_symlink()), None)
            if target is None:
                problems[(request.operation.id, number)] = f"tool {tool.name!r}: envelope {artifact} is missing"
                continue
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                problems[(request.operation.id, number)] = f"tool {tool.name!r}: {artifact} does not match its sha256"
                continue
            key = f"tool:{request.operation.id}:{number}"
            shapes[(request.operation.id, number)] = (key, pose.read_shape(target))
            digests[artifact] = sha
    return shapes, problems, digests


# --------------------------------------------------------------------------- findings and the packet


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return out if out and out[0].isalpha() else f"op_{out}"


def access_findings(entry: OperationAccess) -> list[dict[str, Any]]:
    op = entry.operation
    obligation = f"assembly.access@op:{op.id}"
    base = {"subject": {"assembly_operation": op.id}, "obligation": obligation,
            "method": {"name": ACCESS_METHOD, "tool": PRODUCER},
            "recheck": {"families": ["assembly.access"], "scope": [{"assembly_operation": op.id}], "earliest_stage": "detail"}}
    criterion = {"property": "path_clearance", "operator": ">=", "value": 0, "units": "mm", "source": dict(RULE_SOURCE)}
    findings: list[dict[str, Any]] = []
    for path in entry.paths:
        if path.status != "collision":
            continue
        tool = "/tool/" in path.label
        hits = sorted({c["obstacle"] for c in path.collisions})
        worst = max(path.collisions, key=lambda c: c["overlap_mm3"])
        what = f"tool {op.tools[int(path.label.rsplit('/', 1)[1])].name!r}" if tool else f"{op.part}"
        findings.append({
            "id": f"{_slug(op.id)}_{'tool' if tool else 'part'}_path_{len(findings) + 1}",
            "code": "assembly.tool_blocked" if tool else "assembly.part_path_blocked", **base, "severity": "critical",
            "outcome": "fail", "measured": {"value": worst["overlap_mm3"], "units": "mm3"}, "criterion": criterion,
            "uncertainty": f"{ROUTES[path.route]} at the nominal pose; another path or posture is not assessed",
            "consequence": (f"{what} collides with {', '.join(hits)} on its way in "
                            f"({worst['at_mm']} mm before the end of {path.label}, {ROUTES[path.route]}): "
                            f"the {op.kind} cannot be done as declared"),
            "remedy": (f"clear the {'tool approach' if tool else 'insertion path'} past {', '.join(hits)}, or change the "
                       "sequence so they are installed afterwards"),
            "alternatives": [{"text": "reorder the operations so the obstacle is installed later",
                              "tradeoffs": "recheck that the obstacle itself stays installable"},
                             {"text": "a tool with a slimmer or longer envelope, or another approach",
                              "tradeoffs": "a sourced envelope and a recorded approach are needed"},
                             {"text": "move or relieve the obstacle geometry",
                              "tradeoffs": "changes the obstacle part and its own checks"}],
        })
    reasons = list(entry.unknown) + [f"{p.label}: {len(p.unproven)} segment(s) not proven clear"
                                     for p in entry.paths if p.unproven] \
        + [f"{p.label}: {failure}" for p in entry.paths for failure in p.failures]
    if not entry.paths and not entry.unknown:
        reasons.append(f"{op.id} declares no part path or tool envelope: its access is not established")
    if entry.outcome == "unknown":
        findings.append({
            "id": f"{_slug(op.id)}_access_unknown", "code": "assembly.access_unknown", **base, "severity": "major",
            "outcome": "unknown", "measured": {"status": "unknown", "reason": "; ".join(reasons)},
            "criterion": criterion, "uncertainty": "; ".join(reasons),
            "consequence": f"whether the part and tools of {op.id} reach their positions is not established",
            "remedy": "record the direction, and each tool's approach and sourced envelope solid, then rerun",
        })
    return findings


def access_outcome(entry: OperationAccess, findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {"obligation": f"assembly.access@op:{entry.operation.id}", "outcome": entry.outcome,
                           "level": "geometrically_manufacturable", "method": ACCESS_METHOD, "coverage": ACCESS_COVERAGE}
    mine = [f["id"] for f in findings if f.get("obligation") == row["obligation"]]
    if mine:
        row["findings"] = mine
    if entry.outcome == "unknown":
        row["uncertainty"] = next(f["uncertainty"] for f in findings if f["code"] == "assembly.access_unknown")
    return row


# --------------------------------------------------------------------------- run


def assess(args: argparse.Namespace, version: str | None, record_hw: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the packet, the evidence detail) of one run."""
    import yaml

    assembly_path = record_hw / "assembly.yaml"
    try:
        assembly_data = yaml.safe_load(assembly_path.read_text(encoding="utf-8")) or {}
        operations = operations_module.normalize(assembly_data)
    except (OSError, yaml.YAMLError) as error:
        raise AccessError(f"cannot read {assembly_path}: {error}") from error
    except operations_module.OperationsError as error:
        raise AccessError(f"{assembly_path}: {error} ({error.code})") from error
    record = intent_module.load_record(record_hw)
    assembly, chain, definitions = pose.load_inputs(args)
    order = operations_module.check_order(operations, chain.inventory)
    outcomes, findings = list(order.outcomes), list(order.findings)
    detail: dict[str, Any] = {"operations": operations_module.summary(operations), "paths": {}, "nominal": {}}
    dependencies = {"assembly_yaml": hashlib.sha256(assembly_path.read_bytes()).hexdigest()}
    interfaces_path = record_hw / "interfaces.yaml"
    if interfaces_path.is_file():
        dependencies["interfaces_yaml"] = hashlib.sha256(interfaces_path.read_bytes()).hexdigest()
    if version is not None:
        dependencies["version_manifest"] = pose.input_sha256(args.version_dir)
    if operations.unknown is None:
        requests = operations_module.access_plan(operations, chain.inventory)
        tools, tool_problems, digests = read_tools(requests, record_hw, args.hw)
        for index, (artifact, sha) in enumerate(sorted(digests.items())):
            dependencies[f"tool_envelope_{index + 1}"] = sha
        try:
            interfaces = pair_policy.read_interfaces(record_hw)
            fitted = pair_policy.fitted_pairs(
                pair_policy.declared_press_fits(record_hw, interfaces, assembly),
                pair_policy.declared_running_fits(record_hw, interfaces, assembly),
                pair_policy.declared_locating_fits(record_hw, interfaces, assembly),
                pair_policy.declared_heat_sets(record_hw, interfaces, assembly))
        except pair_policy.PolicyError as error:
            raise AccessError(str(error)) from error
        values = chain.complete({})
        world = chain.transforms(values)
        detail["nominal"] = {jid: value for jid, value in values.items()}
        plans = plan_paths(requests, chain, world, definitions, fitted, tools)
        for entry in plans:
            entry.unknown.extend(reason for (op_id, _), reason in sorted(tool_problems.items())
                                 if op_id == entry.operation.id)
        shapes = {pn: part.shape for pn, part in definitions.items()}
        parts = dict(definitions)
        for key, shape in tools.values():
            shapes[key] = shape
            parts[key] = pose.Part(key, key, shape)
        measurer = pair_measure.PairMeasurer(shapes, geometry_ids=pose.geometry_ids(parts),
                                             rule=pair_measure.RuleVersion.current(), threads=False)
        with pose.stdout_to_stderr(), measurer.session() as session:
            for entry in plans:
                for path in entry.paths:
                    run_path(session, measurer, path, args.step_mm, args.min_step_mm)
        for entry in plans:
            mine = access_findings(entry)
            findings.extend(mine)
            outcomes.append(access_outcome(entry, mine))
            detail["paths"][entry.operation.id] = [p.report() for p in entry.paths]
    detail["limits"] = [ACCESS_COVERAGE, "the order check covers the declared sequence only"]
    expected = [o for o in intent_module.required_checks(record) if o.family in FAMILIES]
    return check_packet.build(record, expected, PRODUCER, version, outcomes, findings, dependencies), detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assembly order and geometric access of the recorded operations")
    pose.add_geometry_arguments(parser)
    parser.add_argument("--step-mm", type=float, default=1.0, help="initial sample spacing along each path (mm)")
    parser.add_argument("--min-step-mm", type=float, default=0.05, help="smallest refined segment (mm)")
    parser.add_argument("--replace", action="store_true", help="retain and replace a published packet with another recipe")
    parser.add_argument("--out", type=Path, help="scratch JSON path, only under .cad-work")
    args = parser.parse_args(argv)
    if not (args.step_mm > 0 and args.min_step_mm > 0 and math.isfinite(args.step_mm)):
        parser.error("--step-mm and --min-step-mm must be positive")
    try:
        version, record_hw = None, args.hw
        if args.version:
            version, version_dir, record_hw = LAYOUT.built_version(args.hw, args.version)
            args.version, args.version_dir, args.record_hw = version, version_dir, record_hw
        result, detail = assess(args, version, record_hw)
        recipe = {"options": {"step_mm": args.step_mm, "min_step_mm": args.min_step_mm, "attach": list(args.attach)},
                  "dependencies": result["dependencies"], "rule_sha256": pair_measure.RuleVersion.current().digest.hex()}
        output, status = check_packet.emit(LAYOUT, args.hw, version, SUBJECT, PRODUCER, result, detail, "access.json",
                                           recipe, out=args.out, replace=args.replace,
                                           record_version=intent_module.RECORD_VERSION)
        json.dump(output, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if status != "pass":
            print(f"access: {status}", file=sys.stderr)
        return 0 if status == "pass" else 1
    except (AccessError, check_packet.PacketError, pose.MotionError, LAYOUT.LayoutError, intent_module.IntentError) as error:
        print(f"access: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
