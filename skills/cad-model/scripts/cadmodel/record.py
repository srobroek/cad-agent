"""The cad-record modules, loaded from their sibling skill, and the record snapshot a version is built from."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

RECORD_SCRIPTS = Path(__file__).resolve().parents[3] / "cad-record" / "scripts"
LAYOUT_SCRIPT = RECORD_SCRIPTS / "layout.py"
INTENT_SCRIPT = RECORD_SCRIPTS / "manufacturing_intent.py"


class RecordError(ValueError):
    """A record snapshot that cad-model cannot read: unparseable YAML or an unsupported record version."""


def _load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cad-record module not found at {path}")
        sys.modules[name] = importlib.util.module_from_spec(spec)  # its dataclasses look the module up here
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def load_layout() -> Any:
    """cad-record `layout.py` under the name `cad_record_layout`, shared with validate.py (references/layout-api.md)."""
    return _load("cad_record_layout", LAYOUT_SCRIPT)


def load_intent() -> Any:
    """cad-record `manufacturing_intent.py` under `cad_record_manufacturing_intent`: the record version 2 contract."""
    return _load("cad_record_manufacturing_intent", INTENT_SCRIPT)


def snapshot(hw: Path, pns: Iterable[str]) -> dict[str, bytes]:
    """The bytes of the record files a version is built from, keyed by their path below hw/: every file
    the cad-record layout's `version.record` entry names that exists (`snapshot_names`): the product,
    requirement, interface and assembly records, parts/<PN>/part.yaml of each PN, the manufacturing
    capability, material and selection records and every supporting source file below
    manufacturing/sources/. Never the findings or evidence ledgers, which checks produce afterwards."""
    return {name: (hw / name).read_bytes() for name in load_layout().snapshot_names(hw, pns)}


def write_snapshot(folder: Path, files: Mapping[str, bytes]) -> dict[str, str]:
    """Write `files` below `folder` (a version's record/) and return each path's sha256."""
    hashes = {}
    for name, data in sorted(files.items()):
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes


def read_folder(folder: Path) -> dict[str, bytes]:
    """The files of a written snapshot folder (a version's record/), keyed by their path below it."""
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file() and not path.is_symlink()}


@dataclass(frozen=True)
class Snapshot:
    """A version's record snapshot, read once: its bytes, its parsed record documents (supporting source
    files stay bytes) and the normalized manufacturing-intent record (`manufacturing_intent.Record`; a
    record version 1 reads with every manufacturing-intent field unknown, never defaulted)."""

    files: Mapping[str, bytes]
    documents: Mapping[str, Any]
    intent: Any

    @property
    def version(self) -> int:
        version: int = self.intent.version
        return version

    @property
    def parts(self) -> dict[str, dict[str, Any]]:
        """The part records, keyed by the PN folder they were read from."""
        found = {}
        for name, data in self.documents.items():
            if name.startswith("parts/") and name.endswith("/part.yaml"):
                if not isinstance(data, dict):
                    raise RecordError(f"hw/{name} is not a mapping")
                found[name.split("/")[1]] = data
        return found


def read_snapshot(files: Mapping[str, bytes]) -> Snapshot:
    """The Snapshot of record files `files`; RecordError for YAML that does not parse or a record version
    cad-agent cannot read."""
    import yaml

    intent = load_intent()
    parsed: dict[str, Any] = {}
    for name, data in sorted(files.items()):
        if intent.schema_for(name) is None:
            continue
        try:
            parsed[name] = yaml.safe_load(data.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as error:
            raise RecordError(f"hw/{name}: {error}") from error
    product = parsed.get("product.yaml")
    try:
        intent.record_version(product if isinstance(product, Mapping) else {})
    except intent.UnsupportedRecordVersion as error:
        raise RecordError(f"hw/product.yaml: {error}") from error
    return Snapshot(files, parsed, intent.normalize_record(parsed))
