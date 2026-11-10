---
name: cad-analysis
description: Use for static FreeCAD finite-element analysis of a hardware case, producing checked stress, displacement, and yield-safety results.
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Analysis

TRIGGER
+ A hardware project is at L4 ANALYSIS and needs a static FEA case.
- Motion, transient, fatigue, buckling, thermal, or nonlinear analysis → report unsupported; this skill handles static linear analysis only.

## Workflow

1. LOAD `references/material-evidence.md`. Prepare a version-bound case with a checked STEP, intended part or occurrence, load/support model, mesh parameters and material conditions matched to the real part and requirement. Unknown or mismatched properties cannot support acceptance.
2. Store each authored case at `hw/analysis/cases/<case>/case.yaml`; include `step`, `fixed_faces`, non-empty `loads`, `mesh_size` and a record-bound `material` profile plus `conditions`. A face selector is exactly `{indices: [<1-based-face-index>, ...]}` or `{plane: {axis: x|y|z, value: <mm>, tolerance: <mm>}}`. Each load is `{selector: <face selector>, force: [Fx, Fy, Fz]}` in N, optionally with `load: <use-case-load-id>`.
3. Run from the project root:

   ```sh
   uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-analysis/scripts/fea_static.py hw/analysis/cases/<case>/case.yaml --hw hw --version vN [--case-id <case>] [--replace]
   ```

   The case YAML is positional. For a sweep worst pose, set `step` and `evidence` to the exact versioned `worst.step` and sibling `worst.step.json`; the solver verifies the STEP hash and full pose and never re-poses it. An intent case publishes analysis under `hw/analysis/vN/<case>/` and its engineering packet at `hw/checks/vN/fea-<case>/`. A standalone run's `--out` must be a new `.cad-work/` path.
4. Inspect warnings, solver versions and `worst_case_pose` (STEP, pose, evidence hash). Record accepted checks in `verification.csv` only with scoped evidence. Static simulation is never physical validation.

## Inputs

- `step`: STEP path, relative to the case YAML or absolute.
- `part`: optional PN-first identifier; required for ambiguous multi-solid STEP except when `occurrence` or `solid_index` selects one. Omit only for a single-solid STEP; a repeated PN needs an occurrence.
- `occurrence`: quoted `"@id"` for a repeated part in a versioned assembly STEP.
- `solid_index`: optional 1-based solid index; mutually exclusive with `part` and `occurrence`, and refused for record v2.
- `pose`: optional joint-value map copied from `worst_case.pose`.
- `evidence`: optional path to sweep `worst.step.json`; it binds STEP hash, full pose and occurrence.
- `fixed_faces`: required selector for constrained faces, using the same `indices` or `plane` forms as each `loads[].selector`.
- `loads`: non-empty list of `{selector, force: [Fx, Fy, Fz]}` in N; `load` optionally identifies its use-case load.
- `mesh_size`: positive maximum element size in mm.
- `required_safety_factor`: optional positive check threshold; a completed solve below it fails.
- `timeout_seconds`: optional positive whole-job limit, default 600.

## Record-bound material and evidence

For v2, use `material: {profile?, conditions: {conditioning, temperature, orientation?}, effective_isotropic?: {decision, justification}}` with `--hw hw --version vN`. A v2 case selects `part` or an `occurrence`; do not pass numeric material properties or `solid_index`. Legacy explicit `{E, nu, density, yield}` is accepted only for standalone/record-v1 exploratory cases and is never service evidence.

Optional case keys: `acceptance: {strength: REQ-###, stiffness: REQ-###}`, `mesh_convergence: {decision: DR-###}`, and `loads[].load`. The referenced requirement needs a structured criterion; convergence needs the approved mesh-study decision. Result blocks include material, evidence, mesh, binding and engineering packet.

Completed results record reproducibility settings in `mesh_settings`: one Gmsh thread, random seed 1, zero 3D random factor, standard and Netgen mesh optimizers disabled, fixed elastic-plus-optimization high-order pass, and CalculiX SPOOLES solver. `mesh_input_hashes` name the normalized `.geo` and BRep inputs. High-order Gmsh optimization can perturb nodes across identical runs; repeat safety factors within 0.5% are this mesh noise, not evidence of a design difference.

`fea_static.py` publishes a v2 engineering packet at `hw/checks/<V>/fea-<case>/`; `stdout.check_bundle` names it. Only `engineering.strength`, `engineering.stiffness` and `engineering.anisotropy` are in static solver scope. Creep, fatigue, impact and life remain physical-validation obligations.

## Exit codes

- `0`: analysis completed and any requested safety-factor threshold passed.
- `1`: analysis completed, but the required safety factor was not met.
- `2`: no result; invalid input, unavailable or failed FreeCAD job, timeout, malformed result, or infrastructure error.

## Solver failures

The job runs the ccx it records in `solver_versions`: beside FreeCAD first, then on PATH. A failed solve names its cause in `error.code`, with evidence in `error.details`:

- `solver_crashed`: ccx died from a signal; details give signal, stage and degrees of freedom. The bundled SPOOLES index limit can fail on very large models; raise `mesh_size`.
- `solver_failed`: ccx stopped with `*ERROR` messages. A nonpositive Jacobian lists distorted second-order elements; lower `mesh_size`.
- `freecad_timeout`: details identify `import`, `mesh`, `solve` or `results`; raise `timeout_seconds` or adjust `mesh_size` for meshing.

## Rules

MUST Match material properties to sourced grade, form, process state, orientation, conditioning and service temperature. Density or an unsourced name never supplies strength or another material's properties. See `references/material-evidence.md`.
MUST Check face selection, units, loads, constraints, contact assumptions, material evidence, mesh/convergence and solver warnings before relying on results.
MUST Treat results as static linear isotropic simulation, not physical validation or evidence of fatigue, creep, wear, thermal behavior, buckling, life or regulatory compliance.
NOT A yield safety factor is not a fatigue, buckling, physical test or certification margin.

```text
L1 ANALYSIS {case}: {PASS|FAIL|NO-RESULT}; {peak stress} MPa, {displacement} mm, {yield safety factor}
   Evidence -- {analysis bundle and engineering check packet paths}
```
CAP 120w clean · 220w with failures

