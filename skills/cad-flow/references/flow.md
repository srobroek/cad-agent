# CAD flow reference

## Stages, gates, and durable outputs

Keep the source of truth in the hardware project's `hw/` design record. `cad-record/references/layout.md` is the generated canonical path table; commands that consume path locations use the `cad-record/references/layout-api.md` API. Every decision, requirement, interface, revision, check result, and release artifact must identify its source or evidence. Preserve millimetres and the product's declared ISO or ASME regime. Stage ordering is intentional: do not detail parts before the layout freezes their interfaces, or release before analysis and critique are reconciled.

| Stage | Work and user gate | Design-record output | Skill / command |
|---|---|---|---|
| GROUND | Inspect needs and existing hardware; on an existing project run `validate.py PROJECT --migration-plan` before freezing. | Product identity, rigor, units, regime, tolerances; initial requirements and interface evidence. The migration plan is read-only; nothing is moved. | `cad-record`: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/validate.py <project-dir> [--migration-plan]` |
| INTENT | Present purpose, users, envelope, constraints, unknowns; wait for approval. | Approved intent, requirement sources, rationales, priorities, and gate decision. | `cad-record`: validate project |
| SPECIFY | Set measurable requirements and verification criteria. | Requirements with rationale, source, priority, verification method and criteria; updated interfaces. | `cad-record`: validate project |
| concept | One `cad-concept-designer` per variant drafts 2-4 rapid three.js variants in one batch; merge them, then refine, merge, or drop by feedback. | Scene, options and selection in a decision record. Preview is not verified CAD. | `cad-concept`: `concept.py check <scene>`; handoff: `concept.py handoff <scene>` |
| VARIANT gate | Get explicit concept selection. | Selected concept and gate evidence in a decision record. | `cad-concept`: checked handoff |
| layout | Define subassemblies, interfaces, joints/limits, BOM skeleton. | Assembly and interfaces; preliminary EBOM. | `cad-record`: validate. `cad-model run hw --label vN`, then `cad-model check vN --hw hw`. `cad-render hw --version vN`. Once a joint moves, `cad-motion` `motion.py render --hw hw --version vN --case default --outputs glb,sheet`. |
| LAYOUT freeze | Present layout, interfaces, BOM skeleton, and the default motion render for approval. | Frozen decision and gate evidence; `flow.recheck_from: layout`. | `cad-viewer`: serve, show version and compare checks; `viewer.py animate --case default` plays the motion. |
| parts | One `cad-part-designer` per subassembly and one `cad-sourcing-researcher` per purchased family, in one batch; build one integrated version; PARTS gate. | Part records and models; PN, revision, material, process, finish, mass target, make/buy, supplier. | Run build, check, render and `cad-design form_check.py --hw hw --parts-dir <version parts> --fidelity F2`. |
| detail | One `cad-part-designer` per made part applies fits, fasteners, tolerances, and process rules in one batch. Build, check, and draw one integrated version for the DETAIL gate. | Part revisions, fit decisions, drawings, and evidence. | `cad-fits`; `cad-design form_check.py` at F3; `cad-drawings` publishes each part drawing for that version. |
| bom | Reconcile part numbers, revisions, quantities, make/buy, suppliers, and assemblies. | Released-intent EBOM. | `cad-bom`: `bom.py --hw hw` or `bom.py --version vN --hw hw` to reconcile against a version snapshot. |
| analysis | One `cad-analyst` per case checks interference, clearance, stack-up, kinematics, static FEA at the worst pose, or a motion case, in one batch; FEA at the worst pose follows the sweep; ANALYSIS gate. | Verification tied to requirements/configurations; analysis artifacts and assumptions; accepted motion cases in `hw/motion/cases/<case>/plan.yaml`. | Use `cad-motion`, `cad-analysis` and `cad-fits` on the selected version. Case results publish through cad-record layout; verification links identify the versioned evidence bundle. The lead publishes an accepted motion plan with `motion.py render --hw hw --version vN --case <case> --outputs glb,sheet,video`; a record with coupled joints adds `--crosscheck freecad` and a verification row from `crosscheck.json`, where only `solved` joints count as independent evidence. |
| critique | Spawn mechanical and DFM reviewers in one parallel read-only batch. | Preserve both verdicts and evidence in the workflow record. Neither reviewer waits for the other. | `cad-review-mech`; `cad-review-dfm` |
| reconcile | Resolve critique findings. Rerun affected checks. | Updated decisions, verification, revisions, dispositions. | `cad-flow`; `cad-recheck` stamps the exact staged `hw/` tree only after checks pass. |
| design_freeze | Present reconciled design, analyses, and record for approval. | Frozen anchor pins the exact built version and its manifest hash. | `cad-record`: validate project and confirm every custom-made part has complete manufacturing outputs. |
| RELEASE gate | Show the frozen version, the revision letter to issue, its contents and verification. Get approval before release. | Gate decision, recorded in `decisions/DR-###.md`. | `cad-record`: `validate.py` and the structured checks. |
| release | After approval, seal the traceable release package. | Immutable release package with definitions, manufacturing outputs, selected bundles, EBOM, verification, and docs. | `cad-record`: `release.py HW --rev A --version vN --decision DR-###`; a missing/stale/failing item refuses and seals nothing. Never reuse a revision. |

When a stage starts the CAD viewer, keep it available through that stage's review. When the stage is complete, tell the user its URL is going away, then run the exact `stop` command printed by `serve` and record `ports_free` before ending the stage.

The canonical `product.yaml` stage enum uses lowercase values: `ground`, `intent`, `specify`, `concept`, `layout`, `parts`, `detail`, `bom`, `analysis`, `critique`, `reconcile`, `design_freeze`, `release`. Prose may use uppercase or L0 to L4; record values use these enum strings.

The full endpoint is STEP AP242 plus drawings, EBOM, verification matrix, and docs. Designs target FDM or CNC, but stop before slicing, CAM, or machine-tool generation. FreeCAD 1.1.4 is the only CAD application. CadQuery 2.8 and build123d 0.13 each run in a separate environment. See `cad-record/references/layout.md` for project paths and bundle inventory.

## Stage loop and recheck boundary

Draft, show live previews, and collect feedback. Continue until the user says “commit” or “commit batch”. Do not check on every edit.
- Each L1-L4 commit batch runs `cad-model run`, then `cad-model check`. Stop on any blocking failure. Render that exact version and show its images at the stage gate.
- An explicit check output is a new path under `.cad-work/`; generated version and producer paths come from the cad-record layout API.
- The user's commit, the layout threshold, or the hardware git pre-commit hook triggers broader changed-parts/dependents recheck. Use `flow.recheck_from` and applicable earlier-stage checks; stamp the exact staged `hw/` only after every required check passes.
- Record changed items, dependents, checks, results, and artifacts. A later edit starts a new batch on the next commit.
- After DESIGN freeze, reopen only affected parts and dependents. Preserve unaffected parts and their evidence. Before another release, build a new version and re-freeze its exact manifest anchor.
- A preview or render is not a check result. Tie reported checks to a design revision/configuration.

Once a version has an independent movable joint, each commit batch also runs the default motion render beside cad-render: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-motion/scripts/motion.py render --hw hw --version vN --case default --outputs glb,sheet`. Only contact-sheet tiles render, so it takes seconds to a minute. Exit 1, a coverage gap or a limit finding, blocks the gate; exit 2 is no result. The LAYOUT freeze gate shows:

```text
L1 MOTION RENDER vN default: PASS|FLAGGED|FAIL, {frames} frames, {segments} segments, {timings_s.total} s
   Coverage -- {summary}
   Files -- {contact-sheet.png absolute path}, {motion.glb absolute path}
```

## Delegation

The lead orchestrates; workers do independent stage units. Fan out every unit, dispatch the full stage in one parallel batch when supported, integrate once all return, build one version, and present one gate. If a harness cannot run parallel subagents, run the same unit briefs in sequence. Only the lead builds `hw/versions/`.

| Stage | Unit | Agent | Worker writes |
|---|---|---|---|
| concept | `variant-<id>` | `cad-concept-designer` | `.cad-work/variant-<id>/<id>.scene.json` |
| parts | `part-<subassembly>` | `cad-part-designer` | `.cad-work/part-<subassembly>/`, then its own `hw/parts/<PN>/` through `scratch.py deliver` |
| parts | `source-<family>` | `cad-sourcing-researcher` | nothing |
| detail | `detail-<PN>` | `cad-part-designer` | `.cad-work/detail-<PN>/`, then `hw/parts/<PN>/` through `scratch.py deliver` |
| analysis | `analysis-<case>` | `cad-analyst` | `.cad-work/analysis-<case>/` |
| critique | reviewer | `cad-mech-reviewer`, `cad-dfm-reviewer` | nothing |

List units from the record: subassemblies from `assembly.yaml`, purchased families from `bom.csv` rows with `make_buy: buy`, made parts, and analysis cases.

A static FEA case at the worst-case pose needs the sweep's `worst_case`. Run it after the sweep. Use the first-batch results for every other independent L4 case.

Each worker's brief is self-contained. It names the unit id, task, and PNs, variant, or case. It gives the absolute-path inputs (record files, version label, frozen interfaces, shared node ids), kernel, fidelity, and write boundary. It also gives the report format with absolute paths. A worker asks the user nothing and does not change shared workflow state or make gate decisions.

Read every report before changing shared files. Accept a part delivery only when its scratch build passed; otherwise dispatch that unit again. Apply accepted proposals to the shared record yourself and record why you declined a proposal. Merge concept variants, adopt cited sourcing values, write verification rows from analysis results, and copy accepted motion plans to `hw/motion/cases/<case>/plan.yaml` before publishing. Run the stage's commit batch once. At L2/L3 this includes `cad-model run` and `check`, form check, render, L3 drawings, and recheck. Present the gate summary and ask the user.

### Scratch workspace

A part designer works in `.cad-work/<unit>/` next to `hw/`; `scratch.py` writes a `.gitignore` there so git ignores the directory.

- `uv run --script scripts/scratch.py init --hw hw --unit <unit> --part <PN> [--part <PN> ...]` copies `hw/` without `versions/`, `renders/`, `releases/`, or `.git/` and records a baseline.
- `scratch.py deliver --hw hw --unit <unit>` accepts only canonical source files under authorized part records (including a PN authorized at `init`), rejects generated subtrees, stray paths and unauthorized PNs, and writes nothing when a conflict is detected. The lead integrates returned part files only after the passing scratch build.

Every path in its JSON output is absolute. `.cad-work/` is disposable once the stage gate passes.

## Gate control

At each gate, present the gate summary and ask the user. Never infer approval from silence, a preview, a successful build, an agent verdict, or a release artifact. When the user approves, record approval in `hw/decisions/DR-###.md` and advance `product.yaml` `flow.stage`; otherwise keep the gate open and the stage unchanged.

Every gate report, question, and summary gives each file it mentions as an absolute path on its own line: records, decisions, model.py, STEP and GLB, check JSON, renders, captures, drawing previews and PDFs, and BOM. A relative path such as `hw/renders/v2/iso.png` is not enough. Add the URL of the live viewer or preview gallery while one runs.
