# Design rules

`scripts/design_rules.py` holds the record's design rules. `validate.py` runs them on the schema-valid record, and each finding carries a stable `code`.

## Clearance margin

A measured distance below a clearance requirement's `min_clearance_mm` fails. A distance at or above the minimum but below minimum + margin is a `margin` warning, never a failure.

- The margin is the interface's `margin_mm`, else product.yaml `design.clearance_margin_mm`, else 0.1 mm.
- cad-model check and cad-motion sweep read it through one loader: `load_clearance_margins(record_dir)`, where `record_dir` is `hw/` or a version's `record/` snapshot. `clearance_margins(product, interfaces)` takes already parsed documents.
- Both return `ClearanceMargins`: `.default_mm`, `.by_interface` (clearance interface id to margin), and `.for_interface(id)`, where `None` names a global minimum and an unknown id raises KeyError.
- A malformed margin raises ValueError; it never falls back to the default.
- Load the module by path as `cad_record_design_rules` and register it in `sys.modules` before running it. It needs PyYAML and the standard library only.

## Retention

Codes `retention.torque_path` and `retention.axial_retainer`. Both are errors from stage `parts` (L2) on and are not reported before it.

- Every revolute declares `torque`. Use `transmitted` when its child carries drive torque: a crank, camshaft, gear or pulley shaft, coupled or not. Use `none` for a hand-positioned or free-running joint.
- A drive train is the child of every revolute not declared `none` plus that child's fixed-joint descendants. An undeclared revolute counts, so one run reports it together with every fixed joint on its child. Every fixed joint inside a drive train declares `torque`: `transmitted` with a `torque_path` (`key`, `set_screw`, `d_flat`, `press_fit`, `spline`, `clamp`, `pin` or `adhesive`), or `none` when it carries no drive torque, such as the set screw or ring itself.
- A `transmitted` revolute needs no `torque_path`. Give one only when its child is driven through the joint by a shaft that is not modelled, such as a coupler clamped on a motor's D-shaft.
- Every `press_fit` and `locating_fit` needs an `axial_retainer`: `shoulder`, `circlip`, `set_screw`, `nut`, `pin`, `cap`, `captured`, `press_fit` or `adhesive`. A `press_fit` retainer is valid on a press fit only. A `running_fit` is a motion fit and is exempt.
- A hardware torque path (`key`, `set_screw`, `pin`) or hardware retainer (`circlip`, `set_screw`, `nut`, `pin`, `cap`) names its modelled occurrences in `parts`, and each must resolve to one occurrence. A decision record naming hardware nobody modelled does not count.
- `decision: DR-###` links either one to the decision record that chose it. `validate.py` rejects a link with no `decisions/DR-###.md`, at every stage.

### Retrofitting an existing record

Run `validate.py` with the product at stage `parts`. Each `retention.*` finding's `path` points at the joint (`assembly.yaml#/joints/J-###`) or the fit (`interfaces.yaml#/interfaces/<n>/limits/<kind>`) to fix.

1. For each `retention.axial_retainer` finding on an existing press fit that its interference holds, add `axial_retainer: {type: press_fit}`. A locating fit names its shoulder, circlip (with `parts`) or other retainer.
2. For each `retention.torque_path` finding on a revolute, add `torque: none` when the joint only positions or idles, or `torque: transmitted` when it carries drive torque. A `none` revolute also clears the findings on the fixed joints of its child.
3. For each remaining finding on a fixed joint, set `torque: transmitted` with its `torque_path`, or `torque: none`.
4. Model any set screw, key, pin or circlip that a path or retainer names but the assembly lacks.

## Optical and transparency requirements

Transparency is a hard functional requirement in manufacturing selection, not a prose warning or an FDM exception. Opaque routes are ineligible. Consider only a traceably identified purchased transparent part or identified transparent stock made by an appropriate evidenced process; criteria include material identity/grade, optical grade, thickness, supplier evidence and interface compatibility. Unknown clear plastic is not a substitute; translucency is not transparent. See `manufacturing-intent.md` and `cad-flow/references/manufacturing-selection.md`.

U7 removes legacy `feasibility.transparent_fdm`, `feasibility.transparency_claim` and prose-matcher `design_rules.transparency_claimed` behaviors. Their removal does not turn transparency into a PASS: the v2 record's selection hard constraint and `optical.route`/`optical.transmission` obligations are the authority. On a historical record, absence of an assessed selection is unknown, never retroactive certification.

## Visibility bindings

A requirements.yaml entry's optional `visibility` lists show-the-motion checks, each `{view, window, targets, min_visible_fraction}`:

- `view` is a cad-render view: `iso`, `front`, `side`, or `dir-X-Y-Z` in assembly coordinates with at least one non-zero component.
- `window` and `targets` are disjoint, non-empty lists of part references (a PN placed once, or `@occurrence`).
- `min_visible_fraction` is in (0, 1]; absent means 0.5.

Rendering only the window and target parts, from `view` at the nominal pose, each target's own visible share of its projected area must reach `min_visible_fraction`; the check fails when any target falls short. Only window parts declared `transparency: transparent` are see-through.

Code `visibility.binding`: an error at every stage when a window or target part does not resolve to one occurrence, or one part is both. cad-render reads the bindings through `visibility_bindings(requirements)`, which returns `VisibilityBinding(requirement, index, view, window, targets, min_visible_fraction)` with the 0.5 default applied.
