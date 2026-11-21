#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

"""Manufacturing guidelines: discovery, validation and layered resolution of design limits.

A guideline set is a YAML file that follows manufacturing-guideline.schema.json. The checks ask one
question through `resolve`: what limit holds for this characteristic on this capability profile and
material, how far is it trusted, and where does it come from? Load this module by path and register
it in sys.modules as `cad_record_guidelines`. PyYAML and jsonschema are imported only when a file is
read.

    catalog = discover(hw)                          # the project's guidelines, then the shipped ones
    found = resolve(record, op, "min_wall", catalog=catalog, material="tst_polymer_printed")
    found.status        # calibrated | guideline | unknown
    found.value         # a Quantity (>=, <=, between) or the categorical requirement (equals, one_of)
    found.source        # the source a criterion cites: the calibration's, or a `guideline` source
                        # (id, revision, sha256 of the file), never the inner citations
    found.provenance    # value, units, layer, id@revision, path, citations, status and shadowed values
    found.warnings      # relaxations taken, relaxations refused, scope facts that were unknown
    found.reasons       # why the status is unknown

Discovery reads two directories and nothing else: the project's hw/manufacturing/guidelines/ and the
shipped skills/cad-record/assets/manufacturing/guidelines/. It never reads a user or home directory.
A file that fails validation is refused by name (`Catalog.refused`) and its id is blocked, so a broken
project override never falls back silently to the shipped file of that id. A project file with the
same id as a shipped file shadows it; both revisions are reported (`Catalog.shadows`).

Resolution, per characteristic:
  1. A calibration of the capability profile (L4, manufacturing_intent.capability_limit) beats every
     guideline and resolves as `calibrated`. A guideline is never `calibrated`.
  2. Otherwise every limit of the applicable guideline sets is a candidate. A set is applicable when
     its scope matches (every selector). A set that the profile's `guidelines` pin names (id@revision)
     is a machine set (L3): it outranks every unpinned set, whatever their scope, and is applicable on
     the same terms. A pin naming another revision of a set excludes that set and warns. Among unpinned
     sets, specificity is the number of selectors beyond `process`, never the load order. The highest
     ranked candidate wins.
  3. A more specific limit may only tighten. One that relaxes needs `relaxes` and is taken with a
     warning; without it the limit is ignored and reported. Candidates of equal specificity that
     disagree keep the intersection (the stricter value); an empty one is unknown.
  4. {relative_to, factor, floor} resolves against the profile's line_width parameter, else its
     nozzle diameter, and never falls below the floor. With neither it is unknown.
  5. A scope selector whose profile or material fact is unknown makes a numeric limit not apply (a
     warning). It makes a categorical limit (equals, one_of: enclosure, nozzle_material) unknown: such
     a requirement never falls through to a lower layer.
  6. A one_of limit on nozzle_diameter lists discrete sizes (`quantities`), compared as whole micrometres, never
     as floats. A set meeting a range (>=, <=, <, >, between), in either layer order or at equal specificity,
     resolves to the members the range admits: a tightening, with no warning; the dropped sizes go in the
     trace. Equally specific limits that differ and come from at least two different guidelines (by the
     guideline that defines the limit, so children inheriting one parent count as one source) are recorded as
     disputed in the trace, unless one of them states the kept intersection; one guideline stating both a set and a range is not disputed. An empty result is unknown with a conflict reason. `relaxes` replaces in both directions. Its
     scope facts follow rule 5 as for any requirement.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parent.parent
SCHEMAS = SKILL / "schemas"
SCHEMA = "manufacturing-guideline.schema.json"
SHIPPED = SKILL / "assets" / "manufacturing" / "guidelines"
DEFAULT_SHIPPED = SHIPPED  # SHIPPED is the directory discover reads; tests point it elsewhere
SHIPPED_COPY = "shipped--"  # file-name prefix of a shipped set copied into a version's record/
PROJECT_RELATIVE = Path("manufacturing") / "guidelines"
CALIBRATED_KINDS = frozenset(("calibration", "test", "supplier"))
NUMERIC = frozenset((">=", "<=", "between"))
SCOPE_FACTS = ("material_family", "filler", "shore", "nozzle_material")
LOWER_OPERATORS = (">=", ">")  # one-sided limits, read as manufacturing_intent.LOWER_BOUND_OPERATORS and UPPER_BOUND_OPERATORS
UPPER_OPERATORS = ("<=", "<")

# REMOVE WITH THE FLAG when calibration is released (dp2). Owner: Package E. Registered in docs/engineering.md.
# While it is off (the release behaviour) a recorded printer calibration is ignored and generic guideline values
# govern. Only the string "1" turns it on, as for CAD_REQUIRE_FREECAD. The name lives in this one constant; every
# check, recipe and seal reads it through the functions below, never from the process environment directly.
CALIBRATION_FLAG = "CAD_CALIBRATION"
COUPON_REMEDY = "for a tighter limit, print a test coupon on the target printer"
MICROMETRE_TOLERANCE = 1e-6  # a conversion such as 0.563 mm -> 562.9999999999999 um is the whole size 563 um


class GuidelineError(ValueError):
    """A call this module refuses."""


def _sibling(name: str, filename: str) -> Any:
    """A cad-record module beside this one, loaded once by path and registered in sys.modules as `name`."""
    import importlib.util  # noqa: PLC0415 - needed only to read files

    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
        if spec is None or spec.loader is None:
            raise ImportError(f"{filename} not found beside {Path(__file__).name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return sys.modules[name]


def _intent() -> Any:
    return _sibling("cad_record_manufacturing_intent", "manufacturing_intent.py")


# --- the catalog -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Guideline:
    id: str
    revision: int
    title: str
    extends: tuple[str, ...]
    abstract: bool
    scope: Mapping[str, Any]
    limits: tuple[Mapping[str, Any], ...]
    path: str  # as discovered: relative to hw/ for the project, relative to the skill for the shipped sets
    origin: str  # project or shipped
    sha256: str
    file: Path | None = None  # where the file is on disk, so a build can copy its bytes

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.revision}"

    @property
    def specificity(self) -> int:
        return len([key for key in self.scope if key != "process"])

    @property
    def layer(self) -> str:
        """generic (L0), material (L1) or material_nozzle (L2), from what the scope selects. A machine (L3) is
        reached only through the capability profile's pin."""
        if "nozzle_diameter" in self.scope or "nozzle_material" in self.scope:
            return "material_nozzle"
        return "material" if self.specificity else "generic"


@dataclass(frozen=True)
class Refusal:
    path: str
    id: str | None
    problems: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.path} is refused: {'; '.join(self.problems)}"


@dataclass(frozen=True)
class Shadow:
    """A guideline set that a set of the same id in a higher directory hides."""

    id: str
    winner: Guideline
    hidden: tuple[Guideline, ...]

    def __str__(self) -> str:
        return f"{self.id}@{self.winner.revision} ({self.winner.path}) shadows " + ", ".join(
            f"{g.ref} ({g.path})" for g in self.hidden)


@dataclass(frozen=True)
class Catalog:
    guidelines: Mapping[str, Guideline] = field(default_factory=dict)
    shadows: tuple[Shadow, ...] = ()
    refused: tuple[Refusal, ...] = ()
    blocked: frozenset[str] = frozenset()  # ids a refused file declared: they resolve to nothing

    def digests(self) -> dict[str, str]:
        """id@revision -> sha256 of the file, for every guideline set of the catalog."""
        return {g.ref: g.sha256 for g in sorted(self.guidelines.values(), key=lambda g: g.id)}


def validate(data: Any) -> list[str]:
    """The reasons a parsed guideline file is invalid: its schema and the rules a schema cannot state."""
    schemas = _sibling("cad_record_schema_registry", "schema_registry.py")
    validator = schemas.document_validator(SCHEMAS, SCHEMA)
    problems = [f"{'/'.join(str(p) for p in e.absolute_path) or '.'}: {e.message}"
                for e in sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path])]
    if problems:
        return problems
    mi = _intent()
    scope = data["scope"]
    if "nozzle_diameter" in scope:
        problems.extend(_quantity_problems(mi, "scope/nozzle_diameter", scope["nozzle_diameter"]))
    for index, limit in enumerate(data["limits"]):
        problems.extend(_limit_problems(mi, index, limit))
    return problems


def _limit_problems(mi: Any, index: int, limit: Mapping[str, Any]) -> list[str]:
    """The quantities of one limit that the resolver could not read: its value, and a relative factor and floor."""
    problems: list[str] = []
    if "quantity" in limit:
        problems.extend(_quantity_problems(mi, f"limits/{index}/quantity", limit["quantity"]))
    if "quantities" in limit:
        problems.extend(_sizes_problems(mi, f"limits/{index}/quantities", limit["quantities"]))
    relative = limit.get("relative")
    if relative is None:
        return problems
    if not math.isfinite(relative["factor"]):
        problems.append(f"limits/{index}/relative/factor: {relative['factor']} is not finite")
    floor_problems = _quantity_problems(mi, f"limits/{index}/relative/floor", relative["floor"])
    problems.extend(floor_problems)
    if not floor_problems and mi.quantity(relative["floor"]).dimension != "length":
        problems.append(f"limits/{index}/relative: the floor of a line-width limit is a length")
    return problems


def _sizes_problems(mi: Any, where: str, items: Sequence[Mapping[str, Any]]) -> list[str]:
    """Why a discrete size set is invalid: an item the resolver cannot read, one that is not a whole number of micrometres,
    or two that are the same size after conversion (0.4 mm and 400 um)."""
    problems: list[str] = []
    seen: dict[int, int] = {}
    for position, item in enumerate(items):
        problem = _quantity_problems(mi, f"{where}/{position}", item)
        if problem:
            problems.extend(problem)
            continue
        exact = mi.quantity(item).to("um").value
        if abs(exact - round(exact)) > MICROMETRE_TOLERANCE:
            problems.append(f"{where}/{position}: {exact:g} um is not a whole number of micrometres")
        elif round(exact) in seen:
            problems.append(f"{where}/{position}: duplicate of {where}/{seen[round(exact)]} ({round(exact)} um)")
        else:
            seen[round(exact)] = position
    return problems


def _quantity_problems(mi: Any, where: str, data: Any) -> list[str]:
    """Why `data` is not a quantity the resolver can read: an unknown unit, a number that is not finite or a reversed interval."""
    try:
        mi.quantity(data)
    except mi.IntentError as error:
        return [f"{where}: {error}"]
    return []


def _read(path: Path, shown: str, origin: str) -> tuple[Guideline | None, Refusal | None]:
    import yaml  # noqa: PLC0415 - needed only to read files

    loader = _sibling("cad_record_yaml_loader", "yaml_loader.py")
    try:
        raw = path.read_bytes()
        data = loader.safe_load(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return None, Refusal(shown, None, (f"cannot read the file: {error}",))
    ident = data.get("id") if isinstance(data, Mapping) and isinstance(data.get("id"), str) else None
    try:
        problems = validate(data)
    except ImportError:
        raise  # a validator that is missing is an infrastructure failure, never a refusal of the file
    except Exception as error:  # noqa: BLE001 - a value the checks cannot read is still a refusal by name
        problems = [f"cannot check the file: {error}"]
    if problems:
        return None, Refusal(shown, ident, tuple(problems))
    return Guideline(data["id"], data["revision"], data["title"], tuple(data.get("extends", ())), bool(data.get("abstract", False)),
                     _freeze(data["scope"]), tuple(_freeze(limit) for limit in data["limits"]), shown, origin,
                     hashlib.sha256(raw).hexdigest(), path), None


def _freeze(value: Any) -> Any:
    return _intent().freeze(value)


def _scan(directory: Path, base: Path, origin: str, copies: bool = False) -> tuple[dict[str, Guideline], list[Refusal]]:
    """The valid sets of one directory by id, and a refusal for each file that is invalid or repeats an id. A file named
    shipped--* is the copy of a shipped set that a version build writes: only a snapshot (`copies`) may hold one."""
    found: dict[str, Guideline] = {}
    refused: list[Refusal] = []
    for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else ():
        shown = path.relative_to(base).as_posix()
        if path.name.startswith(SHIPPED_COPY) and not copies:
            refused.append(Refusal(shown, None, (f"{SHIPPED_COPY}* names are reserved for the shipped sets a version build copies",)))
            continue
        guideline, refusal = _read(path, shown, origin)
        if refusal is not None:
            refused.append(refusal)
        elif guideline is not None and guideline.id in found:
            refused.append(Refusal(shown, guideline.id, (f"{guideline.id} is already defined by {found[guideline.id].path}",)))
        elif guideline is not None:
            found[guideline.id] = guideline
    return found, refused


def _refuse_key_collisions(winners: dict[str, Guideline]) -> list[Refusal]:
    """Remove from `winners` each set whose packet key an earlier id already has, and refuse it."""
    seen: dict[str, str] = {}
    refused: list[Refusal] = []
    for ident in sorted(winners):
        stem = ident.replace(".", "_")
        if stem not in seen:
            seen[stem] = ident
            continue
        gone = winners.pop(ident)
        refused.append(Refusal(gone.path, ident, (f"{packet_key(gone.ref)} is also the packet key of {seen[stem]}",)))
    return refused


def discover(hw: Path | None, *, shipped: Path | None = None, snapshot: bool = False) -> Catalog:
    """The catalog of the project's guideline sets (`hw`/manufacturing/guidelines/, when `hw` is given) over the
    shipped ones (`shipped`, default SHIPPED). No other directory is read. Every file is validated before it loads.
    Two ids whose packet keys collide (fdm.a.b and fdm.a_b) refuse the later id. With `snapshot`, `hw` is a version's
    record/ folder: only its own copies are read, shipped sets included, because a build copied them there."""
    shipped = SHIPPED if shipped is None else shipped
    layers: list[tuple[str, Path, Path]] = []
    if hw is not None:
        layers.append(("project", Path(hw) / PROJECT_RELATIVE, Path(hw)))
    if not snapshot:
        layers.append(("shipped", Path(shipped), SKILL.parent.parent if Path(shipped) == DEFAULT_SHIPPED else Path(shipped)))
    winners: dict[str, Guideline] = {}
    hidden: dict[str, list[Guideline]] = {}
    refused: list[Refusal] = []
    blocked: set[str] = set()
    for origin, directory, base in layers:
        found, refusals = _scan(directory, base, origin, copies=snapshot)
        refused.extend(refusals)
        blocked.update(r.id for r in refusals if r.id is not None)
        for ident, guideline in found.items():
            if ident in winners:
                hidden.setdefault(ident, []).append(guideline)
            elif ident not in blocked:
                winners[ident] = guideline
    collisions = _refuse_key_collisions(winners)
    refused.extend(collisions)
    blocked.update(r.id for r in collisions if r.id is not None)
    shadows = tuple(Shadow(ident, winners[ident], tuple(items)) for ident, items in sorted(hidden.items()) if ident in winners)
    return Catalog(winners, shadows, tuple(refused), frozenset(blocked))


def snapshot_files(hw: Path) -> dict[str, bytes]:
    """The shipped sets a version build must copy beside the project's own (which the layout's `version.record` entry already
    names): the winning shipped sets, byte for byte, as manufacturing/guidelines/shipped--<file name>. A shipped set that a
    project set shadows is not copied. A snapshot read then needs no shipped directory."""
    return {f"{PROJECT_RELATIVE.as_posix()}/{SHIPPED_COPY}{g.file.name}": g.file.read_bytes()
            for g in sorted(discover(hw).guidelines.values(), key=lambda g: g.id) if g.origin == "shipped" and g.file is not None}


def calibration_enabled() -> bool:
    """Whether the calibration flag is on. Read it once per job and pass the value on."""
    return os.getenv(CALIBRATION_FLAG) == "1"


def calibration_recipe(enabled: bool) -> dict[str, dict[str, str]]:
    """The recipe `flags` entry that seals an enabled flag, so a bundle made under another setting is never reused.
    Empty while the flag is off: a job run without it keeps the recipe shape it had before the flag."""
    return {"flags": {CALIBRATION_FLAG: "1"}} if enabled else {}


def calibration_dependencies(enabled: bool) -> dict[str, str]:
    """The packet dependency that seals an enabled flag (a sha256 of `NAME=1`); empty while it is off."""
    if not enabled:
        return {}
    return {"flag_" + CALIBRATION_FLAG.lower(): hashlib.sha256(f"{CALIBRATION_FLAG}=1".encode("ascii")).hexdigest()}


def packet_key(ref: str) -> str:
    """The dependency key of a resolved set in a check-result packet: guideline_<id with . as _>_r<revision>. The check-result
    schema accepts semantic ids as keys, and a dotted id or an `@` would fail it."""
    ident, _, revision = ref.partition("@")
    return f"guideline_{ident.replace('.', '_')}_r{revision}"


def packet_dependencies(digests: Mapping[str, str]) -> dict[str, str]:
    """packet key -> sha256 for id@revision -> sha256. GuidelineError when two refs give one key."""
    found: dict[str, str] = {}
    owner: dict[str, str] = {}
    for ref in sorted(digests):
        key = packet_key(ref)
        if key in owner:
            raise GuidelineError(f"{key} is the key of both {owner[key]} and {ref}")
        owner[key] = ref
        found[key] = digests[ref]
    return found


# --- the scope a limit is asked in -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Context:
    """What a profile and material say about the scope selectors. A fact that is None is unknown."""

    process: str | None
    facts: Mapping[str, str | None] = field(default_factory=dict)  # material_family, filler, shore, nozzle_material
    nozzle_diameter_mm: float | None = None
    line_width_mm: float | None = None
    pin: tuple[str, ...] | None = None  # id@revision allowlist of the profile; None: every matching set applies

    @property
    def base_mm(self) -> float | None:
        """What a line-width multiple multiplies: the line width, else the nozzle diameter."""
        return self.line_width_mm if self.line_width_mm is not None else self.nozzle_diameter_mm


def _text_fact(data: Any) -> str | None:
    if not isinstance(data, Mapping):
        return None
    if data.get("status") == "known" and isinstance(data.get("value"), str):
        return str(data["value"])
    return "none" if data.get("status") == "not_applicable" else None


def _millimetres(fact: Any) -> float | None:
    quantity: Any = getattr(fact, "value", None)
    if getattr(fact, "known", False) and hasattr(quantity, "to") and quantity.dimension == "length" and quantity.value is not None:
        converted: Any = quantity.to("mm")
        return float(converted.value)
    return None


def context_for(profile: Any, material: Any = None) -> Context:
    """The Context of a capability profile and, when given, a material profile (manufacturing_intent classes)."""
    facts: dict[str, str | None] = {name: None for name in SCOPE_FACTS}
    raw = material.raw if material is not None else {}
    for name in ("material_family", "filler", "shore"):
        facts[name] = _text_fact(raw.get(name))
    nozzle_material = profile.parameters.get("nozzle_material")
    facts["nozzle_material"] = str(nozzle_material.value) if nozzle_material is not None and nozzle_material.known \
        and isinstance(nozzle_material.value, str) else None
    pin = profile.raw.get("guidelines")
    return Context(profile.process, facts, _millimetres(profile.parameters.get("nozzle_diameter")),
                   _millimetres(profile.parameters.get("line_width")), tuple(pin) if pin else None)


def _matches(scope: Mapping[str, Any], context: Context) -> bool | None:
    """True when every selector matches, False when one does not, None when none fails but a fact is unknown."""
    unknown = False
    if context.process is not None and scope["process"] != context.process:
        return False
    for name in SCOPE_FACTS:
        if name not in scope:
            continue
        have = context.facts.get(name)
        if have is None:
            unknown = True
        elif have not in scope[name]:
            return False
    if "nozzle_diameter" in scope:
        if context.nozzle_diameter_mm is None:
            unknown = True
        else:
            interval = _intent().quantity(scope["nozzle_diameter"]).to("mm")
            lower, upper = interval.bounds
            if not lower - 1e-9 <= context.nozzle_diameter_mm <= upper + 1e-9:
                return False
    return None if unknown else True


def _unknown_selectors(scope: Mapping[str, Any], context: Context) -> list[str]:
    names = [name for name in SCOPE_FACTS if name in scope and context.facts.get(name) is None]
    if "nozzle_diameter" in scope and context.nozzle_diameter_mm is None:
        names.append("nozzle_diameter")
    return names


def _closure(catalog: Catalog, ident: str, seen: tuple[str, ...] = (), context: Context | None = None) -> list[Guideline]:
    """`ident` and its parents, parents first; GuidelineError for a cycle, a parent that is missing or refused, or (with
    `context`) a set whose revision the profile's pin names differently: the pin holds across the whole extends closure."""
    if ident in seen:
        raise GuidelineError(f"{' -> '.join((*seen, ident))} extends itself")
    if ident in catalog.blocked:
        raise GuidelineError(f"{ident} is refused (see the refused files), so nothing resolves through it")
    guideline = catalog.guidelines.get(ident)
    if guideline is None:
        raise GuidelineError(f"{ident} is not a known guideline set")
    if context is not None and not _allowed(guideline, context):
        pinned = next(pin.split("@", 1)[1] for pin in context.pin or () if pin.startswith(f"{ident}@"))
        raise GuidelineError(f"{ident} is pinned at revision {pinned}, but the catalog has revision {guideline.revision}")
    chain: list[Guideline] = []
    for parent in guideline.extends:
        chain.extend(g for g in _closure(catalog, parent, (*seen, ident), context) if g not in chain)
    chain.append(guideline)
    return chain


def _pinned(guideline: Guideline, context: Context) -> bool:
    """Whether the capability profile's `guidelines` pin names exactly this revision of this set."""
    return context.pin is not None and guideline.ref in context.pin


def _allowed(guideline: Guideline, context: Context) -> bool:
    """False when the pin names another revision of the set: a pin fixes the revision, so a build is reproducible."""
    if context.pin is None:
        return True
    return guideline.ref in context.pin or all(not pin.startswith(f"{guideline.id}@") for pin in context.pin)


def _layer(guideline: Guideline, context: Context) -> str:
    """machine (L3) for a pinned set, else the layer its scope selects."""
    return "machine" if _pinned(guideline, context) else guideline.layer


def pin_warnings(catalog: Catalog, context: Context) -> list[str]:
    """The pin entries that match no set of the catalog: absent, refused or another revision."""
    found = []
    for pin in context.pin or ():
        ident = pin.split("@", 1)[0]
        have = catalog.guidelines.get(ident)
        if have is None:
            found.append(f"pinned {pin} is not in the catalog")
        elif have.ref != pin:
            found.append(f"pinned {pin}, but the catalog has revision {have.revision}: not applied")
    return found


def applicable(catalog: Catalog, context: Context) -> list[Guideline]:
    """The sets that apply to `context`, or may once an unknown scope fact is known, in id order. An abstract set applies
    only when the profile's pin names it (a machine set); otherwise it is reached only through `extends`."""
    return [g for g in sorted(catalog.guidelines.values(), key=lambda g: g.id)
            if (not g.abstract or _pinned(g, context)) and _allowed(g, context) and _matches(g.scope, context) is not False]


def applicable_digests(catalog: Catalog, context: Context) -> dict[str, str]:
    """id@revision -> sha256 of every set the context can resolve through, its parents included. A change to
    one of them is a change to the limits the context resolves."""
    found: dict[str, str] = {}
    for guideline in applicable(catalog, context):
        try:
            chain = _closure(catalog, guideline.id, context=context)
        except GuidelineError:
            chain = [guideline]
        found.update({g.ref: g.sha256 for g in chain})
    return dict(sorted(found.items()))


def record_digests(record: Any, catalog: Catalog) -> dict[str, dict[str, dict[str, str]]]:
    """capability profile id -> material profile name -> applicable_digests of that profile with that material. A record
    without materials has the one name "" (the profile alone). A profile that no set can resolve through is left out; one
    that some material resolves sets for lists every material, so that a material with none is not read as unknown."""
    found: dict[str, dict[str, dict[str, str]]] = {}
    for ident, profile in sorted(record.capabilities.items()):
        materials: dict[str, Any] = dict(record.materials) or {"": None}
        by_material = {name: applicable_digests(catalog, context_for(profile, material))
                       for name, material in sorted(materials.items())}
        if any(by_material.values()):
            found[ident] = by_material
    return found


def digests_for(by_material: Mapping[str, Mapping[str, str]] | None, names: Sequence[str]) -> dict[str, str]:
    """id@revision -> sha256 of the sets that apply to a part made of the material profiles `names`: the union of their
    entries in `by_material` (one capability profile's record_digests). A part that names no material, or one the record
    does not hold, may be made of any of them, so it takes every entry: it goes stale more often, never less."""
    pool = by_material or {}
    chosen = list(names) if names and all(name in pool for name in names) else list(pool)
    return dict(sorted({ref: sha for name in chosen for ref, sha in pool[name].items()}.items()))


# --- values ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Shadowed:
    value: str
    guideline: str  # id@revision
    layer: str
    why: str


@dataclass(frozen=True)
class Provenance:
    value: Any
    units: str | None
    layer: str  # L4 calibration, or generic, material, material_nozzle
    guideline: str  # id@revision, or the calibration id
    path: str
    citations: tuple[Mapping[str, Any], ...]
    status: str  # guideline or calibrated
    shadowed: tuple[Shadowed, ...] = ()


@dataclass(frozen=True)
class Resolved:
    characteristic: str
    status: str  # calibrated, guideline or unknown
    operator: str | None = None
    value: Any = None
    source: Mapping[str, Any] | None = None
    provenance: tuple[Provenance, ...] = ()
    warnings: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Bound:
    """A limit as a constraint: a numeric interval in `units` whose ends are each open or closed, or a set of allowed
    categorical values. The ends are infinite for a one-sided limit (>=, >, <=, <) and finite for `between`. Openness is
    kept through intersection and containment, so `< 5` is tighter than `<= 5` and a closed `between 3..5` never loosens
    `< 5` unnoticed."""

    interval: tuple[float, float] | None
    units: str | None
    options: frozenset[Any] | None  # categorical values, or whole micrometres when `units` is "um"
    lower_open: bool = False
    upper_open: bool = False

    @property
    def operator(self) -> str | None:
        """The operator that states a one-sided bound (>=, >, <=, <), else None."""
        if self.interval is None:
            return None
        lower, upper = self.interval
        if upper == float("inf") and lower != -float("inf"):
            return ">" if self.lower_open else ">="
        if lower == -float("inf") and upper != float("inf"):
            return "<" if self.upper_open else "<="
        return None

    def statable(self) -> bool:
        """Whether `_statement` can state this bound: a categorical set, a one-sided bound or a closed interval."""
        return self.interval is None or self.operator is not None or not (self.lower_open or self.upper_open)

    def one_sided(self) -> tuple[str, float] | None:
        """The (operator, value) pair of a one-sided numeric bound, else None."""
        operator = self.operator
        if operator is None or self.interval is None:
            return None
        return operator, self.interval[0] if operator in LOWER_OPERATORS else self.interval[1]

    def same_side(self, other: _Bound) -> tuple[tuple[str, float], tuple[str, float]] | None:
        """Both one-sided bounds as (operator, value) pairs when they bound the same side, else None."""
        mine, theirs = self.one_sided(), other.one_sided()
        if mine is None or theirs is None or (mine[0] in LOWER_OPERATORS) != (theirs[0] in LOWER_OPERATORS):
            return None
        return mine, theirs

    def intersect(self, other: _Bound) -> _Bound | None:
        """The values both bounds allow, or None when there are none (a closed end meeting an open one is empty)."""
        pair = self.same_side(other)
        if pair is not None:
            return self if _intent().tightens(pair[1], pair[0]) else other
        if self.interval is not None and other.interval is not None:
            lower, lower_open = _higher_lower(self, other)
            upper, upper_open = _lower_upper(self, other)
            if lower > upper or (lower == upper and (lower_open or upper_open)):
                return None
            return _Bound((lower, upper), self.units, None, lower_open, upper_open)
        if self.options is not None and other.options is not None:
            both = self.options & other.options
            return _Bound(None, self.units, both) if both else None
        for sizes, interval in ((self, other), (other, self)):
            if sizes.sizes_set() and interval.interval is not None:
                inside = frozenset(size for size in sizes.options or () if interval.admits(size))
                return _Bound(None, sizes.units, inside) if inside else None
        return None

    def sizes_set(self) -> bool:
        """Whether this bound is a discrete set of sizes in whole micrometres, not a categorical set."""
        return self.options is not None and self.units == "um"

    def meets(self, other: _Bound) -> bool:
        """Whether one of the two is a discrete set of sizes and the other a numeric interval."""
        return (self.sizes_set() and other.interval is not None) or (other.sizes_set() and self.interval is not None)

    def admits(self, micrometres: int) -> bool:
        """Whether a whole number of micrometres lies in this numeric interval, its open ends excluded."""
        assert self.interval is not None and self.units is not None
        mi = _intent()
        lower, upper = (round(mi.convert(end, self.units, "um"), 6) for end in self.interval)
        return (lower < micrometres or (lower == micrometres and not self.lower_open)) and \
            (micrometres < upper or (micrometres == upper and not self.upper_open))

    def within(self, other: _Bound) -> bool:
        """Whether this bound is as strict as `other` or stricter: everything it allows, `other` allows too. Two one-sided
        bounds on the same side are ordered by manufacturing_intent.tightens; otherwise each end must be at least as tight."""
        pair = self.same_side(other)
        if pair is not None:
            return bool(_intent().tightens(pair[1], pair[0]))
        if self.interval is not None and other.interval is not None:
            lower, upper = self.interval
            other_lower, other_upper = other.interval
            lower_ok = lower > other_lower or (lower == other_lower and (self.lower_open or not other.lower_open))
            upper_ok = upper < other_upper or (upper == other_upper and (self.upper_open or not other.upper_open))
            return lower_ok and upper_ok
        if self.sizes_set() and other.interval is not None:
            return all(other.admits(size) for size in self.options or ())
        return self.options is not None and other.options is not None and self.options <= other.options


def _higher_lower(first: _Bound, second: _Bound) -> tuple[float, bool]:
    """The tighter lower end of two intervals, and whether it is open."""
    assert first.interval is not None and second.interval is not None
    a, b = first.interval[0], second.interval[0]
    if a == b:
        return a, first.lower_open or second.lower_open
    return (a, first.lower_open) if a > b else (b, second.lower_open)


def _lower_upper(first: _Bound, second: _Bound) -> tuple[float, bool]:
    """The tighter upper end of two intervals, and whether it is open."""
    assert first.interval is not None and second.interval is not None
    a, b = first.interval[1], second.interval[1]
    if a == b:
        return a, first.upper_open or second.upper_open
    return (a, first.upper_open) if a < b else (b, second.upper_open)


Key = tuple[int, int, int, int]


@dataclass(frozen=True)
class _Candidate:
    guideline: Guideline  # the applicable set the limit is read through
    defined_in: Guideline  # the set that states it
    limit: Mapping[str, Any]
    bound: _Bound
    key: Key  # pinned (machine), scope specificity, applies_to, then minus the distance from the applicable set
    shown: str
    layer: str


def _bound(limit: Mapping[str, Any], context: Context, units: str | None) -> tuple[_Bound | None, str | None]:
    """The limit as a bound, or why it cannot be one. `units` is the unit to state numbers in (None: its own)."""
    mi = _intent()
    operator = limit["operator"]
    if "quantities" in limit:
        return _Bound(None, "um", frozenset(round(mi.quantity(item).to("um").value) for item in limit["quantities"])), None
    if operator in ("equals", "one_of"):
        value = limit["value"]
        return _Bound(None, None, frozenset(value if isinstance(value, (list, tuple)) else [value])), None
    infinity = float("inf")
    if "relative" in limit:
        relative = limit["relative"]
        base = context.nozzle_diameter_mm if relative["relative_to"] == "nozzle_diameter" else context.base_mm
        if base is None:
            return None, ("the profile states no nozzle diameter" if relative["relative_to"] == "nozzle_diameter"
                          else "the profile states neither a line width nor a nozzle diameter")
        floor = mi.quantity(relative["floor"])
        number = round(max(float(relative["factor"]) * base, floor.to("mm").value), 9)
        if not math.isfinite(number):
            return None, f"the computed limit {number} mm is not finite"
        quantity = mi.Quantity("mm", value=number)
    else:
        quantity = mi.quantity(limit["quantity"])
    unit = units or quantity.units
    try:
        lower, upper = quantity.to(unit).bounds
    except mi.IntentError as error:
        return None, str(error)
    if operator in LOWER_OPERATORS:
        return _Bound((lower, infinity), unit, None, operator == ">"), None
    if operator in UPPER_OPERATORS:
        return _Bound((-infinity, upper), unit, None, False, operator == "<"), None
    if operator == "between":
        return _Bound((lower, upper), unit, None), None
    return None, f"operator {operator} is not supported"


def _show(bound: _Bound) -> str:
    if bound.sizes_set():
        return ", ".join(f"{size} um" for size in sorted(bound.options or ()))
    if bound.options is not None:
        return ", ".join(sorted(str(o) for o in bound.options))
    assert bound.interval is not None
    lower, upper = bound.interval
    if bound.operator is not None:
        return f"{bound.operator} {lower if bound.operator in LOWER_OPERATORS else upper:g} {bound.units}"
    opening, closing = ("(" if bound.lower_open else "["), (")" if bound.upper_open else "]")
    return f"{opening}{lower:g}..{upper:g}{closing} {bound.units}"


def _statement(bound: _Bound, mi: Any) -> tuple[str, Any]:
    """The operator and value that state a bound; `statable` says whether it has one."""
    if bound.sizes_set():
        return "one_of", [mi.Quantity("um", value=float(size)) for size in sorted(bound.options or ())]
    if bound.options is not None:
        values = sorted(bound.options, key=str)
        return ("equals", values[0]) if len(values) == 1 else ("one_of", values)
    assert bound.interval is not None
    lower, upper = bound.interval
    operator = bound.operator
    if operator is not None:
        return operator, mi.Quantity(bound.units, value=lower if operator in LOWER_OPERATORS else upper)
    return "between", mi.Quantity(bound.units, lower=lower, upper=upper)


def _applies(limit: Mapping[str, Any], orientation: str | None, feature: str | None) -> int | None:
    """None when the limit's applies_to excludes the request, else how many of its keys the request matched."""
    wanted = limit.get("applies_to", {})
    asked = {"orientation": orientation, "feature": feature}
    if any(asked[key] != value for key, value in wanted.items()):
        return None
    return len(wanted)


def _candidates_of(guideline: Guideline, defined: Guideline, distance: int, context: Context, characteristic: str,
                   orientation: str | None, feature: str | None, notes: tuple[list[str], list[str]]) -> list[_Candidate]:
    """The candidates `defined`'s limits give when read through `guideline`, `distance` sets above it in its extends chain
    (0 for its own limits). A requirement under an unknown scope fact adds to notes[0] (uncertain), a numeric limit under
    one adds to notes[1] (warnings)."""
    matched = _matches(guideline.scope, context)
    found: list[_Candidate] = []
    for limit in defined.limits:
        depth = _applies(limit, orientation, feature) if limit["characteristic"] == characteristic else None
        if depth is None:
            continue
        if matched is not None:
            found.append(_Candidate(guideline, defined, limit, _Bound(None, None, None),
                                    (int(_pinned(guideline, context)), guideline.specificity, depth, -distance), "", _layer(guideline, context)))
            continue
        names = ", ".join(_unknown_selectors(guideline.scope, context))
        if limit["operator"] in ("equals", "one_of"):
            notes[0].append(f"{guideline.ref} applies only if {names} (unknown) match")
        else:
            notes[1].append(f"{guideline.ref} {characteristic} not applied: {names} unknown")
    return found


def _prepare(candidates: list[_Candidate], context: Context, characteristic: str) -> tuple[list[_Candidate], list[tuple[Key, str]]]:
    """The candidates as bounds in one unit, and the rank and reason of each that cannot be one."""
    mi = _intent()
    units = None
    for candidate in candidates:
        if "quantity" in candidate.limit or "relative" in candidate.limit:
            units = mi.quantity(candidate.limit["quantity"] if "quantity" in candidate.limit else candidate.limit["relative"]["floor"]).units
            break
    ready: list[_Candidate] = []
    unresolved: list[tuple[Key, str]] = []
    for candidate in candidates:
        bound, why = _bound(candidate.limit, context, units)
        if bound is None:
            unresolved.append((candidate.key, f"{candidate.defined_in.ref} {characteristic}: {why}"))
            continue
        ready.append(_Candidate(candidate.guideline, candidate.defined_in, candidate.limit, bound, candidate.key, _show(bound),
                                candidate.layer))
    return ready, unresolved


def resolve_guidelines(catalog: Catalog, context: Context, characteristic: str, *, orientation: str | None = None,
                       feature: str | None = None) -> Resolved:
    """The guideline limit of `characteristic` for `context` (steps 2 to 5 of the module docstring)."""
    warnings: list[str] = pin_warnings(catalog, context)
    uncertain: list[str] = []
    candidates: list[_Candidate] = []
    try:
        for guideline in applicable(catalog, context):
            chain = _closure(catalog, guideline.id, context=context)
            for position, defined in enumerate(chain):
                candidates.extend(_candidates_of(guideline, defined, len(chain) - 1 - position, context, characteristic,
                                                 orientation, feature, (uncertain, warnings)))
    except GuidelineError as error:
        return Resolved(characteristic, "unknown", reasons=(str(error),))
    if uncertain:
        return Resolved(characteristic, "unknown", warnings=tuple(warnings),
                        reasons=tuple(f"{characteristic} is a requirement and a scope fact is unknown: {why}" for why in uncertain))
    ready, unresolved = _prepare(candidates, context, characteristic)
    if not ready:
        return Resolved(characteristic, "unknown", warnings=tuple(warnings),
                        reasons=tuple(why for _, why in unresolved) or (f"no applicable guideline states {characteristic}",))
    return _fold(characteristic, ready, unresolved, warnings, _intent())


def _merge(group: list[_Candidate]) -> _Bound | None:
    """The bound that satisfies every candidate of an equally specific group, or None when they share no value."""
    merged: _Bound | None = group[0].bound
    for other in group[1:]:
        merged = merged.intersect(other.bound) if merged is not None else None
    return merged


def _disputed(group: list[_Candidate], merged: _Bound) -> list[Shadowed]:
    """The trace of an equally specific group of two or more distinct defining guidelines: none states the intersection that is kept."""
    if len({c.defined_in.ref for c in group}) < 2 or any(c.bound.within(merged) and merged.within(c.bound) for c in group):
        return []
    return [Shadowed(c.shown, c.defined_in.ref, c.layer, f"equally specific and disputed; the intersection {_show(merged)} is kept")
            for c in group]


def _fold(characteristic: str, ready: list[_Candidate], unresolved: list[tuple[Key, str]], warnings: list[str],
          mi: Any) -> Resolved:
    """Tightens group by group, most general first. A candidate that cannot be a bound and outranks the group the fold
    keeps would lose silently to a looser limit, so it makes the result unknown; one that ties or trails it does not."""
    current: _Bound | None = None
    winners: list[_Candidate] = []
    shadowed: list[Shadowed] = []

    def hide(candidate: _Candidate, why: str) -> None:
        shadowed.append(Shadowed(candidate.shown, candidate.defined_in.ref, candidate.layer, why))

    for key in sorted({c.key for c in ready}):
        # by defining guideline: the result, winners[0] included, does not depend on discovery order
        group = sorted((c for c in ready if c.key == key), key=lambda c: c.defined_in.ref)
        merged = _merge(group)
        if merged is None:
            names = ", ".join(f"{c.defined_in.ref} ({c.shown})" for c in group)
            return Resolved(characteristic, "unknown", warnings=tuple(warnings),
                            reasons=(f"{characteristic}: equally specific guidelines conflict and share no value: {names}",))
        strictest = [c for c in group if c.bound.within(merged) and merged.within(c.bound)] or group
        for loser in (c for c in group if c not in strictest):
            hide(loser, "equally specific; the stricter value is kept")
        shadowed.extend(_disputed(group, merged))
        if current is not None and not merged.within(current):
            if current.meets(merged) and not all("relaxes" in c.limit for c in strictest):
                # a set meeting a range keeps the sizes the range admits: a tightening of both, whichever is more specific
                narrowed = current.intersect(merged)
                if narrowed is None:
                    names = ", ".join(f"{c.defined_in.ref} ({c.shown})" for c in [*strictest, *winners])
                    return Resolved(characteristic, "unknown", warnings=tuple(warnings),
                                    reasons=(f"{characteristic}: a discrete set and a range share no size: {names}",))
                current, winners = narrowed, [*strictest, *winners]
                continue
            if not all("relaxes" in c.limit for c in strictest):
                for ignored in strictest:
                    hide(ignored, "weaker than a less specific limit and states no relaxes")
                warnings.append(f"{characteristic}: {strictest[0].defined_in.ref} ({_show(merged)}) would relax "
                                f"{winners[0].defined_in.ref} ({_show(current)}) without `relaxes`; it is ignored")
                continue
            warnings.append(f"{characteristic} relaxed to {_show(merged)} by {strictest[0].defined_in.ref}: "
                            f"{strictest[0].limit['relaxes']['reason']}")
        for old in winners:
            hide(old, f"{'tightened' if current is None or merged.within(current) else 'relaxed'} by {strictest[0].defined_in.ref}")
        current, winners = merged, list(strictest)
    assert current is not None
    blocking = [why for key, why in unresolved if key > winners[0].key]
    if blocking:
        return Resolved(characteristic, "unknown", warnings=tuple(warnings + [f"{characteristic} not resolved: {why}" for why in blocking]),
                        reasons=tuple(blocking))
    if not current.statable():
        return Resolved(characteristic, "unknown", warnings=tuple(warnings),
                        reasons=(f"{characteristic}: the limit {_show(current)} cannot be stated with one operator "
                                 "(an interval with an open end); restate the sets so that one bound applies",))
    shadowed.extend(_dropped(current, winners))
    return _resolved(characteristic, current, winners, shadowed, warnings, mi)


def _dropped(current: _Bound, winners: list[_Candidate]) -> list[Shadowed]:
    """The sizes of each winning set that another winner leaves out of the resolved set, with the winners that dropped them."""
    if not current.sizes_set():
        return []
    found: list[Shadowed] = []
    for kept in winners:
        lost = (kept.bound.options or frozenset()) - (current.options or frozenset()) if kept.bound.sizes_set() else frozenset()
        if lost:
            by = ", ".join(f"{c.defined_in.ref} ({c.layer})" for c in winners if c is not kept)
            found.append(Shadowed(_show(_Bound(None, "um", lost)), kept.defined_in.ref, kept.layer, f"sizes dropped by {by}"))
    return found


def _resolved(characteristic: str, current: _Bound, winners: list[_Candidate], shadowed: list[Shadowed], warnings: list[str],
              mi: Any) -> Resolved:
    operator, value = _statement(current, mi)
    provenance = tuple(Provenance(_present(candidate.bound, mi), candidate.bound.units, candidate.layer, candidate.defined_in.ref,
                                  candidate.defined_in.path, tuple(_plain(c) for c in candidate.limit["citations"]),
                                  "guideline", tuple(shadowed)) for candidate in winners)
    source = {"kind": "guideline", "reference": winners[0].defined_in.id, "revision": str(winners[0].defined_in.revision),
              "sha256": winners[0].defined_in.sha256}
    return Resolved(characteristic, "guideline", operator, value, source, provenance, tuple(warnings))


def _present(bound: _Bound, mi: Any) -> Any:
    operator, value = _statement(bound, mi)
    return _dict(value)


def _dict(value: Any) -> Any:
    """A resolved value as plain data: a Quantity, or a list of them, as dictionaries."""
    if isinstance(value, list):
        return [_dict(item) for item in value]
    return value.as_dict() if hasattr(value, "as_dict") else value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


# --- the API the checks call -----------------------------------------------------------------------------------

def resolve(record: Any, op: Any, characteristic: str, *, catalog: Catalog, material: str | None = None, on: Any = None,
            orientation: str | None = None, feature: str | None = None) -> Resolved:
    """The limit of `characteristic` for route operation `op` of `record` on `material` (a material profile id).

    A calibration of the operation's capability profile (L4) beats every guideline and resolves as
    `calibrated`. Otherwise the guideline limit resolves as `guideline`, or the result is `unknown` with its
    reasons. A guideline is never `calibrated`."""
    mi = _intent()
    profile = record.capabilities.get(op.capability) if isinstance(op.capability, str) else None
    if profile is None:
        return Resolved(characteristic, "unknown", reasons=("the operation names no capability profile",))
    if material is not None:
        found = mi.capability_limit(profile, characteristic, material=material, on=on, orientation=orientation, feature=feature)
        if found.status == "calibrated" and found.calibration is not None:
            calibration = found.calibration
            source = {key: value for key, value in vars(calibration.source).items() if value is not None}
            return Resolved(characteristic, "calibrated", "between", calibration.limits, source,
                            (Provenance(calibration.limits.as_dict(), calibration.limits.units, "L4", calibration.id or profile.id,
                                        f"hw/manufacturing/capabilities.yaml#{profile.id}", (source,), "calibrated"),))
    context = context_for(profile, record.materials.get(material) if material is not None else None)
    return resolve_guidelines(catalog, context, characteristic, orientation=orientation, feature=feature)


# --- the resolution trace --------------------------------------------------------------------------------------

def trace_document(resolutions: Iterable[Resolved]) -> dict[str, Any]:
    """The resolution trace of a check: each resolved limit with its layer, id@revision, path, citations and the
    values it shadowed. A finding cites the file by hash (method.evidence), not these fields."""
    rows = []
    for item in sorted(resolutions, key=lambda r: r.characteristic):
        rows.append({"characteristic": item.characteristic, "status": item.status, "operator": item.operator,
                     "value": _dict(item.value),
                     "source": dict(item.source) if item.source else None, "warnings": list(item.warnings),
                     "reasons": list(item.reasons),
                     "provenance": [{"value": p.value, "units": p.units, "layer": p.layer, "guideline": p.guideline, "path": p.path,
                                     "citations": list(p.citations), "status": p.status,
                                     "shadowed": [vars(s) for s in p.shadowed]} for p in item.provenance]})
    return {"schema_version": 1, "resolutions": _plain(rows)}


def trace_bytes(resolutions: Iterable[Resolved]) -> bytes:
    return (json.dumps(trace_document(resolutions), indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def trace_sha256(resolutions: Sequence[Resolved]) -> str:
    return hashlib.sha256(trace_bytes(resolutions)).hexdigest()


def write_trace(directory: Path, resolutions: Sequence[Resolved]) -> Path:
    """Write the trace to `directory`/guideline-trace-<sha256 prefix>.json and return the path."""
    data = trace_bytes(resolutions)
    path = Path(directory) / f"guideline-trace-{hashlib.sha256(data).hexdigest()[:16]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
