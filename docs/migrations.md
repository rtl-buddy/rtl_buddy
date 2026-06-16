---
description: How to upgrade an rtl_buddy project across each major version bump — v2→v3 artefact layout, v3→v4 PDK schema, v4→v5 ExecutionContext anchoring, and v5→v6 mutation budget rename. Also covers the one-time submodule-to-uv distribution move.
---

# Migrations

Breaking changes only land on major version bumps. Each section below covers one bump: what changed and what you must update. Crossing several majors at once (e.g. v3 → v6)? The changes are independent — work through every section between your old and new version, in order.

The final [Submodule to uv](#submodule-to-uv) section is a one-time distribution change, independent of which version you run.

## v2 to v3

Per-test outputs moved from `logs/` to `artefacts/` inside the suite directory.

| v2 | v3 |
|----|-----|
| `logs/{test}.log` | `artefacts/{test}/test.log` |
| `logs/{test}.err` | `artefacts/{test}/test.err` |
| `logs/{test}.randseed` | `artefacts/{test}/test.randseed` |
| `logs/{test}.coverage.dat` | `artefacts/{test}/coverage.dat` |
| `logs/{test}.compile.log` | `artefacts/{test}/compile.log` |

`randtest` iterations write to numbered subdirectories (`artefacts/{test}/run-0001/test.log`, …), while shared compile outputs (`compile.log`, `run.f`) stay at `artefacts/{test}/`. The suite-root `test.log` / `test.err` / `test.randseed` symlinks still point at the latest run.

**What to update:** swap `logs/` for `artefacts/` in `.gitignore`; repoint CI and coverage scripts at `artefacts/{test}/coverage.dat` (single run) or `artefacts/{test}/run-*/coverage.dat` (randtest). Hooks that relied on suite-relative paths resolving from the simulator's working directory must now build paths from the preproc hook's `suite_dir`. (v5 changed hook working directories again — see [Hook scripts run at the invocation directory](#hook-scripts-run-at-the-invocation-directory).)

## v3 to v4

The synthesis/PDK config schema was refactored to also drive place-and-route. The flat `cfg-synth-libs` block split into a reusable `cfg-pdks` block (all PDK-bound assets) plus thin platform selectors. In `root_config.yaml`:

```yaml
# v3
cfg-synth-libs:
  - name: nangate45_typ
    path: pdk/.../typical.lib
    lef-paths: [...]

# v4
cfg-pdks:
  - name: nangate45
    corners: { typ: pdk/.../typical.lib }
    tech-lef: pdk/.../tech.lef
    macro-lef: pdk/.../cells.lef
cfg-synth-platforms:
  - { name: nangate45_typ, pdk: nangate45, corner: typ }
```

In `synth.yaml`, the `libraries: [name]` list became a single `platform: name` string naming a `cfg-synth-platforms` entry.

**What to update:** rewrite `cfg-synth-libs` as `cfg-pdks` + `cfg-synth-platforms` (add a `cfg-pnr-platforms` entry only if you use `rb pnr`); change `synth.yaml` `libraries:` to `platform:`. Code calling `RootConfig` directly: `get_synth_lib_cfg` is now `get_synth_platform_cfg` (plus new `get_pdk_cfg` / `get_pnr_platform_cfg`). See the [synthesis](concepts/synthesis.md) and [place-and-route](concepts/pnr.md) concept docs for the full schema.

## v4 to v5

Every config-driven command now anchors its outputs on the directory containing its primary config — the **command root** — via [`ExecutionContext`](concepts/execution-context.md), instead of wherever you happened to run `rb`.

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own log |
| `root_config.yaml` discovery | from invocation cwd | from command root |
| `hier` / `axi-profile` default outputs | invocation cwd | resolved command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

Explicit output paths you pass on the command line (`hier -o diagram.svg`, `axi-profile … -o report.html`, `filelist <model> out.f`) are unchanged — they still resolve against your shell's cwd. Only command-managed artefacts and default output locations moved.

### Hook scripts run at the invocation directory

The change most likely to break a project. `regression` no longer `chdir`s into each suite, so `sweep` / `preproc` hooks now run from your shell's cwd like every other command (single-suite `test` / `randtest` were already this way). Build paths from the `suite_dir` and `artifact_dir` namespace variables, never `os.getcwd()`:

```python
out  = os.path.join(artifact_dir, "gen.sv")          # correct
stim = os.path.join(suite_dir, "vectors", "in.txt")  # correct
```

For a third-party generator that only writes relative to `os.getcwd()`, wrap the call in `os.chdir(suite_dir)` and restore the previous directory afterwards. See [Quirks & Known Issues](known-issues.md) for the failure signature.

**What to update:** repoint CI that looked for `rtl_buddy.log` in the invocation directory to the command root; replace any `os.getcwd()` in hooks with `suite_dir` / `artifact_dir`.

## v5 to v6

`mut.yaml` `budget.per_module_cap` was renamed to `budget.per_file_cap`. The cap counts mutants per scoped *file* (files and modules are not 1:1), so the name now matches the behavior. Unknown keys are dropped silently, so a stale `per_module_cap` is ignored and silently reverts to "no per-file cap" rather than erroring.

**What to update:** rename `budget.per_module_cap` to `budget.per_file_cap` in every `mut.yaml`.

v6 also adds optional hierarchical [`scope`](concepts/mut.md#scope-hierarchical-designs) for multi-file mutation campaigns. This is additive — an empty or absent `scope` keeps v5 single-file behavior byte-for-byte. When `scope` is set, `rtl-buddy-view` must be on `PATH`, because scope resolution shells out to `rb hier --format json`.

## Submodule to uv

Independent of version bumps: this is the one-time move from the legacy `rtl_buddy` git submodule (under `tools/rtl_buddy`, installed via `pip install -e`) to a `uv`-managed PyPI dependency.

If your RTL repo is not already a Python project, initialize one, then add `rtl_buddy`:

```bash
uv init --bare        # only if there is no pyproject.toml yet
uv add rtl_buddy
uv run rb --version
```

Then:

1. Remove the `tools/rtl_buddy` submodule.
2. Fold any `requirements.txt` entries into `pyproject.toml` under `dependencies`, then delete `requirements.txt`.
3. Update local scripts and CI from `tools/rtl_buddy/…` / `python -m rtl_buddy` to `uv run rb …`.
4. Commit `pyproject.toml` and `uv.lock` so other users and CI resolve the same environment.

Pin a specific release with `uv add "rtl_buddy==2.3.0"` when a project must hold back.
