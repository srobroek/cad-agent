# Changelog

## 0.1.0 (2026-10-31)

First standalone release of the CAD skills, forked from [srobroek/omp-plugins](https://github.com/srobroek/omp-plugins) and adapted for Claude Code, OpenAI Codex CLI, OpenCode, and Kiro CLI without OMP or Beads dependencies. It includes the omp-plugins `cad` changes through b476fb9.

- Provide a stage-based workflow from concept and design records through checked STEP AP242 models, drawings, EBOM, verification, and release packages, with user-approved gates recorded as decisions.
- Add skills for CAD modeling, design fidelity, motion, rendering, interactive viewing, fits, analysis, BOM, drawings, record validation, and engineering critique.
- Include FreeCAD 1.1.4, CadQuery, build123d, OCP, and Chromium preflight and setup; generate harness-native agent profiles and install files for supported harnesses.
- Preserve immutable, manifest-bound versions and evidence bundles, manufacturable part outputs, and the lamp-arm reference project.
- Check each built version fail-fast, cheapest first, and close LAYOUT freeze, PARTS, DETAIL and ANALYSIS with two gates: a provisional decision the lead records once the fast checks pass, then the user's approval after the full check. A failed full check, a MAJOR L2 light-review finding or a change the user names voids the provisional decision and reopens that stage and every later stage built on that version. A gate decision's first line is `Status: APPROVED`, `Status: PROVISIONAL` or `Status: VOID`; `validate.py` and `release.py` refuse a PROVISIONAL or VOID decision as the freeze, change or release approval.
- Sweep with between-sample collision and clearance proofs, subsystem runs and merges, a shared pair cache in `hw/.cad/pairs-v2.sqlite`, changed-only static checks and smoke sweeps, and supervised run records under `hw/.cad/runs/` that refuse stray or concurrent runs.
- Catch missing torque paths and axial retainers, and see-through FDM parts, at L2: records at `parts` or later declare `torque` on each revolute and an `axial_retainer` on each press and locating fit.
- Build only changed parts and store each part's outputs once; keep generated results out of git unless `product.yaml` `storage.results_in_git` approves them, with `setup.py --storage` writing the git rules.
- Prune superseded versions after a release, and generate the OKH and DIN SPEC 3105-1 documents from the record.
- Check requirement window visibility in renders and reuse unchanged views; render custom view directions.
- Tolerate cosmetic fillet failures, smoke FreeCAD runs, diagnose CalculiX crashes and timeouts, and dimension hole centres from datum planes.
- Refuse to install the recheck hook over a `core.hooksPath` set in the system or global git config; verify with `stamp.py --verify` instead.
- Check each made part's minimum wall at F2 (FDM 1.2 mm unless `product.yaml` `manufacturing.min_wall_mm` overrides it), and apply `block_like` only to parts whose `form_class` is `solid`.
- Keep shared-environment launches working for scripts named `setup.py`, `pyproject.toml`, or `setup.cfg`; ignore generated output when delivering a scratch workspace; stop at gates when the lead cannot ask the user.

### Coming from the omp-plugins `cad` plugin

- The pair cache moved to `hw/.cad/pairs-v2.sqlite`, and sweep `timings_s` counters are renamed.
- Records at `parts` or later need `torque` on every revolute and an `axial_retainer` on each press and locating fit (see `cad-record/references/design-rules.md`).
- A too-thin or unmeasured wall is a form-check finding, and an invalid `min_wall_mm` override exits 2.
- Gate decisions carry a `Status:` first line instead of Beads gates.
