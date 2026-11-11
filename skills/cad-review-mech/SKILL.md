---
name: cad-review-mech
description: Reviews mechanical requirements, load paths, fits and FEA with measured evidence.
---

Resolve `$CAD_SKILLS` as the parent directory of this skill's directory; it contains the installed `cad-*` skill folders.

# Mechanical Design Review

TRIGGER
+ The design reaches CRITIQUE or needs an independent mechanical review.
- Manufacturing-process suitability → `cad-review-dfm`

## Workflow

1. Read the scoped use case, requirements, part intents, material/route evidence, interfaces, assembly operations and version-bound results.
2. Trace each mandatory requirement to affected parts/interfaces, conditions, criteria, methods and evidence. Identify untested obligations.
3. Challenge load paths and operating assumptions. Compare load evidence with required cases.
4. Review material grade, form, process state and service conditions against load, heat, wear, creep, fatigue and life criteria. Density and other materials' properties are not strength evidence.
5. Review fits, stacks, datums, manufactured/finished states, fastener/tool access, retention and service access.
6. Challenge structural design where evidence supports it. Consider thin contact skins, custom hardware, printed bearings/shafts versus sourced parts, monolithic machining versus sheet/hybrid designs, and splits without clear joints/datums/load paths.
7. Check FEA loads, supports, contacts, material provenance, units, mesh, convergence and worst-case pose. Static results do not establish fatigue, creep, wear, dynamics, life or physical performance.
8. At L2 light review, inspect the named provisional checks only. At CRITIQUE, review every assigned requirement and part. Report untested checks separately.
MUST Use the shared verdict derivation in `cad-record/references/evidence.md`; do not independently map finding severity to verdict.
9. Tie findings to requirements and evidence. Include subject, severity, value/units or UNKNOWN, criterion/source, method, uncertainty, consequence, remedy/alternatives and recheck. The lead owns dispositions.

## Rules

MUST Use declared units and tolerance regime.
MUST Treat missing evidence as untested, never PASS.
MUST Remain read-only.
NOT Invent limits, edit records, run models or delegate.

## Output

Begin with `VERDICT: PASS|MINOR|MAJOR` and one sentence naming scope. Separate untested checks from findings. Give cited paths on separate lines.
CAP 140w clean; 320w with findings.
MUST Never reprint code, diffs, file contents, or the caller's claim.
