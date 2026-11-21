# Engineering guide

This guide records the stack, layout, standards and checks of the Benchhand repository. It is derived from the repository files named in each section. A statement marked "unverified" has no source in the repository and needs an owner to confirm it.

## Stack and toolchain

| Item | Version or pin | Source |
|---|---|---|
| Host Python | 3.12 or later. Most PEP 723 headers say `>=3.12,<3.15`. | `README.md`, `scripts/test_script_floors.py` |
| FreeCAD job Python | 3.11, the Python that FreeCAD bundles. Code that `freecadcmd` runs stays 3.11 compatible. | `README.md`, `scripts/freecad_job_modules.txt` |
| uv | Runs the standalone skill scripts (through their PEP 723 headers), the tests and several tools. CI also runs `scripts/docs_only.py` and `scripts/ci_shards.py files` with plain `python3`. No uv version is pinned; developed with 0.13.x. | `.github/workflows/ci.yml` (lines 90 and 231) |
| FreeCAD | 1.1.4, checked by SHA-256 in CI. Verified on macOS: `/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd --version` prints `FreeCAD 1.1.4 Revision: 20260928`. | `.github/workflows/ci.yml`, `README.md` |
| CalculiX `ccx` | 2.23, verified on macOS against FreeCAD 1.1.4: `/Applications/FreeCAD.app/Contents/Resources/bin/ccx -v` prints `This is Version 2.23`. The test fixtures use the same value in a mocked solver response. At runtime `skills/cad-flow/scripts/fea_solvers.py` probes the installed `ccx` (`tool_version`) and accepts an override through `$CCX`. | `skills/cad-analysis/tests/test_fea_static.py` |
| gmsh | 4.15.0, verified on macOS against FreeCAD 1.1.4: `/Applications/FreeCAD.app/Contents/Resources/bin/gmsh --version` prints `4.15.0`. The test fixtures use the same value in a mocked solver response. At runtime `skills/cad-flow/scripts/fea_solvers.py` probes the installed `gmsh` and accepts an override through `$GMSH`. | `skills/cad-analysis/tests/test_fea_static.py` |
| CadQuery | `cadquery==2.8.0` with `cadquery-ocp==7.9.3.1.1` and `vtk==9.6.2` in the scripts that declare them | PEP 723 headers under `skills/*/scripts` |
| OCP without VTK | `cadquery-ocp-novtk==8.0.1.1.0` | PEP 723 headers under `skills/*/scripts` |
| build123d | `build123d==0.13.0`, `bd_warehouse==0.4.0` | `skills/cad-model/scripts/run_build123d.py` header |
| numpy | `numpy==2.5.5` | PEP 723 headers |
| trimesh, python-fcl | `trimesh==4.*`, `python-fcl==0.7.0.11` | PEP 723 headers |
| jsonschema | `>=4.23` in script headers. Test pin `4.26.0`. | `harness/test-requirements.txt` |
| PyYAML | `>=6.0` in script headers. Test pin `6.0.3`. | `harness/test-requirements.txt` |
| Playwright | `playwright==1.65.2`, with Chromium | `skills/cad-render` and `skills/cad-viewer` headers, `README.md` |
| Test-only pins | `defusedxml==0.7.1`, `hypothesis==6.170.15`, `click==8.5.1` | `harness/test-requirements.txt` |
| mypy, click for the type check | `mypy==2.4.1`, `click==8.5.1`, locked in `scripts/typecheck.py.lock` | `scripts/typecheck.py` header |
| ruff | `0.17.4` through `uvx` and in the ratchet header | `.github/workflows/ci.yml`, `scripts/complexity_ratchet.py` |
| reuse | `reuse[charset-normalizer]==6.3.0` | `README.md` |

Other pins in the script headers (`pillow`, `ocp_viewer`, `imageio-ffmpeg`, `lib3mf`, `lxml`, `networkx`) live with the script that uses them. Read the header of the script for the exact pin. Harness targets are Claude Code, Codex CLI, OpenCode and Kiro CLI (`README.md`).

There is no root `pyproject.toml` and no package install. Dependencies come from one of three places:

- Standalone skill scripts under `skills/*/scripts` declare them in a PEP 723 header, and `uv run --script` builds the environment.
- `scripts/complexity_ratchet.py`, `scripts/repro_check.py` and `scripts/typecheck.py` also carry a PEP 723 header. CI runs the first and the last with `uv run --no-project --script`.
- Tests and the other tooling have no header. These are `harness/generate.py`, `harness/install.py`, `scripts/ci_shards.py`, `scripts/docs_only.py`, `scripts/import_check.py`, `scripts/decision_fixture.py`, `scripts/modload.py` and `scripts/property_profile.py`. They run under the shared files `harness/test-requirements*.txt` (`--no-project --with-requirements`) or with plain Python. CI runs `docs_only.py` and `ci_shards.py files` with `python3` (`.github/workflows/ci.yml` lines 90 and 231). `harness/generate.py` imports PyYAML without a header, so its CI step passes `harness/test-requirements-min.txt`.

Release automation is release-please (`release-please-config.json`, `.github/workflows/release-please.yml`). Renovate keeps dependencies current (`.github/renovate.json5`).

## Layout and dependency direction

The directory layout, the components and the flows are in [architecture.md](architecture.md). This section keeps the import rules.

Dependency direction, as far as the repository shows it:

- Shipped skill code never imports `harness/` or `scripts/`. `skill_files()` in `harness/install.py` (lines 58 to 69) copies `skills/cad-*` without `tests/` or `__pycache__`, so the installed skills carry no path to either directory. `rg -n "harness|scripts/|modload" skills/*/scripts` finds only comments and printed or run commands.
- Tests may import them, through `scripts/modload.py`, the shared test helper (its docstring: "Shared test helpers"). Two cases:
  - `skills/cad-bom/tests/test_bom.py` (lines 25 and 26) puts `scripts/` on `sys.path` and imports `result_payload` from `scripts/modload.py`.
  - `skills/cad-model/tests/test_cad_model.py` (lines 3413 to 3416) loads `harness/install.py` with `importlib` on purpose.
- Every test file sits under a skill's `tests/` directory, which the installer skips. `ShippedSkillFilesTests` in `harness/test_install.py` fails when `skill_files()` returns a test file or a Python file that imports `harness` or `scripts`, or climbs to them with `parents[n] / "scripts"` on `sys.path`.
- Skills are separate script trees. `cad-record` supplies the record schemas, `emit.py` and the validators that other skills read. Skills reach a sibling through a path built from the skill folder (for example `freecad_lookup.py` in `cad-analysis` adds `cad-flow`'s scripts to `sys.path`). No import checker enforces which skill may import which. Unverified.
- Code that `freecadcmd` runs must import on Python 3.11 with only PyYAML and jsonschema. `scripts/freecad_job_modules.txt` lists those modules, and CI checks them (`scripts/import_check.py --manifest`).
- `scripts/test_docs_guard.py` and `scripts/docs_only.py` decide which paths no skill test reads, so a docs-only PR skips the skill shards.

## Standards and approved departures

Standards:

- Every source file carries an SPDX header (`MPL-2.0`, copyright "2026 Sjors Robroek"). Data and documents are covered by `REUSE.toml`, so Markdown files take no header. `reuse lint` enforces both.
- The CLA applies to every contribution (`CONTRIBUTING.md`).
- Host scripts need a `requires-python` floor of 3.12, except modules that FreeCAD runs on 3.11 (`scripts/test_script_floors.py`).
- Text files are opened with an explicit encoding (ruff `PLW1514`, CI).
- Complexity findings `C901`, `PLR0912` and `PLR0915` may not rise above `scripts/complexity-baseline.json`.
- Tests and scripts must not skip git hooks (`scripts/test_no_verify_guard.py`).
- Type checking is incremental: `mypy.ini` lists the checked modules, and some are strict.

Approved departures. The tracking IDs come from the bead ledger, which the repository does not hold, so the ledger facts below are unverified.

| Departure | Why | ID |
|---|---|---|
| A hand-written spatial index in `skills/cad-model/scripts/cadmodel/agreement.py` (`_Surface`, a uniform grid of cells with a box prefilter in `within`) for the GLB to STEP agreement check | The module imports numpy and abstains without it, so the index keeps to numpy. The profiling result, the proposed finer grid and the bead status are unverified (ledger only). | benchhand-1tq |
| Hand-written shell lexing in `scripts/test_no_verify_guard.py` (a `shlex` based word splitter near line 134) | The plan to replace it, the reasons and the bead status are unverified (ledger only). | benchhand-ci0 |
| `skills/cad-model/scripts/cadmodel/safe.py` and `occ_derived.py` stay Apache-2.0 inside the MPL-2.0 repository | They are adapted from AgentCAD 0.7.0 (Apache-2.0). `NOTICE` and `skills/cad-model/NOTICE` record the source and changes. `LICENSES/Apache-2.0.txt` holds the text. | benchhand-76q |
| FEA thresholds in `skills/cad-analysis/scripts/fea_near_support.py`: `TOLERANCE = 0.05` (5% section-resultant residual) and `TORSION_FLOOR = 1e-3` (0.1% of F x L) | Recorded as engineering judgment (decision U-2, 20 Nov). The user chose these values on 20 Nov 2026: `TOLERANCE` stays at 5% and `TORSION_FLOOR` stays at 0.1% of F x L. A section that misses a gate abstains, never a pass or a fail. Neither ASME VIII-2 Annex 5-A nor EN 13445-3 gives a number. | benchhand-2dq |

## Feature flags

Environment flags follow `CAD_REQUIRE_FREECAD` (`skills/cad-flow/scripts/preflight.py`): only the string `1` turns one on.

| Flag | Default | Owner | Effect | Removal condition |
|---|---|---|---|---|
| `CAD_FEA_METALS_NEAR_SUPPORT` | off | package A (FEA) | A metal profile that fails the elongation rule is shown ductile by its form (wrought, sheet or rod). Only the shipped profiles `al_6061_t6_hydro`, `al_6082_t6_hydro`, `steel_dc01_salzgitter` and `stainless_304_outokumpu_core` count as metals, because the profile schema has no metal field. Isotropy is still required. Each strength verdict the flag decides carries `form stand-in, flag on` in `material.ductility.reason` and in the row's uncertainty. A material that passes the elongation rule is not affected. | Delete the flag by 2026-12-31 or when benchhand-y87 adds sourced elongation data to the metal profiles, whichever is first. |
| `CAD_CALIBRATION` | off | Package E | Printer calibration. Off is the release behaviour: a recorded FDM calibration is ignored (an info finding says so), the generic guideline values govern, and a guideline pass is a pass in every service class. A check with no guideline value stays UNKNOWN and names the missing guideline. A stackup contributor may cite a guideline-backed limit, and a calibrated sliding clearance is not used. No remedy asks the user to calibrate; it says to print a test coupon on the target printer. On (`1`) is the behaviour before the flag. The value is read once per job (cad-record `guidelines.calibration_enabled`), and an enabled flag is sealed into the form_check and stackup recipes and the check packets. | Remove when calibration is released (dp2 and the non-FDM equivalents). |

## Build, test and lint commands

All commands run from the repository root. They are copied from `README.md` and `.github/workflows/ci.yml`.

Light batch (no FreeCAD needed):

```sh
uv run --no-project --with-requirements harness/test-requirements-min.txt python -B -m unittest discover -s harness -p 'test_*.py'
uv run --no-project --python 3.12 --with-requirements harness/test-requirements-min.txt python -B -m unittest discover -s scripts -p 'test_*.py'
uvx ruff@0.17.4 check --select PLW1514 --preview skills/*/scripts
uv run --no-project --script scripts/complexity_ratchet.py
uv run --no-project --python 3.12 --with-requirements harness/test-requirements.txt python -B scripts/import_check.py
uv run --no-project --python 3.11 --with-requirements harness/test-requirements-freecad.txt python -B scripts/import_check.py --manifest
uv run --no-project --with-requirements harness/test-requirements-min.txt python -B harness/generate.py --check
uv run --no-project --locked --script scripts/typecheck.py
uvx --from 'reuse[charset-normalizer]==6.3.0' reuse lint
```

`harness/generate.py --check` compares the committed profiles with generator output. `scripts/typecheck.py` runs mypy over the modules in `mypy.ini`. The complexity ratchet reads `scripts/complexity-baseline.json`.

Skill tests. Run each test file on its own with the test pins, from the directory that holds it. The "Tests" section of `README.md` loops over both locations:

```sh
for test in skills/*/test_*.py skills/*/tests/test_*.py; do
  [ -f "$test" ] || continue
  uv run --no-project --with-requirements harness/test-requirements.txt \
    python -B -m unittest discover -s "$(dirname "$test")" -p "$(basename "$test")"
done
```

CI lists the same two globs in `scripts/ci_shards.py` (`git ls-files "skills/*/test_*.py" "skills/*/tests/test_*.py"`) and runs one file at a time with `python -B scripts/ci_shards.py run <shard> <file>`.

CI splits the files into the shards `motion`, `motion-b`, `cad-model`, `freecad-gated` and `rest` (`scripts/ci_shards.py`, the `SHARDS` text in `ci.yml`). The files in the `freecad-gated` shard need FreeCAD 1.1.4 on `PATH` or `FREECADCMD`. Without it those tests skip, and `CAD_REQUIRE_FREECAD=1` turns the skip into a failure, as CI does. The `freecad-gated` shard also runs the mutation corpus rate check: `uv run --script skills/cad-design/tests/mfg_corpus/rates.py --check-baseline`. A pull request that changes only paths no skill test reads skips the shards (`scripts/docs_only.py`).

Agreement timing (not a CI gate): `uv run --script scripts/agreement_timing.py --runs 5` times `compare` on a seeded, generated mesh pair (160000 and 262144 triangles) with no FreeCAD; `skills/cad-model/tests/test_agreement_reference.py` pins the pair.

Git hooks are not defined in the repository (no hook files found). Never pass `--no-verify`; the guard test fails on it.

## Unverified items

- The profiling result, the proposed finer grid and the bead status behind benchhand-1tq.
- The plan, the reasons and the bead status behind benchhand-ci0.
