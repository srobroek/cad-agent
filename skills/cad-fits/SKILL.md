---
name: cad-fits
description: Computes ISO 286 hole/shaft limits and one-dimensional tolerance stacks. Triggers on "what fit for", "H7/g6 limits", or "stack-up".
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Fits and Stack-ups

TRIGGER
+ choosing or checking a fit for a bore, pin, bearing seat, or press fit
+ "what are the limits of 25 H7/k6", "is H7/p6 an interference fit", or a 1D tolerance stack-up
- general tolerances, GD&T or drawing callouts → `cad-drawings`
- interference or clearance between solid models → `cad-model` / `cad-motion`

## Workflow

1. Fit limits: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-fits/scripts/fits.py SIZE FIT [--expect TYPE]`. SIZE is nominal mm; FIT is `H7/g6` or one class; TYPE is `clearance|transition|interference`.
2. An intent stack is `hw/analysis/cases/<case>/case.yaml`, with `intent_version: 2` and `interfaces[].functional.variation.stack_case` naming its case id. LOAD the [manufacturing fit reference](references/process-fits.md). Run from the project root:

   ```sh
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-fits/scripts/stackup.py hw/analysis/cases/<case>/case.yaml --hw hw --version vN [--replace]
   ```

   The stack YAML is positional. An intent result publishes at `hw/analysis/vN/<case>/` and its required-check packet at `hw/checks/vN/stack-<case>/`. A standalone stack can use `--out .cad-work/<unit>/stack.json`, but `--out` cannot combine with `--hw` and `--version`. Exit 0 is pass; 1 is a completed violation or UNKNOWN; 2 is invalid input or infrastructure error.
3. Report the computed worst-case result. Worst case governs collision, binding, lost function and critical fits. `justified_rss` applies only to suitable non-governing consequences and needs sourced stable centered distributions, supported independence/correlation, coverage/probability and approved decision. Legacy `rss` is unqualified quadrature, never a probability claim.

## Rules

MUST Take fit limits and classification from `fits.py` output.
MUST Report stack outcome and method from `stackup.py` output; no statistical method overrides governing worst-case failure.
DEFAULT Express asymmetric tolerances with `tol_plus`/`tol_minus`.
NOT Report ASME B4.1 inch fits; the table covers ISO 286-1:2010 only.

## Manufacturing capability and assembly

Record desired fit separately from measured capability and inspected output. See [process-fits.md](references/process-fits.md) for evidence and assembly access. A stack is gate evidence only when its sealed packet covers the expected obligation.

```text
L1 FIT {size} {fit}: {type}, clearance {max}/{min} µm | STACK {id}: {PASS|FAIL|UNKNOWN} by {method}, {min} to {max} mm
   Limits -- {hole and shaft limits in mm, only for fits}
   Contributors -- {largest shares only for failing stacks}
```
CAP 80w
