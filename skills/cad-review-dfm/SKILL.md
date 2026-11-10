---
name: cad-review-dfm
description: Reviews declared manufacturing routes with evidence. Use for an independent DFM review.
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# Design for Manufacturing Review

TRIGGER
+ CRITIQUE or an independent manufacturing review
- Requirement trace, load path, or structural analysis review → `cad-review-mech`

## Workflow

1. Read the scoped use case, requirements, part intents, ordered route, material/capability evidence, interfaces and results.
2. Inspect every assigned component and operation. Do not infer one route for the product.
3. Challenge the route premise with evidence. Consider orientation, support contact/removal, tool approach and workholding. Also check precision claims against demonstrated capability and material choices against sourced load, heat, wear and creep evidence. Review contact skins, fastener access, engagement and thread preparation. Compare printed shafts/bearings with sourced standard or metal parts. Review custom parts against standard parts, machining against sheet/hybrid designs, and sheet alignment against stiffness needs.
4. Check each operation against its profile and criteria. FDM reviews orientation, contact, regional walls, critical features and support plans. CNC reviews stock, setup, workholding, tools, access, sequence and inspection. Other processes need separate evidence. Never substitute FDM checks.
5. Verify purchased identities, ratings and interfaces from sources. Outsourced custom routes remain in scope.
6. Missing sources, profiles, criteria or handler coverage remain UNKNOWN. Geometry checks prove only their measured observation.
7. Review support plans. A threshold creates a planning obligation. A credible accessible support/bridge strategy can address it. Identify contact and removal access. Check finish, damage and distortion. Critical surfaces need finishing allowance, tool access and revalidation.
8. At L2, report checks outside the light-review scope as untested. At CRITIQUE, review all assigned routes. The lead owns decisions and edits.
9. Tie each finding to a subject, severity, criterion/source, method/evidence, value/units or UNKNOWN, uncertainty, consequence, remedy/alternatives and recheck.
MUST Use the shared verdict derivation in `cad-record/references/evidence.md`; do not independently map finding severity to verdict.
MUST Mark a route violation that prevents manufacture or a mandatory function as a failing major finding; a documented remedy may lower it only when it satisfies the recorded criterion.

## Rules

MUST Keep findings conditional and evidence-based. Challenge unsupported premises, not general process categories.
MUST Distinguish UNKNOWN/untested from PASS.
MUST Remain read-only.
NOT Invent data, infer process from appearance, edit records or run equipment.

## Output

Begin with `VERDICT: PASS|MINOR|MAJOR` and name reviewed parts/processes. Separate untested checks from findings. Give cited paths on separate lines. Use structured make/buy, assembly and check lists.
CAP 120w clean; 280w with findings.
MUST Never reprint code, diffs, file contents, or the caller's claim.
