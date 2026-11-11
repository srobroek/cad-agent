---
name: cad-design
description: Applies CAD appearance fidelity and declared-route manufacturing checks. Triggers on "make it look real", "detail the parts", or a PARTS/DETAIL gate.
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Design Fidelity and Manufacturing Checks

TRIGGER
+ "make it look like a real product", "these parts are just blocks", "detail the parts"
+ an L1, L2, or L3 commit batch, or a PARTS/DETAIL gate
- fit classes, limits, or stack-ups → `cad-fits`
- joint posing, sweeps, or collisions → `cad-motion`
- L0 concept variants → `cad-concept`

## Workflow

1. LOAD `references/fidelity.md` for appearance decisions and `references/process-checks.md` for route obligations. Review recorded material, ordered route, capability, functional requirements and interfaces; infer no service or default route.
2. Work only on the assigned fidelity and source parts. Use the `model.py` contract for CAD implementation; a concept model is not a manufacturing plan.
3. On "commit batch", use `cad-render` to review the integrated build. Preserve scratch, record and gate ownership.
4. For PARTS run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-design/scripts/form_check.py --hw hw --version vN --fidelity F2`. For DETAIL use the same command with `--fidelity F3`. This is the only form-check gate command; `--parts-dir` is legacy diagnostics only. Exit 0 means the form criteria and process packet pass, 1 means form failure or process packet not `pass`, 2 means no result. Use the version-bound `cad-motion` access/play producers and fulfill every packet obligation.
5. Compare the render with `references/fidelity.md`. Report appearance level separately from process and evidence coverage. A new support or inspection finding does not make an undocumented route pass.

## Rules

MUST Use each part's recorded material, route and capability. Unknown does not fall back to FDM.
MUST Treat form and envelope heuristics as diagnostics, not process or physical proof.
MUST Report missing capability, handler, required input or packet as UNKNOWN.
MUST Put every joint bore on its recorded assembly origin and axis; size bores from `cad-fits` output.
NOT Treat a render as a check result or detailed form as process evidence.

```text
L1 DESIGN {stage}: {PN count} parts at {lowest level}, form_check PASS|FAIL
   Findings -- {check, part, and value per form_check finding, only if any}
   Renders -- {absolute PNG paths and one line each on what they show}
```
CAP 80w clean · 160w with findings
[ /Users/sjors/tmp/worktrees/cad-agent/feat-mfg-intent-fixes/skills/cad-fits/SKILL.md#7594]
PUT 1.=51:
---
name: cad-fits
description: Computes ISO 286 hole/shaft limits and 1D worst-case or justified statistical stack-ups. Triggers on "what fit for", "H7/g6 limits", or "stack-up".
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Fits and Stack-ups

TRIGGER
+ choosing or checking a fit for a bore, pin, bearing seat, or press fit
+ "what are the limits of 25 H7/k6", "is H7/p6 an interference fit", or a 1D tolerance-chain stack-up
- general tolerances, GD&T or drawing callouts → `cad-drawings`
- interference or clearance between solid models → `cad-model` / `cad-motion`

## Workflow

1. Fit limits: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-fits/scripts/fits.py SIZE FIT [--expect TYPE]`. SIZE is nominal mm; FIT is `H7/g6` or one class; TYPE is `clearance|transition|interference`.
2. For stack-up, LOAD `references/process-fits.md`. An intent stack is `hw/analysis/cases/<case>/case.yaml`, with `intent_version: 2` and `interfaces[].functional.variation.stack_case` pointing to its case id. Its exact version-bound command is:

   ```sh
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-fits/scripts/stackup.py hw/analysis/cases/<case>/case.yaml --hw hw --version vN [--replace]
   ```

   The stack file is positional. The result publishes under `hw/analysis/vN/<case>/`; for an intent stack it also publishes an evidence packet under `hw/checks/vN/stack-<case>/`. Standalone stacks may use `--out .cad-work/<unit>/stack.json`; `--out` cannot accompany version binding. Exit 0 is pass, 1 is a completed stack violation or unknown, 2 is invalid input or infrastructure failure.
3. State worst-case result and method. Worst case governs collision, binding, lost function and critical fits. Use `justified_rss` only for a suitable non-governing consequence with sourced stable centered distributions, supported independence/correlation, coverage/probability assumptions and approved DR. Legacy `rss` is a quadrature heuristic, never a probability claim.

## Rules

MUST Take fit limits and classification from `fits.py` output.
MUST Report stack outcome and method from `stackup.py` output; no RSS method overrides governing worst-case failure.
DEFAULT Express asymmetric tolerances with `tol_plus`/`tol_minus`.
NOT Report ASME B4.1 inch fits; the table covers ISO 286-1:2010 only.

## Manufacturing capability and assembly

Record desired fit separately from measured capability and inspected output. Use [process-fits.md](references/process-fits.md) for evidence and access. Stack results are evidence only when the sealed check packet covers the expected obligation.

```text
L1 FIT {size} {fit}: {type}, clearance {max}/{min} µm | STACK {id}: {PASS|FAIL|UNKNOWN} by {method}, {min} to {max} mm
   Limits -- {hole and shaft limits in mm, only for fits}
   Contributors -- {largest shares only for failing stacks}
```
CAP 80w
