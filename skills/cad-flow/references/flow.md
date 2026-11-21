# CAD flow reference

## Stages, gates, and durable outputs

Keep the source of truth in the hardware project's `hw/` design record. `cad-record/references/layout.md` is the generated canonical path table; commands that consume path locations use the `cad-record/references/layout-api.md` API. Every decision, requirement, interface, revision, check result, and release artifact must identify its source or evidence. Preserve millimetres and the product's declared ISO or ASME regime. Stage ordering is intentional: do not detail parts before the layout freezes their interfaces, or release before analysis and critique are reconciled.

| Stage | Work and user gate | Design-record output | Skill / command |
|---|---|---|---|
| GROUND | Inspect needs and existing hardware. Ask focused use-case questions about purpose/users, service class, loads, motion/duty/life, environment, accuracy/finish, failure consequences/standards, quantity/budget/lead time, maintenance/repair and actual equipment/operator/outsourcing. | Product use case; requirements, preferences, bounded assumptions and open questions kept separately; sources and capability availability distinguished. | `cad-record`; see [manufacturing intent and route selection](manufacturing-selection.md) |
| INTENT | Compare sourced whole-architecture, per-part material and ordered process routes against the recorded service and mandatory criteria. Unknowns stay conditional; preferences rank only candidates passing every hard constraint. Wait for the existing approval gate before consequential changes. | Candidate comparison, primary and conditional alternatives, costs/lead times as sourced or uncertain estimates, evidence gaps, approved selection decision. | `cad-record` `selection.py`; see [manufacturing intent and route selection](manufacturing-selection.md) |
| SPECIFY | Turn approved intent into measurable requirements/evidence obligations and functional interfaces, fits, tolerance states, assembly access and release scope. | Requirement criteria, interfaces, per-part function/material/route/capability refs, evidence plan and open questions. | `cad-record`, `cad-fits`; see [manufacturing intent and route selection](manufacturing-selection.md) |
| concept | One `cad-concept-designer` per variant drafts 2-4 rapid three.js variants, 3 at a time; merge them, then refine, merge, or drop by feedback. | Scene, options and selection in a decision record. Preview is not verified CAD. | `cad-concept`: `concept.py check <scene>`; handoff: `concept.py handoff <scene>` |
| VARIANT gate | Get explicit concept selection. | Selected concept and gate evidence in a decision record. | `cad-concept`: checked handoff |
| layout | Define subassemblies, interfaces, joints/limits, BOM skeleton. Lay out each clearance at its minimum plus the [clearance margin](#clearance-margin). | Assembly and interfaces; preliminary EBOM. | `cad-record`: validate. Each version runs the [fail-fast checks](#fail-fast-check-order), then the full check. |
| LAYOUT freeze | Provisional gate after the fast checks; L2 may start. Human gate after the full check: present layout, interfaces, BOM skeleton, and the default motion render for approval. | Provisional and frozen decisions with gate evidence. | `cad-viewer`: serve, show version and compare checks; `viewer.py animate --case default` plays the motion. |
| parts | One `cad-part-designer` per subassembly of at most 4 PNs and one `cad-sourcing-researcher` per purchased family, within the writer cap; build one integrated version; a light mechanical and DFM review runs beside its full check; PARTS gates. | Part records and models; PN, revision, material, process, finish, mass target, make/buy, supplier; torque paths and axial retainers. | Fail-fast checks with `cad-design form_check.py --hw hw --parts-dir <version parts> --fidelity F2`; `cad-mech-reviewer` and `cad-dfm-reviewer` for the light review. |
| detail | One `cad-part-designer` per group of at most 3 made parts applies fits, fasteners, tolerances, and process rules. Build and check one integrated version, and draw it while its full check runs, for the DETAIL gates. | Part revisions, fit decisions, drawings, and evidence. | `cad-fits`; `cad-design form_check.py` at F3; `cad-drawings` publishes each part drawing for that version. |
| bom | Reconcile part numbers, revisions, quantities, make/buy, suppliers, and assemblies. | Released-intent EBOM. | `cad-bom`: `bom.py --hw hw` or `bom.py --version vN --hw hw` to reconcile against a version snapshot. |
| analysis | One `cad-analyst` per case checks interference, clearance, stack-up, kinematics, static FEA, or a motion case. The joint sweep, then FEA at its worst pose, run as the stage's full check; ANALYSIS gates. | Verification tied to requirements/configurations; analysis artifacts and assumptions; accepted motion cases in `hw/motion/cases/<case>/plan.yaml`. | Use `cad-motion`, `cad-analysis` and `cad-fits` on the selected version. Case results publish through cad-record layout; verification links identify the versioned evidence bundle. The lead publishes an accepted motion plan with `motion.py render --hw hw --version vN --case <case> --outputs glb,sheet,video`; a record with coupled joints adds `--crosscheck freecad` and a verification row from `crosscheck.json`, where only `solved` joints count as independent evidence. |
| critique | Spawn mechanical and DFM reviewers as parallel read-only units. They may start on the provisional ANALYSIS gate, while the full check runs. | Preserve both verdicts and evidence in the workflow record. Neither reviewer waits for the other. | `cad-review-mech`; `cad-review-dfm` |
| reconcile | Resolve critique findings. Rerun affected checks. | Updated decisions, verification, revisions, dispositions. | `cad-flow`; `cad-recheck` stamps the exact staged `hw/` tree only after checks pass. |
| design_freeze | After every stage's human gate, present reconciled design, analyses, and record for approval. | Frozen anchor pins the exact built version and its manifest hash. | `cad-record`: validate project and confirm every custom-made part has complete manufacturing outputs. |
| RELEASE gate | Show the frozen version, the revision letter to issue, its contents and verification. Get approval before release. | Gate decision, recorded in `decisions/DR-###-slug.md`. | `cad-record`: `validate.py` and the structured checks. |
| release | After approval, seal the traceable release package. | Immutable release package with definitions, manufacturing outputs, selected bundles, EBOM, verification, and docs. | `cad-record`: first `build_guide.py hw --version vN --rev A --date YYYY-MM-DD --out hw/docs` (the UTC date of the release run), then `release.py HW --rev A --version vN --decision DR-###`; a missing/stale/failing item refuses and seals nothing. Never reuse a revision. Add `--prune` to print the plan of superseded versions once A is sealed (a rerun on a sealed A skips sealing); show the user its `keep` reasons and `remove` list, then rerun with `--prune --yes <digest>`. Only versions older than vN go; released, latest, frozen, change-baseline, DR-named and manifest-referenced versions stay. `prune_incomplete` means rerun the same `--yes` command to finish; it also runs `cad_model.py store gc`. |

When a stage starts the CAD viewer, keep it available through that stage's review. When the stage is complete, tell the user its URL is going away, then run the exact `stop` command printed by `serve` and record `ports_free` before ending the stage.

The canonical `product.yaml` stage enum uses lowercase values: `ground`, `intent`, `specify`, `concept`, `layout`, `parts`, `detail`, `bom`, `analysis`, `critique`, `reconcile`, `design_freeze`, `release`. Prose may use uppercase or L0 to L4; record values use these enum strings.
The endpoint is a CAD definition, drawings, EBOM, scoped verification and documentation. The supported manufacturing process set is defined by route records and implemented check coverage, not by the historic FDM/CNC examples. A missing handler or capability is reported as unknown coverage, never a substitute process or PASS. The plugin does not generate CAM/slicer toolpaths, operate equipment, or certify a physical or regulated product. See `cad-record/references/layout.md` for paths.

## Stage loop and recheck boundary

Draft, show live previews, and collect feedback. Continue until the user says “commit” or “commit batch”. Do not check on every edit.
Allow only one active lead session per project. Before resuming, stop or reconcile the previous turn, then re-read the `hw/` record before writing.
- Each L1-L4 commit batch builds one version and checks it in the [fail-fast order](#fail-fast-check-order). Stop on any blocking failure. Show that exact version's renders at the stage gates.
- An explicit check output is a new path under `.cad-work/`; generated version and producer paths come from the cad-record layout API.
- The user's commit or the layout threshold triggers broader changed-parts/dependents recheck. Use `flow.recheck_from` and applicable earlier-stage checks; stamp the exact staged `hw/` only after every required check passes.
- Verify each staged `hw/` tree before committing. From the project directory, run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-recheck/scripts/stamp.py --verify` after staging. Exit 0 permits the commit. Exit 2 means do not commit; fix stderr's reason, restamp, and verify again.
- Record changed items, dependents, checks, results, and artifacts. A later edit starts a new batch on the next commit.
- After DESIGN freeze, reopen only affected parts and dependents. Preserve unaffected parts and their evidence. Before another release, build a new version and re-freeze its exact manifest anchor.
- A preview or render is not a check result. Tie reported checks to a design revision/configuration.

Once a version has an independent movable joint, each commit batch also runs the default motion render beside cad-render: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-motion/scripts/motion.py render --hw hw --version vN --case default --outputs glb,sheet`. Only contact-sheet tiles render, so it takes seconds to a minute. Exit 1, a coverage gap or a limit finding, blocks the gate; exit 2 is no result. The LAYOUT freeze gate shows:

```text
L1 MOTION RENDER vN default: PASS|FLAGGED|FAIL, {frames} frames, {segments} segments, {timings_s.total} s
   Coverage -- {summary}
   Files -- {contact-sheet.png absolute path}, {motion.glb absolute path}
```

### Clearance margin

Lay out every clearance at its required minimum plus the margin: the interface's `margin_mm`, else `product.yaml` `design.clearance_margin_mm` (0.1 mm when absent). A 0.5 mm minimum is laid out at 0.6 mm. A measured clearance at or above the minimum but inside the margin is a `margin` warning, never a failure; below the minimum it fails.

## Fail-fast check order

Check each version a commit batch builds, cheapest first. Steps 1-4 are the fast checks, and step 5 is the full check. `evidence.py run --hw hw --version vN --scope commit` runs steps 1-4, and `--scope full --stage <gate stage>` runs step 5. The provisional approval has already moved `flow.stage` on, so the full scope refuses to start without the stage of the gate. Each run starts with `validate.py` in process, then runs the commit checks in parallel or the full checks one after another. It reads their results in step order and stops the checks that still run at the first failure. After a failure, fix the cause and build a new version: never start step 5 on the failed one.

1. Build the changed parts: `cad-model run hw --label vN`. Run the record validator `validate.py <project-dir>`, which enforces the record version, recorded selection and evidence gates. From L2, run the form check on the built version; legacy `--parts-dir` diagnostics do not establish route manufacturability. Record-v2 process-dispatched checks and `cad-motion` access/play producers publish packets for their own obligations. Missing handlers, coverage, capability or required packets are UNKNOWN, not PASS. See the skill-specific manufacturing references for commands and findings.
2. Static interference on the changed pairs: `cad-model check vN --hw hw --changed-only [--since-version vM]`. The baseline defaults to the previous built version; without one, every pair is selected.
3. Once the version has an independent movable joint, the smoke sweep on the changed pairs: `cad-motion/scripts/sweep.py --hw hw --version vN --smoke --changed-only [--since-version vM]`. It samples every 60 degrees of each revolute range plus its endpoints, and each TDC and dwell event.
4. Renders: `cad-render/scripts/render.py hw --version vN`, which also runs the visibility check of each `visibility` binding in `requirements.yaml`. It exits 1 when a binding fails; each target must reach the binding's `min_visible_fraction` on its own, and `render.json` `visibility` holds the results, summarised as `visibility.<REQ>/<index>`. Once a joint moves, also the default motion render.
5. After the provisional gate, run the full static check and motion sweep where applicable. Run each producer needed by the record and stage, then verify its packet covers its expected obligations. Stack-up results currently lack a producer packet and stay UNKNOWN until accepted ledger evidence is recorded. See the owning skills for producer commands.

A changed-only check or a smoke sweep is fast-check evidence only. Its result reports an incomplete scope (the static check's `scope.complete: false`) and never counts as a full check, a full assembly pass, or verification evidence. The pair cache makes unchanged pairs cheap in step 5; never skip step 5 because an earlier version passed it.

## Checks by iteration

Each producer runs at the iteration below and not earlier. "Only when stale" means `impact.py` marks the evidence family stale or open, and due at this stage. Unaffected parts keep their evidence. `evidence.py run --scope commit` runs the commit checks below that its matrix holds: `validate.py`, the form check, static `--changed-only`, the smoke sweep, the renders and the default motion render. `--scope full` runs the full gate's `validate.py`, form check, static check over every pair, full sweep and motion render. Every other command runs by hand.

**Inner loop.** A designer iterates in a scratch workspace on the unit's own PNs.
- Always run:
  - `validate.py`
  - the scratch build (`scratch.py build`), from L1
  - the form check on the unit's PNs (F1 at L1, F2 at L2, F3 from L3), which the scratch build runs
  - `evidence.py check --hw .cad-work/<unit>/hw --version <label> --stage <stage>`, with the label from the build report
- The target static check in the loop is `--changed-only`. Today `scratch.py build` runs the static check over every pair. Accept that run. <!-- TODO(stage-machine): `evidence.py run --scope inner` narrows the static check to the changed pairs. -->
- The evidence check covers the whole scratch record, including PNs outside the unit. <!-- TODO(stage-machine): `evidence.py run --scope inner` scopes the evidence check to the unit's PNs. -->
- At L3, also run fits and stack-up. Check drawing completeness for the unit's own PNs.
- If a moving pair changed, run a smoke sweep.
- If a requirement binds visibility, render.
- An analyst runs a coarse-mesh FEA smoke case before the real case.
- Never run these in the loop:
  - the full sweep
  - the motion render
  - the integrated drawings
  - a separate all-pairs check

**Commit (batch vN).** The lead builds one version and checks it in the [fail-fast order](#fail-fast-check-order).
- Always run:
  - `validate.py` and the build
  - static `--changed-only`
  - the BOM, from L1
  - fits and stack-up, from L3
- If the form check of a changed PN is stale, run it on that PN.
- Once a joint exists, run the smoke sweep.
- If a part's drawings are stale, draw them from L3.
- If sourcing and selection are stale, run them.
- At each git commit of `hw/`, run the recheck stamp.
- Today the batch also runs the renders and the default motion render, because the provisional gate cites them. <!-- TODO(stage-machine): the motion render moves from the commit to the provisional scope. -->

**Provisional gate.** It reads the commit results and runs no static or sweep check again.
- It needs these results for vN:
  - `validate.py`
  - the 4-view render with its visibility check
  - the default motion render, once a joint moves
  - the BOM, fits and stack-up
  - the form, static and smoke results
- It runs `evidence.py check` for the gate's stage and records them in the provisional decision.
- After it writes the decision and advances `flow.stage`, run `evidence.py check --stage <gate stage>` again, because `product.yaml` is a record input and the check refuses an earlier packet. The commit's stamp is at the gate stage just passed, one stage behind `flow.stage`; `stamp.py --verify` accepts that stage or `flow.stage`, never one below `flow.recheck_from`.

**Full gate (approval).** The full check on the same vN, before the human gate.
- Always run:
  - `validate.py`
  - the static check over every pair
  - the form check for every due obligation
  - the motion render
  - the BOM, fits and stack-up
  - `evidence.py check` and the recheck stamp
- Run the full sweep at L1 and L4. Also run it at L2 and L3: a skip needs a hash-proven carry-forward, and no tool proves one yet. <!-- TODO(stage-machine): carry-forward of a passing full sweep at L2 and L3, checked by evidence.py run. -->
- At L3, draw the parts and the assembly with `jobs` above 1.
- At L4, run every FEA case. At RECONCILE, run only the cases `impact.py` marks stale.
- At INTENT and L2, run sourcing and selection.

**Design freeze.**
- Run:
  - `validate.py`
  - the BOM
  - fits and stack-up
  - `evidence.py check`
  - the stamp
- If the full sweep, drawings or FEA are stale, run them again.
- `build_guide.py` may preview.

**Release.**
- Run:
  - `validate.py`
  - the BOM
  - `build_guide.py`
  - `release.py`
- `release.py` runs the evidence check itself.

## Long jobs

The full sweep, a FreeCAD assembly run, an FEA case, and the first run of a new tool are long jobs.

- Run no foreground command longer than 600 s. Start any command that can exceed it as a background job.
- Smoke first: run the sweep with `--smoke` before the full sweep, a FreeCAD run on its first few frames, and a new tool on a tiny fixture. Start the full run only after its smoke run passed.
- Before starting, check for stray runs: a sweep refuses while a recorded process group still runs and prints the `kill -TERM -- -PGID` command. Stop a run by its process group, never by its parent PID.
- Run it as a background job. When the command exits, read the output path its report names. Exit code 2 means no result: rerun it.
- Read the path from the producer's stdout JSON, as the table says. Never guess a result filename.
- While it runs, prepare the gate packet ([Gate control](#gate-control)).

| Producer | Where the output path appears | Key or location |
|---|---|---|
| `cad_model.py run` | stdout JSON | `version_dir`, `label` |
| `cad_model.py check` | stdout JSON only; no path. With `--out <path under .cad-work/>` it also writes that file | the `--out` path you passed; read stdout otherwise |
| `scratch.py build` | stdout JSON report | `scratch_hw`, `version_dir`, `label`; `json` and `log` in each step |
| `form_check.py --version` | stdout JSON | `manufacturing.bundle`; with `--out`, the path you passed |
| `sweep.py` | stdout JSON | `bundle` for a completed versioned run, smoke or full |
| `motion.py render` | stdout JSON | `bundle`, plus `paths` on a fresh run only (a reused bundle omits `paths`); with `--out-dir`, `out_dir` and `paths` |
| `render.py` | stdout JSON | `out_dir` |
| `fea_static.py --hw --version` | stdout JSON | `bundle`, `check_bundle` |
| `fea_static.py` on a bare case | stdout JSON, with no path | pass `--out`; the result file is that path |
| `drawing.py` | stdout JSON | `output_dir` in each entry of `parts` |
| `assembly_drawing.py` | stdout JSON | `output_dir` |

<!-- TODO(stage-machine): evidence.py run and work.py report every output path in one JSON document. -->

## Storage of generated results

Project-start question 9 asks whether generated results (STEP, GLB, renders and check bundles) are stored in git. Its answer in `flow.profile` `storage_results_in_git` is the setting, so setup does not ask again. Without a profile answer, ask the user before the first build and record `product.yaml` `storage.results_in_git` with a DR. If both are set they must agree, or setup refuses. Then run `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/setup.py --storage --yes` from the project root. It writes the rules for the answer into `hw/.gitignore` or `hw/.gitattributes` (cad-record `references/layout.md` "Git storage"); run it again whenever the answer changes. Only `true` tracks them, with Git LFS for STEP and GLB. With `false` they stay out of git, and the record remains valid. While the setting is absent the question is open: ask it, and commit no generated result.

A `.gitignore` untracks nothing. When git already tracks results and the answer is not `true`, setup lists those paths. The user stops tracking them with `git rm -r --cached <path>` (the files stay on disk) and a commit; never run it yourself. With `true`, setup never runs `git lfs` either: the user installs Git LFS and runs `git lfs install` once per clone.

## Delegation

The lead orchestrates; workers do independent stage units. Only the lead builds `hw/versions/`.

- Run at most 3 writer units at once.
- A writer unit writes files. Every unit except a reviewer or a sourcing researcher is one.
- Read-only units (reviewers and sourcing researchers) run alongside the writers. They do not count toward the cap.
- When a running unit returns, start the next queued unit. A stage with 4 variants runs 3, then the fourth.
- When a unit's final report arrives, merge it:
  - apply an accepted part delivery
  - adopt cited sourcing values
  - write verification rows
- After you merge the last unit of the stage, run the integration build once.
- A report that says background work is still running is partial. The unit is not done. Merge nothing from it and wait for its final report.
- If a harness cannot run parallel subagents, run the same briefs in sequence.

<!-- TODO(stage-machine): work.py ready replaces the lead's manual unit list; work.py stale replaces the manual stall check. -->

### Unit sizes

| Stage | Unit | Width | Tier |
|---|---|---|---|
| ground, intent | lead; optional read-only `source-<subject>` researchers | at most 3 | standard |
| concept | `variant-<id>` | 2-4 variants, 3 at once | standard |
| layout | lead, in scratch `layout` | 1 | strong |
| parts | `part-<subassembly>`, at most 4 PNs each; read-only `source-<family>` | 3 writer units at once | strong; standard for research |
| detail | `detail-<group>`, at most 3 PNs each | 3 writer units at once | strong |
| bom | `clerk-<chore>` | 1 | cheap |
| analysis | `analysis-<case>` | cores ÷ solver threads, at most 3 | standard |
| critique | 2 read-only reviewers | 2 | strong |
| reconcile | rework units scoped by `impact.py`, sized as parts or detail | 3 writer units at once | as parts or detail |
| design_freeze, release | lead | 1 | lead |

Tier guard: the cheap tier never runs these agents:
- reviewers
- analysts
- researchers
- any agent that issues a verdict

Each gate cites the evidence packet, never a summary.

### Stalled units

A subagent sends no heartbeat. Check each unit that runs past its expected duration.

1. If a unit's lease expired and it has no live run, send it one follow-up asking for status or its final report.
2. If the unit makes no progress for 10 more minutes:
   - Interrupt it.
   - Release its claim.
   - Dispatch it again with the same brief under a new unit id: `<unit>-r2`, then `-r3`, and so on.
3. Leave the old workspace untouched. The new unit id gets its own workspace.
4. Never merge output from the interrupted run.

<!-- TODO(stage-machine step 6): stale workspace cleanup via scratch.py/work.py -->

No tool records a lease yet. The lease is the lead's own note of when it dispatched the unit and when it last heard from it.

<!-- TODO(stage-machine): work.py records the claim and lease; scratch.py heartbeats replace the lead's manual timestamp. -->

| Stage | Unit | Agent | Worker writes |
|---|---|---|---|
| concept | `variant-<id>` | `cad-concept-designer` | `.cad-work/variant-<id>/<id>.scene.json` |
| parts | `part-<subassembly>` | `cad-part-designer` | `.cad-work/part-<subassembly>/`, then its own `hw/parts/<PN>/` through `scratch.py deliver` |
| parts | `source-<family>` | `cad-sourcing-researcher` | nothing |
| bom | `clerk-<chore>` | `cad-clerk` (cheap tier; mechanical rows and summaries only) | `.cad-work/clerk-<chore>/`; the lead applies the rows, and gates cite evidence packets, never the clerk's summary |
| detail | `detail-<group>`: at most 3 made parts | `cad-part-designer` | `.cad-work/detail-<group>/`, then each `hw/parts/<PN>/` through `scratch.py deliver` |
| analysis | `analysis-<case>` | `cad-analyst` | `.cad-work/analysis-<case>/` |
| analysis full check | `analysis-<case>-<vN>`: the joint sweep, then FEA at its worst pose | `cad-analyst` | `.cad-work/analysis-<case>/` |
| parts light review | reviewer | `cad-mech-reviewer`, `cad-dfm-reviewer` | nothing |
| critique | reviewer | `cad-mech-reviewer`, `cad-dfm-reviewer` | nothing |

List units from the record: subassemblies from `assembly.yaml`, purchased families from `bom.csv` rows with `make_buy: buy`, made parts, and analysis cases.

At L4 the joint sweep is the stage's full check. Dispatch it once the provisional ANALYSIS gate is recorded, with the version in its unit id, so that a rerun on a fixed version is a fresh unit. A static FEA case at the worst-case pose needs the sweep's `worst_case`: dispatch those cases as the next batch, once the sweep returns. Every other L4 case runs in the first batch.

The L2 light review runs `cad-mech-reviewer` and `cad-dfm-reviewer` read-only in parallel while the PARTS full check runs. Its scope is the rules a check can miss at L2: every torque path and axial retainer, the anchorage of covers and mounts, and each made part's walls against its process minimum. It is not the final critique.

Each worker's brief is self-contained. It names the unit id, task, and PNs, variant, or case; a motion case also names the requirements and use cases its plan must show. It gives the absolute-path inputs (record files, version label, frozen interfaces, shared node ids), kernel, fidelity, and write boundary; a part or detail unit also gets each `--attach REF=PARENT_REF` the lead's integrated build passes to `cad-model run`, for its `scratch.py build`. It also gives the report format with absolute paths. A worker asks the user nothing and does not change shared workflow state or make gate decisions.

Before you change shared files, read each final report. Accept a part delivery only when its scratch build passed; otherwise dispatch that unit again. Apply accepted proposals to the shared record yourself and record why you declined a proposal. Merge concept variants, adopt cited sourcing values, write verification rows from analysis results, and copy accepted motion plans to `hw/motion/cases/<case>/plan.yaml` before publishing. Then run the stage's commit batch once on `hw/`: the build and the fast checks in the [fail-fast order](#fail-fast-check-order), and the recheck. Present the stage's gates with the absolute path of every file they show.

### Scratch workspace

A part designer works in `.cad-work/<unit>/` next to `hw/`; `scratch.py` writes a `.gitignore` there so git ignores the directory.

- `uv run --script scripts/scratch.py init --hw hw --unit <unit> --part <PN> [--part <PN> ...]` copies `hw/` without `versions/`, `renders/`, `releases/`, `.cad/` (part store, pair cache, run records) or `.git/` and records a baseline. `deliver` ignores the scratch copy's own `.cad/`.
- `scratch.py build --hw hw --unit <unit> --fidelity F2|F3 [--kernel auto|cadquery|build123d] [--attach REF=PARENT_REF ...]` builds and checks the scratch copy under its own label and runs the form check on the unit's PNs; `hw/versions/` is never touched. Pass the same `--attach` entries as the lead's integrated build, so the unit checks the same rigid attachments; the report echoes them as `attach`.
- `scratch.py deliver --hw hw --unit <unit>` accepts only canonical source files under authorized part records, including PNs authorized at init. It ignores generated `versions/`, `renders/`, and `releases/` output. It rejects other generated subtrees, stray paths, and unauthorized PNs without writing when a conflict exists. The lead integrates returned parts only after the scratch build passes.

Every path in its JSON output is absolute. `.cad-work/` is disposable once the stage's human gate passes.

## Gate control

INTENT, VARIANT, DESIGN freeze and RELEASE are single human gates. Every other stage gate approves a built version and comes as two gates: LAYOUT freeze, PARTS, DETAIL and ANALYSIS, and the same stages when an ECO or post-freeze change reopens them. Each such stage runs three steps in order, and its state lives in `product.yaml` `flow.stage` and `hw/decisions/`:

Every gate decision is a MADR 4.0.0 record that you write with `write_decision` as `hw/decisions/DR-###-slug.md`. Its front matter `status` names the gate: `accepted` is the user's approval, `proposed` is a provisional gate and `rejected` is a voided one. `validate.py` refuses a `flow.frozen` or `flow.change` decision, and `release.py` a release decision, whose status is not `accepted`. A file named `DR-###.md`, or a record without MADR front matter, is no decision.

1. **Provisional gate.** Record it yourself, without asking the user, once version vN passed every fast check: write `hw/decisions/DR-###-slug.md` with `write_decision`, `status: proposed` and the reason `temporary until the full check: vN passed fast checks 1-4`, citing the result files, mark the stage's design entry provisional too, and advance `flow.stage` to the next stage. It lets the next stage start. It approves, freezes and releases nothing.
2. **Full check.** Run fail-fast step 5 on the same vN. At L2 the light review runs beside it, and the full check counts as done only after both reviews have returned; at L4 the joint sweep case is the full check. While it runs, prepare the human gate's packet: renders and contact sheet, fast-check and form results, review verdicts, decisions, open findings, the sweep's unproved between-sample intervals, and a short summary, and at L3 the drawings. Record its result files in the stage's design entry once they pass.
3. **Human gate.** It opens only after the full check passed and after the previous stage's human gate passed. Present the packet and ask the user; only explicit user approval resolves it, recorded as a new `hw/decisions/DR-###-slug.md` with `status: accepted` that cites the provisional decision. When the sweep left unproved between-sample intervals, the user either accepts them, which you record as a DR that the approval cites, or asks for a finer run (a smaller `--continuous-grid-deg`, a deeper `--continuous-depth` up to 6, finer `--steps`, or `--subsystem NAME` for the unproved pairs). For a finer run, rerun only the full check on the same version and present the gate again.

A failing full check, a MAJOR light-review finding, or a change the user names at the human gate voids the provisional approval and reopens the stage and everything built on it, even after the full check passed:

1. Stop the stage's long jobs still running by their process groups.
2. Record a decision with `status: rejected` and the reason `vN <cause>: <result file>` that supersedes the provisional decision, and the provisional decision of every later stage built on vN; decisions are append-only, so never edit or delete the voided ones.
3. Set `product.yaml` `flow.stage` back to the voided stage. Every later stage built on vN is open again: its full check and human gate wait until it is rebuilt on the fixed version.

Build a fixed version in the voided stage; once it passes the fast checks, record a new provisional decision with the new evidence, then run its full check. Each later human gate stays closed until its own full check passes again on a version built from the fix. Rebuild each reopened later stage on the fixed version: its delivered parts stay integrated, and a unit whose delivery the fix invalidates is dispatched again. Rerun a full-check case with the new version in its unit id. Nothing is frozen or released before the human gates.

At each human gate, if a question is cancelled or unanswered, it is not consent. List open questions and proposed assumptions in the summary; keep the gate open and stage unchanged, state that work waits for the user, and stop. Never record assumptions as approval or infer approval from silence, a preview, a successful build, a provisional gate, an agent verdict, or a release artifact. Record approval in `hw/decisions/DR-###-slug.md` with `status: accepted`, and at a single human gate advance `product.yaml` `flow.stage` only after the user explicitly approves.

Every gate report, question, and summary gives each file it mentions as an absolute path on its own line: records, decisions, model.py, STEP and GLB, check JSON, renders, captures, drawing previews and PDFs, and BOM. A relative path such as `hw/renders/v2/iso.png` is not enough. Add the URL of the live viewer or preview gallery while one runs.
