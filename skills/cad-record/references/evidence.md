# Manufacturing evidence and findings

The versioned producer result is immutable evidence; authored ledgers do not rewrite it. The JSON schemas and `manufacturing_intent.py` are the field and policy authority.

## Independent evidence facets

Record each evidence claim by requirement or obligation, subject (product/part/component/operation/interface), version/manifest/geometry, method, criterion, source, conditions, dependencies, uncertainty, coverage and result. Levels are independent: `cad_valid`, `geometrically_manufacturable`, `process_planned`, `simulation_supported`, and `physically_validated`. A CAD-valid model does not imply geometric manufacturability; a geometric check does not imply a process plan; a plan does not prove process capability or successful manufacture; simulation is not physical validation. Do not promote a result between levels or scope by implication.

Do not claim actual toolpath or print success from geometry, dimensional capability from machine resolution or a general tolerance, fit from nominal CAD clearance, assembled access from the final pose, material strength from density/family/another material, fatigue/creep/wear/life from static linear FEA, a test outside its specimen/configuration/conditions, or regulatory certification from plugin checks. A planned test/coupon is an obligation, not passing physical evidence. A test supports only the tested configuration and scope.

Physical validation requires an as-built specimen/configuration, actual material and process state, appropriate protocol and setup, traceable calibrated instrument, results, date, scope, responsible approver and evidence bytes. Verification requirements remain criterion-specific. A status word or five-column verification row cannot override missing, stale, incomplete or inapplicable evidence.

## Finding and disposition

A finding names a stable code and ID, subject, severity (`critical|major|minor|info`), outcome, measured value/range and units or reason for UNKNOWN, criterion/operator/source and linked requirement, method and hash-bound evidence, uncertainty/assumptions/coverage, functional consequence, remedy and alternatives/tradeoffs, and recheck family/scope/inputs/earliest affected stage. Geometric findings are limited to the observation actually measured.

## Review verdicts

Reviewers use the same verdict derivation:

| Situation | Verdict |
|---|---|
| Any `critical` or `major` finding with outcome `fail` | `MAJOR` |
| A mandatory or safety-relevant obligation has outcome `unknown` | `MAJOR` |
| Only `minor` or `info` findings remain, or noncritical scope is explicitly untested | `MINOR` |
| No findings remain and every applicable in-scope check is evidenced | `PASS` |

The finding severity enum is defined above. Findings retain their own severity/outcome; the review verdict summarizes the whole assigned scope. Untested required checks are never PASS.

Producer packets remain immutable. `manufacturing/findings.yaml` contains dispositions tied to the exact producer bundle and manifest hash, status, reason, decision and any replacement evidence. Resolution requires newly applicable evidence; acceptance or decline does not convert FAIL/UNKNOWN to PASS. A waiver is scoped to subject, criterion, operating envelope, expiry or change, reason, consequence and approving authority; forbidden safety, regulatory and geometry failures cannot be waived. A permitted trial waiver remains explicitly `waived` and untested.
`evidence.py check --hw hw --version vN` assesses expected obligations from the current record, sealed check packets and accepted evidence ledger. `digest` computes the acceptance inventory digest used by the freeze anchor. Results distinguish pass, fail, waived, not applicable, stale, planned and unknown; none can be promoted by a CSV status. The gate packet includes selection status and digest. `gate.selection` blocks unsupported or contradictory choices, stale confirmations and, at design freeze/release, missing material/process selection. An approved confirmation is checked against its decision (`gate.selection_approval`). `impact.py --hw hw --since-version vM --version vN` reports changed fingerprint domains, stale obligations/evidence, reopened decisions, the earliest affected stage, and whether the existing selection input digest changed; it never passes results forward automatically. The recheck stamp recomputes the obligation and selection gates from the staged record. Its installed hook includes the selection evaluator, so `--passed` cannot mask a changed or unresolved decision.

The freeze records `flow.frozen.evidence_sha256` from the evidence digest plus the approving decision. Release manifest v3 seals the acceptance inventory, evidence policy, material/process route and service scope/limits. Old v1/v2 releases remain inspectable in their original format; they are historical, not re-certified by the new policy.

## Freshness, impact and release claims

A prior result carries only when its exact subject, geometry/configuration, requirements, criterion, conditions, capability, sources and dependency fingerprints still apply. Missing dependency metadata is unknown and requires rerun or a documented assessed carry-forward into a current version; old bundles and seals are never edited. `impact.py --since-version vM` reports changes across use case, part/component geometry, route/orientation/material, loads, requirements, interfaces, profiles, capability, selection digest, evidence and decisions. It lists stale obligations and reopened decisions with earliest affected stage; it does not change the stamp threshold or automatically pass old results. A selection digest change requires a new assessment and confirmation through the approval gate. Re-evaluate from the earliest stage that accepted the changed fact.

Release claims state purpose, scope, operating limits, exclusions, required obligations, evidence levels and any approved trial status. Keep two axes distinct. Service class describes intended-use maturity and consequence. Values are `appearance_concept`, `fit_assembly`, `functional_demo`, `working_prototype`, `production` and `safety_critical`. Product rigor uses `maker`, `pro` or `regulated` and describes documentation depth. Prototype is a service class, never rigor. Increasingly consequential use needs evidence at the appropriate level. Unknown service blocks final selection and new manufacturing freeze/release. CAD and static simulation never certify physical performance or regulatory compliance.

Evidence codes include `evidence.unqualified`, `.stale`, `.subject`, `.carry_forward`, `.altered`, `.csv_unsupported`, `.physical_claim`; `packet.invalid`, `.unexpected`; `waiver.unapproved`, `.expired`, `.scope`, `.forbidden`; and `finding.critical`.
Gate/release codes include `gate.record`, `.fail`, `.stale`, `.unresolved`, `.use_case`, `.selection`, `.selection_approval`; `release.service_class`, `.scope`, `.trial_not_permitted`, `.waiver_forbidden`.
Selection codes include `selection.use_case_incomplete`, `.not_assessed`, `.candidate_eliminated`, `.recorded_route_eliminated`, `.primary_unknown`, `.candidate_unknown`, `.no_feasible_candidate`, `.evaluation_conflict`, `.evaluation_mismatch`, `.assessment_stale`, `.recorded_recommendation_eliminated`, `.final_unsupported`, `.recommendation_differs`. Reports carry subject, severity, outcome and evidence fields. Freeze and release block on missing candidates/selection.
