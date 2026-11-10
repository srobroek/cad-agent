---
name: cad-model
description: Builds part model scripts into versioned STEP AP242, GLB, and metrics, then checks or diffs versions. Triggers on "build v3", "check the model", "diff".
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Model Build and Check

Stages L1 LAYOUT to L4 ANALYSIS: every commit batch builds a version; at or after
`flow.recheck_from` the recheck consumes `check`. L0 CONCEPT stays in `cad-concept`.

TRIGGER
+ "build the parts", "build v3", "export STEP", "rebuild after the model.py edit"
+ "check the model", "does anything overlap", "is the B-rep valid", "is the mesh watertight"
+ "diff v2 v3", "what changed in the upper link", "add a cut without breaking the solid"
- joint posing, sweeps, worst-case pose → `cad-motion`
- the BOM roll-up → `cad-bom`; fits and stack-ups → `cad-fits`
- pushing to a viewer → out of scope; read `part-map.json` and `latest.json` instead

## Workflow

1. Write each `hw/parts/<PN>/model.py`: one kernel per file, `KERNEL`, `PARAMS`, `build(params)`,
   modelled in place at the zero pose; Booleans through `cadmodel.safe` (`safe_cut`, `safe_fuse`,
   `safe_intersect`). Add `drawing_features(params, shape)` for a part that needs a production
   drawing. Add `build_manufacture(params)`, declared in part.yaml `manufacture`, for a part made in
   another state than it is installed in, such as a strap printed flat. LOAD references/contract.md
   when writing or reviewing a model.py. For a cosmetic round that may fail (`StdFail_NotDone`),
   use the [Fillet fallback](references/fillets.md) helper.
2. Build: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-model/scripts/cad_model.py run hw --label vN [--kernel auto]`
   → `hw/versions/vN/` and `hw/versions/latest.json`; JSON summary on stdout. `auto` runs each part in its declared kernel's own uv environment; `cadquery` or `build123d` additionally requires every part to declare that kernel. The version snapshots its records in `record/` and holds `manufacturing/<PN>/` for every custom-made part: `part.step` (AP242, mm), `part.stl` for additive FDM/resin on the approved bed pose, and `manufacturing.json`. Multi-component outputs are also under `manufacturing/<PN>/components/<COMP>/`; each component corresponds to declared `part.intent.components` and multiplicity. Build checks output integrity; it does not prove manufacturing capability. Only parts whose build inputs changed are built; unchanged outputs are reused byte for byte (`cache` in the summary, `references/outputs.md`).
3. Check: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-model/scripts/cad_model.py check vN --hw hw [--out .cad-work/<unit>/check.json] [--changed-only [--since-version vM]] [--exact] [--jobs 1|N|auto] [--cache DIR | --no-cache]` (explicit results must be new `.cad-work/` paths).
   → check result JSON and the v2 `cad.readback` packet at `hw/checks/<V>/cad/` (stdout `packet`). That packet records readback and sealed-output integrity evidence. Declared locating/interference fits use the shared pair policy. Build-volume fit is a non-blocking geometry diagnostic, not manufacturability evidence. Changed-only checks report `scope.complete: false` and never replace a full check.
   Record-v2 route checks are a separate `cad-design/scripts/form_check.py --hw hw --version vN` producer. It checks every operation and component; missing process/capability coverage remains UNKNOWN. Run `cad-motion` access/play producers and other required evidence producers for their obligations. See each skill reference.
4. Diff: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-model/scripts/cad_model.py diff vA vB --hw hw` → per-part volume and bbox deltas, added/removed parts, exact shared / A-only / B-only volumes, and manufacturing state, tolerance, output policy and placement changes per custom-made part.
5. Recheck: write the full check JSON with `--out`, then pass it to `cad-recheck` `stamp.py --checks`, adding `--passed` only when `check` exited 0. A check with `status: warn` is advisory.
6. LOAD references/outputs.md when reading a version file, a check field, or a threshold.

Exit codes: 0 pass; 1 a part model or a manufacturing output failed (`run`, no version written) or
a blocking check failed (`check`); 2 usage or infrastructure error. `diff` exits 0 or 2. When `run`
exits 2 because it sealed a version but could not write `latest.json`, run the `cad_model.py latest
hw --label vN` command it names; never rebuild the sealed label.

## Rules

MUST Pick the next label above the newest version; versions are immutable and `run` refuses an existing or lower label.
MUST Treat `check` exit 1 as a failed stage gate; report `blocking_failures` and the numbers verbatim.
MUST Stop the runs a `check` exit 2 names as still running or left behind with the `kill` commands it prints (`uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/process_lifecycle.py strays --hw hw` lists them); never delete a run record whose processes still run.
NOT Stamp, gate or release from a `--changed-only` check; run the full check first.
MUST Use the cad-record layout API for version and manufacturing paths; its generated `references/layout.md` is the one canonical path table.
MUST For record-v2, use the selected material profile for mass and properties; missing density remains unknown. The legacy `assets/materials.yaml` density table is an estimate for v1 only and never provides structural properties.
MUST Declare locating, press and running fits, and heat-set inserts in `hw/interfaces.yaml`; only the shared pair policy determines allowed geometric overlap.
DEFAULT Use `--allow-interference PN:PN` only for an undeclared, intentional overlap.
NOT Treat build-volume fit, solid-count check or geometric integrity as process capability or physical evidence.
NOT Put a short timeout around `run`: a cold OCP import took 170 to 320 s per fresh environment.

```text
L1 MODEL {RUN|CHECK|DIFF} vN: {PASS|FAIL}, {parts} parts, {wall s}
   Blocking: {each blocking_failures entry with its part or pair and number}
   Changed: {each changed part with volume delta, only for diff}
```
CAP 60w clean · 140w with findings
