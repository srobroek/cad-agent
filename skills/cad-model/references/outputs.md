# cad-model outputs, checks, and thresholds

## Version layout (`run`)

`hw/versions/vN/`, written to a hidden partial folder and renamed when complete; a failed run
leaves nothing, and `latest.json` keeps naming the previous version. Labels increase: `run` refuses
an existing label and any label at or below the newest built version. Paths follow the cad-record
layout (`cad-record/references/layout.md`); `run` refuses to seal a file the layout does not
admit.

| File | Content |
|---|---|
| `parts/<PN>.step` | AP242 (`AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF`), one free product `"<PN> <title>"`: the definition in the part's `model_frame` |
| `assembly.step` | AP242, top product `"<product id> vN"`, one product per PN and one component per occurrence at T = D P (nominal pose); an explicit occurrence's component is named `"<PN> <title> [@id]"` (cad-motion `pose.write_assembly_step`) |
| `assembly.glb` | binary glTF 2.0 of `assembly.step`: metres, +Y up, one node per occurrence named by its instance name (`"<PN> <title>"`, or `"<PN> <title> [@id]"`), mesh names = product names |
| `part-map.json` | `schema_version: 2`; `occurrences.<id>`: `PN`, `ref`, `nodes`, `node_indices`, `meshes`, matched on the exact instance name (authoritative for selection and motion); `parts.<PN>`: `product`, `occurrences`, and the aggregated `nodes`, `node_indices`, `meshes` |
| `metrics.json` | `schema_version: 2`; `mass_source` (`material_profiles`, record v2, or `legacy_density_table`, record v1) and the density table (v1 only). `parts.<PN>` contains model-frame geometry, solid/shell/face/hole facts and the part material label. The label is never a property source. Mass metrics include `density_g_cm3`, `mass_g`, `mass_target_g` and `mass` detail. Density or COM stays null when unresolved. Per-component profiles may produce mass bounds instead of one value. `occurrences.<id>` contains PN, transform, placed geometry and hole facts. `totals` aggregates occurrences and their known mass and COM.
| `drawing-features.json` | only when a model defines `drawing_features`: `label`, `units`, `frames.<PN>` (its `model_frame`), and `parts.<PN>` = that model's locators in that frame (semantic ID → locator, `references/contract.md`); parts without the hook are absent |
| `record/` | byte copies of the records the version was built from, every file cad-record `layout.snapshot_names` names that exists: `product.yaml`, `requirements.yaml`, `interfaces.yaml`, `assembly.yaml`, `parts/<PN>/part.yaml` of every part record, `manufacturing/capabilities.yaml`, `materials.yaml`, `selection.yaml` and every file below `manufacturing/sources/` (exact bytes). Never the findings or evidence ledgers. `run` reads them once, before the kernels, and refuses to seal if any record, source or `model.py` changed during the run |
| `manufacturing/<PN>/` | every custom-made PN with an output contract (below): `part.step`, `part.stl` for an additive part, `components/<COMP>/` for each component of a part of several, and `manufacturing.json` |
| `manufacturing-features.json` | only when a model defines `manufacture_features`: `label`, `units`, and `parts.<PN>`: `state`, `decision`, `frame` (`manufacture`), `step_sha256` of the state STEP, `features` (its locators in the native frame of the state) and `correspondence` (`shared`, `manufacture_only` and `installed_unmapped` semantic ids) |
| `manifest.json` | `schema_version: 2`; `record_version` (the record version of its snapshot; a manifest without it predates snapshots of requirements and manufacturing records). Per PN, `parts.<PN>`: `kernel`, `model_frame`, `occurrences`, `model`, `source_sha256`, `record_sha256` (part.yaml), `params` and the kernel's volume, solids and validity. Per occurrence, `occurrences.<id>`: `PN`, `ref`, `explicit`, `group` and `rest` (P). `pose` holds `values` (independent joints at nominal, coupled joints derived from them; a derived value outside its limits fails the run) and, per occurrence, `transforms` (T) and `motions` (D). `joints` lists each joint with occurrence-id `parent` and `child` (a coupled joint's `nominal` is null); `joint_pairs` lists their sorted pairs with `joint` id and `type` (an `--attach` joint is fixed, id `attach:<REF>`). `record` maps each snapshot path to its sha256; `manufacturing.<PN>` is each custom-made PN's manufacturing entry (below). Also: `ground`, `tools`, `glb`, `drawing_features`, `manufacturing_features` and the `files` sha256 map of every file but itself. |

A file without `schema_version` is a version 1 version, written before occurrences: it reads as one
occurrence per PN whose id is the PN (cad-render `scripts/versions.py`, `built_occurrences`, the
one reader for render, viewer and check). A newer schema is refused. Versions are never rewritten.
Definition metrics and bboxes are in each part's model frame, read back from the per-part STEP.
Every volume is BRepGProp's adaptive integration to 1e-9 relative (cad-motion `pose.VOLUME_EPS`,
through `pose.volume_properties`, which cad-motion's sweep uses too); the default fixed-order rule
is off by up to ~6e-6 on revolved and filleted B-spline faces.
Mass is solid-material mass at a uniform, fully dense density: `volume_mm3 * density / 1000`. A record
version 2 part takes the density its material profile resolves (hw/manufacturing/materials.yaml,
cad-record `resolve_property`), per component when its components name different profiles; `mass`
holds `status` (known or unknown), `source`, `basis`, `method`, `densities` (per profile: `profile`,
`status`, `value` g_cm3, `basis`, `sources`, `conditions`, `reasons`), `value_g` (a number, or
`lower`/`upper` for an interval density) and `reasons`. An unresolved density leaves `mass_g` and the
centre of mass null; it is never replaced by a default or another material's value. A record version 1
part keeps the labelled estimate from the density-only table `assets/materials.yaml`
(`source: legacy_density_table`, `basis: estimate`), which supplies no other property.

`holes` lists the part's cylindrical bores (`cadmodel/holes.py`), sorted by (`diameter_mm`,
`position`), `[]` when there are none:

```json
{"id": "H1", "diameter_mm": 5.0, "axis": [0.0, 0.0, -1.0], "position": [10.0, 15.0, 8.0],
 "depth_mm": 8.0, "through": true}
```

- A bore is the concave cylindrical faces of one solid on one axis line with one radius (within
  1e-4 mm) that close the full circle on each axial slice; slices are cut at every face boundary.
  Seam-split faces side by side close it. Faces that only follow each other along the axis (a
  half-round channel cut in two pieces) do not. A convex cylinder (boss, pin) is not a hole;
  neither is a stretch that covers less than the full circle (slot end, concave fillet, open
  channel). Slots and non-cylindrical cut-outs are not listed.
- An end is open when a face across its rim reaches more than 1e-4 mm outside the bore radius:
  a boundary face, a slot, a cross bore, a larger coaxial step, or a lead-in chamfer or round.
  There is no minimum lead-in angle: a chamfer of half-angle α and axial length L opens the end
  once L·tan α > 1e-4 mm. An end is closed when every face across its rim stays within the
  radius: a flat floor, a drill point, a ball end, a corner radius, or a counterbore shoulder.
- Two stretches on one axis make one bore only when both facing ends are open, as in a clevis pin
  bore across its slot. Otherwise each is its own bore, as with two blind holes drilled from
  opposite faces.
- Each coaxial step is its own bore. A Ø5 through hole with a Ø9 x 3 counterbore lists the Ø5
  through from the shoulder, and the Ø9 blind with depth 3 down to the shoulder. They share the
  axis line, and the larger bore's floor (`position + axis * depth_mm`) is one end of the smaller
  bore. Steps are not merged into one entry because each is its own tool and depth, which is what
  a manufacturability check reads, and because the step chain stays recoverable from the axis.
- `diameter_mm` is rounded to 3 decimals. `depth_mm` is the axial length of the cylindrical wall,
  entry chamfers, rounds and drill points excluded, gaps across slots included.
- `through` is true when the bore is open at both ends.
- `axis` is the unit drilling direction and `position` the axis point at the entry end. A blind
  bore enters at its open end. A through bore enters so its axis points down: the first non-zero
  component of z, y, x is negative.
- Detection reads the STEP read-back, so both kernels list the same holes.

`hw/versions/latest.json` names the newest successful version, every path relative to `hw/`:

```json
{"label": "v3", "assembly_step": "versions/v3/assembly.step",
 "parts": {"LMP-0001": "versions/v3/parts/LMP-0001.step"},
 "glb": "versions/v3/assembly.glb", "metrics": "versions/v3/metrics.json",
 "part_map": "versions/v3/part-map.json", "manifest": "versions/v3/manifest.json",
 "manufacturing": {"LMP-0001": "versions/v3/manufacturing/LMP-0001"}}
```

With a `drawing-features.json` it also has `"drawing_features": "versions/v3/drawing-features.json"`.
When `run` sealed a version but could not write `latest.json`, it exits 2, keeps the version and
names the command that points the pointer at it: `cad_model.py latest <hw> --label vN`. That
command checks every file of the newest version against its manifest first and rebuilds nothing.

Reused outputs: `run` builds only the parts whose build inputs changed since an earlier build of the
same project:

- the files of `hw/parts/<PN>/` other than `part.yaml` (`model.py`, `sources/**`; hidden names and
  `__pycache__/` excepted);
- the part.yaml fields the runner uses: the product name `"<PN> <title>"`, `model_frame` and
  `manufacture.relationship`;
- the kernel environment: the interpreter, platform and every installed distribution, which
  `run_<kernel>.py --tools` reports without importing the kernel;
- the plugin and project Python files the runner loaded, recorded with the build and hashed again.

Every other part reuses its earlier STEP (and `build_manufacture` state) byte for byte, with the
runner facts the manifest records (`params`, kernel volume, solids, validity, drawing features,
warnings). A kernel whose parts are all reused does not start; the manifest's `tools.<kernel>` then
comes from `--tools` and has no `import_s`. A custom-made part's `manufacturing/<PN>/` files,
placement and checks are reused the same way while its manifest facts, its state's geometry and the
orchestrator's code and runtime are unchanged. The parts built in a run that then fails are kept
for the next run. A part is cached only when the runner executed the model bytes and folder files
that keyed it and the folder did not change during the build; otherwise it is built but not cached
(`cache.unsaved`), and when the change remains the run refuses to seal. An object that `store gc`
removed during a run is a miss: the part is built again. `scratch.py init` seeds a scratch copy's
store with hard links to these objects and records.

Each output is stored once in the part store, `hw/.cad/parts/<sha256>`, named by the sha256 of its
bytes; `hw/.cad/builds/<sha256>.json` maps the sha256 of a build's inputs to its objects and facts.
A version holds a hard link to each object where the filesystem allows one, else a byte copy, at
its usual path. An output is reused only after its object still hashes to its name. Manifests hash
file content, so verification is unchanged. Objects, and the version files linked to or copied from
them, are read-only (0444): an in-place write fails instead of changing every version that shares
the bytes, so a tool that must change a sealed file has to write a new file. The store ignores itself
in git. After deleting versions, `cad_model.py store gc <hw> [--dry-run]` frees what they alone held:
it removes every object no version or release manifest references and no build record of a
referenced output needs (such as a part's state STEP), drops every other build record, and reports
`removed_bytes` and `freed_bytes` (bytes no other link still holds). It refuses while a version is
being staged or a manifest cannot be read, and never removes an object a manifest names.

`run` stdout: `status`, `label`, `version_dir`, per-part `kernel`/`build`/`volume_mm3`/`solids`/`mass_g`,
`manufacturing` per custom-made PN (`status`, `complete`, `path`, `process`, `state`, `build`),
`cache` (`parts` and `manufacturing`, each `{built, reused}`: how many outputs this run made and how
many it reused), `warnings` (`[{pn, message}]`, including non-functional fillet edges skipped after
all radius retries; a reused part repeats its build's warnings), and `timings_s` (`<kernel>_import_s`
and `<kernel>_runner_wall_s` for each kernel that started, `kernels_s`, `manufacturing_s`, `glb_s`,
`total_s`). `build` is `built` or `reused` (null for a manufacturing entry without a model).
On exit 1: `findings`, one per failed part: `pn`, `check`, `type`, `message`, `where`, plus
`runners` timings when the kernels ran. `check` is one of:

- `contract`: a model folder not named by its PN, or a missing or unsupported `KERNEL`
  (`type` `ContractError`, `where` null)
- `kernel`: the part's `KERNEL` differs from `--kernel` (`ContractError`, `where` null)
- `build`: the runner's exception `type`, with `where` = `model.py:<line> in <function>`, null when
  the failure is not in model.py
- `manufacture`: a refused part.yaml `manufacture` declaration (`ContractError`, before any kernel
  runs), or a failed manufacturing check (`ManufactureError`). The message names each failed
  check and condition, and `manufacturing.<PN>` carries the part's rows.

`product.yaml` can declare a printer volume for FDM parts:

```yaml
build_volume:
  x_mm: 256 # Bambu Lab X1/P1 class printer envelope
  y_mm: 256
  z_mm: 256
  orientations: [xyz, xzy, yxz, yzx, zxy, zyx] # optional; defaults to all six axis permutations
  margin_mm: 5 # optional; removed from both ends of each usable axis
```

`cadmodel/orientation.py` is the one owner of build orientation, for `run` and `check`. It tests the
part's axis-aligned box under each permitted permutation. A permutation names which part axis each
printer axis takes: `xzy` maps part X to printer X, part Y to printer Z and part Z to printer Y.
Alone that can be a mirror, so the printer X row is reversed whenever the plain permutation has
determinant -1; every rotation is proper. `xyz` wins when it fits, else the lowest print height,
then the largest XY clearance, then the name. The margin reduces each usable printer axis at both
ends.

When no permutation fits, it tries the minimum-volume oriented box. Its axes, each signed so that
its largest component is positive, go to the printer axes in every order under the same
preference. Every candidate is rotated and its box measured again; only a measured fit counts.

`check.build_volume` is a diagnostic (`blocking: false`). An envelope fit is geometric evidence only. The part's route, capability profile and approved orientation govern process feasibility.
For FDM/resin parts, the check uses manufacturing STEP when present; otherwise it uses the definition. The manufacturing STEP is already in the built orientation. If that orientation is approved, the check compares that pose with the recorded envelope fields `approved`, `envelope_source` and `reserved_mm`. It tries no other pose.
For parts without an approved orientation, the legacy search proposes `pose: auto_fit_proposal`. The row reports orientation, method, size, model-frame size, rotation axes and determinant. It also reports rotated axes when a fit exists. A failed or missing envelope produces a warning.

## Manufacturing outputs

A part is custom-made when part.yaml has `make_buy: make` or `procurement.mode: made_to_drawing`
(cad-record `layout.custom_made`); a catalogue buy has no manufacturing output. `run` writes
`manufacturing/<PN>/` for each custom-made PN whose primary process has an output contract, once per
PN however many occurrences place it. A process without one (`other`) gets `path: null`,
`coverage: unknown` and no file: nothing is invented, and freeze and release refuse it.

| File | Content |
|---|---|
| `part.step` | AP242, exact B-rep, millimetres, one product `"<PN> <title>"` (the state is metadata, never a second PN) with its geometry in the bed frame, not placed by a location |
| `part.stl` | additive (fdm, resin) only: binary STL in millimetres, tessellated from `part.step` with absolute deflections, 0.05 mm and 0.1 rad unless part.yaml `manufacture.mesh` declares others. STL carries no units; `manufacturing.json` states them |
| `components/<COMP>/part.step`, `part.stl` | a part of several components only: each component's own solids, in its own pose, with the part's process outputs |
| `manufacturing.json` | the manifest entry below, `files` (sha256 of every output), and the check rows `run` measured |

The state is the installed definition, or the `build_manufacture(params)` shape of a part whose
part.yaml declares `manufacture.relationship: different` (`references/contract.md`). Every
disconnected solid of the state is a manufacturing component (`cadmodel/components.py`): a sole solid
is the implicit `main`; a record version 2 part of several declares them in part.yaml
`intent.components`, each bound by an interior `witness` (native frame, mm) or a `locator` feature
whose patch points lie on it, and a `multiplicity` n also takes the n - 1 other solids congruent to it
(equal volume, area and principal moments: a mirror image counts as congruent, so declare mirrored
pieces separately). Identity is never guessed: a witness within 1e-3 mm of a surface or inside two
solids, a feature on several solids, two components on one solid, overlapping solids, a solid no
component binds, and a group without exactly its count of congruent members fail the run
(`components`). A record version 1 part of several solids keeps one combined output whose identity is
unknown, incomplete.

An additive component sits on the bed (its box centred in X and Y on a declared envelope, its lowest
point at Z 0) in the pose its route fixes: the approved `orientation.rotation` of the first additive
operation that shapes it (`cadmodel/orientation.py` `rotation_of`; machine = rotation x native). A
proposed or unknown orientation, or no additive operation shaping it, exports its native frame and is
incomplete. The envelope is that operation's capability profile `envelope` when x, y and z are known
lengths, else product.yaml `build_volume`, a declaration. A record version 1 part keeps the legacy
envelope auto-fit (`pose: unapproved_auto_fit`, or `unapproved_native` without a fit), labelled, never
approved. Any other part keeps its model frame (identity). Components in different poses leave the
combined part.step in their native frame on the bed (`pose: per_component`).

The manifest entry `manufacturing.<PN>` holds:

- identity: `PN`, `rev`, `title`, `product`, `material`, `process`, `finish`, and `eligibility`
  (`make_buy`, `procurement_mode`)
- `state`: `relationship`, `name`, `reason`, `decision`, and `source` (`build_manufacture` or
  `installed definition`)
- `tolerance`: `volume_rel`, `volume_abs_mm3` and `allowed_mm3`, the larger of the absolute floor
  and the relative share of the installed volume. cad-record `manufacture_policy` gives 1e-9
  relative for a part made as installed, and the declared values otherwise.
- `installed` (`step`, `sha256`, the kernel volume and solids) and `native` (the manufacturing
  state's kernel volume, solids, validity and errors)
- `intent`: `record_version`; for record version 2 `material_profile`, `route` (per operation `id`,
  `kind`, `capability`, `provider`, `components`, `depends_on`, `known_kind`), `profiles` (sha256 of
  each material and capability profile the part names), `sources` (sha256 of each cited source
  artifact's snapshot bytes) and `inputs_sha256`; for version 1 `status: unknown` and its `reason`
- `units` mm, `up` +Z, `build_volume` (the envelope used: size, margin, usable size, orientations,
  `source`) and `mesh` (additive: format and deflections)
- `placement`: `method` (approved, proposed, native, axis_aligned, oriented or identity),
  `orientation`, `bed` (centred, origin or null), `frame` (bed or model), `transform` (3x4 rows,
  unrounded), `determinant`, `pose` (approved, proposed, not_applicable, unknown, unapproved_auto_fit,
  unapproved_native, model_frame or per_component), `approved`, `operation`, `decision`, `reason`
  and `build_volume`
- `components.<COMP>`: `binding` (implicit, witness or locator), `points_mm` (native frame),
  `locator`, `multiplicity`, `material_profile` (the component's, else the part's), `operations`
  (the route operations that shape it; `[]` unplanned, null while the route is unknown), `planned`,
  `files`, `placement` (as above), `volume_mm3` and `members` (per solid, native frame:
  `volume_mm3`, `area_mm2`, `centre_of_volume_mm`, `bbox_mm`, `principal_moments_mm5`);
  `component_outputs` (the component files) and `component_identity` (`status` bound or unknown, with
  the unbound `solids` of a version 1 part)
- `path`, `outputs` (the process's files), `files` (sha256 of every output, the sidecar included),
  `status`, `complete` and `blocking_failures`

A custom-made PN without a model.py gets `path: null`, `status: warn`, `complete: false` and a
`message`. A complete pass claims output integrity in the recorded pose only: never process
feasibility, a calibrated capability or any physical validation.

Each part row has `status` (pass|warn|fail), `complete`, `blocking_failures` and `checks`; each
check has `status`, `blocking` and `failed`, the conditions that do not hold:

| Check | Fails when |
|---|---|
| `outputs` | a file the layout requires for the process or a component is missing, another file is present, or a file's sha256 differs from the manifest (`check` only) |
| `record` | `PN`, `rev`, `material`, `process` or `finish` differs from the version's part.yaml snapshot |
| `native` | the manufacturing state is not a valid B-rep (BRepCheck in the kernel), has no solid, or has no positive volume |
| `identity` | the manufacturing state's kernel volume differs from the installed one by more than `allowed_mm3`, compared unrounded, or their solid counts differ. Equal volume is not proof of the same holes, walls or material |
| `step` | `part.step` is not AP242 or does not declare millimetres only. It holds anything but one product `"<PN> <title>"` at identity. It is an invalid B-rep, or has another solid count than the state. Its read-back volume differs from the state's kernel volume by more than 1e-6 relative |
| `mesh` | additive: `part.stl` is not binary, or holds a non-finite coordinate. After welding by position and dropping collapsed triangles, a triangle has an area of 1e-12 mm^2 or less. It is not watertight, not consistently wound, or has non-positive signed volume (inward). It has another shell count than the STEP. Its volume differs from the STEP's by more than area x (deflection + float32 rounding), or a box face sits farther than the deflection inside the STEP's. Nothing is repaired, so a crack stays open |
| `placement` | the transform is not a proper rotation (orthonormal, determinant +1 within 1e-9); a part on a bed does not touch Z 0 within 1e-6 mm; a model-frame part is not at identity. A record version 2 part on a bed outside its approved orientation, outside the reserved extents of its envelope (X and Y: margin to size - margin; Z: 0 to size - 2 margin), or without an envelope, warns and is incomplete |
| `components` | (`run`) the solids cannot be bound to the declared components without guessing. (`run` and `check`) per component: its own `step` (its member count and volume), `mesh` and `placement` as above, and `binding`: its witness, moved by its placement, lies inside exactly one solid of its file (feature points on it) |

Any failed check fails the run (exit 1): no version, no new `latest.json`. A part is `complete` when
every check passes; incompleteness (no approved orientation, no envelope, no fit, no model, unbound
version 1 solids) warns without blocking.
cad-model does not resolve `drawing.manufacturing.build_direction` datums; cad-drawings compares the
declared direction with `placement.transform`.

## Check result (`check`)

Top level: `status` pass|fail, `label`, `pose: nominal`, `baseline` (previous label or null),
`scope`, `blocking_failures`, `warnings`, `evaluation` (`mode` fast|exact, `mesh`, `band_mm`,
`rule_sha256`), `checks`, `suspects`, `timings_s` (`total_s`; `kernel_evaluations`, `exact_hits`,
the exact readings the pair cache held, `decision_hits`, `bucket_hits`, `cleared` by method, and
`reevaluations` of suspects). Every check has `status` (pass|fail|warn, `skipped` for a check a
partial scope cannot run) and `blocking`.

`scope` says what the check covered. `{kind: all_pairs, complete: true}` is the default;
`named_pairs` (`--pair`) is not complete. `--changed-only` gives
`{kind: changed_pairs, complete: false, baseline, selected_pairs, unchanged_pairs, reasons,
removed_pairs, changes}`, with `full_selection` when there is no baseline. It measures only the
pairs cad-motion `incremental.py` selects against `--since-version` (default: the newest built
version before the checked one): new pairs, pairs whose part geometry changed (the STEP DATA
section, so a rebuild that only changes the header is no change), whose relative placement has
other bits, however small the move, or whose declarations (joints, fits, inserts, clearance
requirement and margin) changed. A displacement of the whole product can leave rounding-only
differences; those pairs are measured again and meet their cached readings in the pair cache.
`interference` and `pair_clearance` cover the selected pairs only. `min_clearance` is null, and
`connection` is `skipped`. A changed-only result is early evidence, never a complete assembly
check: never stamp or release from it.

| Check | Blocking | Fails when |
|---|---|---|
| `step_readback` | yes | The STEP schema is not AP242, a product name differs from `"<PN> <title>"` in the part or assembly STEP, or the top product is missing. The read-back volume differs from the kernel volume by more than 1e-6 relative, or the read-back solid count differs from the kernel's. |
| `build_volume` | no | a diagnostic: an additive part in its approved pose does not fit the envelope its export recorded, or the legacy search finds no permutation or oriented box of the printed shape within the declared envelope, or no envelope is declared; it warns and never blocks (an envelope fit is not process feasibility) |
| `artifact_integrity` | yes | a file of the version is missing, changed, unlisted in the manifest `files`, a link or special file (`problems`; cad-record `layout.verify`) |
| `manufacturing` | yes | a custom-made PN of the record snapshot has no manufacturing entry, a manufacturing entry or folder belongs to no custom-made PN, or a part row fails (`parts.<PN>`, the rows above, judged again on the sealed files). `complete` is true when every part is complete; an incomplete part warns. A version built before manufacturing outputs warns, `blocking: false` |
| `brep_validity` | yes | BRepCheck rejects the kernel's in-memory shape or the STEP read-back |
| `solid_structure` | yes | a part has no solid, or its solid count differs from `--expect-solids`, else from the count its record snapshot declares (a record version 2 part made as installed: the sum of its part.yaml `intent.components` multiplicities, `baseline: intent.components`), else from the previous version; `--expect-solids` naming a PN outside the version fails (`unknown_parts`) |
| `interference` | yes | Two parts share more than 1e-3 mm^3 at the nominal pose, unless the pair is a declared interference fit or heat-set insert (below) or `--allow-interference` names an undeclared intentional overlap. A declared interference fit that measures no interference is reported in `declared_fit_findings` and fails. A pair whose measurement failed or whose reading is an unresolved suspect (`method` null, `error`) fails even when allowed. `--pair` or `--allow-interference` naming a PN outside the version fails (`unknown_pairs`, `unknown_allowed`). Joint-connected pairs are listed under `joint_pairs`; others under `pairs`. A measured row has `method: exact` with `clearance_mm` and `interference_mm3`; a far row has `method` box, mesh, `mesh (exact suspect)` or cache-bound, `lower_mm`, `upper_mm` (null: unbounded) and null distance and volume: its bounds prove it does not overlap; `min_clearance` names the closest pair |
| `pair_clearance` | yes | a clearance requirement of the version's `interfaces.yaml` (`type: clearance`, `pair`, `min_clearance_mm`, resolved by cad-motion's `pair_policy.py`) measures below its `min_clearance_mm` at the nominal pose, compared unrounded; its pair's measurement failed (`error`); or the version has no such occurrence pair. A requirement binds its pair whatever joins it, so it also binds a joint-connected or fitted pair. A clearance at or above the requirement but below it plus `margin_mm` (cad-record's margin: the interface's `margin_mm`, else product.yaml `design.clearance_margin_mm`, default 0.1) is `status: warn`, `warning: margin`, never a failure; the check then warns. Each `requirements` row has `pair` (occurrence ids), `refs`, `required_mm`, `margin_mm`, `clearance_mm`, `method`, `status` (pass/warn/fail), `interface` and YAML `source`; a far pair passes on its `lower_mm`, which its measurement put above requirement plus margin; with no requirement declared the check passes |
| `connection` | yes | the two parts of a fixed joint or `--attach` are more than 0.01 mm apart (`fixed_joints`: `joint`, `pair`, `distance_mm`, `ok`); or a part is `disconnected` (nearest part > 2.0 mm away), or `floating` (0.01 to 2.0 mm) without a movable joint (revolute, prismatic) linking its rigid body to the nearest part's; `touching` is <= 0.01 mm. Fixed joints weld their parts into one rigid body, so a float to a part of the same body is never a joint clearance. Each `parts` row has `nearest`, `distance_mm` (always measured: a far pair that could be a part's nearest is measured exactly), `joint` (the movable joint between the two bodies, else null), `joint_connected` |
| `mesh_watertight` | yes | a part's GLB triangles, welded by position, are not a closed 2-manifold (trimesh); triangles whose corners weld to fewer than three vertices (BRepMesh emits one at a sphere pole on a face boundary) are dropped first and counted in `collapsed_triangles` |
| `slivers_tolerances` | no (warn) | a face is smaller than 1e-3 mm^2, or a vertex/edge/face tolerance exceeds 0.01 mm |

`--pair PN:PN` restricts `interference` to the named pairs; every pair still feeds `connection`.
Clearance and shared volume come from cad-motion's pair measurer (`pair_measure.py`, around
`pose.interference`), the one `sweep.py` uses, under the same rule. It measures the per-part STEP
definitions, identified by their DATA sections. It places part b's definition in part a's frame as
the sweep does at the nominal pose: through the joints from the pair's common ancestor, rebuilt from
the version's record snapshot, which must place every occurrence where the manifest does. A version
without a snapshot uses T_a^-1 T_b of the assembly STEP components. The distance is the BRepExtrema
minimum distance, 0 when one part lies inside a solid of the other, a solid of a compound part
included. Within 1e-6 mm, the shared volume is the non-destructive Boolean common (fuzzy 1e-7 mm),
measured with the adaptive volume rule. Fast mode, the default, leaves a pair unmeasured when its box
gap or mesh bound (python-fcl) clears 1e-6 mm, its clearance requirement plus margin and the 2 mm
band. A declared interference fit and a fixed joint are always measured. `--exact` measures every
pair. Each reading is checked against its own box and mesh bounds; a contradicted one (a degenerate
BRepExtrema 0.0 mm) is a suspect, listed in `suspects`. It is re-evaluated with part b moved 0.01 mm
both ways, with each independent joint 0.1 off nominal where that moves the pair, and swapped. A
resolved suspect passes on its bounds (`mesh (exact suspect)`); an unresolved one, or one on an
always-measured pair, fails its pair. By default (`--jobs 1`), check measures in this process on
OCCT's thread pool. `--jobs N` measures in N worker processes that die with the check; `auto` is the
CPU count, at most 8. Results go through cad-motion's pair cache, `hw/.cad/pairs-v2.sqlite`
(`--cache DIR` moves it, `--no-cache` skips it), shared with the sweep. It is keyed by both parts'
geometry, the exact placement and the rule. A check of v2 after v1 measures only the pairs whose
part or placement changed, and a hit is the value a measurement gives.

A check is a run recorded in `hw/.cad/runs/` (cad-flow `process_lifecycle.py`). It exits 2 without
checking while another check runs, or while an earlier run of any kind left processes behind, and
prints the `kill` commands that stop them.

Declared fits and inserts come from the version's record snapshot `interfaces.yaml`. `declared_pairs` lists each press fit with
its `fit`, `classification`, `interface` and YAML `source`. A fit that names feature endpoints
instead of a `pair` also carries `endpoints`: `hole` and `shaft`, each with `PN`, `feature` and an
optional `region`. `declared_heat_sets` lists each `limits.heat_set` entry with its sorted `pair`,
`host` PN, `insert` designation, `hole_d`, `interface` and `source`. A heat-set pair passes with or
without measured overlap, because a model can draw the insert at its bore size.
`declared_locating_fits` lists each `limits.locating_fit` the same way as a press fit, its
`classification` the ISO 286 fit type or `unclassified` (a dowel or vendor limit). cad-motion
`pair_policy` decides which pairs may overlap, for check and the sweep alike: an interference press
or locating fit, which must then measure an overlap (else `declared_fit_not_realized`), and a heat-set
insert; a clearance, transition or unclassified locating fit allows none.

A tolerant B-rep fails `step_readback` on purpose: when filleted edges carry tolerances of
0.01 to 0.1 mm, a STEP round trip rebuilds their face boundaries and moves the volume by about
1e-5, and again on each further trip. Fix the model (tangent-continuous outlines under fillets),
not the bound.

## Diff result (`diff`)

`a`, `b`, `added`, `removed`, `changed`, `error`, and `parts.<PN>`: `status`
(unchanged|changed|added|removed|error), `volume_a_mm3`, `volume_b_mm3`, `volume_delta_mm3`,
`volume_delta_rel`, `bbox_delta_mm` {min, max, size}, `solids_a`, `solids_b`, and the exact
Boolean volumes `shared_mm3`, `a_only_mm3`, `b_only_mm3` of the zero-pose part shapes. A part is
`unchanged` when both exclusive volumes are within 1e-6 relative of zero. When the kernel fails
on those Booleans the part is `error`, its exact volumes are null and `error` carries the
message; whether it changed is unknown.

`manufacturing` compares the manifest manufacturing entries apart from the definitions, so a
change to the manufacturing state alone shows. It has `changed`, the PNs that changed, were added
or were removed, and `parts.<PN>` with `status` (unchanged, changed, added or removed) and
`changed`, the aspects that differ: `state`, `tolerance`, `output_policy` (mesh deflections or
build volume), `geometry` (the state's kernel volume or solids) and `placement`. Each row also has
`relationship_a`/`_b` and `volume_a_mm3`/`_b`. When a version was built before manufacturing
outputs, it is `available: false`.
