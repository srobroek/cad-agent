---
name: cad-flow
description: Leads mechanical CAD from a rapid concept through a verified STEP AP242 release. Triggers on take this product from prototype to CAD release.
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# CAD Flow Lead

TRIGGER
+ “take this product from prototype to CAD release” or “run the CAD design flow”
+ a new or changing mechanical product needs a traceable design record and release
- a single modeling operation without a product workflow → use the relevant CAD tool directly

## Workflow

1. At GROUND, from the project root run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/setup.py`. Setup checks FreeCAD 1.1.4 first. A missing command or wrong version makes setup exit nonzero before any other action. Follow `cad-drawings` to install FreeCAD, then rerun setup.
   Add `--yes` to execute the remaining user-level setup actions. Pass `--harness claude|codex|opencode|kiro` to install harness files. Setup reruns preflight and prints its summary.
   Start the background warm-up with `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/preflight.py --warm --background` from the project root.
   Continue GROUND work while it runs.
   Before the first L1 LAYOUT build, run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/preflight.py --status --wait 90` from the project root.
   Act on its exit code:
   - 0: continue.
   - 3: run the status command again.
   - 1: fix the required check, then warm in the foreground until exit 0.
   - 2: warm in the foreground.
2. Establish the project's `hw/` design record and approved scope using `references/flow.md`.
   `cad-record` owns validation and path resolution. `../cad-record/references/layout.md` is the canonical layout table; `../cad-record/references/layout-api.md` is the command API.
   For an existing project, run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/validate.py PROJECT --migration-plan` before freezing. It reports a read-only move/rebuild plan.
   Keep legacy versions and v1 releases unchanged. Never reissue them.
   Before the first build, ask the user explicitly whether generated results (STEP, GLB, renders, check bundles) are stored in git, and record the answer as `product.yaml` `storage.results_in_git` with a DR. Then run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/setup.py --storage --yes` from the project root; it writes the git rules for that answer, and again whenever the answer changes. Only `true` tracks them, with Git LFS for STEP and GLB; with `false` they stay out of git and the record remains valid. While the setting is absent, commit no generated result.
   At INTENT, record the planned processes as `product.yaml` `manufacturing.processes` and ask whether each process and material can meet each appearance or property requirement: a transparent part printed in FDM is a known contradiction. Resolve each `feasibility.*` finding of `validate.py` with a DR and a record change before L1 LAYOUT.
3. Lead the stages in this order: GROUND, INTENT, SPECIFY, L0 CONCEPT, VARIANT gate, L1 LAYOUT, LAYOUT freeze, L2 PARTS, PARTS gate, L3 DETAIL, DETAIL gate, BOM, L4 ANALYSIS, ANALYSIS gate, CRITIQUE, RECONCILE, DESIGN freeze, RELEASE gate, RELEASE. Use the stage-enum spellings in `references/flow.md`. Do not skip a gate or freeze.
   Record RELEASE approval as `DR-###` before running `release.py`. At L0 CONCEPT, follow `../cad-concept/SKILL.md` to start and stop the viewer.
   Each L1-L4 gate is two gates: a provisional gate you record once the version passed every fast check, then the full check, then the human gate. The next stage may start after the provisional gate; nothing is frozen or released before the human gate. Read `references/flow.md` "Gate control" before the first L1 build.
4. Delegate every stage listed below: one worker per unit, all stage units in one parallel batch, then integrate and present the stage's gates. Do every other stage yourself. Read `references/flow.md` "Delegation" before the first batch.
5. At each stage, draft and preview without checks; collect feedback before commit. Each L1-L4 batch builds a new immutable version with `cad-model run` and checks it in the fail-fast order of `references/flow.md`: (1) the build, `validate.py` (record, retention and feasibility rules) and from L2 the form check; (2) `cad-model check --changed-only`; (3) once a joint moves, the smoke sweep (`sweep.py --smoke --changed-only`); (4) renders, the default motion render once a joint moves, and the visibility check; (5) after the provisional gate, the full check: `cad-model check` over every pair and the full sweep with its between-sample check. Run steps 1-4 in parallel where they do not depend on each other; any blocking failure stops the batch. Pass explicit check output as a new `.cad-work/` path. A coverage gap or limit finding of the motion render blocks the gate. Use `../cad-record/references/layout.md` and its API for every version or producer path.
   At L1, lay out each clearance at its required minimum plus the margin (`design.clearance_margin_mm`, 0.1 mm when absent, or the interface's `margin_mm`): 0.5 mm required is 0.6 mm designed.
   Run every long job (the full sweep, a FreeCAD assembly run, FEA, a new tool) as a smoke run first, then as a background job; wait for its result file, not its process. Prepare the gate packet while it runs.
6. Before PARTS, `cad-design form_check.py` must pass on the integrated version at F2; before DETAIL, at F3. Each made part needs the required fidelity. At L4, use producer commands to publish version-bound evidence bundles; version inputs come from the `record/` snapshot, and explicit output overrides use new `.cad-work/` paths. Never publish to ad-hoc directories.
7. At L3 DETAIL and release, use `cad-drawings` to draft, review and publish drawings for a selected version. Follow its documented CLI and scratch-output options; rejected drafts remain scratch. Record accepted drawing checks in `verification.csv`.
8. On COMMIT, record the design entry with batch, changes, artifacts, checks, and outcomes. If the user wants more detail, continue in the stage. Otherwise show renders and present the stage's gates.
   When the cad-recheck hook is not installed (`install-hook.py` exits 3 because `core.hooksPath` is managed outside the repository, or 2), run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/stamp.py --verify` from the project directory after staging each commit of `hw/` changes. Exit 2 means do not commit: fix the reason on stderr and verify again.
9. Keep gate evidence in the design record. Record a provisional gate yourself only with its fast-check evidence: a decision in `hw/decisions/DR-###.md` marked PROVISIONAL, temporary until the full check, then advance `product.yaml` `flow.stage` so the next stage can start. A failing full check, a MAJOR light-review finding, or a change named at the human gate voids it, also after the full check passed: record a VOID decision that supersedes it, set `flow.stage` back to the voided stage, and rebuild that stage and every later stage built on that version, as `references/flow.md` "Gate control" lists. List the sweep's unproved between-sample intervals at the human gate; the user accepts them with a DR or asks for a finer run. At a human gate, ask the user. Only on explicit approval, record it as a decision in `hw/decisions/DR-###.md` and advance `product.yaml` `flow.stage` where its provisional gate has not.
10. Finish at a CAD-only release: STEP AP242, drawings, EBOM, verification matrix, and documentation. Do not produce slicer, CAM, or machine-tool output.

## Delegation

| Stage | One worker per | Agent | You integrate |
|---|---|---|---|
| L0 CONCEPT | variant, 2-4 | `cad-concept-designer` | Merge the variants into the scene, check it, and run the preview |
| L2 PARTS | subassembly; purchased family; light review | `cad-part-designer`; `cad-sourcing-researcher`; `cad-mech-reviewer` and `cad-dfm-reviewer` | Apply accepted proposals, then build and fast-check one version; run the light review beside its full check |
| L3 DETAIL | made part | `cad-part-designer` | Apply accepted fit proposals, then build, check, F3-check, render, and draw one version |
| L4 ANALYSIS | case: sweep, FreeCAD assembly, stack-up, FEA, motion; the joint sweep and FEA at its worst pose run as the full check | `cad-analyst` | Write verification rows, publish accepted motion cases, and render the analysed version |
| CRITIQUE | reviewer | `cad-mech-reviewer`, `cad-dfm-reviewer` | RECONCILE |
| ECO, post-freeze change | affected part, then affected case | `cad-part-designer`, `cad-analyst` | As L3, then as L4 |

GROUND, INTENT, SPECIFY, L1 LAYOUT, BOM, RELEASE, and every gate stay with you. Keep a unit list and each unit's state in your plan. Dispatch all units of a stage in one parallel batch when supported, and integrate when all return; otherwise run the same briefs in sequence.

## Rules

MUST Preserve shared record names, fields, units, PN/revision patterns, and schemas in `references/flow.md`.
MUST Route a stage to its listed skill and exact command; a skill or agent marked later is unavailable, not permission to invent its procedure.
MUST Run each L1-L4 commit batch as `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-model/scripts/cad_model.py run hw --label vN [--kernel auto] [--attach REF=PARENT_REF ...]`, then the fast checks of `references/flow.md` "Fail-fast check order": `validate.py`, the form check from L2, `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-model/scripts/cad_model.py check vN --hw hw --changed-only [--since-version vM] [--out FILE]`, once a joint moves `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-motion/scripts/sweep.py --hw hw --version vN --smoke --changed-only`, and `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-render/scripts/render.py hw [--version latest|vN] [--views iso front side exploded closeup-<PN>]` for that version, which also runs the visibility check and exits 1 when a `visibility` binding fails (results in `render.json` `visibility`). Give part and detail workers the same `--attach` entries for their `scratch.py build`. Press fits in `hw/interfaces.yaml` are automatically allowed and a blocking check failure stops the batch. Once it has an independent movable joint, also run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-motion/scripts/motion.py render --hw hw --version vN --case default --outputs glb,sheet`; exit 1, a coverage gap or limit finding, blocks the gate; exit 2 is no result. After the provisional gate, run the full check: `cad_model.py check vN --hw hw` over every pair and, once a joint moves, `sweep.py --hw hw --version vN` with its between-sample check.
MUST Use FreeCAD 1.1.4 as the CAD application. CadQuery 2.8 and build123d 0.13 use separate environments.
MUST Keep rough-to-detailed iteration: concepts establish direction, layout freezes interfaces and envelopes, parts precede fits and drawings, and release follows verification and critique.
MUST At a human gate, present the gate summary, ask the user, and record approval as a DR decision only after explicit approval; a single human gate advances `product.yaml` `flow.stage` only then. Record a provisional gate only with its fast-check evidence, its decision marked PROVISIONAL; it advances `flow.stage` so the next stage can start and approves nothing.
MUST If you cannot ask at a human gate, write open questions and proposed assumptions in its summary. Keep the gate open and stage unchanged, then stop. Do not proceed on assumptions.
MUST Dispatch every unit of a stage in the same parallel batch when the harness supports parallel subagents; otherwise run the units in sequence with the same briefs.
MUST Own the shared record yourself: `product.yaml`, `requirements.yaml`, `assembly.yaml`, `interfaces.yaml`, `bom.csv`, `verification.csv`, `decisions/`, concept scene, all `hw/versions/` builds, renders, integrated drawings, gates, and questions to the user. A worker writes only `.cad-work/<unit>/`; a part designer also replaces its own `hw/parts/<PN>/` through `scratch.py deliver`. Give each PN to exactly one unit.
MUST Give every file you mention in a report, question, gate, or summary as an absolute path on its own line: records, decisions, model.py, STEP and GLB, check JSON, renders, captures, drawings, and BOM. Add the URL of the live viewer or preview gallery while one runs.
MUST In release, integration, and gate reports, present print/make files, parts to buy (designation/order codes and OPEN identities with reasons), assembly steps, and checks as structured lists or tables. Give every file path as its own absolute-path line; never report buy or assembly items in inline comma prose.
NOT Claim a design is checked because it was previewed or because an unchanged part passed an earlier run.
NOT Count a changed-only check, a smoke sweep, or a provisional gate as a full check, verification evidence, or user approval; never freeze or release on one.

