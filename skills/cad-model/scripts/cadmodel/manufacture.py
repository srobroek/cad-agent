"""The manufacturing outputs of a version's custom-made parts, and the checks that judge them.

`run` writes hw/versions/<V>/manufacturing/<PN>/ for every custom-made part (cad-record layout
`custom_made`) whose primary process has an output contract: part.step (AP242, exact B-rep,
millimetres), part.stl for an additive part (binary STL, millimetres) and manufacturing.json. A part
whose process has none (`other`) still builds; its entry records its manufacturing coverage as unknown
and no file is invented. The state is the installed definition, or the `build_manufacture(params)`
shape of a part whose part.yaml declares `manufacture.relationship: different`.

Every disconnected solid of the state is a manufacturing component (cadmodel.components): a sole solid
is the implicit `main`; a record version 2 part with several declares them, and each is also exported
alone to components/<COMP>/part.step (and part.stl), in its own pose. An additive component is
exported in the pose its route fixes: the approved orientation of the additive operation that shapes
it. A proposed or unknown orientation exports the native frame and leaves the output incomplete; the
envelope auto-fit (cadmodel.orientation.fit) exports only a record version 1 part, labelled
unapproved. `inspect` reads the written files back and judges them; `run` seals the version only
when no part fails a blocking check, and `check` repeats `inspect` on the sealed files.

A part's outputs are a function of its manifest facts (its record fields, state, tolerance and its
manufacturing intent: route, components, orientations, the material and capability profiles, the
requirements and the source bytes they name), its state's geometry and feature locators, and the code
and runtime of this process, so `make` keys them by those (`cadmodel.store`) and links the files,
placement, components and row of an earlier identical make instead of exporting again; only an
output that did not fail is kept. The findings and evidence ledgers are never read here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, cast

from cadmodel import components, mesh, occ, orientation, provenance, stepfile, store
from cadmodel.motion import load_pose
from cadmodel.record import Snapshot, load_intent, load_layout

SIDECAR = "manufacturing.json"
SIDECAR_SCHEMA = 2  # 2 adds intent, components, component_outputs and the export pose
OUTPUTS_FORMAT = 2  # bump when what make writes changes without any keyed input changing
BED_MM = 1e-6  # bed contact, and how far a measured bound may pass a reserved extent
FLOAT32_EPS = 2.0 ** -23  # STL stores float32 coordinates
COMPONENT_DIR = "components"
PART_FILES = ("part.step", "part.stl")


@dataclass(frozen=True)
class Plan:
    """What `run` makes of one custom-made part, decided from its record snapshot before any kernel runs."""

    pn: str
    record: Mapping[str, Any]  # its part.yaml
    policy: Any  # cad-record layout ManufacturePolicy
    version: int  # the snapshot's record version
    part: Any  # its cad-record manufacturing_intent.Part (record version 2), or None
    intent: Mapping[str, Any]  # the manifest `intent` block: what the outputs depend on beyond geometry
    declared: orientation.BuildVolume | None  # product.yaml build_volume
    envelopes: Mapping[str, orientation.BuildVolume]  # additive operation id -> its capability's measured envelope

    @property
    def legacy(self) -> bool:
        legacy_version: int = load_intent().LEGACY_RECORD_VERSION
        return self.version == legacy_version

    @property
    def additive(self) -> bool:
        return self.record.get("process") in load_intent().ADDITIVE_KINDS


def _thaw(value: Any) -> Any:
    return load_intent().thaw(value)


def reference(value: Any) -> Any:
    """A profile id as itself; an unknown or not-applicable fact as its status and reason."""
    if load_intent().is_fact(value):
        return {"status": value.status, "reason": value.reason}
    return value


def _named(value: Any, keys: frozenset[str]) -> set[str]:
    """Every string a key of `keys` names anywhere in `value`, as one value or a list of them."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in keys:
                if isinstance(item, str):
                    found.add(item)
                elif isinstance(item, (list, tuple)):
                    found.update(entry for entry in item if isinstance(entry, str))
            found |= _named(item, keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _named(item, keys)
    return found


def _route_summary(route: Any) -> Any:
    if load_intent().is_fact(route):
        return reference(route)
    return [{"id": op.id, "kind": op.kind, "capability": reference(op.capability), "provider": op.provider,
             "components": list(op.components), "depends_on": list(op.depends_on), "known_kind": op.known_kind}
            for op in route]


def _digest(value: Any) -> str:
    """sha256 of record data as canonical JSON; YAML dates and other scalars keep their text form."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _intent_block(snapshot: Snapshot, part: Any) -> dict[str, Any]:
    """The manifest `intent` of one part: its route and profiles, and `inputs_sha256` over everything
    of the record its outputs depend on: the part's raw intent, the material and capability profiles and
    requirements it names, and the bytes of every source artifact those cite."""
    intent = load_intent()
    if snapshot.version != intent.RECORD_VERSION or part is None:
        return {"record_version": snapshot.version, "status": "unknown", "reason": intent.LEGACY_REASON}
    raw = _thaw(part.raw.get("intent"))
    materials = {name: _thaw(snapshot.intent.materials[name].raw) if name in snapshot.intent.materials else None
                 for name in sorted(_named(raw, frozenset({"material_profile"})))}
    capabilities = {name: _thaw(snapshot.intent.capabilities[name].raw) if name in snapshot.intent.capabilities else None
                    for name in sorted(_named(raw, frozenset({"capability"})))}
    named = _named(raw, frozenset({"requirements", "requirement"}))
    listed = (snapshot.documents.get("requirements.yaml") or {}).get("requirements") or []
    requirements = {item["id"]: item for item in listed if isinstance(item, Mapping) and item.get("id") in named}
    cited = _named([raw, materials, capabilities, requirements], frozenset({"artifact"}))
    sources = {name: hashlib.sha256(snapshot.files[name]).hexdigest() if name in snapshot.files else None
               for name in sorted(cited)}
    inputs = {"part": raw, "materials": materials, "capabilities": capabilities, "requirements": requirements,
              "sources": sources}
    return {"record_version": snapshot.version,
            "material_profile": reference(part.intent.material_profile),
            "route": _route_summary(part.intent.route),
            "profiles": {"materials": {name: None if data is None else _digest(data) for name, data in materials.items()},
                         "capabilities": {name: None if data is None else _digest(data)
                                          for name, data in capabilities.items()}},
            "sources": sources,
            "inputs_sha256": _digest(inputs)}


def _envelope(profile: Any) -> orientation.BuildVolume | None:
    """A capability profile's measured working envelope, when its x, y and z are all known lengths."""
    intent = load_intent()
    data = profile.raw.get("envelope")
    if not isinstance(data, Mapping):
        return None
    size: list[float] = []
    for axis in ("x", "y", "z"):
        try:
            value = intent.fact(_thaw(data.get(axis) or {}))
        except intent.IntentError:
            return None
        if not value.known or not isinstance(value.value, intent.Quantity) or value.value.value is None:
            return None
        try:
            size.append(value.value.to("mm").value)
        except intent.IntentError:
            return None
    x, y, z = size
    return orientation.BuildVolume((x, y, z), 0.0, orientation.ORIENTATIONS, f"capability {profile.id} envelope")


def _orientations(part: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """(operation id, orientation mapping) of every route operation that declares a rotation."""
    route = part.intent.route
    if load_intent().is_fact(route):
        return []
    return [(op.id, op.orientation) for op in route if isinstance(op.orientation, Mapping)]


def plan(snapshot: Snapshot) -> tuple[dict[str, Plan], list[tuple[str, str]], dict[str, str]]:
    """A Plan per custom-made part record of `snapshot`, (PN, message) for each record whose declaration
    is refused, and the message of each custom-made part whose process has no output contract."""
    layout, intent = load_layout(), load_intent()
    product = snapshot.documents.get("product.yaml")
    declared = orientation.BuildVolume.declared(product if isinstance(product, Mapping) else {})
    plans: dict[str, Plan] = {}
    refused: list[tuple[str, str]] = []
    uncovered: dict[str, str] = {}
    for pn, record in sorted(snapshot.parts.items()):
        if not layout.custom_made(record):
            continue
        try:
            policy = layout.manufacture_policy(record)
        except layout.UnsupportedProcess as error:
            uncovered[pn] = (f"{error}; the installed definition is built, and no manufacturing file is written or "
                             "invented for it")
            continue
        except (layout.LayoutError, TypeError, ValueError) as error:
            refused.append((pn, f"part.yaml manufacture: {error}"))
            continue
        declared_state = record.get("manufacture") or {}
        if policy.relationship == "different":
            missing = [key for key in ("state", "reason", "decision") if not declared_state.get(key)]
            if missing:
                refused.append((pn, f"part.yaml manufacture.relationship: different also needs {', '.join(missing)}"))
                continue
            relative, absolute = policy.volume_rel_tolerance, policy.volume_abs_tolerance_mm3
            if not (0 <= relative <= 1 and absolute >= 0 and (relative > 0 or absolute > 0)):
                refused.append((pn, f"part.yaml manufacture volume tolerance refused: volume_rel_tolerance {relative} "
                                    f"must be in (0, 1], volume_abs_tolerance_mm3 {absolute} must be positive"))
                continue
        part = snapshot.intent.parts.get(pn)
        envelopes: dict[str, orientation.BuildVolume] = {}
        if snapshot.version == intent.RECORD_VERSION:
            if part is None:
                refused.append((pn, f"hw/parts/{pn}/part.yaml does not name PN {pn}"))
                continue
            problems = []
            for op_id, data in _orientations(part):
                try:
                    orientation.rotation_of(data.get("rotation"))
                except ValueError as error:
                    problems.append(f"part.yaml intent route {op_id} orientation: {error}")
            if problems:
                refused.append((pn, "; ".join(problems)))
                continue
            route = part.intent.route
            for op in () if intent.is_fact(route) else route:
                profile = snapshot.intent.capabilities.get(op.capability) if isinstance(op.capability, str) else None
                found = _envelope(profile) if op.kind in intent.ADDITIVE_KINDS and profile is not None else None
                if found is not None:
                    envelopes[op.id] = found
        plans[pn] = Plan(pn, record, policy, snapshot.version, part, _intent_block(snapshot, part), declared, envelopes)
    return plans, refused, uncovered


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def facts(plan: Plan, product: str, installed: Mapping[str, Any], native: Mapping[str, Any], path: str) -> dict[str, Any]:
    """The declared claims of one part's outputs, before export: identity, state, tolerance, intent and inputs."""
    record, policy = plan.record, plan.policy
    procurement = value if isinstance(value := record.get("procurement"), Mapping) else {}
    return {
        "schema_version": SIDECAR_SCHEMA, "PN": plan.pn, "rev": record.get("rev"), "title": record.get("title"),
        "product": product, "material": record.get("material"), "process": record.get("process"),
        "finish": record.get("finish"),
        "eligibility": {"make_buy": record.get("make_buy"), "procurement_mode": procurement.get("mode")},
        "state": {"relationship": policy.relationship, "name": policy.state, "reason": policy.reason,
                  "decision": policy.decision,
                  "source": "build_manufacture" if policy.relationship == "different" else "installed definition"},
        "tolerance": {"volume_rel": policy.volume_rel_tolerance, "volume_abs_mm3": policy.volume_abs_tolerance_mm3,
                      "allowed_mm3": policy.volume_tolerance_mm3(installed["kernel_volume_mm3"])},
        "installed": dict(installed),
        "native": {key: native[key] for key in ("kernel_volume_mm3", "kernel_solids", "kernel_valid", "kernel_errors")},
        "units": {"length": "mm"}, "up": "+Z",
        "intent": dict(plan.intent),
        "build_volume": plan.declared.describe() if plan.additive and plan.declared is not None else None,
        "mesh": ({"format": "binary STL", "units": "mm", "linear_deflection_mm": policy.linear_deflection_mm,
                  "angular_deflection_rad": policy.angular_deflection_rad} if "part.stl" in policy.outputs else None),
        "path": path, "outputs": list(policy.outputs),
    }


def pose(plan: Plan, component: str | None, shape: Any) -> tuple[orientation.Pose, orientation.BuildVolume | None]:
    """The pose component `component` (None: the whole part) is exported in, and the envelope it is set in."""
    intent = load_intent()
    if plan.legacy:
        if not plan.additive:
            return orientation.Pose("model_frame", orientation.IDENTITY, "identity", "identity"), None
        found = orientation.fit(shape, plan.declared)[1] if plan.declared is not None else None
        why = "a record version 1 part: the envelope auto-fit is a legacy diagnostic, never an approved orientation"
        if found is not None:
            return orientation.Pose("unapproved_auto_fit", found.rotation, found.method, found.orientation,
                                    reason=why), plan.declared
        return orientation.Pose("unapproved_native", orientation.IDENTITY, "native", "xyz", reason=why), plan.declared
    route = plan.part.intent.route
    shaping = None if intent.is_fact(route) else next(
        (op for op in route if op.kind in intent.ADDITIVE_KINDS and (not op.components or component in op.components)),
        None)
    if shaping is None:
        if not plan.additive:
            return orientation.Pose("model_frame", orientation.IDENTITY, "identity", "identity"), None
        reason = ("the part's route is unknown" if intent.is_fact(route)
                  else f"no additive operation of the route shapes component {component or 'main'}")
        return orientation.Pose("unknown", orientation.IDENTITY, "native", "native", reason=reason), plan.declared
    volume = plan.envelopes.get(shaping.id, plan.declared)
    data = shaping.orientation
    if isinstance(data, Mapping):
        status = str(data.get("status"))
        return orientation.Pose(status, orientation.rotation_of(data.get("rotation")), status, status, shaping.id,
                                data.get("decision")), volume
    if intent.is_fact(data):
        return orientation.Pose(data.status, orientation.IDENTITY, "native", "native", shaping.id, data.decision,
                                data.reason), volume
    return orientation.Pose("unknown", orientation.IDENTITY, "native", "native", shaping.id,
                            reason=f"route operation {shaping.id} records no orientation"), volume


def _placement_facts(chosen: orientation.Pose, placement: Any, volume: orientation.BuildVolume | None,
                     approved: bool | None = None) -> dict[str, Any]:
    bed = chosen.status != "model_frame"
    return {"method": chosen.method, "orientation": chosen.name,
            "bed": ("centred" if volume is not None else "origin") if bed else None, "frame": "bed" if bed else "model",
            "transform": [[*(v + 0.0 for v in placement.rot[i]), placement.trans[i] + 0.0] for i in range(3)],
            "determinant": orientation.determinant(placement.rot), **chosen.describe(),
            **({"approved": approved} if approved is not None else {}),
            "build_volume": volume.describe() if volume is not None else None}


def _place(shape: Any, chosen: orientation.Pose, volume: orientation.BuildVolume | None) -> Any:
    if chosen.status == "model_frame":
        return load_pose().Rigid.identity()
    return orientation.bed_placement(shape, chosen.rotation, volume)


def _write(folder: Path, entry: Mapping[str, Any], shape: Any, placement: Any) -> None:
    """part.step of `shape` moved by `placement`, and part.stl tessellated from its read-back when the entry has a mesh."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

    pose_module = load_pose()
    placed = BRepBuilderAPI_Transform(shape, pose_module.to_trsf(placement), True).Shape()
    folder.mkdir(parents=True)
    pose_module.write_part_step(folder / "part.step", pose_module.Part(entry["PN"], entry["product"], placed))
    if entry["mesh"] is not None:
        written, problem = read_part(folder / "part.step", entry["PN"])
        if written is None:
            raise occ.GeometryError(f"cannot tessellate {folder / 'part.step'}: {problem}")
        mesh.write_stl(written.shape, folder / "part.stl", entry["mesh"]["linear_deflection_mm"],
                       entry["mesh"]["angular_deflection_rad"])


def _component_facts(plan: Plan, bound: components.Bound, placement: Mapping[str, Any], files: list[str]) -> dict[str, Any]:
    """What a process check, fit or access check reads of one component: its binding, profile, planned
    operations, files, export placement and each member solid's native-frame geometry."""
    intent = load_intent()
    declared = next((c for c in plan.part.intent.components if c.id == bound.id), None) if plan.part is not None else None
    profile = declared.material_profile if declared is not None and declared.material_profile is not None else (
        plan.part.intent.material_profile if plan.part is not None and not plan.legacy else None)
    route = None if plan.part is None or plan.legacy else plan.part.intent.route
    operations = (None if route is None or intent.is_fact(route)
                  else [op.id for op in route if not op.components or bound.id in op.components])
    return {"binding": bound.binding, "points_mm": [list(point) for point in bound.points],
            "locator": _thaw(bound.locator) if bound.locator is not None else None,
            "multiplicity": len(bound.members),
            "material_profile": (reference(profile) if profile is not None
                                 else {"status": "unknown", "reason": intent.LEGACY_REASON}),
            "operations": operations, "planned": None if operations is None else bool(operations),
            "files": files, "placement": dict(placement), "volume_mm3": bound.volume_mm3,
            "members": [member.facts() for member in bound.members]}


def export(target: Path, plan: Plan, entry: Mapping[str, Any], shape: Any,
           features: Mapping[str, Any] | None) -> dict[str, Any]:
    """Bind `shape` (the manufacturing state, in its native frame) to the part's components, place each
    in its pose and write part.step, and part.stl when the entry lists it, into `target`, and each
    component's own files below components/<COMP>/ when there are several. Returns what the export
    derived: the placement, the components, their files and the identity status.
    BindingError when the solids cannot be bound without guessing."""
    declared = () if plan.legacy or plan.part is None else plan.part.intent.components
    try:
        bound = components.bind(shape, declared, features)
    except components.BindingError as error:
        if not plan.legacy:
            raise
        chosen, volume = pose(plan, None, shape)
        placement = _place(shape, chosen, volume)
        _write(target, entry, shape, placement)
        return {"placement": _placement_facts(chosen, placement, volume), "components": {}, "component_outputs": [],
                "component_identity": {"status": "unknown", "reason": f"{load_intent().LEGACY_REASON}: {error}",
                                       "solids": [solid.facts() for solid in components.solids(shape)]}}
    poses = {item.id: pose(plan, item.id if declared else None, item.shape) for item in bound}
    distinct = {(chosen.status, chosen.rotation, chosen.operation, volume) for chosen, volume in poses.values()}
    if len(distinct) == 1:
        top, top_volume = next(iter(poses.values()))
        approved = None
    else:
        top = orientation.Pose("per_component", orientation.IDENTITY, "native", "per_component",
                               reason="the components are exported in their own poses")
        top_volume, approved = None, all(chosen.approved for chosen, _ in poses.values())
    top_placement = _place(shape, top, top_volume)
    _write(target, entry, shape, top_placement)
    top_facts = _placement_facts(top, top_placement, top_volume, approved)
    several = len(bound) > 1
    found, written = {}, []
    for item in bound:
        chosen, volume = poses[item.id]
        if several:
            placement = _place(item.shape, chosen, volume)
            _write(target / COMPONENT_DIR / item.id, entry, item.shape, placement)
            files = [f"{COMPONENT_DIR}/{item.id}/{name}" for name in PART_FILES if name in entry["outputs"]]
            written += files
            facts_of = _placement_facts(chosen, placement, volume)
        else:
            files, facts_of = [name for name in PART_FILES if name in entry["outputs"]], top_facts
        found[item.id] = _component_facts(plan, item, facts_of, files)
    return {"placement": top_facts, "components": found, "component_outputs": sorted(written),
            "component_identity": {"status": "bound"}}


def read_part(path: Path, pn: str) -> tuple[Any, str | None]:
    """(the cad-motion Part of a one-product part STEP, None), or (None, why it cannot be read as one)."""
    pose_module = load_pose()
    try:
        step = pose_module.read_assembly_step(path)
    except (pose_module.MotionError, OSError) as error:
        return None, str(error)
    if set(step.definitions) != {pn} or len(step.components) != 1:
        return None, f"holds products {sorted(step.definitions)} in {len(step.components)} components, expected one {pn}"
    if not step.components[0].location.close_to(pose_module.Rigid.identity()):
        return None, "places its product with a location; the geometry must sit in the bed frame itself"
    return step.definitions[pn], None


def _judge(conditions: Mapping[str, bool], **fields: Any) -> dict[str, Any]:
    """One blocking sub-check: pass when every condition holds, else fail naming the ones that do not."""
    failed = [name for name, held in conditions.items() if not held]
    return {"status": "fail" if failed else "pass", "blocking": True, "failed": failed, **fields}


def _incomplete(message: str, **fields: Any) -> dict[str, Any]:
    """A sub-check that cannot pass yet and never blocks: the output is incomplete, not wrong."""
    return {"status": "warn", "blocking": False, "failed": [], "message": message, **fields}


def _box(shape: Any) -> tuple[list[float], list[float]]:
    low, high = occ.bbox(shape)
    return list(low), list(high)


def required(entry: Mapping[str, Any], sidecar: bool = True) -> list[str]:
    """The files the entry's folder holds: its process outputs and its components' own files."""
    names = [name for name in entry["outputs"] if sidecar or name != SIDECAR]
    return sorted([*names, *(entry.get("component_outputs") or [])])


def inspect(target: Path, entry: Mapping[str, Any], record: Mapping[str, Any], expected: Iterable[str],
            hashes: Mapping[str, str] | None = None) -> tuple[dict[str, Any], Any]:
    """Judge the outputs in `target` against `entry` (the manifest facts) and `record` (the part.yaml
    snapshot). `expected` are the files (paths below `target`) that must be there and no others; `hashes`
    binds their bytes (check passes the manifest's). Returns (the part's row, its read-back STEP shape or None)."""
    native, installed, allowed = entry["native"], entry["installed"], entry["tolerance"]["allowed_mm3"]
    expected = sorted(expected)
    present = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) if target.is_dir() else []
    changed = sorted(name for name, sha in (hashes or {}).items() if name in present and sha256(target / name) != sha)
    fields = ("PN", "rev", "material", "process", "finish")
    difference = abs(native["kernel_volume_mm3"] - installed["kernel_volume_mm3"])
    checks: dict[str, Any] = {
        "outputs": _judge({"required_present": all(name in present for name in expected),
                           "no_other_files": all(name in expected for name in present), "hashes_match": not changed},
                          required=expected, present=present, changed=changed),
        "record": _judge({f"same_{key.lower()}": entry.get(key) == record.get(key) for key in fields},
                         record={key: record.get(key) for key in fields}),
        "native": _judge({"valid": bool(native["kernel_valid"]), "solid": native["kernel_solids"] > 0,
                          "positive_volume": native["kernel_volume_mm3"] > 0}, **native),
        "identity": _judge({"volume_within_tolerance": difference <= allowed,
                            "same_solids": native["kernel_solids"] == installed["kernel_solids"]},
                           installed_volume_mm3=installed["kernel_volume_mm3"],
                           manufacture_volume_mm3=native["kernel_volume_mm3"], difference_mm3=difference,
                           allowed_mm3=allowed, installed_solids=installed["kernel_solids"],
                           manufacture_solids=native["kernel_solids"]),
    }
    part = None
    if "part.step" in present:
        part, checks["step"] = _step(target / "part.step", entry, native["kernel_volume_mm3"], native["kernel_solids"])
    else:
        checks["step"] = _judge({"present": False})
    if entry["mesh"] is not None:
        checks["mesh"] = (_mesh(target / "part.stl", entry, part.shape) if "part.stl" in present and part is not None
                          else _judge({"present_with_readable_step": False}))
    checks["placement"] = _placement(entry, entry["placement"], part.shape if part is not None else None)
    if entry.get("components"):  # sidecar schema 2; earlier versions hold one unbound output
        checks["components"] = _components(target, entry, present, part.shape if part is not None else None)
    elif (entry.get("component_identity") or {}).get("status") == "unknown":
        checks["components"] = _incomplete("the part's solids are not bound to components: "
                                           + entry["component_identity"]["reason"])
    blocking = sorted(name for name, row in checks.items() if row["status"] == "fail")
    complete = not blocking and all(row["status"] == "pass" for row in checks.values())
    row = {"status": "fail" if blocking else ("pass" if complete else "warn"), "complete": complete,
           "blocking_failures": blocking, "checks": checks}
    return row, part.shape if part is not None else None


def failure_message(row: Mapping[str, Any]) -> str:
    """The failed conditions of a part row, each check's message after them, for a run finding."""
    parts = []
    for name in row["blocking_failures"]:
        check = row["checks"][name]
        parts.append(f"{name}: " + "; ".join(filter(None, (", ".join(check["failed"]), check.get("message")))))
    return "; ".join(parts)


def _step(path: Path, entry: Mapping[str, Any], volume: float, solids_expected: int) -> tuple[Any, dict[str, Any]]:
    """A part STEP read back: AP242 in millimetres, one product named `<PN> <title>` in place, a valid
    B-rep with the expected solid count, and the expected volume within the read-back drift."""
    schema, units = stepfile.schema(path), stepfile.length_units(path)
    part, problem = read_part(path, entry["PN"])
    header = {"ap242": schema is not None and schema.startswith(stepfile.AP242), "millimetres": units == ["mm"]}
    if part is None:
        return None, _judge({**header, "one_product_in_place": False}, schema=schema, units=units, message=problem)
    errors = occ.check_statuses(part.shape)
    measured, solids = occ.volume(part.shape), occ.count(part.shape, "SOLID")
    rel = abs(measured - volume) / max(abs(volume), 1e-12)
    low, high = _box(part.shape)
    return part, _judge({**header, "one_product_in_place": True, "product_name": part.name == entry["product"],
                         "valid": not errors, "same_solids": solids == solids_expected,
                         "volume_drift": rel <= stepfile.READBACK_REL},
                        schema=schema, units=units, product=part.name, errors=errors, solids=solids,
                        shells=occ.count(part.shape, "SHELL"), volume_mm3=measured, rel_error=rel,
                        tolerance_rel=stepfile.READBACK_REL, bbox_mm={"min": low, "max": high})


def _mesh(path: Path, entry: Mapping[str, Any], shape: Any) -> dict[str, Any]:
    """The STL against the STEP it was tessellated from: binary, finite, without degenerate triangles,
    closed and consistently wound outward, one mesh shell per B-rep shell, and within the declared
    chord deviation of the exact solid: in volume (at most area x deflection) and on each box face.
    Welding is by position only; nothing is repaired, so a crack stays open."""
    try:
        report = mesh.stl_report(path)
    except Exception as error:  # noqa: BLE001 - an unreadable STL is a failed output, not an infrastructure error
        return _judge({"readable": False}, message=f"cannot read {path.name}: {error}")
    deflection = entry["mesh"]["linear_deflection_mm"]
    step_volume, area = occ.volume(shape), occ.area(shape)
    low, high = _box(shape)
    slack = 2 * FLOAT32_EPS * max(report["max_abs_mm"], 1.0) + 1e-9  # float32 rounding of a coordinate
    bound = area * (deflection + slack)
    finite = report["finite"]
    volume_error = abs(report["volume_mm3"] - step_volume) if finite else None
    m_low, m_high = report["bbox_mm"]["min"], report["bbox_mm"]["max"]
    shells = occ.count(shape, "SHELL")
    return _judge({"binary": report["binary"], "finite": finite,
                   "no_degenerate_triangles": finite and report["degenerate_triangles"] == 0,
                   "watertight": report["watertight"], "winding_consistent": report["winding_consistent"],
                   "outward": finite and report["volume_mm3"] > 0, "shells": report["shells"] == shells,
                   "volume_within_deflection": finite and volume_error <= bound,
                   "box_within_deflection": all(low[i] - slack <= m_low[i] <= low[i] + deflection + slack
                                                and high[i] - deflection - slack <= m_high[i] <= high[i] + slack
                                                for i in range(3))},
                  **report, step_shells=shells, step_volume_mm3=step_volume, volume_error_mm3=volume_error,
                  volume_bound_mm3=bound, linear_deflection_mm=deflection,
                  angular_deflection_rad=entry["mesh"]["angular_deflection_rad"])


def _placement(entry: Mapping[str, Any], placement: Mapping[str, Any], shape: Any) -> dict[str, Any]:
    """A proper transform. A part set on a bed touches it at Z 0 and, with an envelope, sits inside its
    reserved extents (X and Y: margin to size - margin; Z: 0 to size - 2 margin); a record version 2
    part on a bed is complete only in its approved orientation. Any other part keeps its model frame."""
    transform = placement["transform"]
    proper = orientation.is_proper(tuple(tuple(row[:3]) for row in transform))  # type: ignore[arg-type]
    if shape is None:
        return _judge({"proper_rotation": proper, "readable_step": False}, **placement)
    low, high = _box(shape)
    facts_of = {**placement, "bbox_mm": {"min": low, "max": high}}
    frame = placement.get("frame") or ("bed" if entry["process"] == "fdm" else "model")  # sidecar schema 1 has no frame
    if frame != "bed":
        identity = transform == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]
        return _judge({"proper_rotation": proper, "model_frame": identity}, **facts_of)
    on_bed = _judge({"proper_rotation": proper, "bed_contact": abs(low[2]) <= BED_MM}, **facts_of)
    if on_bed["status"] == "fail":
        return on_bed
    legacy = (entry.get("intent") or {}).get("record_version", load_intent().LEGACY_RECORD_VERSION) == \
        load_intent().LEGACY_RECORD_VERSION
    if not legacy and not placement.get("approved"):
        return _incomplete(f"exported in its {placement.get('pose')} pose, not an approved orientation"
                           + (f": {placement['reason']}" if placement.get("reason") else ""), **facts_of)
    if placement.get("pose") == "per_component":  # the combined file of components each set in its own pose
        return {**on_bed, "note": "each component is placed and judged in its own pose (checks.components)"}
    volume = orientation.BuildVolume.described(placement.get("build_volume", entry.get("build_volume")))
    if volume is None:
        return _incomplete("no machine envelope is declared; exported on the bed origin", **facts_of)
    reserved = orientation.reserved_extents(volume)
    if not orientation.pose_fit(tuple(low), tuple(high), volume, BED_MM):  # type: ignore[arg-type]
        return _incomplete("the exported pose does not fit the envelope (a diagnostic, not process feasibility)",
                           **facts_of, reserved_mm=reserved)
    return {**on_bed, "reserved_mm": reserved}


def _moved(points: Iterable[Iterable[float]], transform: list[list[float]]) -> list[tuple[float, float, float]]:
    return [tuple(sum(row[k] * p[k] for k in range(3)) + row[3] for row in transform)  # type: ignore[misc]
            for p in (list(point) for point in points)]


def _components(target: Path, entry: Mapping[str, Any], present: list[str], whole: Any) -> dict[str, Any]:
    """Each component's own files read back: one product in place with the component's member count and
    volume, its mesh, its placement, and its binding points (witness inside, feature points on the
    surface) moved by that placement onto exactly one of its solids."""
    rows: dict[str, Any] = {}
    for ident, facts_of in sorted(entry["components"].items()):
        volume = facts_of["volume_mm3"]
        if facts_of["files"] and facts_of["files"][0].startswith(f"{COMPONENT_DIR}/"):
            step_name = facts_of["files"][0]
            if step_name in present:
                part, step = _step(target / step_name, entry, volume, facts_of["multiplicity"])
            else:
                part, step = None, _judge({"present": False})
            shape = part.shape if part is not None else None
            row: dict[str, Any] = {"step": step}
            stl_name = f"{COMPONENT_DIR}/{ident}/part.stl"
            if entry["mesh"] is not None:
                row["mesh"] = (_mesh(target / stl_name, entry, shape) if stl_name in present and shape is not None
                               else _judge({"present_with_readable_step": False}))
            row["placement"] = _placement(entry, facts_of["placement"], shape)
        else:
            shape, row = whole, {}
        expected = "in" if facts_of["binding"] == "witness" else "on"
        if facts_of["points_mm"]:
            held = shape is not None and components.check_points(
                shape, _moved(facts_of["points_mm"], facts_of["placement"]["transform"]), expected)
            row["binding"] = _judge({"points_bind": held}, binding=facts_of["binding"], expected=expected)
        status = ("fail" if any(r["status"] == "fail" for r in row.values())
                  else "pass" if all(r["status"] == "pass" for r in row.values()) else "warn")
        rows[ident] = {"status": status, "checks": row}
    failed = sorted(ident for ident, row in rows.items() if row["status"] == "fail")
    status = "fail" if failed else ("pass" if all(row["status"] == "pass" for row in rows.values()) else "warn")
    return {"status": status, "blocking": True, "failed": failed, "components": rows}


def _relative(hw: Path, label: str, pn: str) -> str:
    """manufacturing/<PN>, the part's folder below its version, from the cad-record layout."""
    layout = load_layout()
    return cast(str, layout.manufacturing_dir(hw, label, pn).relative_to(layout.version_dir(hw, label)).as_posix())


def make(work: Path, hw: Path, label: str, plans: Mapping[str, Plan], uncovered: Mapping[str, str],
         products: Mapping[str, str], definitions: Mapping[str, Any], built: Mapping[str, Mapping[str, Any]],
         states: Mapping[str, Any], parts_store: store.Store, state_shas: Mapping[str, str],
         features: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], set[str]]:
    """Write and judge the outputs of every planned part below the partial version folder `work`.

    `definitions` are the installed read-back Parts, `built` the runner's per-part results, `states`
    the read-back `build_manufacture` Parts, `state_shas` their STEP files' sha256 and `features` each
    state's feature locators (the installed drawing_features, or manufacture_features of a different
    state). Returns (the manifest entry of each part, its row, the PNs whose outputs `parts_store`
    already held); a part without a model, and one whose process has no output contract (`uncovered`),
    gets an incomplete entry and no folder."""
    entries: dict[str, Any] = {}
    rows: dict[str, Any] = {}
    reused: set[str] = set()
    for pn, message in sorted(uncovered.items()):
        entries[pn] = rows[pn] = {"PN": pn, "path": None, "status": "warn", "complete": False, "blocking_failures": [],
                                  "coverage": "unknown", "message": message}
    identity = {"runtime": provenance.runtime({}), "sources": provenance.sources([provenance.SKILLS])}
    for pn, part_plan in sorted(plans.items()):
        if pn not in definitions:
            entries[pn] = rows[pn] = {"PN": pn, "path": None, "status": "warn", "complete": False,
                                      "blocking_failures": [],
                                      "message": f"hw/parts/{pn}/ has no model.py, so no manufacturing output was built"}
            continue
        result, different = built[pn], part_plan.policy.relationship == "different"
        installed = {"step": f"parts/{pn}.step", "sha256": sha256(work / "parts" / f"{pn}.step"),
                     "kernel_volume_mm3": result["kernel_volume_mm3"], "kernel_solids": result["kernel_solids"]}
        entry = facts(part_plan, products[pn], installed, result["manufacture"] if different else result,
                      _relative(hw, label, pn))
        target = work / entry["path"]
        located = features.get(pn)
        outputs_key = store.key({"format": OUTPUTS_FORMAT, "kind": "manufacturing", "entry": entry,
                                 "state": state_shas.get(pn) if different else None, "features": located,
                                 "process": identity})
        cached = parts_store.record(outputs_key)
        if cached is not None and all(parts_store.place(sha, target / name) for name, sha in cached["objects"].items()):
            entry.update(cached["derived"])
            row, files = cached["row"], dict(cached["objects"])
            reused.add(pn)
        else:
            shutil.rmtree(target, ignore_errors=True)  # a reuse that store gc cut short: export into an empty folder
            try:
                derived = export(target, part_plan, entry, (states[pn] if different else definitions[pn]).shape, located)
            except components.BindingError as error:
                rows[pn] = {"status": "fail", "complete": False, "blocking_failures": ["components"],
                            "checks": {"components": _judge({"bound": False}, message=str(error))}}
                entries[pn] = {**entry, **rows[pn]}
                continue
            except (occ.GeometryError, load_pose().MotionError) as error:
                rows[pn] = {"status": "fail", "complete": False, "blocking_failures": ["export"],
                            "checks": {"export": _judge({"written": False}, message=str(error))}}
                entries[pn] = {**entry, **rows[pn]}
                continue
            entry.update(derived)
            row, _ = inspect(target, entry, part_plan.record, required(entry, sidecar=False))
            outputs = {name: sha256(target / name) for name in required(entry, sidecar=False) if (target / name).is_file()}
            (target / SIDECAR).write_text(json.dumps({**entry, "files": outputs, **row}, indent=2) + "\n",
                                          encoding="utf-8")
            files = {**outputs, SIDECAR: sha256(target / SIDECAR)}
            if row["status"] != "fail":
                parts_store.save(outputs_key, {"kind": "manufacturing", "pn": pn, "derived": derived, "row": row,
                                               "objects": {name: parts_store.intern(target / name) for name in files}})
        rows[pn] = row
        entries[pn] = {**entry, "files": files, **{key: row[key] for key in ("status", "complete", "blocking_failures")}}
    return entries, rows, reused


def version_check(folder: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """`checks.manufacturing` of the built version in `folder`, and each part's read-back manufacturing shape.

    The custom-made parts come from the version's record snapshot. Each manifest entry's files are
    judged again by `inspect`, bound to the manifest's hashes. A version built before manufacturing
    outputs warns as incomplete without blocking."""
    import yaml

    entries = manifest.get("manufacturing")
    snapshot = folder / "record"
    if not isinstance(entries, Mapping) or not snapshot.is_dir():
        return {"status": "warn", "blocking": False, "complete": False, "blocking_failures": [], "parts": {},
                "message": "this version was built before manufacturing outputs; build a new version"}, {}
    layout = load_layout()
    records: dict[str, Any] = {}
    for path in sorted((snapshot / "parts").glob("*/part.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        records[path.parent.name] = data if isinstance(data, dict) else {}
    custom = {pn for pn, data in records.items() if layout.custom_made(data)}
    made = folder / "manufacturing"
    on_disk = {path.name for path in made.iterdir()} if made.is_dir() else set()
    hw, label = folder.parent.parent, folder.name
    rows: dict[str, Any] = {}
    shapes: dict[str, Any] = {}
    for pn in sorted(custom | set(entries) | on_disk):
        entry = entries.get(pn)
        try:
            expected_path = _relative(hw, label, pn)
        except layout.LayoutError:
            expected_path = None
        if entry is None:
            why = ("is custom-made in the record snapshot but has no manufacturing entry" if pn in custom
                   else "has a manufacturing folder but no manifest entry")
        elif pn not in custom:
            why = "has a manufacturing entry but is not custom-made in the record snapshot"
        elif entry.get("path") not in (None, expected_path):
            why = f"has its manufacturing entry at {entry['path']!r}, not {expected_path!r}"
        elif entry.get("path") is None and pn in on_disk:
            why = "has no manufacturing output in its entry but a manufacturing folder"
        else:
            why = None
        if why is not None or entry is None:
            rows[pn] = {"status": "fail", "complete": False, "blocking_failures": ["inventory"], "message": f"{pn} {why}"}
        elif entry.get("path") is None:
            rows[pn] = {"status": "warn", "complete": False, "blocking_failures": [], "message": entry.get("message"),
                        **({"coverage": entry["coverage"]} if "coverage" in entry else {})}
        else:
            rows[pn], shape = inspect(folder / entry["path"], entry, records[pn], required(entry), entry["files"])
            if shape is not None:
                shapes[pn] = shape
    blocking = sorted(pn for pn, row in rows.items() if row["status"] == "fail")
    complete = all(row["complete"] for row in rows.values())
    return {"status": "fail" if blocking else ("pass" if complete else "warn"), "blocking": True,
            "complete": complete, "blocking_failures": blocking, "parts": rows}, shapes
