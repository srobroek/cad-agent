# Manufacturing intent: project guide

The CAD record captures the intended service, requirements, per-part routes, capability and evidence. Schemas and deterministic checks own field names and calculations. See the [selection reference](../skills/cad-flow/references/manufacturing-selection.md) for the workflow and the [record reference](../skills/cad-record/references/manufacturing-intent.md) for v2 fields and commands.

## Establish service before selection

Record intended-use service class: `appearance_concept`, `fit_assembly`, `functional_demo`, `working_prototype`, `production` or `safety_critical`. This is separate from product rigor (`maker`, `pro`, `regulated`), which controls documentation depth. A prototype is a service class, never a rigor level.

Ask about purpose/users, loads, motion, duty/life, environment, required accuracy/finish/stability, failure consequences/standards, quantity/budget/lead time, maintenance/repair, actual equipment/operators and outsourcing. Keep requirements, preferences, bounded assumptions and open questions distinct. Cite facts. Geometry does not establish service. Unknown facts remain unknown.

For a v1 project, run `cad-record/scripts/migrate.py <project|hw> [--dry-run|--apply] [--json]`. Dry-run is the default and writes nothing. Review every proposed per-file change. Only the project owner applies migration. Migration records unknowns and questions; it invents no process, material, capability, criterion or certification. Historical versions/releases remain unchanged and readable. Gated actions and new record-writing builds wait for migration.

## Compare complete routes

Compare architecture, material and process together for each part. Include ordered secondary operations, joining, finishing, inspection, capability, interfaces, assembly, quantity-specific cost/time, maintenance and tradeoffs. Cite material and supplier evidence. State estimate basis and uncertainty. A material family or machine envelope does not prove grade properties or accuracy.

Evaluate mandatory requirements before preferences. FAIL eliminates a candidate. UNKNOWN stays conditional and out of feasible rankings. Never invent price, ratings, capability, allowables or service life. Density does not give strength. An optical requirement eliminates opaque routes. Consider an identified purchased transparent part or identified transparent stock on a suitable evidenced route. State identity, grade, optical properties, thickness, supplier and interface criteria. Do not propose see-through FDM or unidentified clear plastic.

A prototype route may differ from end use. State what a geometry/fit trial or physical test can establish and what it cannot. Recommend useful prototype, end-use and fallback routes with limits and change triggers. Use the existing human approval gate for consequential product changes.

## Plan and verify each part

Record function, service, material profile/unknown, components, ordered route, capability, setup/orientation, critical features/datums, support/removal, allowance, finishing, inspection and rationale. Keep capability separate from requirements. Check each route operation. Unsupported processes have UNKNOWN coverage.

For critical interfaces, record function, datum, fit, dimension state, finish, verification and tolerance contributors. Worst-case governs collision, binding or lost function. RSS needs justified statistical evidence and approval. Include environmental and joining effects when relevant. Record assembly order, closure, insertion/removal, tool access, adjustment, retention, inspection and service access.

Evidence has independent facets: CAD-valid, geometrically manufacturable, process planned, simulation supported and physically validated. Bind each claim to its requirement, subject, configuration and conditions. A preview, nominal model, envelope fit, geometric heuristic, FEA result or planned coupon does not prove physical performance. Static FEA does not establish fatigue, creep, wear or life. Physical validation needs an as-built configuration, actual material/process state, suitable protocol, calibrated instrument, measured results, scope and accountable approval. A coupon supports only its tested scope. The plugin does not certify regulatory compliance.

When use, load, material, route, capability, orientation, support, finish, geometry, interface, tolerance, source or criterion changes, identify affected decisions and stale evidence. Recheck from the earliest affected stage. Do not carry PASS forward by default.

## Supported scope and limits

Route-check coverage follows declared process intent and the installed handlers. Missing handlers, sources, criteria, capability or required physical evidence remain gaps. The plugin supplies CAD and geometric evidence tools. It does not generate CAM/slicer toolpaths or operate equipment. It does not replace engineering review, supplier qualification, physical tests or the governing regulatory authority.
