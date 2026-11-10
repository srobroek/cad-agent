"""The closed dispatch table: which handler judges each expected obligation, and the check-result packet.

`expected(record)` is the subset of `manufacturing_intent.required_checks(record)` this producer owns,
fixed before any handler runs: every `process.<kind>.*`, `purchase.*` and `route.*` obligation and
`optical.route`, except physically_validated ones (print trials and fit coupons are physical evidence the
ledger holds; software never passes them). Every owned obligation gets exactly one outcome: a family
without a handler, and every `process.<kind>.coverage` of a kind with no check contract, is UNKNOWN,
never a pass and never another process's checks.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import additive, machining, purchased, route, secondary, shaping, sheet
from .core import (FAIL, IMPLICIT, PASS, UNKNOWN, Context, Draft, Geometry, Handler, Result, access_directions,
                   direction_key, intent, policy, stable_id, text)

PRODUCER = "cad-design form_check.py process_checks 1"
OWNED_PREFIXES = ("process.", "purchase.", "route.")
OWNED_FAMILIES = frozenset({"optical.route"})
EXCLUDED_LEVELS = frozenset({"physically_validated"})

TABLE: Mapping[str, Mapping[str, Handler]] = {
    "fdm": additive.FDM, "resin": additive.RESIN, "cnc": machining.CNC,
    "laser": sheet.LASER, "waterjet": sheet.WATERJET, "plasma": sheet.PLASMA, "sheet_forming": sheet.SHEET_FORMING,
    "casting": shaping.CASTING, "moulding": shaping.MOULDING, "extrusion": shaping.EXTRUSION,
    "drilling": secondary.DRILLING, "reaming": secondary.REAMING, "tapping": secondary.TAPPING,
    "finishing": secondary.FINISHING, "coating": secondary.COATING, "joining": secondary.JOINING,
    "inspection": secondary.INSPECTION, "purchased": purchased.PURCHASED,
}
ROUTE: Mapping[str, Handler] = {"route.consistency": route.consistency, "route.operation_plan": route.operation_plan,
                                "optical.route": route.optical}


class HandlerError(RuntimeError):
    """A handler broke the packet contract: a failing finding linked to an obligation it did not fail."""


class NotAssessable(ValueError):
    """The record cannot be assessed: a record version 1 record, before migrate.py."""


def owned(obligation: Any) -> bool:
    if obligation.level in EXCLUDED_LEVELS:
        return False
    return obligation.family in OWNED_FAMILIES or obligation.family.startswith(OWNED_PREFIXES)


def expected(record: Any) -> tuple[Any, ...]:
    """The obligations this producer must answer, enumerated from the record before anything runs."""
    if record.version != intent().RECORD_VERSION:
        problem = intent().migration_problem(dict(record.product))
        raise NotAssessable(f"{problem.message}; {problem.action}" if problem is not None
                            else "the record is not record version 2")
    return tuple(o for o in intent().required_checks(record) if owned(o))


def handler_for(family: str, kind: str | None) -> Handler | None:
    if family in ROUTE:
        return ROUTE[family]
    if family.startswith("purchase."):
        return TABLE["purchased"].get(family.split(".", 1)[1])
    if family.startswith("process.") and kind is not None:
        prefix, _, check = family.partition(".")[2].partition(".")
        if prefix == kind:
            return TABLE.get(kind, {}).get(check)
    return None


def coverage_gap(ctx: Context, obligation: Any) -> Result:
    kind = ctx.op.kind if ctx.op is not None else obligation.family.split(".")[1]
    draft = Draft("process.coverage_gap", "major", UNKNOWN, text(f"route kind {kind}"),
                  policy("route_kind", "one_of", sorted(TABLE)), "dispatch table", "no handler exists for this kind",
                  f"{ctx.label}: {kind} has no check contract, so its manufacturability is not assessed; no FDM or other "
                  "process's checks stand in, and freeze and release stay blocked",
                  remedy=f"choose a supported process for this operation, or add a {kind} check profile and handler",
                  recheck=[f"process.{kind}"], earliest_stage="parts")
    return Result(UNKNOWN, "dispatch table", f"{kind}: coverage gap", [draft])


def _no_handler(ctx: Context, obligation: Any) -> Result:
    draft = Draft("process.handler_missing", "major", UNKNOWN, text(obligation.family),
                  policy("handler", "equals", "one per expected family"), "dispatch table",
                  "this producer has no handler for the family", f"{obligation.id} is not assessed",
                  remedy="add the handler to process_checks", recheck=[obligation.family], earliest_stage=obligation.stage)
    return Result(UNKNOWN, "dispatch table", "no handler", [draft])


@dataclass(frozen=True)
class Assessment:
    expected: tuple[Any, ...]
    outcomes: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]

    @property
    def complete(self) -> bool:
        return {o["obligation"] for o in self.outcomes} >= {o.id for o in self.expected}

    @property
    def result(self) -> str:
        values = [o["outcome"] for o in self.outcomes]
        if FAIL in values:
            return FAIL
        if UNKNOWN in values or not self.complete:
            return UNKNOWN
        return PASS

    def outcome(self, obligation_id: str) -> dict[str, Any] | None:
        return next((o for o in self.outcomes if o["obligation"] == obligation_id), None)

    def by_code(self, code: str) -> list[dict[str, Any]]:
        return [f for f in self.findings if f["code"] == code]

    def packet(self, version: str, dependencies: Mapping[str, str], *, scope: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """The manufacturing-check-result.schema.json packet of this assessment for built `version`."""
        pns = sorted({o.subject["PN"] for o in self.expected if o.subject.get("PN")})
        return {"schema_version": 1, "record_version": intent().RECORD_VERSION, "producer": PRODUCER, "version": version,
                "scope": scope or ([{"PN": pn} for pn in pns] or [{"product": True}]),
                "expected": [o.as_dict() for o in self.expected], "outcomes": [dict(o) for o in self.outcomes],
                "findings": [dict(f) for f in self.findings], "dependencies": dict(sorted(dependencies.items())),
                "complete": self.complete, "result": self.result}


def _operation(record: Any, pn: str, ident: str | None) -> Any | None:
    if ident is None:
        return None
    route_ = record.parts[pn].intent.route
    return None if intent().is_fact(route_) else next((op for op in route_ if op.id == ident), None)


def assess(record: Any, geometry: Mapping[tuple[str, str], Geometry] | None = None, *, today: date | None = None) -> Assessment:
    """One outcome per expected obligation, from the record and the measured component geometry
    (keyed (PN, component); the sole solid is `main`). Without geometry every geometric check is UNKNOWN."""
    mi = intent()
    obligations = expected(record)
    geometry = dict(geometry or {})
    today = today or date.today()
    outcomes, findings = [], []
    for obligation in obligations:
        subject = obligation.subject
        pn = subject["PN"]
        component = subject.get("component") or IMPLICIT
        op = _operation(record, pn, subject.get("operation"))
        ctx = Context(record, pn, component, op, geometry.get((pn, component)), today, geometry)
        if obligation.family.endswith(".coverage") and obligation.family.startswith("process."):
            result = coverage_gap(ctx, obligation)
        else:
            handler = handler_for(obligation.family, op.kind if op is not None else None)
            result = handler(ctx) if handler is not None else _no_handler(ctx, obligation)
        drafted = _findings(ctx, obligation, result, geometry.get((pn, component)))
        for item in drafted:
            if item.get("obligation") == obligation.id and item["outcome"] == FAIL and result.outcome != FAIL:
                raise HandlerError(f"{obligation.id}: finding {item['code']} fails it but the outcome is {result.outcome}")
        findings += drafted
        row = {"obligation": obligation.id, "outcome": result.outcome, "level": obligation.level,
               "method": result.method, "coverage": result.coverage}
        if result.source:
            row["source"] = dict(result.source)
        if result.uncertainty:
            row["uncertainty"] = result.uncertainty
        linked = [f["id"] for f in drafted if f.get("obligation") == obligation.id]
        if linked:
            row["findings"] = linked
        outcomes.append(mi.thaw(row))
    return Assessment(obligations, tuple(outcomes), tuple(findings))


def _findings(ctx: Context, obligation: Any, result: Result, geometry: Geometry | None) -> list[dict[str, Any]]:
    mi = intent()
    seen: dict[tuple[str, str | None], int] = {}
    out = []
    for draft in result.findings:
        key = (draft.code, draft.feature)
        seen[key] = seen.get(key, 0) + 1
        subject = dict(ctx.subject)
        if draft.feature:
            subject["feature"] = draft.feature
        method: dict[str, Any] = {"name": draft.method, "tool": PRODUCER}
        if geometry is not None and geometry.evidence and "STEP" in draft.method:
            method["evidence"] = [dict(row) for row in geometry.evidence]
        item: dict[str, Any] = {
            "id": stable_id(obligation.id, draft.code, draft.feature, seen[key]), "code": draft.code,
            "subject": subject, "severity": draft.severity, "outcome": draft.outcome, "measured": dict(draft.measured),
            "criterion": dict(draft.criterion), "method": method, "uncertainty": draft.uncertainty or "none stated",
            "consequence": draft.consequence,
            "recheck": {"families": sorted(set(draft.recheck or [obligation.family])), "scope": [subject],
                        "earliest_stage": draft.earliest_stage},
        }
        if draft.link:
            item["obligation"] = obligation.id
        if draft.remedy:
            item["remedy"] = draft.remedy
        elif draft.outcome in (FAIL, UNKNOWN):
            item["remedy"] = "provide the missing evidence named in the uncertainty, then recheck"
        if draft.alternatives:
            item["alternatives"] = [dict(a) for a in draft.alternatives]
        if draft.coverage:
            item["coverage"] = draft.coverage
        if draft.assumptions:
            item["assumptions"] = list(draft.assumptions)
        if draft.requirements:
            item["requirements"] = list(draft.requirements)
        out.append(mi.thaw(item))
    return out


def probe_plan(record: Any, pn: str, component: str, *, today: date | None = None) -> dict[str, Any]:
    """What form_check must measure on one component before the handlers run: the ray directions
    (access sides and pull senses, keyed as the handlers read them) and the wall threshold that the
    calibrations of its operations name (None: no calibrated wall, so no scan)."""
    today = today or date.today()
    part = record.parts[pn]
    route_ = part.intent.route
    directions: dict[str, list[float]] = {}
    walls: list[float] = []
    for op in () if intent().is_fact(route_) else route_:
        if op.components and component not in op.components:
            continue
        ctx = Context(record, pn, component, op, None, today)
        parsed, _ = access_directions(((op.setup or {}).get("access") or {}).get("sides") or ())
        for _, vector in parsed:
            key = direction_key(vector)  # a parsed side is an axis direction, so it always has a key
            if key is not None:
                directions[key] = list(vector)
        if op.kind in ("casting", "moulding"):
            pull = shaping.pull_axis(ctx)
            if pull is not None:
                directions["+pull"] = list(pull)
                directions["-pull"] = [-c for c in pull]
        if ctx.profile is not None and op.kind != "purchased":
            for name in ("min_wall", "min_web"):
                cal = ctx.calibrated(name)
                if cal.known:
                    walls.append(cal.bounds("mm")[1])
    return {"directions": directions, "wall_mm": max(walls) if walls else None}


def code_sha256() -> str:
    """sha256 over this package's source files: the producer code a packet depends on."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()
