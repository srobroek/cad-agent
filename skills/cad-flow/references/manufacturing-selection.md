# Manufacturing intent and route selection

Use this reference during GROUND, INTENT, SPECIFY, PARTS and every affected change review. Schemas and the record validator own field names and numeric validation. `selection.py` owns candidate evaluation and comparison. Prose must not duplicate thresholds or engineering calculations.

## GROUND: establish the use case

Ask these focused questions before selecting material or process:

- What must the product do? Who uses it, and where?
- Which loads apply, and where do they enter the product? Include magnitude, direction, impact, vibration, cyclic and sustained loading.
- What motion, speed, duty cycle, friction, wear and service life apply?
- Which temperature range and heat sources apply? Which moisture, conditioning, chemical, UV and corrosion exposures matter?
- What accuracy, repeatability, finish and dimensional stability does function need?
- What are the failure consequences and forbidden outcomes? Which standards or safety rules apply? Who supervises and approves?
- What quantity, budget and lead time apply? How will the product be maintained and repaired?
- Which equipment, stock, tools, operators and measurement capability are available? Is outsourcing acceptable?
- What may the release claim? What operating limits, exclusions and evidence are needed?

Separate **requirements**, **preferences**, **assumptions** and **open questions**. Bound and assign each assumption. Give every open question an owner and mark whether it affects choice or safety. A missing fact stays unknown. Do not infer service or loads from illustrative geometry, available equipment, a material label or a capability profile. Resolve safety- or material-critical unknowns before a final recommendation. Otherwise give conditional alternatives with clear limits.

### One use-case classification table

Use the user's description to record the service class; ask when the description does not resolve a row. The service class describes intended-use maturity and consequence. Choose rigor separately as documentation depth using `maker`, `pro` or `regulated`; never derive rigor from a prototype label.

| User's description | `service_class` | Required clarification |
|---|---|---|
| Appearance model or concept only | `appearance_concept` | Confirm no fit, functional or service claim is intended. |
| Fit or assembly prototype | `fit_assembly` | Ask which interfaces and assembly evidence are needed. |
| Supervised demonstration with limited loads/use | `functional_demo` | Record permitted loads, use limits and supervision. |
| Prototype representative of intended service | `working_prototype` | Confirm representative loads, environment, duty and life. |
| Production or end-use product | `production` | Record release purpose, required standards and evidence scope. |
| Safety-critical, regulated or high-consequence use | `safety_critical` | Identify the governing authority, standards, risk criteria and accountable approver. |

If a user says only "prototype", ask whether it is for fit/assembly, a supervised functional demonstration, or representative intended service. Keep the answer open until resolved.
For an existing v1 record, keep the record unchanged during assessment. Run the documented migration dry run and review every proposed file diff. Migration is an explicit owner action. It adds unknowns/questions and cannot certify a historical design.

## INTENT: compare complete routes

A candidate joins architecture, per-part material, ordered operations, capability, finishing, inspection and maintenance. Assess each physical part and contacting pair. A product may combine purchased hardware with polymer, metal, sheet or additive routes. Consider credible local and outsourced options. Do not choose material and process independently of design and interfaces.

1. Enumerate credible combinations with sourced evidence and matched conditions. Label estimates and uncertainty. Missing supplier property, rating or capability remains unknown.
2. Evaluate each mandatory requirement as PASS, FAIL or UNKNOWN using its criterion, source, method, value and uncertainty. Eliminate FAIL. Keep UNKNOWN conditional and out of feasible rankings. Preferences can rank only options that meet all mandatory criteria. A score never offsets safety or performance failure.
3. Compare feasible options on suitability and material behavior: strength, stiffness, fatigue, creep, heat and wear. Assess accuracy, finish and stability. Include equipment, setup, finishing, inspection, interfaces, assembly and design changes. Compare quantity-specific cost and lead time, maintenance, repair and evidence confidence. Show obligations and tradeoffs.
4. Recommend a primary route, and useful prototype, end-use and fallback routes. State evidence limits, conditional alternatives and what would change the recommendation. A prototype route may differ from end use. Say what the prototype can establish and what it cannot. Obtain the existing human approval before consequential design changes.

An optical or transparency requirement is a hard constraint. Opaque routes cannot meet it. Consider a sourced, identified purchased transparent part or known transparent stock processed by an evidenced suitable route, such as approved laser cutting or CNC. State identity/grade, optical grade, thickness, supplier evidence and interface criteria. Do not propose see-through FDM, downgrade transparency to translucency or substitute unidentified clear plastic.

Capability profiles describe supplied equipment, stock, operators, tools, suppliers and calibrated/demonstrated limits. They do not set product requirements or prove compliance. Nominal resolution, build volume and drawing tolerance labels do not verify dimensional capability. Material-family guidance does not establish a grade property or allowable. Density does not provide strength. Do not transfer values between materials.

## SPECIFY and record

Translate accepted choices into measurable, sourced requirements and evidence obligations. Capture each part's make/buy status, function, material profile or unknown, service scope, components, ordered route, capability reference, setup/workholding, orientation, critical surfaces/datums, supports/removal, finishing allowance, inspection and alternatives.

For critical interfaces, record function, endpoints, fit, datums, dimension states and verification. Use worst-case stacks when collision, binding or lost function matters. RSS needs justified distributions, centering, correlation/independence, coverage and approval. Include assembly order, closure state, tool access, engagement, retention, adjustment, inspection, service and disassembly. Nominal motion and final seated geometry do not prove insertion or tool access.

## Checks, evidence and change impact

Dispatch checks by each part's declared operation, process, material state, component and function. Assess every step of a multi-process route, including transfer, allowance and access. Catalogue purchases need sourced identity, dimensions, ratings, compatibility, mounting and availability. They do not need made-part geometry checks. Outsourced made-to-drawing work still needs route review. Unsupported routes produce coverage UNKNOWN, never FDM fallback or PASS. See `cad-design/references/process-checks.md` for process obligations and support policy.

Evidence has separate facets: CAD-valid, geometrically manufacturable, process planned, simulation supported and physically validated. Bind each claim to a requirement, subject, configuration and condition. A CAD preview, B-rep, envelope fit, heuristic, nominal motion, check exit code, FEA result, material-family name, planned coupon or uncalibrated process does not establish physical function, print success, strength, accuracy, service life or compliance.
Physical claims need an as-built configuration, suitable protocol and calibrated instrument. Record the measured result, scope and approval. A coupon supports only its tested scope.

For every change, compare impact on use case, loads, environment, duty/life, material, route, capability, setup, tools, orientation, support, finish, geometry, interfaces, tolerance and acceptance evidence. Recheck dependent plans/results from the earliest affected stage. Missing dependency metadata stays unknown and requires rerun or assessed carry-forward. Prior PASS does not carry by default.

Evidence must follow two separate axes. The use-case **service class** (`appearance_concept`, `fit_assembly`, `functional_demo`, `working_prototype`, `production`, `safety_critical`) describes use maturity and consequence. Product **rigor** (`maker`, `pro`, `regulated`) describes documentation depth. A prototype is a service class, never a rigor level; neither axis substitutes for the other. Fit prototypes need interface, stack and assembly evidence. Supervised demos need evidence for claimed loads/use limits. Working prototypes need representative service evidence. Production and high-consequence/regulated use need governing requirements, qualified route/material evidence, inspection and accountable authority. Unknown service blocks final selection and new manufacturing freeze/release. A permitted trial waiver must be safe, approved and scoped; it remains waived and untested.
