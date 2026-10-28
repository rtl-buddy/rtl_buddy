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

### Machine output and reservation keys

Machine-mode stderr summaries omit `PASS`, `SKIP`, and `XFAIL` rows but retain the full tally. Read stdout results or `summary.rows` for every row. Human mode still shows all rows unless `--print-failures-only` is set. Per-phase status lines require `--verbose` or `--debug`; dispatch progress, job announcements, hooks, warnings, and errors remain visible.

Update consumers of `payload.reservation_advice`:

- Stop reading `event`; findings omit it.
- Treat missing `reg_level`, `allocated`, `suggested_total`, and `aggregate_delta` as null. Regression levels remain per finding, absent on build-job advice.
- Keep applying the explicit `edit_hint`; names, units, ordering, and provenance are unchanged. See [Apply reservation advice](concepts/dispatch.md#apply-reservation-advice) for the payload contract.

`rightsize.advice` moves from INFO to DEBUG. Use `--debug` for individual events or read the complete machine payload. A nonempty list logs INFO `rightsize.summary` with `total` and direction `counts` (`raise`, `reduce`).

Unknown reservation and dispatch keys now stop loading with exit code 2. Correct or remove every reported key; the [YAML reference](reference/yaml.md#parallel-dispatch) lists checked blocks. Mode names remain free-form; mode contents remain strict.

Log consumers must replace WARNING `config.unknown_key` with ERROR `config.unknown_key_rejected`. Diagnostics include the file, block, key, nearest spelling, and valid keys. Nested locations use `cfg-dispatch: compile.verilate` instead of `cfg-dispatch.compile.verilate`.

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

A run with a located macro or an exact pin compares them with the routed DEF and fails with `fail_stage: routed_check` when one moved; `xfail:` does not excuse it. In v6 nothing compared them, so a run whose `pin-constraints` file or platform hook moved a located macro or an exact pin passed. Remove the conflicting placement or the floorplan entry. See [Place IO pins](concepts/pnr.md#place-io-pins).

### A flow that ends early is a failure

A run whose OpenROAD session exits 0 before its flow's end marker fails with `fail_stage: incomplete`; `xfail:` does not excuse it. In v6 a run was judged on the exit code and the log's `[ERROR` lines, so a run whose `platform-tcl`, `layer-rc-tcl`, `tracks-tcl` or `tapcell-tcl` file, `pin-constraints` file or `pdn-config` called `exit` passed with missing metrics and outputs. Remove the `exit`, or the `return -level 2`, from the file that the step named in the message sources. See [Interpret results](concepts/pnr.md#interpret-results).

### Supply pins of added cells are connected

The flow applies the PDK's global connections again (`global_connect`) at the end of every stage, before detailed routing, after fill and before the final outputs. In v6 only the cells present when `pdngen` ran had their supply pins connected; the buffers, tie cells and fill added later kept them unconnected in the routed ODB, DEF and every checkpoint. The generated `pnr.tcl` of every run gains the call, and the routed DEF of a run with a `pdn-config` lists the added cells in its power and ground nets. Detailed routing now starts from a design with connected supply pins, so a run's route and slack can differ slightly. No `pnr.yaml` change is needed. See [Plan the power grid](concepts/pnr.md#plan-the-power-grid).

### Exact pins leave side and group rules

A port with an exact location in `floorplan.pins` is left out of every side or group rule that also matches it. In v6 it stayed in the rule's list: the run failed with `PPL-0107` when the pin lay outside the rule's edge, and otherwise the rule's other pins were placed around it. Such a config now places the rule's other pins differently.

### The routed netlist declares no supply ports

`<top>.routed.v` is the logical netlist in every run: it declares no supply ports and connects no cell supply pin. In v6 it declared the supply nets the power grid created, such as `VDD` and `VSS`, as `inout` ports that nothing inside the module connected to. A hardened block's `<top>.lib` lists those supply pins with `direction : input`, where v6 wrote `inout`. A script or testbench that connects the supply ports of a routed netlist by name must drop those connections. See [Inspect artefacts](concepts/pnr.md#inspect-artefacts).

### Stage checkpoints

The `global_route` checkpoint holds the design after the `after-global-route` hooks and the global-route repair (`global-route-hold-repair` or `global-route-repair`), before the `before-detailed-route` hooks: the design detailed routing starts from. In v6 it held the design as `global_route` returned, before the hold repair. A `detailed_route` checkpoint (`05_detailed_route.odb`, `.def` and `.sdc`) follows detailed routing, so `checkpoints: true` writes five stages and `rb pnr-export --checkpoint detailed_route` exports the route before fill. Checkpoint SDC files are written with `write_sdc -no_timestamp -digits 9`. No `pnr.yaml` change is needed; a script that reads `04_global_route` as the design before hold repair gets the repaired design. See [Keep stage checkpoints](concepts/pnr.md#keep-stage-checkpoints).

A one-session run that reaches its end without writing a checkpoint it keeps fails with `fail_stage: checkpoint`. In v6 it passed without that checkpoint, which happened when a checkpoint's write failed or when a hook wrapped the stage's boundary command (`global_placement`, `clock_tree_synthesis`, `global_route`) with `rename`. Fix the write's cause or remove the wrapper, or set `stages: true`.

### OpenROAD runs in the artefact directory

Every OpenROAD session runs in the run's artefact directory, `artefacts/<run>/`. In v6 it ran in the directory `rb` was started from, so a relative path in a run hook, `platform-tcl`, `layer-rc-tcl`, `tapcell-tcl`, `pin-constraints` file or `pdn-config` (`source`, `open`, `read_*`, `write_*`) resolved there. Such a path now resolves from `artefacts/<run>/`. Build paths from the script's own location instead, `[file join [file dirname [info script]] <name>]`, or from the variables the flow sets (`OUT_DIR`). See [Run Tcl hooks](concepts/pnr.md#run-tcl-hooks).

### A multi-corner platform times each corner in its own SDC mode

On a platform with `corners:`, `rb pnr` times each corner in an OpenSTA SDC mode and scene of its own, and the final reports read the routed SPEF once per corner. In v6 the corners shared one mode and one SPEF read, so a generated clock's latency came from one corner and the later corners reused the first corner's parasitics. Multi-corner slack and repairs change; single-corner runs do not.

- An SDC command in a hook constrains the primary corner only. Put it in the SDC, or run it after `set_mode <corner>` for each corner, then `set_mode <primary>`.
- `max_fanout_violation_count` counts a pin once per corner it violates at.
- Each corner also gets `<top>.<corner>.routed.sdc` and, in checkpoints, `<NN>_<stage>.<corner>.sdc`. `<top>.routed.sdc` holds the primary corner's constraints, and a checkpoint's `.sdc` every mode.
- A `--resume` of a multi-corner run started by v6 is refused; rerun it from the start.
- OpenROAD 26Q2 keeps one shared mode and warns `pnr.corner_modes_unsupported`. An OpenROAD that does not answer rb's mode check fails setup.

See [Sign off at several corners](concepts/pnr.md#sign-off-at-several-corners).

### Synthesis repair buffers the unplaced netlist

A synthesis effort with `openroad.repair: true` runs `repair_design -pre_placement` when no instance is placed after its `pre-sta-tcl`. In v6 it ran plain `repair_design`, which on an unplaced netlist only resizes, so high-fanout nets stayed unbuffered. Such runs usually report more area and less negative WNS and TNS, and their options digest changes. No config change is needed. See [Select an effort](concepts/synthesis.md#select-an-effort).

## v7 to v8

### P&R defaults follow OpenROAD and ORFS

Each default `rb pnr` applies is now the OpenROAD command's own default or the setting ORFS uses. Two keys that neither has a default for are required, and the rest of the defaults below changed, so a run that relies on them places, routes and times differently. To keep the v7 flow, set the v7 value:

| Key | v7 default | v8 default | v7 behaviour |
| --- | --- | --- | --- |
| `cfg-pdks` `pin-layers` | `metal3` / `metal2` | None; `rb pnr` needs both | `pin-layers: {horizontal: metal3, vertical: metal2}` |
| `floorplan.utilization` | `0.55` | None; required unless `die-area` and `core-area` are set | `floorplan: {utilization: 0.55}` |
| `floorplan.core-margin` | `2.0` | `1.0`, ORFS's `CORE_MARGIN` | `core-margin: 2.0` |
| `placement.padding` | `1` | `0`, as `global_placement` and ORFS | `placement: {padding: 1}` |
| `placement.macro-cell-halo` | `1.0` | `0`, no blockage | `placement: {macro-cell-halo: 1.0}` |
| `tapcell-tcl` unset | No tap-cell step | `cut_rows`, 2 µm around each placed macro, as ORFS | A `tapcell-tcl` file with no commands |
| `placement.macro-halo` unset, `macro-placement: rtl-mp` | RTL-MP gets a 20 µm halo | RTL-MP keeps its own channels, as ORFS | `placement: {macro-halo: 20}` |
| `post-cts-setup-repair` | `false` | `true`: setup, then hold, as ORFS repairs both after CTS | `post-cts-setup-repair: false` |
| `global-route.congestion-iterations` | `20` | `30`, as ORFS | `global-route: {congestion-iterations: 20}` |

A PDK without both `pin-layers` fails every `rb pnr` run on it at setup (`pnr.pin_layers_missing`); one used only by `rb synth` or `rb power` needs nothing. A `pnr.yaml` run without `floorplan.utilization` or `die-area` fails to load. The packer's `placement.macro-halo` default stays 20 µm. See [Configure the physical platform](concepts/pnr.md#configure-the-physical-platform).

The hardened-block config digest records these defaults, so every abstract hardened with v7 is stale. Harden the blocks again, or pass `--accept-stale` to use them as they are. See [Harden a block](concepts/pnr.md#harden-a-block).

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
