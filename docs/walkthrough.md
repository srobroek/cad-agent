# CAD workflow walkthrough

Use this guide with the lamp-arm example in `examples/lamp-arm/` or with a new product in a project that has the CAD skills installed. The skill owns the commands and record schemas. This guide is a prompt-by-prompt companion. Type each prompt after the harness entry point:

- Interactive Claude Code or OpenCode: `/cad-flow <prompt>`
  Interactive Kiro V3: `/cad-flow <prompt>`
- Non-interactive Kiro: ask for the `cad-flow` skill in prose. A bare `/cad-flow` is a CLI subcommand, not chat input. See the [Kiro headless mode docs](https://kiro.dev/docs/cli/headless/).
- Codex CLI: `$cad-flow <prompt>` or select the skill in `/skills`, then type the prompt

If you run Claude Code with `-p`, set `--max-turns` above the turns needed to finish the stage. Claude Code errors when it reaches the limit. To answer a gate, capture the session ID and resume it with `claude --resume <session-id> -p "<your explicit gate decision>"`. See the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and [headless mode](https://code.claude.com/docs/en/headless).

Codex's `workspace-write` sandbox can block loopback listeners, Chromium, or FreeCAD/Qt. The [README's Codex sandbox notes](../README.md#codex-cli-sandbox-requirements) describe required permissions and full-access risks.

The generated Kiro agent profiles use V3 configuration. Run the flow with a working V3 engine, such as `kiro-cli chat --v3`. Do not use the legacy V1 engine with these profiles. See the [Kiro V3 agent configuration docs](https://kiro.dev/docs/cli/v3/agent-config/).

The Claude plugin entry point is `/cad-agent:cad-flow`. To complete a gate, give explicit approval. The lead records the approval and rationale in a new `hw/decisions/DR-###.md`. A preview, successful check, or agent recommendation is not approval.

LAYOUT freeze, PARTS, DETAIL and ANALYSIS each come as two gates. Once the built version passes the fast checks, the lead records a decision marked PROVISIONAL, without asking you, and the next stage may start. The human gate follows the full check, which checks every pair and runs the full motion sweep; only your approval closes it. A failed full check, a MAJOR finding of the L2 light review, or a change you name at the human gate voids the provisional decision. The lead then reopens that stage and every later stage built on that version, and `hw/product.yaml` `flow.stage` returns to the reopened stage.

## Before the flow: check and prepare

Run the environment check from the project root:

```sh
uv run --script /path/to/cad-agent/skills/cad-flow/scripts/preflight.py
```

If Chromium or the render cache needs installation, run `preflight.py --warm`. Setup can plan or run user-level setup:

```sh
uv run --script /path/to/cad-agent/skills/cad-flow/scripts/setup.py
uv run --script /path/to/cad-agent/skills/cad-flow/scripts/setup.py --yes
```

The shared launcher also works for `setup.py` and keeps its declared dependencies in the shared environment: `uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-flow/scripts/setup.py [--yes]`.

FreeCAD 1.1.4 and its TechDraw templates are required. If a check fails, resolve it before proceeding. Tests that use FreeCAD skip when it is unavailable.

## GROUND

**Type:** `Inspect the lamp-arm example [or <product>]. Establish the product identity, intended use, design rigor, units, standards regime, tolerance policy, and known constraints. Identify missing inputs and propose an initial scope. Do not assume unanswered requirements.`

**Review:** existing records, available parts and project context, open questions, initial requirements, and the proposed product record. For a new project, establish `hw/product.yaml` and add context under `hw/docs/`.

**Approve:** confirm the problem and scope before intent is fixed. Record substantive approvals and their rationale as decisions. A preview alone does not count as geometry or verification evidence.

**Files:** `hw/product.yaml`, initial `hw/requirements.yaml` and `hw/interfaces.yaml` as known, `hw/decisions/DR-###.md`, and optional `hw/docs/` notes. Existing-project migration plans are read-only. They move no files.

## INTENT

**Type:** `State the product purpose, users, use environment, envelope, constraints, success measures, and unknowns for the agreed scope. Show conflicts and assumptions before changing the record.`

**Review:** each intent with its source, rationale, priority, and unresolved questions.

**Approve:** approve the intent or request changes. Add approved intent and its decision to the record before proceeding.

**Files:** updated `hw/product.yaml`, `hw/requirements.yaml`, and `hw/decisions/DR-###.md`.

## SPECIFY

**Type:** `Turn the approved intent into measurable requirements with sources, rationale, priority, verification method, and pass criteria. Identify interfaces and uncertainties. Do not invent values for unresolved constraints.`

**Review:** requirement-to-verification mapping, proposed interfaces and tolerances, and unresolved items.

**Approve:** confirm requirements and interface assumptions. Mark open items explicitly instead of treating them as satisfied.

**Files:** `hw/requirements.yaml`, `hw/interfaces.yaml`, `hw/verification.csv`, and decisions for approved choices.

## L0 CONCEPT and VARIANT gate

**Type:** `Draft 2-4 rapid concept variants for <product>, compare them against the approved requirements, and show the live preview. Keep them as exploratory concepts, not verified CAD.`

**Review:** variants, trade-offs, requirement fit, and preview. Workers deliver variants separately. The lead merges and checks them before presenting options.

**Approve:** at the VARIANT gate, select a concept, request a refinement round, or decline all variants. Record the selection and rationale before layout.

**Files:** `hw/concept/<product>.scene.json`, generated `hw/concept/handoff.json`, preview bundles under `hw/concept/screenshots/`, and `hw/decisions/DR-###.md`.

## L1 LAYOUT and LAYOUT freeze

**Type:** `Develop the selected concept into a layout. Define parts, subassemblies, interfaces, joints, motion limits, and the preliminary BOM. Build and check a version. Render it, and show the default motion render if any joint moves.`

**Review:** the integrated assembly and interface layout, build and check results, rendered images, BOM skeleton, and motion coverage/contact sheet where applicable. Resolve blocking failures before the gate.

**Approve:** at LAYOUT freeze, approve the exact layout, interfaces, and envelope, or request revisions. The freeze decision records the approved state and version.

**Files:** `hw/assembly.yaml`, `hw/interfaces.yaml`, part records under `hw/parts/<PN>/`, `hw/versions/vN/`, `hw/versions/latest.json`, `hw/checks/vN/`, `hw/renders/vN/`, optional `hw/motion/vN/`, and `hw/decisions/DR-###.md`.

## L2 PARTS and PARTS gate

**Type:** `Design parts for the approved layout. Resolve purchased-part identities, materials, processes, finishes, and mass targets. Build and check an integrated version. Run the F2 form check, render the version, and present the PARTS gate.`

**Review:** made-part proposals, sourcing evidence, integrated build and checks, F2 findings, version renders, and the light mechanical and DFM review that runs beside the full check. The lead dispatches subassembly and sourcing workers together, then integrates their results.

**Approve:** approve the part architecture and sourcing direction, or identify changes for detail design.

**Files:** `hw/parts/<PN>/part.yaml` and applicable `model.py`, accepted procurement evidence under `hw/parts/<PN>/sources/`, `hw/versions/vN/`, `hw/checks/vN/`, `hw/renders/vN/`, and `hw/decisions/DR-###.md`.

## L3 DETAIL, drawings, and DETAIL gate

**Type:** `Detail each made part with its fits, fasteners, tolerances, and manufacturing rules. Build and check the integrated version. Pass the F3 form check. Draft and review part and assembly drawings for that version.`

**Review:** detailed geometry, fit decisions, checks, F3 findings, drawing previews, and completeness results. Rejected drawing drafts remain scratch outputs. Accepted drawings are versioned evidence.

**Approve:** at the DETAIL gate, approve the detailed design and drawings or request corrections. Record the decision and drawing evidence.

**Files:** updated `hw/parts/<PN>/` records and models, `hw/versions/vN/`, `hw/checks/vN/`, `hw/renders/vN/`, `hw/drawings/vN/<PN>/` and assembly package as applicable, plus `hw/decisions/DR-###.md`.

## BOM

**Type:** `Reconcile the EBOM against the part records and integrated assembly. Check part numbers, revisions, quantities, make/buy, suppliers, and subassemblies. Show mismatches and open identities.`

**Review:** the BOM roll-up and unresolved rows. Confirm that assembly and part-record quantities agree.

**Approve:** confirm the EBOM intent. Resolve open procurement items or retain them with reasons.

**Files:** generated `hw/bom.csv`, approved updates to part records, and decisions for material choices or exceptions.

## L4 ANALYSIS and ANALYSIS gate

**Type:** `Plan the required analysis cases against the selected version and requirement criteria. Run interference/clearance and motion sweeps first. Run FEA at its required worst-case pose. Render the default or accepted motion case, then show findings with version-bound evidence.`

**Review:** sweep and fit results, FEA assumptions and solver evidence, motion coverage and limit findings, motion renders/contact sheets, and verification status for each requirement. Use the sweep's worst-case pose as an FEA input when required.

**Approve:** approve the evidence and disposition of findings, or require corrective design work. Tie each verified requirement to passing evidence for the correct version and configuration.

**Files:** analysis cases under `hw/analysis/cases/`, accepted motion plans under `hw/motion/cases/`, version-bound bundles under `hw/motion/vN/`, `hw/analysis/vN/`, and `hw/checks/vN/`, plus `hw/verification.csv` and `hw/decisions/DR-###.md`.

## CRITIQUE and RECONCILE

**Type:** `Run independent read-only mechanical and FDM/CNC design-for-manufacture critiques on the integrated candidate. Reconcile each finding as accepted, resolved, or declined with a reason. Rerun checks affected by changes.`

**Review:** both critique reports and their evidence, the disposition for every finding, changed records, and rerun checks. Reviewers work in parallel. The lead owns shared record edits.

**Approve:** confirm dispositions and remaining risks before freezing the design. Resolve blocking findings before release.

**Files:** `hw/decisions/DR-###.md`, updated part/interface/requirement records where needed, rerun version/check/render/analysis bundles, and `hw/verification.csv`.

## DESIGN freeze

**Type:** `Validate the reconciled design and evidence for freeze. Confirm the exact built version and manifest hash, manufacturing outputs for every custom part, required drawings, BOM, verification matrix, and residual risks.`

**Review:** record validation, selected version and hash, manufacturing outputs, evidence coverage, and open non-blocking risks.

**Approve:** approve the exact design anchor for release preparation, or return affected stages for correction. Record the version and manifest in the decision.

**Files:** frozen state and version/hash in `hw/product.yaml`, complete `hw/versions/vN/`, linked check, render, drawing, motion and analysis bundles, `hw/bom.csv`, `hw/verification.csv`, and `hw/decisions/DR-###.md`.

## RELEASE gate and RELEASE

**Type:** `Prepare the release review for frozen version <vN> and proposed revision <A>. Show release contents, STEP and manufacturing outputs, drawings, EBOM, verification matrix, documentation, and remaining blockers. Wait for my approval before sealing the package.`

**Review:** `validate.py` results, the frozen version, proposed revision, package inventory, and open procurement or assembly items.

**Approve:** explicitly approve the proposed revision at the RELEASE gate. The lead records release approval as a dated decision before running `release.py`. After approval, prompt: `Seal revision <A> from the approved version <vN> using release decision <DR-###>, then show the package manifest and validation result.` Stop publication if a prerequisite is missing, stale, or failing. Assign each release revision once.

**Files:** release package `hw/releases/<REV>/` with its manifest, definition and manufacturing outputs, selected drawings and evidence, BOM, verification snapshot, and docs. `hw/decisions/DR-###.md` records gate approval.

The deliverable is a verified CAD release: STEP AP242, engineering drawings, EBOM, verification evidence, and documentation. It excludes slicer, CAM, and machine-tool output.
