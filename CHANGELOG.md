# Changelog

## Unreleased

- Keep shared-environment launches working for scripts named `setup.py`, `pyproject.toml`, or `setup.cfg`, while preserving their PEP 723 dependencies and interpreter selection.
- Ignore generated version, render, and release output when delivering a built scratch workspace, and stop at gates when the lead cannot ask the user.
- Port the omp-plugins `cad` changes through dc89664:
  - Check each built version fail-fast, cheapest first, and close LAYOUT freeze, PARTS, DETAIL and ANALYSIS with two gates: a provisional decision the lead records once the fast checks pass, then the user's approval after the full check. A failed full check, a MAJOR L2 light-review finding or a change the user names voids the provisional decision and reopens that stage and every later stage built on that version. A gate decision's first line is `Status: APPROVED`, `Status: PROVISIONAL` or `Status: VOID`; `validate.py` and `release.py` refuse a PROVISIONAL or VOID decision as the freeze, change or release approval.
  - Sweep v2 with between-sample collision and clearance proofs, subsystem runs and merges, a shared pair cache, changed-only static checks and smoke sweeps, and supervised run records that refuse stray or concurrent runs. **Breaking:** the pair cache moves to `hw/.cad/pairs-v2.sqlite`, sweep run records live under `hw/.cad/runs/`, and `timings_s` counters are renamed.
  - Catch missing torque paths and axial retainers, and see-through FDM parts, at L2. **Breaking:** existing records at `parts` or later need an `axial_retainer` on each press and locating fit.
  - Build only changed parts and store each part's outputs once; keep generated results out of git unless `product.yaml` `storage.results_in_git` approves them, with `setup.py --storage` writing the git rules.
  - Prune superseded versions after a release, and generate the OKH and DIN SPEC 3105-1 documents from the record.
  - Check requirement window visibility in renders and reuse unchanged views; render custom view directions.
  - Tolerate cosmetic fillet failures, smoke FreeCAD runs, diagnose CalculiX crashes and timeouts, and dimension hole centres from datum planes.
  - Refuse to install the recheck hook over a `core.hooksPath` managed outside the repository; verify with `stamp.py --verify` instead.
  - Check each made part's minimum wall at F2 (FDM 1.2 mm unless `product.yaml` `manufacturing.min_wall_mm` overrides it), and apply `block_like` only to parts whose `form_class` is `solid`. **Breaking:** a too-thin or unmeasured wall is now a form-check finding, and an invalid `min_wall_mm` override exits 2.

## 0.1.0

First standalone release of the CAD skills, forked from [srobroek/omp-plugins](https://github.com/srobroek/omp-plugins) and adapted for Claude Code, OpenAI Codex CLI, OpenCode, and Kiro CLI without OMP or Beads dependencies.

- Provide a stage-based workflow from concept and design records through checked STEP AP242 models, drawings, EBOM, verification, and release packages, with user-approved gates recorded as decisions.
- Add skills for CAD modeling, design fidelity, motion, rendering, interactive viewing, fits, analysis, BOM, drawings, record validation, and engineering critique.
- Include FreeCAD 1.1.4, CadQuery, build123d, OCP, and Chromium preflight and setup; generate harness-native agent profiles and install files for supported harnesses.
- Preserve immutable, manifest-bound versions and evidence bundles, manufacturable part outputs, and the lamp-arm reference project.
