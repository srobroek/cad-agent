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

**Type:** `Establish the product purpose and users, intended-use service class (concept, fit/assembly prototype, supervised functional demo, working prototype, production, safety-critical/regulated), operating environment, loads and motion, service life, required accuracy/finish, consequences/standards, quantity/cost/lead time, maintenance, operators/equipment and willingness to outsource. Keep service class separate from rigor (maker/pro/regulated documentation depth); prototype is service class, never rigor. For each unknown, identify its owner and whether it changes material/process choice or safety. Inspect the existing record; for a v1 record, run the migration dry run and review the exact per-file diff.`

**Review:** requirements, preferences, assumptions and open questions separately; cited sources; evidence and scope gaps. Capability availability is not a product requirement. Do not infer operating conditions from illustrative CAD geometry or a lamp example.

**Approve:** confirm the use case, unknowns and scope before a consequential selection. Only the explicit owner applies a migration (`migrate.py --apply`) or changes shared design records. A preview is not CAD, process or verification evidence.

**Files:** `hw/product.yaml`, `hw/requirements.yaml`, the migration report/note when relevant, and `hw/decisions/DR-###.md`.

## INTENT

**Type:** `Compare credible complete architecture, per-part material and ordered process routes against each mandatory requirement. Include sourced property/condition evidence, real capability, setup, secondary operations, finishing, inspection, interfaces, assembly, quantity-specific cost/time, maintenance and accepted tradeoffs. Eliminate any mandatory FAIL before preference scoring. Mark UNKNOWN conditional and do not rank it as feasible. Present primary, prototype, end-use and fallback routes as useful, with evidence limits and what would change the recommendation. Do not invent prices, ratings, properties, capability or life. Do not silently change requirements or the product.`

**Review:** comparison rows show requirement outcome, method, criterion/source, value/units, uncertainty, route/capability evidence, pros/cons/tradeoffs and unresolved questions. A prototype service class may use a route different from end use; state what it can validate and what it cannot. Rigor (maker/pro/regulated) remains a separate documentation-depth choice.

**Approve:** approve, revise or decline consequential choices explicitly. An optical/transparency requirement eliminates opaque routes; consider only identified transparent purchased parts or identified stock on an appropriate sourced/capable route. No see-through FDM or unidentified clear plastic.

**Files:** `hw/product.yaml`, `hw/requirements.yaml`, `hw/manufacturing/selection.yaml`, affected per-part intent, source/profile evidence, and `hw/decisions/DR-###.md`.

## SPECIFY

**Type:** `Turn the approved use case and chosen direction into sourced measurable requirements and evidence obligations. Specify functional interfaces, datum/fit/tolerance and dimension state, worst-case stack contributors where function depends on them, contacting materials, assembly order/closure and tool/service access. Capture per-part function, material profile or unknown, ordered process, capability, setup/orientation, critical surfaces, supports/removal, finish, allowances and inspection. Keep capabilities separate from requirements. Do not copy a fit-table class as demonstrated process accuracy.`

**Review:** each criterion and evidence method, interface/assembly assessment, route obligation and unknown. Distinguish worst-case results from statistically justified RSS; do not hide a forbidden collision with score or statistics. Use the deterministic fit and process check tools.

**Approve:** confirm acceptance criteria, remaining open questions and scope before requirements are used to approve design.

**Files:** `hw/requirements.yaml`, `hw/interfaces.yaml`, `hw/assembly.yaml`, part intent, `hw/verification.csv` and decisions.

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

**Type:** `Plan each part's function, material grade/form/condition, process route, capability, component identity, setup/orientation, critical features, support/removal, finish, inspection and alternatives from the approved requirements. Verify purchased-part identity and ratings against the actual catalogue/datasheet. Check every declared route and required operation; unknown profile/coverage remains UNKNOWN. Keep sourced facts and uncertain cost/time estimates distinct. Build and check an integrated version and present the PARTS gate; form-level review is not process capability or physical evidence.`

**Review:** part-intent/route proposals, exact buy identities and evidence, per-component/per-operation check outcomes, unknowns and process-plan gaps, geometry/form diagnostics, version renders, and evidence from the scoped mechanical/DFM review.

**Approve:** select or revise part architecture, procurement and routes explicitly. No silent substitutions. Record remaining unknowns; approval cannot convert them to PASS.

**Files:** `hw/parts/<PN>/part.yaml`, model, accepted procurement sources, manufacturing profiles/selection, version and scoped check bundles, renders and decision.

## L3 DETAIL, drawings, and DETAIL gate

**Type:** `Complete functional interfaces, datums, worst-case fits/stacks, manufactured versus finished dimensions, operation order, support/finish allowances, tool and fastener access, assembly/closure/service access and inspection. Check every route operation using its own capability evidence. Drawings state required dimensions and tolerances; neither a fit class nor general tolerance proves achieved precision.`

**Review:** design checks and route checks. For every critical feature, show the datum, fit and evidence state. Include the worst-case stack and any assembly/tool access issue. List finishing, inspection obligations and drawing-preview limits. Keep appearance fidelity separate from manufacturability.

**Approve:** approve details and residual evidence obligations at the existing gates; request changes for any mandatory failure or unknown needed for the claimed service.

**Files:** updated per-part/interface/assembly intent, route evidence, version/check bundles, drawings, verification and decisions.

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

**Type:** `Run independent, read-only mechanical and DFM premise challenges against the approved use case, requirements, every scoped part/route/buy, interface, assembly, profile and evidence. Reconcile findings as resolved, accepted, or declined with reason and required recheck. Rerun affected evidence after changes.`

**Review:** both reports with subject, severity, measured/derived value and units or UNKNOWN, criterion/source, method/evidence, uncertainty, functional consequence, remedy/alternatives/tradeoffs and recheck; separate untested coverage from findings. Reviewers challenge the premise conditionally; no blanket material/process rules and no silent requirement changes.

**Approve:** confirm dispositions, changed evidence and residual risks. A disposition is not PASS or physical validation.

**Files:** decisions/dispositions, updated part/interface/requirements as approved, and rerun version/check/render/analysis evidence.

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
