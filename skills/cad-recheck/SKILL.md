---
name: cad-recheck
description: Use when committing a batch of CAD hardware project changes at or after its recheck threshold.
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Recheck

TRIGGER
+ A user commits a batch of design changes in a CAD hardware project.
+ A hardware project's current flow stage reaches its recheck threshold.
- Live preview or draft edits → do not run checks or write a stamp.

## Workflow

1. On a batch commit only, read the staged `hw/product.yaml` fields `flow.stage` and `flow.recheck_from`.
2. Before the threshold, build and render only; do not run checks or write a stamp.
3. At or after the threshold, run the producers for the changed inputs and all impacted dependents, then recompute the gate packet from the evidence producer:

   ```sh
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/evidence.py check --hw hw --version vN --stage <stage> --out .cad-work/recheck/evidence.json
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/stamp.py --stage <stage> --checks .cad-work/recheck/evidence.json [--passed]
   ```

   The evidence input MUST be the JSON packet printed/written by `evidence.py check`, not a `cad_model.py check` result. Include `--passed` only when every blocking obligation passes; omit it for a failed gate. A record v1 must be migrated and rebuilt before it can be stamped.
4. Stage the design changes and `hw/.cad/recheck.json`. The pre-commit hook accepts only a passing stamp for the exact staged `hw/` tree.
5. Install the hook in a hardware project's repository:

   ```sh
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/install-hook.py --project <project-directory>
   ```

   The installer refuses a conflicting repository-local `core.hooksPath` or ignores no-sample hooks unless `--force` is explicit. Commit the generated `.githooks/` directory. A system/global `core.hooksPath` makes it exit 3 with no installation, even with `--force`; another refusal exits 2.

For an absent or uninstalled hook, stage the hardware change then verify the staged index from the project root:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/stamp.py --verify
```

Exit 0 permits the commit; exit 2 means do not commit. Fix stderr's reason, restamp, and verify again.

The installer refuses to replace a different repository-local `core.hooksPath` or ignore non-sample hooks in `.git/hooks/`. Integrate the CAD hook with existing hooks, or explicitly pass `--force` to replace or ignore them. Commit the generated `.githooks/` directory so the hook and its stamp verifier are available to other checkouts.

When the system or global git config sets `core.hooksPath` (Git Defender does), the installer exits 3 and changes nothing, even with `--force`. A repository-local hooks path would stop those managed hooks from running. In that case, verify the stamp yourself before each commit of `hw/` changes. Stage the changes. Then run this command from the project repository:

```
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/stamp.py --verify
```

Commit only when it exits 0. Exit 0 means the stamp matches the staged `hw/` tree, or the staged stage is below `flow.recheck_from`. Exit 2 means do not commit, and stderr gives the reason. This is the same check the hook runs.

`stamp.py` parses staged YAML with `yaml.safe_load` and declares `pyyaml>=6.0` for `uv run --script`; keep that PEP 723 dependency declaration when changing its dependencies.

## Changed parts and dependents

`hw/assembly.yaml` places each part definition (PN) as one or more occurrences. Each copy of a part is one more occurrence of its PN, and every occurrence of a PN shares that PN's per-part results. Select the checks to rerun from the change:

- A part definition changes: its model, its geometry parameters or its `model_frame`. Rerun:
  - that PN's per-part checks, once: model read-back, form, DFM, fits and the part drawing;
  - `cad-model check` interference, connection and clearance requirements (`pair_clearance`) for every pair that includes one of its occurrences;
  - the `cad-motion` sweeps that move one of its occurrences;
  - the FEA cases that select it by `part` or `occurrence`.
- One occurrence's `placement` changes. Rerun that occurrence's pair checks and the sweeps that include it. The PN's per-part checks stay valid.
- A joint's origin, axis, limits or endpoints change. Rerun these checks for its child occurrence and every occurrence below it in the joint tree:
  - their pair checks;
  - the sweeps that move them;
  - the FEA cases on a posed STEP.
- A joint's `coupling`, or a `profiles` or `mechanisms` entry it uses, changes. Every joint driven by it, directly or through other coupled joints, moves differently (cad-motion `coupling.py`). Rerun `cad-record` validation. Rerun these checks for those joints' child occurrences and every occurrence below them:
  - their pair checks;
  - every sweep whose independent joints drive them, with its worst-case STEP and `worst.step.json`;
  - the FEA cases that cite that `worst.step.json` as `evidence`.
- An occurrence is added or removed. Rerun the BOM row of its PN (the quantity is the occurrence count), the pair checks of a new occurrence, and the assembly drawings that list the PN.
- A material or process changes. Rerun that PN's per-part checks, the quantity-weighted assembly mass, and the FEA cases on that PN.
- A batch adds or changes a clearance requirement in `hw/interfaces.yaml` (`type: clearance` with `pair` and `min_clearance_mm`). Rerun `cad-record` validation, `cad-model check`, whose `pair_clearance` holds it at the nominal pose, and every sweep that moves either occurrence of the pair, which holds it at every sample.

The stamp hashes the whole staged `hw/` tree, so any staged change needs a new stamp. This list decides only which checks to rerun before that stamp.

The stamp command and its hook audit the complete `hw/` tree with cad-record's layout API. Any staged or untracked path whose layout finding blocks under `validate.blocking` prevents stamping or commit, including hidden generated subtrees under `parts/<PN>/`.

## Rules

MUST Re-run the checks and stamp after any staged `hw/` file changes.
MUST Preserve `hw/.cad/recheck.json` as generated project metadata, not as design input to its own tree hash.
NOT Run checks or stamp during live preview or draft feedback.
