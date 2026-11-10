# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["cadquery-ocp-novtk==8.0.1.1.0", "jsonschema>=4.23", "pyyaml>=6.0"]
# ///
"""Mechanism play: the `motion.play` check of every interface and part the record obliges, under its
declared play and loads, as one required-check packet.

Usage: uv run --script play.py --hw hw (--version vN | --parts-dir DIR | --step STEP) [--attach REF=PARENT]...
       [--steps 9] [--replace] [--out .cad-work/play.json]

Obligations are cad-record required_checks' motion.play family: motion.play@IF-### for a critical
interface that declares play or rotates or slides, and motion.play@PN for a made part that rotates or
slides, which combines the interfaces of that part that declare play or that motion.

Joint. An interface's play belongs to the joint that directly connects its `from` and `to` parts;
none, several, or a --attach fixture keeps it unknown.

Play (interfaces.yaml functional.play). Each component is the total play about the nominal, centred
pose, an interval's upper bound governing; half of it, plus the declared compliance under load, is the
extreme offset to either side:
  - axial: translation along the joint axis;
  - radial: translation across it, along +-u and +-v (perpendicular to the axis);
  - angular: tilt of the axis about +-u and +-v through the joint origin;
  - backlash: rotation about the axis (a revolute joint, an angle) or translation along it (a prismatic
    joint, a length).
The offset sits in the parent's frame at the joint (child motion = parent motion x offset x joint
motion), so the child and everything it carries move. The components an interface's function needs
are relevant: rotating needs axial, radial and angular; sliding and locating need radial and angular;
a joint driven through a coupling also needs backlash. A relevant component the record does not declare,
or declares unknown, keeps the obligation unknown: undeclared play is never zero. Not applicable is a
declaration of no play in that component. Units the joint cannot use (a backlash length on a revolute
joint) are unknown.

Loads. An interface that carries loads (functional.loads, a known contact load, or a spring function)
deflects under them: its compliance (play.compliance) must be declared for each relevant axial, radial
and angular component, or the loaded extremes stay unknown.

Scenario cases. Each case is a corner of the declared play box: every component at one of its extremes
(at most 2 x 4 x 4 x 2 = 64 cases), the nominal, centred case reported apart. Each runs through
cad-motion's sweep evaluator (sweep.plan, pair_measure): at the frames of the interface's
play.scenario_cases (hw/motion/cases/<ID>/plan.yaml), else at the per-joint sweep (--steps samples per
independent joint, the others at nominal); over the pairs with exactly one part in the moving subtree,
joint-connected pairs excepted as in sweep.py; the overlap pair_policy declares is allowed, and the
clearance requirements of interfaces.yaml apply.

Outcome per obligation: fail when any case, the nominal one included, collides or breaks a clearance
requirement; unknown when any reason above or a kernel failure leaves it open; else pass. Not covered:
motion between samples, intermediate offsets and other directions, loads beyond the declared
compliance, contact forces, friction and dynamics.

With --version the packet is published to hw/checks/<V>/motion-play/ (bundle.checks): result.json
(schemas/manufacturing-check-result.schema.json) and evidence/play.json (every case and sample).
Exit 0 = every obligation passes, 1 = a failure or an unknown, 2 = usage or input error.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import itertools
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any


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
sweep = _load("cad_motion_sweep", HERE / "sweep.py")
plan_module = _load("cad_motion_plan", HERE / "plan.py")
pair_measure = _load("cad_motion_pair_measure", HERE / "pair_measure.py")
check_packet = _load("cad_record_check_packet", SKILLS / "cad-record" / "scripts" / "check_packet.py")
intent_module = _load("cad_record_manufacturing_intent", SKILLS / "cad-record" / "scripts" / "manufacturing_intent.py")
LAYOUT = pose.LAYOUT

PRODUCER = "cad-motion play.py"
SUBJECT = "motion-play"
FAMILY = "motion.play"
COMPONENTS = ("axial", "radial", "angular", "backlash")
COMPLIANT = ("axial", "radial", "angular")  # the components play.compliance may add to
RELEVANT = {"rotating": ("axial", "radial", "angular"), "sliding": ("radial", "angular"), "locating": ("radial", "angular")}
MOTION_FUNCTIONS = frozenset(("rotating", "sliding"))
MAX_LISTED = 20
PLAY_METHOD = ("declared play extremes: each corner of the declared play box (half the total play plus the declared "
               "compliance, axial along the joint axis, radial and angular along +-u and +-v across it, backlash along the "
               "joint coordinate) applied at the interface's joint, through cad-motion's sweep pair evaluator at the "
               "scenario poses (cad-motion play.py)")
PLAY_COVERAGE = ("the corners of the declared play box and the nominal, centred case at the sampled poses of the scenario "
                 "cases, else the per-joint sweep with the other joints at nominal; motion between samples, intermediate "
                 "offsets and other directions, loads beyond the declared compliance, contact forces, friction and "
                 "dynamics are not covered")
RULE_SOURCE = {"kind": "policy", "reference": "cad-motion play extremes rule", "origin": "cad-agent",
               "document": "skills/cad-motion/scripts/play.py"}


# --------------------------------------------------------------------------- declared play


@dataclass
class Bounds:
    """An interface's extreme offsets: `halves` per component (in its `units`, mm or deg) that the cases
    use, `notes` what the record declares of each component, `unknown` why the extremes are not the
    whole declared play."""

    halves: dict[str, float] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)


def interface_functions(interface: Any) -> set[str] | None:
    """The interface's declared functions, None when they are unknown."""
    functions = interface.functional.functions
    return None if intent_module.is_fact(functions) else {entry.function for entry in functions}


def carries_loads(functional: Any, functions: set[str]) -> str | None:
    """What loads the interface (its use-case loads, a known contact load, a spring function), or None."""
    raw = functional.raw
    loads = list(raw.get("loads") or ())
    if loads:
        return f"loads {', '.join(loads)}"
    contact = raw.get("contact") if isinstance(raw.get("contact"), Mapping) else {}
    load = contact.get("load")
    if isinstance(load, Mapping) and load.get("status") == "known":
        return "its contact load"
    return "its spring function" if "spring" in functions else None


def _bound(data: Any, units: str) -> tuple[float | None, str]:
    """(the upper bound of a play or compliance fact in `units`, or None, what the record declares)."""
    if data is None:
        return None, "undeclared"
    try:
        fact = intent_module.fact(data)
    except intent_module.IntentError as error:
        return None, f"invalid: {error}"
    if fact.not_applicable:
        return 0.0, f"not applicable: {fact.reason}"
    if not fact.known:
        return None, f"unknown: {fact.reason}"
    try:
        upper = fact.value.to(units).bounds[1]
    except (intent_module.IntentError, AttributeError) as error:
        return None, f"not a {units} quantity ({error})"
    if upper < 0:
        return None, f"negative ({upper:g} {units})"
    return upper, "declared"


def play_bounds(if_id: str, interface: Any, joint: Any, coupled: bool) -> Bounds:
    """The extreme offsets of `interface` at `joint` (see the module): its relevant components declared
    or unknown, its other declared ones applied, its compliance added where it carries loads."""
    bounds = Bounds()
    functional = interface.functional
    raw = functional.raw
    declared_play = raw.get("play")
    play: Mapping[str, Any] = declared_play if isinstance(declared_play, Mapping) else {}
    declared_compliance = play.get("compliance")
    compliance: Mapping[str, Any] = declared_compliance if isinstance(declared_compliance, Mapping) else {}
    functions = interface_functions(interface)
    if functions is None:
        bounds.unknown.append(f"{if_id}: its functions are unknown, so which play components matter is unknown")
        functions = set()
    relevant = {name for function in functions for name in RELEVANT.get(function, ())}
    if coupled:
        relevant.add("backlash")
    loaded = carries_loads(functional, functions)
    for name in COMPONENTS:
        if name == "backlash":
            units = {"revolute": "deg", "prismatic": "mm"}.get(joint.type)
            if units is None:
                if "backlash" in play:
                    bounds.unknown.append(f"{if_id}: backlash on fixed joint {joint.id}, which has no coordinate to lash")
                    bounds.notes[name] = "not usable on a fixed joint"
                continue
        else:
            units = "deg" if name == "angular" else "mm"
        bounds.units[name] = units
        upper, note = _bound(play.get(name), units)
        bounds.notes[name] = note
        if upper is None:
            if name in relevant or name in play:
                bounds.unknown.append(f"{if_id}: {name} play is {note}" + (
                    "; undeclared play is unknown, never zero" if note == "undeclared" else ""))
            continue
        half = upper / 2.0
        if name in COMPLIANT and (loaded or name in compliance):
            extra, extra_note = _bound(compliance.get(name), units)
            if extra is not None:
                half += extra
                bounds.notes[name] += f"; compliance {extra_note} ({extra:g} {units})"
            elif half > 0 or name in relevant:
                cause = f"carries {loaded}" if loaded else "declares a compliance"
                bounds.unknown.append(f"{if_id} {cause}: its {name} compliance under them is {extra_note}, so the "
                                      "loaded extremes are unknown")
        if half > 0:
            bounds.halves[name] = half
    return bounds


# --------------------------------------------------------------------------- scenario cases


@dataclass(frozen=True)
class Case:
    label: str
    offset: Any  # pose.Rigid, in the parent's frame at the joint


def _perpendicular(axis: Sequence[float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """(u, v): unit vectors across `axis`, right-handed with it."""
    e = min(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), key=lambda c: abs(sum(a * b for a, b in zip(axis, c))))
    u = (axis[1] * e[2] - axis[2] * e[1], axis[2] * e[0] - axis[0] * e[2], axis[0] * e[1] - axis[1] * e[0])
    length = math.hypot(*u)
    u = (u[0] / length, u[1] / length, u[2] / length)
    v = (axis[1] * u[2] - axis[2] * u[1], axis[2] * u[0] - axis[0] * u[2], axis[0] * u[1] - axis[1] * u[0])
    return u, v


def corner_cases(joint: Any, halves: Mapping[str, float]) -> list[Case]:
    """Every corner of the play box `halves` at `joint`: each component at one of its extremes."""
    axis, origin = joint.axis, joint.origin
    u, v = _perpendicular(axis)
    across = (("u", u), ("v", v))

    def shift(vector: Sequence[float], distance: float) -> Any:
        return pose.Rigid.translation(tuple(vector), distance)

    options: list[list[tuple[str, Any]]] = []
    if "axial" in halves:
        options.append([(f"axial{sign}", shift(axis, s * halves["axial"])) for sign, s in (("+", 1), ("-", -1))])
    if "radial" in halves:
        options.append([(f"radial{sign}{n}", shift(w, s * halves["radial"]))
                        for n, w in across for sign, s in (("+", 1), ("-", -1))])
    if "angular" in halves:
        options.append([(f"tilt{sign}{n}", pose.Rigid.rotation(w, s * halves["angular"], origin))
                        for n, w in across for sign, s in (("+", 1), ("-", -1))])
    if "backlash" in halves:
        lash = halves["backlash"]
        options.append([(f"backlash{sign}", pose.Rigid.rotation(axis, s * lash, origin) if joint.type == "revolute"
                         else shift(axis, s * lash)) for sign, s in (("+", 1), ("-", -1))])
    cases = []
    for combo in itertools.product(*options) if options else ():
        offset = pose.Rigid.identity()
        for _, rigid in combo:
            offset = offset @ rigid
        cases.append(Case(" ".join(label for label, _ in combo), offset))
    return cases


if TYPE_CHECKING:  # mypy reads Joint from pose.py beside this file; the run subclasses the loaded module's class
    from pose import Joint as _Joint
else:
    _Joint = pose.Joint


@dataclass(frozen=True)
class OffsetJoint(_Joint):
    """A joint whose child sits at `offset`, a rigid motion in the parent's frame, before its own motion."""

    offset: Any = None

    def motion(self, value: float) -> Any:
        return self.offset @ super().motion(value)


def offset_chain(chain: Any, joint: Any, offset: Any) -> Any:
    """`chain` with `joint`'s child placed at `offset` (its whole subtree moves with it)."""
    moved = OffsetJoint(**{f.name: getattr(joint, f.name) for f in dataclasses.fields(pose.Joint)}, offset=offset)
    return dataclasses.replace(chain, joints=tuple(moved if j.id == joint.id else j for j in chain.joints))


def subtree(chain: Any, root: str) -> set[str]:
    """`root` and every occurrence a joint path below it carries."""
    children: dict[str, list[str]] = {}
    for joint in chain.joints:
        children.setdefault(joint.parent, []).append(joint.child)
    found, stack = set(), [root]
    while stack:
        node = stack.pop()
        if node not in found:
            found.add(node)
            stack.extend(children.get(node, ()))
    return found


def joint_of(if_id: str, interface: Any, chain: Any) -> tuple[Any | None, str | None]:
    """(the joint that directly connects the interface's two parts, None and why not)."""
    raw = interface.raw
    try:
        ends = {chain.resolve(raw.get("from") or "", f"{if_id} from: "), chain.resolve(raw.get("to") or "", f"{if_id} to: ")}
    except pose.MotionError as error:
        return None, f"{if_id}: its parts are not occurrences of the posed assembly ({error})"
    joints = [j for j in chain.joints if {j.parent, j.child} == ends]
    if len(joints) != 1:
        return None, (f"{if_id}: {'no joint' if not joints else 'several joints'} connect {raw.get('from')} and "
                      f"{raw.get('to')} directly; record the joint whose clearance the play is")
    if joints[0].id.startswith("attach:"):
        return None, f"{if_id}: {raw.get('from')} and {raw.get('to')} are joined only by --attach, which has no axis"
    return joints[0], None


# --------------------------------------------------------------------------- evaluation


@dataclass
class CaseResult:
    """One case over the scenario samples: collisions (undeclared overlap), clearance requirements
    broken, kernel failures, the closest exact reading, and the samples a joint limit skipped."""

    label: str
    collisions: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    closest: dict[str, Any] | None = None
    limited: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.collisions or self.violations)

    def report(self) -> dict[str, Any]:
        return {"label": self.label, "status": "fail" if self.failed else "unknown" if self.failures else "clear",
                "collisions": self.collisions[:MAX_LISTED], "collision_count": len(self.collisions),
                "violations": self.violations[:MAX_LISTED], "failures": self.failures[:MAX_LISTED],
                "closest": self.closest, "samples_skipped_by_limits": self.limited}


@dataclass
class Evaluator:
    """The sweep evaluator of one run: the measuring session, the declared pair policy and the samples."""

    session: Any
    allowed: set[tuple[str, str]]
    fitted: set[tuple[str, str]]
    requirements: dict[tuple[str, str], dict[str, Any]]
    margins: Any
    checked: list[tuple[str, str]]

    def run(self, chain: Any, label: str, samples: list[Any], moving: set[str]) -> CaseResult:
        crossing = [p for p in self.checked if (p[0] in moving) != (p[1] in moving)]
        required = {p: r for p, r in self.requirements.items() if (p[0] in moving) != (p[1] in moving)}
        sweep_plan = sweep.plan(chain, samples, crossing, self.fitted, required, 0.0, self.margins)
        outcomes = self.session.measure(sweep_plan.requests)
        result = CaseResult(label)
        for planned in sweep_plan.samples:
            if planned.transforms is None:
                result.limited += 1
                continue
            sample = planned.sample
            for k, index in enumerate(sweep_plan.selected):
                outcome = outcomes[planned.start + k]
                pair = sweep_plan.measured[index]
                refs = [chain.ref(pair[0]), chain.ref(pair[1])]
                where = {"sample": sample.index, "source": sample.source, "pose": sweep.rounded(sample.values), "pair": refs}
                if outcome is None or isinstance(outcome, pair_measure.Failure):
                    result.failures.append(f"{' / '.join(refs)} at sample {sample.index}: "
                                           f"{outcome.message if outcome is not None else 'not measured'}")
                    continue
                distance, volume = pair_measure.reading(outcome)
                if pair in sweep_plan.checked and tuple(sorted(pair)) not in self.allowed and volume > pose.INTERFERENCE_MM3:
                    result.collisions.append({**where, "volume_mm3": round(volume, 4)})
                if pair in required and distance < required[pair]["required_mm"]:
                    result.violations.append({**where, "clearance_mm": round(distance, 4),
                                              "required_mm": required[pair]["required_mm"],
                                              "requirement": required[pair]["source"]})
                if isinstance(outcome, pair_measure.Measured) and (
                        result.closest is None or distance < result.closest["distance_mm"]):
                    result.closest = {**where, "distance_mm": round(distance, 4)}
        return result


@dataclass
class Assessment:
    """One interface under its play: the joint, what moves, the declared bounds, the cases and why it stays open."""

    interface: str
    joint: Any | None = None
    moving: list[str] = field(default_factory=list)
    bounds: Bounds | None = None
    samples: dict[str, Any] = field(default_factory=dict)
    nominal: CaseResult | None = None
    cases: list[CaseResult] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        results = [r for r in (self.nominal, *self.cases) if r is not None]
        if any(r.failed for r in results):
            return "fail"
        if self.unknown or self.nominal is None or any(r.failures for r in results):
            return "unknown"
        return "pass"

    def reasons(self) -> list[str]:
        failures = [f"{r.label}: {failure}" for r in (self.nominal, *self.cases) if r is not None for failure in r.failures]
        return list(self.unknown) + failures[:MAX_LISTED]

    def report(self) -> dict[str, Any]:
        return {"interface": self.interface, "joint": None if self.joint is None else self.joint.id,
                "moving": self.moving, "outcome": self.outcome,
                "components": {} if self.bounds is None else {
                    name: {"declared": note, "half_extreme": self.bounds.halves.get(name, 0.0),
                           "units": self.bounds.units.get(name)}
                    for name, note in self.bounds.notes.items()},
                "samples": self.samples, "nominal": None if self.nominal is None else self.nominal.report(),
                "cases": [case.report() for case in self.cases], "unknown": self.reasons()}


def scenario_samples(chain: Any, hw: Path, cases: Sequence[str], steps: int) -> tuple[list[Any], dict[str, Any], list[str]]:
    """(the samples, their description, why a scenario case could not be used): the frames of the
    declared scenario cases, else the per-joint sweep; the nominal pose when nothing moves."""
    problems: list[str] = []
    samples: list[Any] = []
    seen: set[tuple[float, ...]] = set()
    for case in cases:
        try:
            frames = plan_module.expand(chain, plan_module.load(chain, hw, LAYOUT, case))
        except (plan_module.PlanError, pose.MotionError) as error:
            problems.append(f"scenario case {case}: {error}")
            continue
        for frame in frames:
            key = sweep.sample_key(chain, frame.settings)
            if key not in seen:
                seen.add(key)
                samples.append(sweep.Sample(len(samples), f"case:{case}#{frame.index}", dict(frame.settings), dict(frame.values)))
    if cases:
        return samples, {"source": "scenario_cases", "cases": list(cases), "count": len(samples)}, problems
    samples = sweep.generate_poses(chain, steps, 0)
    if not samples:
        samples = [sweep.Sample(0, "nominal", {}, chain.complete({}))]
    return samples, {"source": "per_joint_sweep", "steps": steps, "count": len(samples)}, problems


def assess_interface(if_id: str, interface: Any, chain: Any, hw: Path, steps: int, evaluator: Evaluator) -> Assessment:
    entry = Assessment(if_id)
    joint, problem = joint_of(if_id, interface, chain)
    if joint is None:
        entry.unknown.append(problem or f"{if_id}: no joint")
        return entry
    entry.joint = joint
    moving = subtree(chain, joint.child)
    entry.moving = sorted(chain.ref(oid) for oid in moving)
    entry.bounds = play_bounds(if_id, interface, joint, joint.id in chain.couplings.laws)
    entry.unknown.extend(entry.bounds.unknown)
    play = interface.functional.raw.get("play") if isinstance(interface.functional.raw.get("play"), Mapping) else {}
    samples, entry.samples, problems = scenario_samples(chain, hw, list(play.get("scenario_cases") or ()), steps)
    entry.unknown.extend(problems)
    if not samples:
        entry.unknown.append(f"{if_id}: no scenario pose to evaluate")
        return entry
    entry.nominal = evaluator.run(chain, "nominal", samples, moving)
    for case in corner_cases(joint, entry.bounds.halves):
        entry.cases.append(evaluator.run(offset_chain(chain, joint, case.offset), case.label, samples, moving))
    return entry


# --------------------------------------------------------------------------- findings and the packet


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return out if out and out[0].isalpha() else f"if_{out}"


def play_findings(entry: Assessment, obligation: str | None) -> list[dict[str, Any]]:
    subject = {"interface": entry.interface}
    base: dict[str, Any] = {"subject": subject, "method": {"name": PLAY_METHOD, "tool": PRODUCER},
                            "recheck": {"families": [FAMILY], "scope": [subject], "earliest_stage": "analysis"}}
    if obligation is not None:
        base["obligation"] = obligation
    collision = {"property": "undeclared_overlap", "operator": "<=", "value": pose.INTERFERENCE_MM3, "units": "mm3",
                 "source": dict(RULE_SOURCE)}
    findings = []
    results = [r for r in (entry.nominal, *entry.cases) if r is not None]
    hits = [(r, c) for r in results for c in r.collisions]
    if hits:
        result, worst = max(hits, key=lambda hit: hit[1]["volume_mm3"])
        cases = sorted({r.label for r, _ in hits})
        at = "the nominal, centred pose" if result.label == "nominal" else f"play extreme {result.label}"
        findings.append({
            "id": f"{_slug(entry.interface)}_play_collision", "code": "motion.play_collision", **base, "severity": "critical",
            "outcome": "fail", "measured": {"value": worst["volume_mm3"], "units": "mm3"}, "criterion": collision,
            "uncertainty": "the corners of the declared play box at the sampled scenario poses",
            "consequence": (f"{' and '.join(worst['pair'])} collide at {at}, pose {worst['pose']} "
                            f"({len(cases)} of {len(results)} cases collide): the mechanism binds or jams within its play"),
            "remedy": "add clearance at the colliding pair, reduce the play (a closer fit, a preload or a guide), or "
                      "restrict the motion range",
            "alternatives": [{"text": "a closer running fit or a preloaded bearing at the joint",
                              "tradeoffs": "tighter tolerances and their stack; friction and wear change"},
                             {"text": "relieve the obstacle geometry", "tradeoffs": "changes that part and its own checks"}],
        })
    breaks = [(r, v) for r in results for v in r.violations]
    if breaks:
        result, worst = min(breaks, key=lambda hit: hit[1]["clearance_mm"] - hit[1]["required_mm"])
        findings.append({
            "id": f"{_slug(entry.interface)}_play_clearance", "code": "motion.play_clearance", **base, "severity": "major",
            "outcome": "fail", "measured": {"value": worst["clearance_mm"], "units": "mm"},
            "criterion": {"property": "clearance", "operator": ">=", "value": worst["required_mm"], "units": "mm",
                          "source": dict(RULE_SOURCE)},
            "uncertainty": f"the clearance requirement {worst['requirement']} at the sampled scenario poses",
            "consequence": (f"{' and '.join(worst['pair'])} come to {worst['clearance_mm']} mm at "
                            f"{'the nominal pose' if result.label == 'nominal' else 'play extreme ' + result.label}, below "
                            f"the required {worst['required_mm']} mm"),
            "remedy": "add clearance at the pair or reduce the play",
        })
    if entry.outcome == "unknown":
        reasons = "; ".join(entry.reasons())
        findings.append({
            "id": f"{_slug(entry.interface)}_play_unknown", "code": "motion.play_unknown", **base, "severity": "major",
            "outcome": "unknown", "measured": {"status": "unknown", "reason": reasons}, "criterion": collision,
            "uncertainty": reasons,
            "consequence": f"whether {entry.interface} works under its play and loads is not established",
            "remedy": "declare each relevant play component (a bound, or not_applicable with its reason), the compliance "
                      "under the interface's loads, and the joint between its parts, then rerun",
        })
    return findings


def outcome_row(obligation: str, outcome: str, findings: Sequence[Mapping[str, Any]], uncertainty: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"obligation": obligation, "outcome": outcome, "level": "simulation_supported",
                           "method": PLAY_METHOD, "coverage": PLAY_COVERAGE}
    if findings:
        row["findings"] = [f["id"] for f in findings]
    if outcome == "unknown":
        row["uncertainty"] = uncertainty or "not established"
    return row


def part_interfaces(pn: str, record: Any, chain: Any) -> list[str]:
    """The interfaces of `pn` that declare play or a rotating or sliding function."""
    found = []
    for if_id, interface in sorted(record.interfaces.items()):
        ends = []
        for key in ("from", "to"):
            ref = interface.raw.get(key)
            try:
                ends.append(chain.pn(chain.resolve(ref, "")) if isinstance(ref, str) else None)
            except pose.MotionError:
                ends.append(ref)
        moves = (interface_functions(interface) or set()) & MOTION_FUNCTIONS
        if pn in ends and (interface.functional.raw.get("play") or moves):
            found.append(if_id)
    return found


def assess(args: argparse.Namespace, version: str | None, record_hw: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the packet, the evidence detail) of one run."""
    record = intent_module.load_record(record_hw)
    expected = [o for o in intent_module.required_checks(record) if o.family == FAMILY]
    dependencies: dict[str, str] = {}
    for name in ("assembly.yaml", "interfaces.yaml"):
        if (record_hw / name).is_file():
            dependencies[name.replace(".", "_")] = hashlib.sha256((record_hw / name).read_bytes()).hexdigest()
    if version is not None:
        dependencies["version_manifest"] = pose.input_sha256(args.version_dir)
    detail: dict[str, Any] = {"interfaces": {}, "limits": [PLAY_COVERAGE]}
    outcomes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if not expected:
        return check_packet.build(record, expected, PRODUCER, version, outcomes, findings, dependencies), detail
    assembly, chain, definitions = pose.load_inputs(args)
    allowed, fitted, clearances = sweep.declared_policy(record_hw, assembly)
    shapes = {pn: part.shape for pn, part in definitions.items()}
    measurer = pair_measure.PairMeasurer(shapes, geometry_ids=pose.geometry_ids(definitions),
                                         rule=pair_measure.RuleVersion.current(), threads=False)
    wanted = {o.id: o for o in expected}
    by_part = {o.subject["PN"]: part_interfaces(o.subject["PN"], record, chain) for o in expected if "PN" in o.subject}
    to_assess = sorted({o.subject["interface"] for o in expected if "interface" in o.subject}
                       | {if_id for ids in by_part.values() for if_id in ids})
    assessments: dict[str, Assessment] = {}
    with pose.stdout_to_stderr(), measurer.session() as session:
        evaluator = Evaluator(session, allowed, fitted, sweep.pair_requirements(chain, clearances, []),
                              sweep.load_margins(record_hw), sweep.pairs_to_check(chain, [])[0])
        for if_id in to_assess:
            assessments[if_id] = assess_interface(if_id, record.interfaces[if_id], chain, args.hw, args.steps, evaluator)
    for case in sorted({c for interface in to_assess for c in (
            (record.interfaces[interface].functional.raw.get("play") or {}).get("scenario_cases") or ())}):
        path = plan_module.case_path(args.hw, case, LAYOUT)
        if path.is_file():
            dependencies[f"motion_case_{case}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    by_interface: dict[str, list[dict[str, Any]]] = {}
    for if_id, entry in assessments.items():
        obligation = f"{FAMILY}@{if_id}"
        mine = play_findings(entry, obligation if obligation in wanted else None)
        findings.extend(mine)
        by_interface[if_id] = mine
        detail["interfaces"][if_id] = entry.report()
        if obligation in wanted:
            outcomes.append(outcome_row(obligation, entry.outcome, mine, "; ".join(entry.reasons())))
    for pn, ids in sorted(by_part.items()):
        obligation = f"{FAMILY}@{pn}"
        if not ids:
            subject = {"PN": pn}
            reason = f"{pn} rotates or slides, but no interface of it declares that motion or its play"
            mine = [{"id": f"{_slug(pn)}_play_unknown", "code": "motion.play_unknown", "obligation": obligation,
                     "subject": subject, "severity": "major", "outcome": "unknown",
                     "measured": {"status": "unknown", "reason": reason},
                     "criterion": {"status": "unknown", "reason": reason},
                     "method": {"name": PLAY_METHOD, "tool": PRODUCER}, "uncertainty": reason,
                     "consequence": f"whether {pn} works under its play is not established",
                     "remedy": "record the interface between the part and its neighbour, with its function and play",
                     "recheck": {"families": [FAMILY], "scope": [subject], "earliest_stage": "analysis"}}]
            findings.extend(mine)
            outcomes.append(outcome_row(obligation, "unknown", mine, reason))
            continue
        states = {assessments[i].outcome for i in ids}
        state = "fail" if "fail" in states else "unknown" if "unknown" in states else "pass"
        uncertain = "; ".join(r for i in ids for r in assessments[i].reasons())
        outcomes.append(outcome_row(obligation, state, [f for i in ids for f in by_interface[i]],
                                    uncertain or f"through {', '.join(ids)}"))
    return check_packet.build(record, expected, PRODUCER, version, outcomes, findings, dependencies), detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mechanism function under declared play and loads")
    pose.add_geometry_arguments(parser)
    parser.add_argument("--steps", type=int, default=9, help="samples per independent joint without scenario cases")
    parser.add_argument("--replace", action="store_true", help="retain and replace a published packet with another recipe")
    parser.add_argument("--out", type=Path, help="scratch JSON path, only under .cad-work")
    args = parser.parse_args(argv)
    if args.steps < 2:
        parser.error("--steps must be >= 2")
    try:
        version, record_hw = None, args.hw
        if args.version:
            version, version_dir, record_hw = LAYOUT.built_version(args.hw, args.version)
            args.version, args.version_dir, args.record_hw = version, version_dir, record_hw
        result, detail = assess(args, version, record_hw)
        recipe = {"options": {"steps": args.steps, "attach": list(args.attach)}, "dependencies": result["dependencies"],
                  "rule_sha256": pair_measure.RuleVersion.current().digest.hex()}
        output, status = check_packet.emit(LAYOUT, args.hw, version, SUBJECT, PRODUCER, result, detail, "play.json",
                                           recipe, out=args.out, replace=args.replace,
                                           record_version=intent_module.RECORD_VERSION)
        json.dump(output, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if status != "pass":
            print(f"play: {status}", file=sys.stderr)
        return 0 if status == "pass" else 1
    except (check_packet.PacketError, pose.MotionError, LAYOUT.LayoutError, intent_module.IntentError) as error:
        print(f"play: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
