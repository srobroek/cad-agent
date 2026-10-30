# CAD Agent

Agent-assisted mechanical CAD takes a product from rapid concept through a verified STEP AP242 release. Deliverables include drawings, an engineering bill of materials (EBOM), verification evidence, and design documentation. Designs target FDM or CNC. The workflow does not produce slicer, CAM, or machine-tool output.

This standalone project supports Claude Code, OpenAI Codex CLI, OpenCode, and Kiro CLI through native skills and agent profiles.

## Requirements

- Python 3.11 or later.
- [uv](https://docs.astral.sh/uv/).
- FreeCAD 1.1.4 with `freecadcmd` and TechDraw templates. Install the official release from [FreeCAD 1.1.4](https://github.com/FreeCAD/FreeCAD/releases/tag/1.1.4).
  - macOS: use the arm64 or x86_64 DMG. Place FreeCAD.app in `/Applications`.
  - Linux x86_64 or aarch64: download the matching Python 3.11 AppImage and `-SHA256.txt`. Verify the checksum, extract the AppImage, and put `freecadcmd` on `PATH`.
  - See [`cad-drawings/SKILL.md`](skills/cad-drawings/SKILL.md#install-freecad) for commands. If FreeCAD is not on `PATH` or in the macOS app bundle, set `FREECADCMD`.
- Chromium. Preflight and setup install it through Playwright.
- Claude Code, Codex CLI, OpenCode, or Kiro CLI. The installer copies native skills and agent profiles to each harness's project paths. Claude Code also supports plugin marketplace installation.

## Install

Run the installer from the project where you want to use CAD. Set `CAD_AGENT` to the repository checkout path:

```sh
CAD_AGENT=/path/to/cad-agent
python3 "$CAD_AGENT/harness/install.py" --harness claude
```

Choose `claude`, `codex`, `opencode`, `kiro`, or `all`. Add `--user` for a user-level install:

```sh
python3 "$CAD_AGENT/harness/install.py" --harness all --user
```

Use `--project DIR` to select a project directory. `--dry-run` previews changes, and `--uninstall` removes installed CAD files. `python3 "$CAD_AGENT/harness/install.py" --help` lists the options.

Alternatively, install Claude Code as a plugin. The private GitHub marketplace requires GitHub access. Authenticate with `gh auth` first, or use a local checkout:

```text
/plugin marketplace add srobroek/cad-agent
/plugin install cad-agent@cad-agent
```

For a local checkout, add `/plugin marketplace add /path/to/cad-agent`. Then install with `/plugin install cad-agent@cad-agent`. The plugin exposes `/cad-agent:cad-flow`. The installer route exposes `/cad-flow`.

## Start and prepare a project

In Claude Code, OpenCode, or Kiro, start with `/cad-flow`. In Codex CLI, invoke the skill with `$cad-flow` or select it from `/skills`. Describe the product or ask to continue the existing design record. For example: `Take this desk lamp arm from concept through a verified CAD release.`

At the project root, set `$CAD_SKILLS` to the directory containing installed `cad-*` skill folders. For a project install, this is `.claude/skills`, `.agents/skills`, `.opencode/skills`, or `.kiro/skills` for the selected harness. If using a repository checkout directly, set it to `"$CAD_AGENT/skills"`.

Run preflight to see missing prerequisites:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/preflight.py"
```

Setup plans environment creation and user-level actions. Add `--yes` to run those actions. Setup warms the CAD kernels, Chromium, and the pinned three.js cache:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/setup.py"
uv run --script "$CAD_SKILLS/cad-flow/scripts/setup.py" --yes
```

Setup checks FreeCAD 1.1.4 first. It stops if FreeCAD is absent or the version is wrong. Run preflight again after setup. Use `uv run --script "$CAD_SKILLS/cad-flow/scripts/preflight.py" --warm` to install or check Chromium and warm the render cache.

### Skill script launcher

Use the shared launcher for scripts that need the shared CAD environment:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" <skill>/scripts/<script>.py [args...]
```

The target path is relative to `$CAD_SKILLS`, such as `cad-model/scripts/cad_model.py`. Resolve each skill's scripts, assets, and documents relative to that skill folder. Run lightweight standalone scripts directly with `uv run --script`.

## Workflow

The `cad-flow` skill guides these stages:

1. GROUND, INTENT, and SPECIFY
2. L0 CONCEPT and the VARIANT gate
3. L1 LAYOUT and LAYOUT freeze
4. L2 PARTS, L3 DETAIL, and BOM
5. L4 ANALYSIS, CRITIQUE, RECONCILE, and DESIGN freeze
6. RELEASE gate and RELEASE

The skill maintains the `hw/` design record. Each L1-L4 batch builds an immutable version, runs checks, and renders the result. The lead dispatches independent stage units to harness subagents in a batch, then integrates their results.

The lead records user-approved gates as dated decisions in `hw/decisions/`. Gates require explicit user approval. Agent verdicts and successful checks are not approval. The workflow has no ledger.

See the [stage-by-stage walkthrough](docs/walkthrough.md) for prompts, review material, approvals, and project files. The [canonical `hw/` record layout](skills/cad-record/references/layout.md) lists design files and generated evidence.

Agents receive instructions to keep worker scratch and temporary files under `.cad-work/` beside `hw/`. Harness policy is not a cross-harness operating-system security boundary. Enforcement varies across harnesses and file-writing surfaces. Use an OS sandbox or container for a hard write boundary.

## Tests

Run each skill test file separately with the pinned test dependencies:

```sh
for test in skills/*/test_*.py skills/*/tests/test_*.py; do
  [ -f "$test" ] || continue
  uv run --no-project --with pyyaml==6.0.3 --with jsonschema==4.26.0 --with defusedxml==0.7.1 \
    python -B -m unittest discover -s "$(dirname "$test")" -p "$(basename "$test")"
done
python3 -m unittest discover -s harness -p 'test_*.py'
python3 harness/generate.py --check
```

Tests that use FreeCAD skip when FreeCAD 1.1.4 is unavailable. Check changed Markdown with `uvx slopvac README.md CHANGELOG.md NOTICE docs/walkthrough.md`.

## Skills and agents

| Skill | Responsibility |
|---|---|
| `cad-flow` | Stage sequence, gates, and project workflow |
| `cad-record` | Structured design record, validation, and release |
| `cad-concept` | Rapid concept variants and previews |
| `cad-model` | CadQuery/build123d model builds and checks |
| `cad-design` | F0-F3 fidelity and form checks |
| `cad-motion` | Assembly poses, motion sweeps, and motion renders |
| `cad-render` | Versioned PBR renders |
| `cad-viewer` | Interactive OCP inspection and Studio look-dev |
| `cad-analysis` | Static finite-element analysis |
| `cad-bom` | EBOM roll-up |
| `cad-drawings` | Part and assembly engineering drawings |
| `cad-fits` | Fits and tolerance stack-ups |
| `cad-recheck` | Changed-part and dependent rechecks |
| `cad-review-dfm` | Evidence-based FDM/CNC manufacturability critique |
| `cad-review-mech` | Evidence-based mechanical design critique |

Harness-native agent profiles support concept design, part design, sourcing research, analysis, and mechanical and DFM review. The harness installer and generator use Python's standard library.
