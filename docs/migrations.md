---
description: Required project changes for rtl_buddy major-version upgrades and the legacy submodule-to-uv migration.
---

# Migrations

Apply each section between your installed and target major versions, in order. The [submodule-to-uv migration](#submodule-to-uv) does not depend on the package version.

## v2 to v3

Per-test outputs moved from `logs/` to `artefacts/` inside the suite directory.

| v2 | v3 |
|----|-----|
| `logs/{test}.log` | `artefacts/{test}/test.log` |
| `logs/{test}.err` | `artefacts/{test}/test.err` |
| `logs/{test}.randseed` | `artefacts/{test}/test.randseed` |
| `logs/{test}.coverage.dat` | `artefacts/{test}/coverage.dat` |
| `logs/{test}.compile.log` | `artefacts/{test}/compile.log` |

`randtest` iterations write to numbered subdirectories such as `artefacts/{test}/run-0001/test.log`. Shared compile outputs (`compile.log`, `run.f`) stay in `artefacts/{test}/`. The suite-root `test.log`, `test.err`, and `test.randseed` are symlinks to the latest run.

Change `logs/` to `artefacts/` in `.gitignore`, CI, and coverage scripts. Coverage is `artefacts/{test}/coverage.dat` for one run and `artefacts/{test}/run-*/coverage.dat` for randomized runs. Build hook paths from `suite_dir`; v5 changed the hook working directory again.

## v3 to v4

`cfg-synth-libs` is replaced by reusable PDK data plus flow-specific platform selectors:

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

- In `synth.yaml`, replace `libraries: [name]` with `platform: name`.
- Add `cfg-pnr-platforms` only if you run `rb pnr`.
- API users: replace `get_synth_lib_cfg` with `get_synth_platform_cfg`, and use `get_pdk_cfg` and `get_pnr_platform_cfg` for the new layers.

See [Synthesis](concepts/synthesis.md) and [Place-and-Route](concepts/pnr.md).

## v4 to v5

Managed output is anchored on the primary config's directory, the [`command_root`](concepts/execution-context.md).

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own log |
| `root_config.yaml` discovery | from invocation cwd | from command root |
| `hier` / `axi-profile` default outputs | invocation cwd | command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

Explicit CLI paths still resolve from the invocation directory. Only managed artifacts and default output locations moved. Point CI at the command root for `rtl_buddy.log`.

### Hook scripts run at the invocation directory

`sweep` and `preproc` hooks run from the invocation directory. Build paths from the injected `suite_dir` and `artifact_dir`, never `os.getcwd()`:

```python
out  = os.path.join(artifact_dir, "gen.sv")
stim = os.path.join(suite_dir, "vectors", "in.txt")
```

If a third-party generator writes only relative to the working directory, wrap the call in a temporary `os.chdir(suite_dir)`.

## v5 to v6

Rename `budget.per_module_cap` to `budget.per_file_cap` in every `mut.yaml`. The old key is ignored, which removes the cap; it does not fail validation.

## v6 to v7

### Unknown P&R keys are errors

An unknown key in a P&R run, a P&R platform or a PDK is a config error. Before, it was dropped without a word, so a misspelt key changed the flow silently: a blockage written with `lifetme: global-placement` loaded as a permanent one.

The check covers:

- Every mapping of a `pnr.yaml` run: the run, `floorplan`, `floorplan.blockages[]`, `floorplan.pins[]`, `floorplan.macros[]`, `pdn`, `pdn.ring`, `pdn.stripes[]`, `abstract`, `placement` and `blocks[]`.
- Every `cfg-pnr-platforms` entry, with its `routing-layers` and `placement` blocks.
- Every `cfg-pdks` entry, with its `pin-layers` and `placement` blocks.

Each unknown key is reported with the file, its location, the nearest valid key and every valid key:

```text
pnr.yaml: pnr run 'top': floorplan.blockages[2]: unknown key 'lifetme', did you mean 'lifetime'? (valid keys: rect, type, max-density, lifetime)
```

Remove the key or correct its spelling. A `pnr.yaml` error stops `rb pnr`. A `cfg-pdks` or `cfg-pnr-platforms` error stops every command that loads `root_config.yaml`. See [YAML formats](reference/yaml.md#pnryaml).

### The routed DEF must keep located macros and exact pins

A run with a located macro or an exact pin compares them with the routed DEF and fails with `fail_stage: routed_check` when one moved; `xfail:` does not excuse it. In v6 nothing compared them, so a run whose `pin-constraints` file or platform hook moved a located macro or an exact pin passed. Remove the conflicting placement or the floorplan entry. See [Check the routed floorplan](concepts/pnr.md#check-the-routed-floorplan).

### Exact pins leave side and group rules

A port with an exact location in `floorplan.pins` is left out of every side or group rule that also matches it. In v6 it stayed in the rule's list: the run failed with `PPL-0107` when the pin lay outside the rule's edge, and otherwise the rule's other pins were placed around it. Such a config now places the rule's other pins differently.

### Stage checkpoints

The `global_route` checkpoint holds the design after the `after-global-route` hooks and the global-route hold repair (`global-route-hold-repair`), before the `before-detailed-route` hooks: the design detailed routing starts from. In v6 it held the design as `global_route` returned, before the hold repair. A `detailed_route` checkpoint (`05_detailed_route.odb`, `.def` and `.sdc`) follows detailed routing, so `checkpoints: true` writes five stages and `rb pnr-export --checkpoint detailed_route` exports the route before fill. Checkpoint SDC files are written with `write_sdc -no_timestamp -digits 9`. No `pnr.yaml` change is needed; a script that reads `04_global_route` as the design before hold repair gets the repaired design. See [Keep stage checkpoints](concepts/pnr.md#keep-stage-checkpoints).

A one-session run that reaches its end without writing a checkpoint it keeps fails with `fail_stage: checkpoint`. In v6 it passed without that checkpoint, which happened when a checkpoint's write failed or when a hook wrapped the stage's boundary command (`global_placement`, `clock_tree_synthesis`, `global_route`) with `rename`. Fix the write's cause or remove the wrapper, or set `stages: true`.

### OpenROAD runs in the artefact directory

Every OpenROAD session runs in the run's artefact directory, `artefacts/<run>/`. In v6 it ran in the directory `rb` was started from, so a relative path in a run hook, `platform-tcl`, `layer-rc-tcl`, `tapcell-tcl`, `pin-constraints` file or `pdn-config` (`source`, `open`, `read_*`, `write_*`) resolved there. Such a path now resolves from `artefacts/<run>/`. Build paths from the script's own location instead, `[file join [file dirname [info script]] <name>]`, or from the variables the flow sets (`OUT_DIR`). See [Run Tcl hooks](concepts/pnr.md#run-tcl-hooks).

## Submodule to uv

Replace the `tools/rtl_buddy` submodule and editable pip install with a `uv` dependency:

```bash
uv init --bare        # only if there is no pyproject.toml yet
uv add rtl_buddy
uv run rb --version
```

1. Remove the `tools/rtl_buddy` submodule.
2. Move any `requirements.txt` entries into `dependencies` in `pyproject.toml`, then delete `requirements.txt`.
3. Change local scripts and CI from `tools/rtl_buddy/…` or `python -m rtl_buddy` to `uv run rb …`.
4. Commit `pyproject.toml` and `uv.lock` so other users and CI resolve the same environment.

For an exact pin, use `uv add "rtl_buddy==<version>"`.
