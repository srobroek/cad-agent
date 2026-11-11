# Release procedure

Before release, build record-derived guides for the frozen version and proposed revision. Run from the project root; use the UTC date that `release.py` will seal:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/build_guide.py hw --version vN --rev A --date YYYY-MM-DD --out hw/docs
```

If this command refuses because a guide was not generated or was edited since generation, review its contents and rerun with `--replace` only when replacing those edits is intended. Then validate and issue the release after explicit RELEASE-gate approval recorded in `hw/decisions/DR-###.md`:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/validate.py hw
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/release.py hw --rev A --version vN --decision DR-###
```

`validate.py`: exit 0 has no error findings, exit 1 has blocking findings, exit 2 is invalid input or infrastructure failure. `release.py`: exit 0 sealed the release, exit 1 refused it with findings and wrote nothing outside `.cad-work/`, exit 2 is invalid input or infrastructure failure. A used `hw/releases/<rev>/` is never overwritten.

Record v2 release writes **manifest schema v3**. It copies the frozen assembly definition and record snapshot, specification artifacts referenced by purchased parts, manufacturing outputs for each custom-made PN, every declared part or assembly drawing, every analysis bundle for a case under `hw/analysis/cases/<case>/`, every bundle cited by `verification.csv`, the current BOM and verification matrix, docs, and the acceptance inventory under `acceptance/`. Check, drawing, motion, render and capture bundle route kinds and manufacturing output files are defined by the generated canonical [layout table](layout.md); a route with no output contract blocks release. All supported process kinds' files go under `manufacturing/<PN>/`, not into separate per-process package folders.

`flow.frozen` must name vN, the version manifest's `manifest_sha256`, the output of `evidence.py digest --hw hw --version vN` in `evidence_sha256`, and the approving DESIGN-freeze decision. The release approval is a separate DR passed as `--decision`. Required evidence and selection must pass the release gate; unknown never passes. See [evidence.md](evidence.md) for the acceptance inventory and [manufacturing-intent.md](manufacturing-intent.md) for record policy.

To plan pruning after release, run the release command above with `--prune`; it prints a digest-bound plan without removing anything. Review `keep` reasons and the `remove` list, then apply only with the exact digest printed by the plan:

```sh
uv run --script "$CAD_SKILLS/cad-flow/scripts/run.py" cad-record/scripts/release.py hw --rev A --version vN --decision DR-### --prune --yes <64-character-digest>
```

Only versions older than the released version are eligible. The plan retains released/latest/frozen/change-baseline versions, versions named by decisions and versions referenced by surviving manifests. A stale plan digest is refused.