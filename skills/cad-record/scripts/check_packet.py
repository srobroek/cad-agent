"""A producer's required-check packet and its publication: the one definition cad-motion access.py and
play.py, cad-analysis fea_static.py and cad-fits stackup.py publish through (cad-design form_check.py
builds its packet in process_checks and publishes it the same way). Load it by path as
`cad_record_check_packet`.

A packet (cad-record schemas/manufacturing-check-result.schema.json) lists every obligation the record
expects of the check's families (manufacturing_intent.required_checks), its outcome and the findings:
complete only when every expected obligation has an outcome, its result fail on any failure, unknown
on any unknown or a missing outcome, else pass. With a version it is validated against that schema (an
invalid packet is refused, never sealed) and published, with its evidence detail, to hw/checks/<V>/<subject>/
(layout bundle.checks: result.json and evidence/<name>), or a published bundle of the same recipe is
reused; without one it is printed and, with --out, written to a .cad-work scratch path. Publishing needs
jsonschema; everything else is stdlib.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
SCHEMA_ID = "https://srobroek.com/cad/schemas/manufacturing-check-result.schema.json"


class PacketError(ValueError):
    """A packet that cannot be published as asked: a usage error (exit 2)."""


def build(record: Any, expected: Iterable[Any], producer: str, version: str | None, outcomes: list[dict[str, Any]],
          findings: list[dict[str, Any]], dependencies: Mapping[str, str]) -> dict[str, Any]:
    """The packet of `expected` (manufacturing_intent Obligations) from this run's outcomes and findings."""
    wanted = [o.as_dict() for o in expected]
    ids = {o["id"] for o in wanted}
    covered = {row["obligation"] for row in outcomes}
    complete = ids <= covered
    states = [row["outcome"] for row in outcomes if row["obligation"] in ids]
    result = "fail" if "fail" in states else "unknown" if ("unknown" in states or not complete) else "pass"
    scope = [o["subject"] for o in wanted if not o["subject"].get("product")] or [{"product": True}]
    unique_scope = [dict(s) for s in {json.dumps(s, sort_keys=True): s for s in scope}.values()]
    return {"schema_version": 1, "record_version": record.version, "producer": producer, "version": version,
            "scope": unique_scope, "expected": wanted, "outcomes": outcomes, "findings": findings,
            "dependencies": dict(sorted(dependencies.items())), "complete": complete, "result": result}


@cache
def _validator() -> Any:
    """A Draft 2020-12 validator of manufacturing-check-result.schema.json, offline: every bundled schema resolves by $id."""
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - only publishing needs it
    from referencing import Registry, Resource  # noqa: PLC0415

    resources = [(contents["$id"], Resource.from_contents(contents))
                 for contents in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMAS.glob("*.schema.json")))]
    return Draft202012Validator({"$ref": SCHEMA_ID}, registry=Registry().with_resources(resources))


def validate(packet: Mapping[str, Any]) -> None:
    """Raise PacketError naming each way `packet` breaks manufacturing-check-result.schema.json."""
    errors = sorted(_validator().iter_errors(packet), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        problems = "; ".join(f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message[:200]}"
                             for e in errors[:5])
        more = f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""
        raise PacketError(f"packet does not match manufacturing-check-result.schema.json: {problems}{more}")


def emit(layout: Any, hw: Path, version: str | None, subject: str, producer: str, packet: Mapping[str, Any],
         detail: Mapping[str, Any], evidence: str, recipe: Mapping[str, Any], *, out: Path | None, replace: bool,
         record_version: int) -> tuple[dict[str, Any], str]:
    """(the stdout JSON, its status) of one run. With `version`, the packet of a current record
    (`record_version`) that matches its schema is published under bundle.checks with `detail` as
    evidence/<evidence>, or the bundle `recipe` already published is reused; an invalid packet raises
    PacketError. Without, it is written to `out` when given."""
    output: dict[str, Any] = {**packet, "detail": detail}
    status = packet["result"]
    if version is None:
        if out is not None:
            target = layout.scratch_path(hw, out, "--out")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        return output, status
    if out is not None:
        raise PacketError("--out is only for an unversioned run")
    if packet["record_version"] != record_version:
        raise PacketError(f"record version {packet['record_version']}: run migrate.py before publishing a {subject} packet")
    validate(packet)
    params = {"V": version, "S": subject}
    reused = layout.reusable(hw, "bundle.checks", params, recipe, replace=replace)
    if reused is not None:
        output = {**json.loads((reused / "result.json").read_text(encoding="utf-8")), "bundle": str(reused), "reused": True}
        return output, output["result"]
    stage = layout.staging(hw, "bundle.checks")
    (stage / "result.json").write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    (stage / "evidence").mkdir()
    (stage / "evidence" / evidence).write_text(json.dumps(detail, indent=2) + "\n", encoding="utf-8")
    published = layout.publish(hw, "bundle.checks", params, stage, recipe=recipe, producer=producer, status=status,
                               replace=replace)
    output.update(bundle=str(published.path), reused=published.reused)
    return output, status
