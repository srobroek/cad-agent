"""Record version 2 projects for cad-model's manufacturing-state tests, written with cad-record's shared
fixture builder (cad-record/tests/fixtures/manufacturing_intent.py, loaded by path).

`loose_project` is one made part, TST-0001, built by fixtures/<kernel>/loose_components.py: a body and
loose tabs bound as components by interior witnesses, printed in an approved orientation on a printer
whose capability profile records its measured envelope, of a material profile whose density cites a
frozen source file. Every number is a synthetic test value, never material, machine or supplier data.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SHARED = HERE.parents[2] / "cad-record" / "tests" / "fixtures" / "manufacturing_intent.py"
PN = "TST-0001"
BODY_WITNESS = [20.0, 10.0, 5.0]  # inside the 40 x 20 x 10 body
TAB_WITNESS = [65.0, 2.5, 1.0]  # inside the first tab, at x 60..70
ENVELOPE_MM = (100.0, 100.0, 50.0)
DENSITY_G_CM3 = 1.2
SOURCE = "manufacturing/sources/tst-polymer.pdf"
SOURCE_BYTES = b"%PDF-1.4 TST synthetic polymer datasheet\n"
IDENTITY_ORIENTATION = {"status": "approved", "rotation": {"quaternion": [1, 0, 0, 0]}, "decision": "DR-001"}
MULTIPROCESS_ROUTE = (("cut", "laser"), ("ream", "reaming"), ("coat", "coating"), ("inspect", "inspection"))


def shared() -> Any:
    """cad-record's shared fixture builder module."""
    name = "cad_record_mi_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SHARED)
        assert spec and spec.loader
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def printer(ident: str = "tst_printer", process: str = "fdm") -> dict[str, Any]:
    fx = shared()
    measured = [fx.source("calibration", f"{ident} envelope measurement")]
    return fx.capability_profile(ident, process, envelope={axis: fx.known(fx.quantity(size, "mm"), "measured", measured)
                                                           for axis, size in zip("xyz", ENVELOPE_MM)})


def polymer(density: float = DENSITY_G_CM3, *, with_density: bool = True, form: str = "printed") -> dict[str, Any]:
    """A polymer grade profile (printed, or sheet stock); its density cites SOURCE when it has one."""
    fx = shared()
    properties = []
    if with_density:
        prop = fx.material_property("density", density, "g_cm3", orientation=fx.not_applicable("density is a bulk property"))
        prop["value"] = fx.known(fx.quantity(density, "g_cm3"), "supplier_typical", [
            fx.source("datasheet", "TST polymer datasheet", artifact=SOURCE,
                      sha256=hashlib.sha256(SOURCE_BYTES).hexdigest(), conditions="TST-STD-1, 23 deg_C, dry")])
        properties.append(prop)
    return fx.material_profile("tst_polymer", form=form, properties=properties)


def components(tab_multiplicity: int = 1, tab_witness: list[float] | None = None) -> list[dict[str, Any]]:
    tab: dict[str, Any] = {"id": "tab", "witness": tab_witness or TAB_WITNESS}
    if tab_multiplicity > 1:
        tab["multiplicity"] = tab_multiplicity
    return [{"id": "body", "witness": BODY_WITNESS}, tab]


def documents(*, process: str = "fdm", orientation: Any = None, parts_of: list[dict[str, Any]] | None = None,
              print_components: list[str] | None = None, density: float = DENSITY_G_CM3,
              with_density: bool = True, material: str = "TST polymer") -> dict[str, Any]:
    """The record of the loose project. `parts_of` is intent.components (None: a sole solid); the print
    operation shapes `print_components` (None: every component). Process `laser` is MULTIPROCESS_ROUTE:
    sheet stock laser cut, then reamed, coated and inspected in that order, none of it additive."""
    fx = shared()
    extra: dict[str, Any] = {}
    if print_components is not None:
        extra["components"] = print_components
    if process == "cnc":
        route = [fx.operation("mill", "cnc", "tst_mill", **extra)]
        capabilities = [printer(), printer("tst_mill", "cnc")]
    elif process == "laser":
        route = [fx.operation(ident, kind, "tst_laser" if kind == "laser" else None, **extra)
                 for ident, kind in MULTIPROCESS_ROUTE]
        capabilities = [printer(), printer("tst_laser", "laser")]
    else:
        route = [fx.operation("print", process, "tst_printer",
                              orientation=copy.deepcopy(orientation or IDENTITY_ORIENTATION), **extra)]
        capabilities = [printer()]
    intent_extra = {"components": parts_of} if parts_of is not None else {}
    part = fx.part(PN, "Loose kit", process=process, material=material, part_intent=fx.intent(
        functions=[{"function": "cosmetic"}], material_profile="tst_polymer", route=route, **intent_extra))
    form = "sheet" if process == "laser" else "printed"
    return fx.record(parts=[part], capabilities=capabilities,
                     materials=[polymer(density, with_density=with_density, form=form)], sources={SOURCE: SOURCE_BYTES})


def set_model(hw: Path, kernel: str, tabs: list[list[float]]) -> None:
    """hw/parts/TST-0001/model.py: the kernel's loose_components fixture with `tabs`."""
    source = (HERE / kernel / "loose_components.py").read_text(encoding="utf-8")
    source = re.sub(r"^PARAMS = .*$", f"PARAMS = {{'tabs': {tabs!r}}}", source, count=1, flags=re.M)
    folder = hw / "parts" / PN
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "model.py").write_text(source, encoding="utf-8")


def loose_project(hw: Path, kernel: str, tabs: list[list[float]], **record: Any) -> Path:
    """Write the loose project's record (`documents(**record)`) and model below `hw`."""
    shared().write_record(hw, documents(**record))
    set_model(hw, kernel, tabs)
    return hw


def rewrite_part(hw: Path, **record: Any) -> None:
    """Replace the record documents the loose project's model builds from, keeping hw/'s other files."""
    import yaml

    for rel, document in documents(**record).items():
        if rel.startswith("_"):
            continue
        (hw / rel).write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
