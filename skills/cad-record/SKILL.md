---
name: cad-record
description: Creates, migrates, validates and releases a hardware design record. Triggers on "create a CAD record", "validate this hardware project", or "release the design".
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Design Record

TRIGGER
+ creating or validating a hardware project's `hw/` design record
+ "what goes in the CAD design record", "validate this hardware project", or preparing a release
- modeling a part or assembly → `cad-model`

## Workflow

1. For a new record, from the project root create `hw/` from the template:

   ```sh
   cp -R "$CAD_SKILLS/cad-record/assets/project-template/hw" hw
   ```

   Stop if `hw/` already exists; do not merge a template into a record. The template has record-file placeholders and no part folders. Add `hw/parts/<PN>/part.yaml` and `model.py` for each PN matching `^[A-Z]{2,5}-[0-9]{4}$`; record the EBOM, verification and decisions in their canonical paths from [layout.md](references/layout.md). Never copy `.cad/recheck.json`; `cad-recheck` generates it.
2. At GROUND, read [manufacturing selection](../cad-flow/references/manufacturing-selection.md). Follow its single decision table for service class, rigor and optical requirements. For record v1, run the migration dry run and have the project owner review and explicitly apply it before a gated action or new record-writing build: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/migrate.py hw --dry-run` then, only after that approval, `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/migrate.py hw --apply`.
3. In v2, keep requirements, preferences, assumptions and open questions separate; record each part's material/process intent, capability and evidence. Use the existing validators, selectors and producer checks; do not create a parallel workflow. LOAD [manufacturing-intent.md](references/manufacturing-intent.md) for the record contract and [evidence.md](references/evidence.md) for evidence and gates.
4. Build record-derived guides after `vN` exists:

   ```sh
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/build_guide.py hw --version vN --out hw/docs
   ```

   Before release, regenerate with `--rev <rev> --date YYYY-MM-DD` using the UTC release date. Add `--replace` only when intentionally replacing a non-generated or edited guide. `assembly.yaml` drawing-operation `tools` and `consumables` are included only when the record states them; never infer tools or process settings.
5. Validate from the project root: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/validate.py hw`. Exit 0 means no error findings, 1 means blocking findings, and 2 means usage or infrastructure error. Read the JSON report; warnings do not fail validation.
6. At DESIGN freeze, first run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/evidence.py check --hw hw --version vN --stage design_freeze`. After all required evidence is accepted, run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/evidence.py digest --hw hw --version vN`; copy its `evidence_sha256` into `product.yaml` `flow.frozen.evidence_sha256` beside the built version's `manifest_sha256` and the approving decision. No recorded selection blocks freeze and release.
7. Release only a frozen, validated version after the RELEASE gate approves. LOAD [release.md](references/release.md) for exact commands, refusal conditions, manifest and package contents.

## Layout

`layout.json` is the authority for paths, owners, output kinds and release routes. Read the generated [layout.md](references/layout.md) for project work; use [layout-api.md](references/layout-api.md) only when implementing or reviewing a CAD script that resolves layout paths.

MUST Put every record and generated output at the path its layout entry names; use `.cad-work/` beside `hw/` for scratch.
MUST Keep every blocking finding and required UNKNOWN unresolved until applicable evidence resolves it; a ledger status word is not evidence.
NOT Edit generated bundles, frozen versions, or releases in place.

LOAD [design-rules.md](references/design-rules.md) when repairing a coded design-rule finding.

## Output

Report validator, evidence, freeze or release status from the command result and list cited record paths.
CAP 100w
