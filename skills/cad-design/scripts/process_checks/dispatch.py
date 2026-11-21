# SPDX-FileCopyrightText: 2026 Sjors Robroek
#
# SPDX-License-Identifier: MPL-2.0

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
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import additive, machining, purchased, route, secondary, shaping, sheet
from .core import (FAIL, IMPLICIT, MINIMUM, PASS, UNKNOWN, Context, Draft, Geometry, Handler, Result, access_directions,
                   component_contexts, direction_key, guidelines, intent, policy, stable_id, text)

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
    """The record cannot be assessed: it is not record version 2."""


def owned(obligation: Any) -> bool:
    if obligation.level in EXCLUDED_LEVELS:
        return False
    return obligation.family in OWNED_FAMILIES or obligation.family.startswith(OWNED_PREFIXES)


def expected(record: Any) -> tuple[Any, ...]:
    """The obligations this producer must answer, enumerated from the record before anything runs."""
    if record.version != intent().RECORD_VERSION:
        problem = intent().version_problem(dict(record.product))
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
    guideline_digests: Mapping[str, str] = types.MappingProxyType({})  # id@revision -> sha256 the limits can come from
    resolutions: tuple[Any, ...] = ()  # guidelines.Resolved of every guideline limit looked up (the trace's rows)
    traced: frozenset[str] = frozenset()  # ids of the findings of obligations that consulted a guideline
    calibration: bool = False  # the calibration flag this assessment ran under; the packet seals it when on

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

    def _cited(self, finding: Mapping[str, Any], trace: Mapping[str, str] | None) -> dict[str, Any]:
        item = dict(finding)
        if trace is not None and item["id"] in self.traced:
            method = dict(item["method"])
            method["evidence"] = [*method.get("evidence", ()), dict(trace)]
            item["method"] = method
        return item

    def packet(self, dependencies: Mapping[str, str], *, scope: list[dict[str, Any]] | None = None,
               trace: Mapping[str, str] | None = None) -> dict[str, Any]:
        """The manufacturing-check-result.schema.json packet of this assessment. Its dependencies are `dependencies`
        and every guideline set the operations' limits can resolve through (guidelines.packet_dependencies). `trace`
        (a file_hash row of the resolution trace) is cited in method.evidence of every finding of an obligation that
        consulted a guideline."""
        findings = [self._cited(f, trace) for f in self.findings]
        dependencies = {**dependencies, **guidelines().packet_dependencies(self.guideline_digests),
                        **guidelines().calibration_dependencies(self.calibration)}
        pns = sorted({o.subject["PN"] for o in self.expected if o.subject.get("PN")})
        return {"schema_version": 1, "record_version": intent().RECORD_VERSION, "producer": PRODUCER,
                "scope": scope or ([{"PN": pn} for pn in pns] or [{"product": True}]),
                "expected": [o.as_dict() for o in self.expected], "outcomes": [dict(o) for o in self.outcomes],
                "findings": findings, "dependencies": dict(sorted(dependencies.items())),
                "complete": self.complete, "result": self.result}


def _operation(record: Any, pn: str, ident: str | None) -> Any | None:
    if ident is None:
        return None
    route_ = record.parts[pn].intent.route
    return None if intent().is_fact(route_) else next((op for op in route_ if op.id == ident), None)


def assess(record: Any, geometry: Mapping[tuple[str, str], Geometry] | None = None, *, today: date | None = None,
           catalog: Any = None, calibration: bool | None = None) -> Assessment:
    """One outcome per expected obligation, from the record and the measured component geometry
    (keyed (PN, component); the sole solid is `main`). Without geometry every geometric check is UNKNOWN. `catalog`
    (cad-record guidelines.discover) gives the guideline limits where no calibration holds; without it only
    calibrations count. `calibration` is the flag value (guidelines.calibration_enabled, read here once when None): with it
    off, a recorded FDM calibration is ignored, an info finding says so, and a guideline pass is a pass in every class."""
    calibration = guidelines().calibration_enabled() if calibration is None else calibration
    obligations = expected(record)
    geometry = dict(geometry or {})
    today = today or date.today()
    outcomes, findings = [], []
    digests: dict[str, str] = {}
    resolutions: list[Any] = []  # shared by every Context: the trace's rows
    traced: set[str] = set()
    refused_reported = calibration_reported = False
    ignored: list[Any] = []  # recorded FDM calibrations skipped with the flag off (shared by every Context)
    for obligation in obligations:
        subject = obligation.subject
        pn = subject["PN"]
        component = subject.get("component") or IMPLICIT
        op = _operation(record, pn, subject.get("operation"))
        ctx = Context(record, pn, component, op, geometry.get((pn, component)), today, geometry, catalog, resolutions,
                      calibration=calibration, ignored=ignored)
        scope = ctx.guideline_context()
        if scope is not None:
            digests.update(guidelines().applicable_digests(catalog, scope))
        result = _result(ctx, obligation)
        if scope is not None and catalog.refused and not refused_reported:
            refused_reported = True  # once per assessment, on the first obligation that could use a guideline
            result.findings.append(_refused(catalog.refused))
        if ctx.ignored and not calibration_reported:
            calibration_reported = True  # once per assessment, on the first obligation that met a recorded calibration
            result.findings.append(_calibration_off(ctx.ignored))
        drafted = _findings(ctx, obligation, result, geometry.get((pn, component)))
        for item in drafted:
            if item.get("obligation") == obligation.id and item["outcome"] == FAIL and result.outcome != FAIL:
                raise HandlerError(f"{obligation.id}: finding {item['code']} fails it but the outcome is {result.outcome}")
        findings += drafted
        if ctx.consulted:
            traced.update(f["id"] for f in drafted)
        row = _row(obligation, result, drafted)
        outcomes.append(intent().thaw(row))
    return Assessment(obligations, tuple(outcomes), tuple(findings), types.MappingProxyType(dict(sorted(digests.items()))),
                      tuple(resolutions), frozenset(traced), calibration)


def _result(ctx: Context, obligation: Any) -> Result:
    if obligation.family.endswith(".coverage") and obligation.family.startswith("process."):
        return coverage_gap(ctx, obligation)
    handler = handler_for(obligation.family, ctx.op.kind if ctx.op is not None else None)
    return handler(ctx) if handler is not None else _no_handler(ctx, obligation)


def _row(obligation: Any, result: Result, drafted: list[dict[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {"obligation": obligation.id, "outcome": result.outcome, "level": obligation.level,
                           "method": result.method, "coverage": result.coverage}
    if result.source:
        row["source"] = dict(result.source)
    if result.uncertainty:
        row["uncertainty"] = result.uncertainty
    linked = [f["id"] for f in drafted if f.get("obligation") == obligation.id]
    if linked:
        row["findings"] = linked
    return row


def _refused(refused: Any) -> Draft:
    """The guideline files a catalog refused: a limit may have been judged without them, so the result says so."""
    names = "; ".join(str(r) for r in refused)
    return Draft("process.guideline_refused", "minor", UNKNOWN, text(names),
                 policy("guideline_files", "equals", "every file valid", note="a refused file supplies no limit"),
                 "guideline catalog discovery", "the refused files are not read at all",
                 "a project guideline that should tighten or replace a limit is not applied",
                 remedy="fix or remove the refused guideline files, then recheck", recheck=["process"], link=False)


def _calibration_off(ignored: Sequence[Any]) -> Draft:
    """Recorded FDM calibrations that the off flag skipped: generic guideline values govern instead."""
    names = ", ".join(sorted(str(i) for i in ignored))
    return Draft("process.fdm.calibration_off", "info", PASS, text(f"recorded calibrations ignored: {names}"),
                 policy("printer_calibration", "equals", "off", note="generic guideline values govern"),
                 "calibration flag", "none", "the limits come from the generic guideline values, not from this printer's calibration",
                 remedy=guidelines().COUPON_REMEDY, recheck=["process.fdm"], link=False)


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
        if draft.remedy and ctx.calibration_off() and "calibrat" in draft.remedy.lower():
            item["remedy"] = guidelines().COUPON_REMEDY  # an FDM finding never asks the user to calibrate while the flag is off
        elif draft.remedy:
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


def probe_plan(record: Any, pn: str, component: str, *, today: date | None = None, catalog: Any = None,
               calibration: bool | None = None) -> dict[str, Any]:
    """What form_check must measure on one component before the handlers run: the ray directions
    (access sides and pull senses, keyed as the handlers read them) and the wall threshold that the
    limits of its operations name: a calibration, else a guideline value from `catalog` (None: no wall
    limit, so no scan). `calibration` is the flag value, as for `assess`."""
    today = today or date.today()
    calibration = guidelines().calibration_enabled() if calibration is None else calibration
    directions: dict[str, list[float]] = {}
    walls: list[float] = []
    for ctx in component_contexts(record, pn, component, catalog, today, calibration):
        op = ctx.operation
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
                limit = ctx.limit(name, MINIMUM)
                if limit.known:
                    walls.append(limit.threshold("mm"))
    return {"directions": directions, "wall_mm": max(walls) if walls else None}


def code_sha256() -> str:
    """sha256 over this package's source files: the producer code a packet depends on."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()
