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
   Project-start question 9 asks whether generated results (STEP, GLB, renders, check bundles) are stored in git; its answer in `flow.profile` `storage_results_in_git` is the setting, so ask nothing again. Without a profile answer, ask the user before the first build and record `product.yaml` `storage.results_in_git` with a DR; if both are set they must agree, or setup refuses. Then run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/setup.py --storage --yes` from the project root; it writes the git rules for that answer, and again whenever the answer changes. Only `true` tracks them, with Git LFS for STEP and GLB; with `false` they stay out of git and the record remains valid. While the setting is absent, commit no generated result.
   At GROUND, ask the focused use-case questions in `references/manufacturing-selection.md`. Keep requirements, preferences, bounded assumptions and open questions separate. Do not assume an available printer, material or process.
   At INTENT, run `cad-record/scripts/selection.py <project|hw> [--json] [--out .cad-work/<report>.json]` to assess recorded alternatives; it is report-only. Record its `input_sha256` as the selection assessment digest, then record any confirmation and approval decision. Re-assess and re-confirm when digest inputs change. Unknown material/process selection blocks design freeze and release, as does any other blocking unknown. Compare full per-part architecture/material/route candidates against mandatory requirements before ranking preferences. Optical requirements exclude opaque routes and require traceable purchased transparent parts or identified transparent stock on a suitable evidenced route. Ask for approval before consequential changes.
   At SPECIFY, record requirements, criteria, interfaces, capability separately from requirements, part routes and evidence obligations. Use `cad-design/references/process-checks.md`, `cad-fits/references/process-fits.md` and the owning producer scripts for route-specific checks and calculations.
3. Lead the stages in order: GROUND, INTENT, SPECIFY, L0 CONCEPT, VARIANT gate, L1 LAYOUT, LAYOUT freeze, L2 PARTS, PARTS gate, L3 DETAIL, DETAIL gate, BOM, L4 ANALYSIS, ANALYSIS gate, CRITIQUE, RECONCILE, DESIGN freeze, RELEASE gate, RELEASE. Use the stage-enum spellings in `references/flow.md`; do not skip a gate or freeze. Run `release.py` only after the RELEASE gate approves, with that approval recorded as its `DR-###`.
   Each L1-L4 gate is two gates: a provisional gate you resolve once the version passed every fast check, then the full check, then the human gate. The next stage may start after the provisional gate; nothing is frozen or released before the human gate. LOAD `references/flow.md` "Gate control" before the first L1 build.
   At L0 CONCEPT, follow `../cad-concept/SKILL.md` for launching and stopping the concept viewer.
4. Delegate each stage that the Delegation table lists. Use one worker per unit, sized as the table says. Run at most 3 writer units at once. Read-only units (reviewers, sourcing researchers) run alongside them. When a unit's final report arrives, merge it. After the last merge, run one integration build and the stage's gates. Do every other stage yourself. LOAD `references/flow.md` "Delegation" before the first batch.
5. Run each check at the iteration where `references/flow.md` "Checks by iteration" places it: inner loop, commit, provisional gate, full gate. At each stage, iterate on previews without checks. Before a commit, collect feedback. Each L1-L4 batch builds a new immutable version with `cad-model run`. Check it with `evidence.py run --scope commit`, which runs the fail-fast order of `references/flow.md`:
   1. `validate.py` (record and intent gates) and, from L2, the form check.
   2. `cad-model check --changed-only`.
   3. Once a joint moves, the smoke sweep (`sweep.py --smoke --changed-only`).
   4. `cad-render`, which also runs the visibility check, and the default motion render.
   5. After the provisional gate, the full check with `evidence.py run --scope full --stage <gate stage>`. The provisional approval has already moved `flow.stage` on, so name the stage of the gate it passed. The check covers `cad-model check` over every pair and the full sweep with its between-sample check.
6. Before PARTS, `cad-design form_check.py` must pass on the integrated version at F2; before DETAIL, at F3. Each made part needs the required fidelity. Record-v2 process checks, assembly-access/play and required evidence packets must also cover their obligations. Read `cad-design/references/process-checks.md`; a missing handler, packet or capability is UNKNOWN, not PASS. L4 uses producer commands to publish version-bound evidence bundles. Version inputs come from the `record/` snapshot. Put explicit output overrides under `.cad-work/`; never publish to ad-hoc directories.
7. At L3 DETAIL and release, use `cad-drawings` to draft, review and publish drawings for a selected version. Follow its documented CLI and scratch-output options; rejected drafts remain scratch. Record accepted drawing checks in `verification.csv`.
8. On COMMIT, record the design entry with batch, changes, artifacts, checks, and outcomes. If the user wants more detail, continue in the stage. Otherwise show renders and present the stage's gates.
   Before each commit of `hw/` changes, run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/stamp.py --verify` from the project directory after staging. Exit 2 means do not commit. Fix the stderr reason and verify again.
9. Keep gate evidence in the design record. Record a provisional gate yourself only with its fast-check evidence: a decision that `write_decision` writes to `hw/decisions/DR-###-slug.md` with `status: proposed`, temporary until the full check, then advance `product.yaml` `flow.stage` so the next stage can start. A failing full check, a MAJOR light-review finding, or a change the user names at the human gate voids it. Record a decision with `status: rejected` that supersedes it. Set `flow.stage` back to the voided stage and rebuild that stage and every later stage built on that version. List the sweep's unproved between-sample intervals at the human gate. The user accepts them with a DR or asks for a finer run, as `references/flow.md` describes.
10. Finish at a CAD-only release: STEP AP242, drawings, EBOM, verification matrix, and documentation. An FDM part's 3MF carries geometry, orientation and material name only. Do not produce slicer, CAM, or machine-tool output.

## Delegation

| Stage | One worker per | Agent | You integrate |
|---|---|---|---|
| L0 CONCEPT | variant, 2-4 (3 run at once) | `cad-concept-designer` | Merge the variants into the scene, check it, and run the preview |
| L2 PARTS | subassembly of at most 4 PNs; purchased family; light review | `cad-part-designer`; `cad-sourcing-researcher`; `cad-mech-reviewer` and `cad-dfm-reviewer` | Apply accepted proposals, then build and fast-check one version; run the light review beside its full check |
| L3 DETAIL | group of at most 3 made parts | `cad-part-designer` | Apply accepted fit proposals, then build, check, F3-check, render, and draw one version |
| L4 ANALYSIS | case (width = cores ÷ solver threads, at most 3): sweep, FreeCAD assembly, stack-up, FEA, motion; the joint sweep and FEA at its worst pose run as the full check | `cad-analyst` | Write verification rows, publish accepted motion cases, and render the analysed version |
| CRITIQUE | reviewer | `cad-mech-reviewer`, `cad-dfm-reviewer` | RECONCILE |
| ECO, post-freeze change | affected part, then affected case | `cad-part-designer`, `cad-analyst` | As L3, then as L4 |

GROUND, INTENT, SPECIFY, L1 LAYOUT, BOM, RELEASE, and every gate stay with you. Keep a unit list and each unit's state in your plan. When one of 3 running writer units returns, start the next unit. Merge each unit on its final report. A report that says background work is still running is partial: the unit is not done, so merge nothing from it. If the harness cannot run subagents in parallel, run the same briefs in sequence. <!-- TODO(stage-machine): work.py ready lists the units to dispatch and work.py stale lists expired leases. -->

## Rules

MUST Preserve shared record names, fields, units, PN/revision patterns, and schemas in `references/flow.md`.
MUST Route a stage to its listed skill and exact command; a skill or agent marked later is unavailable, not permission to invent its procedure.
MUST Run each L1-L4 commit batch as `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-model/scripts/cad_model.py run hw --label vN [--kernel auto] [--attach REF=PARENT_REF ...]`, then the fast checks of `references/flow.md` "Fail-fast check order" with `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/evidence.py run --hw hw --version vN --scope commit [--since-version vM] [--out .cad-work/<path>.json]`. It runs `validate.py`, the form check from L2, `cad_model.py check --changed-only`, once a joint moves the smoke sweep, `render.py` with its visibility check (results in `render.json` `visibility`) and the default motion render, and stops at the first blocking result. Exit 1 blocks the gate. Exit 2 is no result or a stray run, and the report lists the run's `stop_commands`. Give part and detail workers the same `--attach` entries for their `scratch.py build`. The checks allow the press fits in `hw/interfaces.yaml`. After the provisional gate, run the full check with `--scope full --stage <gate stage>`. The approval has already moved `flow.stage` on, so the run refuses to start without the gate's stage. It runs `cad_model.py check` over every pair and, once a joint moves, the full sweep with its between-sample check. Its exit 1 includes a blocked stage gate.
MUST Use FreeCAD 1.1.4 as the CAD application. CadQuery 2.8 and build123d 0.13 use separate environments.
MUST Keep rough-to-detailed iteration: concepts establish direction, layout freezes interfaces and envelopes, parts precede fits and drawings, and release follows verification and critique.
MUST At a human gate, present the gate summary, ask the user, and record approval as a `status: accepted` DR decision only after explicit approval. A single human gate advances `product.yaml` `flow.stage` only then. Record a provisional gate only with its fast-check evidence and a `status: proposed` decision; it advances `flow.stage` so the next stage can start and approves nothing.
MUST If you cannot ask at a human gate, or the question is cancelled or unanswered, that is no consent. Write open questions and proposed assumptions in its summary. Keep the gate open and stage unchanged, state that work waits for the user, and stop. Never record assumptions as approval.
MUST Run at most 3 writer units at once, plus any read-only units. When a unit's final report arrives, merge it. "Background work still running" means partial, not done. Handle a stalled unit as `references/flow.md` "Stalled units" says.
MUST Run no foreground command longer than 600 s. Start a longer job in the background. When the command exits, read the output path the producer reports; `references/flow.md` "Long jobs" lists each producer's key.
MUST Allow only one active lead session per project. Before resuming, stop or reconcile the previous turn and re-read `hw/` before writing.
MUST Own the shared record yourself: `product.yaml`, `requirements.yaml`, `assembly.yaml`, `interfaces.yaml`, `bom.csv`, `verification.csv`, `decisions/`, concept scene, all `hw/versions/` builds, renders, integrated drawings, gates, and questions to the user. A worker writes only `.cad-work/<unit>/`; a part designer also replaces its own `hw/parts/<PN>/` through `scratch.py deliver`. Give each PN to exactly one unit.
MUST Give every file you mention in a report, question, gate, or summary as an absolute path on its own line: records, decisions, model.py, STEP and GLB, check JSON, renders, captures, drawings, and BOM. Add the URL of the live viewer or preview gallery while one runs.
MUST In release, integration, and gate reports, present print/make files, parts to buy (designation/order codes and OPEN identities with reasons), assembly steps, and checks as structured lists or tables. Give every file path as its own absolute-path line; never report buy or assembly items in inline comma prose.
NOT Claim a design is checked because it was previewed or because an unchanged part passed an earlier run.
NOT Count a changed-only check, a smoke sweep, or a provisional gate as a full check, verification evidence, or user approval; never freeze or release on one.
MUST Never default a process, material, property, capability, tolerance, evidence result or service requirement; unknown is not PASS.
MUST Eliminate mandatory failures before preference scoring, and rank only candidates that pass every mandatory requirement.
MUST Distinguish geometry, process planning, simulation and physical evidence; no preview, heuristic, envelope fit, solver result or planned test proves physical performance.
MUST Treat changes to service, material/condition, route/capability, orientation/support/finish, geometry, interfaces, criteria or source as possible evidence impacts; recheck from the earliest affected stage.
MUST Record service class as intended-use maturity and consequence. Prototype is a service class.
MUST Block design freeze and release when material/process selection is unrecorded or another required input/evidence remains unknown.
NOT Claim see-through FDM; optical/transparency requirements require traceable transparent purchase/stock criteria and a suitable route.
