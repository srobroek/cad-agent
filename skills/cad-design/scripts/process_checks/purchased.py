"""Purchased-item handler: identity, specification, ratings, mounting and availability only. A bought item
never gets a made-part check (wall, overhang, cutter, kerf); its function is its rating."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .core import PASS, UNKNOWN, Context, Draft, Result, policy, text, unknown

TRACEABLE = frozenset({"datasheet", "standard", "supplier", "test", "calibration"})


def _fact(purchase: Any, name: str) -> Any:
    data = (purchase or {}).get(name)
    return data if isinstance(data, Mapping) else None


def _known(data: Any) -> bool:
    return isinstance(data, Mapping) and data.get("status") == "known"


def _missing(ctx: Context, family: str, what: str, consequence: str, severity: str = "major") -> Result:
    draft = Draft(f"purchase.{family}_unknown", severity, UNKNOWN, unknown(f"{what} not recorded"),
                  policy(f"purchase_{family}", "equals", "known and traceable"), "part.yaml route purchase", "not recorded",
                  consequence, remedy=f"record the {what} from the supplier's datasheet or the standard",
                  recheck=[f"purchase.{family}"], earliest_stage="parts")
    return Result(UNKNOWN, f"purchase {family}", f"{what} unknown", [draft])


def identity(ctx: Context) -> Result:
    data = _fact(ctx.operation.purchase, "identity")
    if not _known(data):
        return _missing(ctx, "identity", "manufacturer and part number or standard designation",
                        "the bought item is not identified: anything could be supplied")
    return Result(PASS, "purchase identity", f"identified as {data['value']}; certificates and incoming inspection are "
                  "evidence the ledger holds")


def rating(ctx: Context) -> Result:
    data = _fact(ctx.operation.purchase, "ratings")
    if not _known(data):
        return _missing(ctx, "rating", "ratings (load, speed, temperature)",
                        "whether the item survives its service is unknown")
    return Result(PASS, "purchase ratings", f"ratings recorded ({data['value']}); comparing them with the service loads "
                  "is the interface and engineering checks'")


def dimensions(ctx: Context) -> Result:
    purchase = ctx.operation.purchase or {}
    data = _fact(purchase, "specification")
    if not _known(data):
        return _missing(ctx, "dimensions", "specification (standard, grade or datasheet)",
                        "the item's dimensions and material are not traceable")
    sources = list(data.get("sources") or ()) + list(purchase.get("supplier_evidence") or ())
    if not any(s.get("kind") in TRACEABLE for s in sources):
        draft = Draft("purchase.dimensions_untraceable", "minor", UNKNOWN, text(f"specification {data['value']}"),
                      policy("specification_source", "one_of", sorted(TRACEABLE)), "part.yaml route purchase sources",
                      "only user statements back the specification", "the modelled dimensions rest on no datasheet",
                      remedy="attach the supplier datasheet or the standard", recheck=["purchase.dimensions"], earliest_stage="parts")
        return Result(UNKNOWN, "purchase specification", "specification not traceable", [draft])
    return Result(PASS, "purchase specification", "traceable specification; the modelled envelope against the datasheet and "
                  "the interface fit are not compared here")


def mounting(ctx: Context) -> Result:
    pn = ctx.pn
    interfaces = [i for i, item in ctx.record.interfaces.items() if pn in (item.raw.get("from"), item.raw.get("to"))]
    operations = ctx.record.assembly_operations
    ops = [] if ctx.mi.is_fact(operations) else [o.get("id") for o in operations if pn in (o.get("part"), o.get("into"))]
    if not interfaces and not ops:
        draft = Draft("purchase.mounting_unknown", "major", UNKNOWN, unknown("no interface or assembly operation"),
                      policy("mounting", "equals", "an interface and an assembly operation that retain it"),
                      "interfaces.yaml and assembly.yaml operations", "not recorded",
                      "how the item is mounted and retained is unknown",
                      remedy="record its interface and the assembly operation that installs and retains it",
                      recheck=["purchase.mounting"])
        return Result(UNKNOWN, "purchase mounting", "mounting unknown", [draft])
    return Result(PASS, "purchase mounting", f"mounted through {', '.join(interfaces + ops)}; retention and access are the "
                  "interface and assembly checks'")


def availability(ctx: Context) -> Result:
    if not _known(_fact(ctx.operation.purchase, "availability")):
        return _missing(ctx, "availability", "availability (supplier, lead time)", "the item may not be obtainable",
                        severity="minor")
    return Result(PASS, "purchase availability", "availability recorded")


PURCHASED = {"identity": identity, "rating": rating, "dimensions": dimensions, "mounting": mounting,
             "availability": availability}
