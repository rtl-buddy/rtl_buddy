---
description: Run OpenROAD gate-level power analysis from synthesis or P&R outputs using static, synthetic, SAIF, or VCD activity.
---

# Power Analysis

`rb power` runs OpenROAD `report_power` on a mapped design and reports total, internal, switching, and leakage power. FPGA runs report Vivado power directly and do not use this command.

## Install OpenROAD

OpenROAD 25Q1 or newer must be on `PATH` or configured under `cfg-power-tools`. Only `tool: openroad` is supported; other tools report `SKIP`.

The run's `platform` supplies the PDK and Liberty corner. A platform with `corners:` analyses every listed corner in one session; see [Place-and-Route: Sign off at several corners](pnr.md#sign-off-at-several-corners).

## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` (default) | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and parasitics (see below) | `rb synth`, then `rb pnr` |

The `synth` source suits early leakage and activity comparisons but underestimates switching because it has no routed wire capacitance. Use `pnr` for a post-route estimate. If the routed ODB is missing, rerun `rb pnr`.

## Extracted parasitics

A `pnr` source reads the P&R run's extracted SPEF with `read_spef` when the PDK sets [`rcx-rules`](pnr.md#tune-the-process-dependent-steps). Otherwise it uses `estimate_parasitics -global_routing`.

The SPEF is read only when the P&R run vouches for it: its `pnr.tcl` must contain `write_spef`, and the SPEF must be no older than that `pnr.tcl`. Otherwise the SPEF is left unread with a `power.spef_rejected` warning that names the reason, and the run uses the estimate.

The run's `parasitics` result field is `spef` or `estimated` (absent on a `synth` source). It also appears in the summary's Parasitics column and in the model's options digest, so one ODB measured both ways counts as two experiments.

Extraction usually raises switching power and lowers slack compared with the estimate, because it sees the detailed routes, vias and coupling capacitance. On the template's flat sky130hd pipeclean, switching power rose from 372 to 467 µW and setup WNS fell from +3.61 to +2.68 ns. Internal power and leakage did not change.

## Define power runs

```yaml
rtl-buddy-filetype: power_config

runs:
  - name: demo_power_static
    desc: Static post-synthesis power
    tool: openroad
    mode: static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
    constraints: ../../synth/demo/constraints.sdc
    platform: nangate45_typ
    reglvl: 1000

  - name: demo_power_saif
    desc: Simulation-driven post-route power
    tool: openroad
    mode: dynamic
    netlist-source: pnr
    pnr: demo_pnr_nangate45
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: nangate45_typ
    activity:
      saif: ../../verif/demo/artefacts/csr_smoke/dump.saif
      scope: tb_top/u_dut
    reglvl: 1000
```

Paths resolve from `power.yaml`.

- A `synth` source requires `synth`, `synth-path` and `constraints`.
- A `pnr` source requires `pnr` and `pnr-path`. It uses the routed SDC unless `constraints` overrides it.
- `phys-run` is optional; see [Pair the model with a synthesis run](#pair-the-model-with-a-synthesis-run).

All fields are in [YAML Formats: power.yaml](../reference/yaml.md#poweryaml).

## Give hard macros a library

The `platform` corner characterises standard cells only. A hard macro such as an SRAM, PLL or hardened partition gets its Liberty from the run that placed it. Without it, the macro stays in the design and in `power_instances.rpt` but contributes exactly zero:

```text
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
```

The power run inherits the libraries of the run it references, so the usual case needs no configuration:

| `netlist-source` | Inherited | Why |
| --- | --- | --- |
| `pnr` | The P&R entry's `lib-paths` | The routed ODB already carries every placed master, so no LEF is read |
| `synth` | The synthesis entry's `lib-paths` and `lef-paths` | `link_design` builds the database from LEF masters |

A hardened block named under [`blocks:`](pnr.md#assemble-hardened-blocks) is resolved too. On the `synth` side its abstract's `.lef` is read after `lef-paths`, without the staleness check `rb pnr` applies, because the analysis reads what was routed. A block with no abstract fails the run before OpenROAD starts and names the block. The result's `blocks` field lists the abstracts used.

The abstract's `.lib` is not read, and must not be added to `lib-paths`. It comes from `write_timing_model`, so it has timing arcs but no power tables. Loading it stops OpenSTA from propagating activity to the block's outputs, and a SAIF, VCD or `set_power_activity` value on them is ignored.

Without the `.lib`, a block output acts like a primary input and takes the trace's activity or the default toggle rate. The block still reports 0 W and appears in the [unpowered-cell warning](#read-the-unpowered-cell-warning).

To add a library only the analysis needs, such as a corner the upstream run was not routed against, set `lib-paths` on the `power.yaml` entry:

```yaml
runs:
  - name: demo_power_macro
    netlist-source: pnr
    pnr: demo_pnr
    pnr-path: ../../pnr/demo/pnr.yaml
    platform: sky130hd_tt
    lib-paths: ["../../pdk/sram/sram_TT_1p8V_25C.lib"]
```

Paths resolve from `power.yaml`. Inherited libraries are read first, so a macro library never shadows a standard cell, and a library named on both sides is read once. A configured file missing on disk fails the run before OpenROAD starts.

## Read the unpowered-cell warning

An instance whose master no Liberty covers reports `0.00e+00` in all four columns, indistinguishable in the report from a cell that burns nothing. A run that finds one warns:

```text
power run "demo_power_macro": 1 instance(s) have no Liberty power data and report 0 W — sram_32x256.
```

The run description carries the same qualifier, and `--machine` output adds `unpowered_cells`, `unpowered_cell_count` and `unpowered_instance_count`. All three are absent when nothing was found.

The verdict does not change. The reported watts are a real measurement of everything that had a library, and a design with a LEF-only macro on purpose, such as a placeholder `fakeram45`, could not otherwise pass. Gate on `unpowered_cell_count` if your flow signs off on power.

A cell is reported only when its master is in none of the Liberty files the script read, its total power is exactly zero, and it is not one of the PDK's `fill-cells`. `power_instances.rpt` and `phys-model.json` show which instances are affected.

## Select activity

| Mode and fields | Applied activity |
| --- | --- |
| `mode: static` | No activity command |
| `mode: dynamic` with `activity.saif` | Per-signal SAIF activity |
| `mode: dynamic` with `activity.vcd` | Per-signal VCD activity |
| `mode: dynamic` with no trace | Global synthetic toggle rate and static probability |

`activity.saif` and `activity.vcd` are mutually exclusive. Set `activity.scope` only with a trace, to the hierarchy containing the design, such as `tb_top/u_dut`.

Synthetic defaults are a 0.1 toggle rate and 0.5 static probability. Override them with `activity.default-toggle-rate` and `activity.default-static-prob`.

## Capture SAIF activity

Convert a debug waveform with `rb saif`, then run the analysis:

```bash
rb -M debug test csr_smoke
rb saif verif/demo/artefacts/csr_smoke/dump.fst \
  verif/demo/artefacts/csr_smoke/dump.saif
rb power demo_power_saif -c power/demo/power.yaml -l 1000
```

The converter accepts FST or VCD and writes SAIF v2.0. If the trace starts at the testbench, set `activity.scope` so OpenROAD can find the design top.

## Run power analysis

```bash
rb power --list -c power/demo/power.yaml
rb power demo_power_saif -c power/demo/power.yaml
rb power -c power/demo/power.yaml -l 1000
rb power-regression -c power_regression.yaml -l 1000
```

A regression manifest lists power configs relative to itself:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs:
  - power/block_a/power.yaml
  - power/block_b/power.yaml
```

OpenROAD runs on one thread unless the run sets `threads:` to a positive integer or `auto` (the CPUs of the current allocation). The value is recorded as `openroad_threads`. See [OpenROAD threads](pnr.md#openroad-threads).

## Interpret results

The summary names the design source and activity source, then reports total, internal, switching, and leakage power in readable SI units.

On a multi-corner platform the reported watts are those of the worst corner, the one with the highest design total:

- `worst_corner` names it, and the summary shows it in a `Worst Corner` column.
- `corners` holds each corner's `total_w`, `internal_w`, `switching_w` and `leakage_w`.
- `power.rpt`, `power_instances.rpt`, `phys-model.json` and the model manifest's `corner` option all describe that corner.
- The run fails if any corner's `power.<corner>.rpt` is missing or unparseable.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`. It skips when filtered by `reglvl` or when its tool has no backend.

## Pair the model with a synthesis run

A complete physical model needs the synthesis run's per-module cells and area and the power run's per-instance watts, in one artefact directory. Each run publishes into its own directory by default. If the suites are split into `synth/` and `power/`, or the runs have different names, the halves land apart, `rb phys instance` exits 2 on the synthesis directory, and `rb phys module` finds no modules in the power run's.

`phys-run` pairs them explicitly:

```yaml
runs:
  - name: demo_power_static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
```

- The value is the name of a run in the `synth.yaml` that `synth-path` points at. The power half is published into that run's `artefacts/<run>/`. The directory is derived from the name, so moving the suite keeps the pairing.
- The log, reports and netlist copy stay in the power run's own artefact directory.
- The directory need not exist yet, so the power run may land first.
- A `phys-run` that names no entry in `synth.yaml`, or resolves outside the project, fails as a configuration error.
- `phys-run` requires `netlist-source: synth`. A `pnr` run records no netlist hash, so its half could never merge with a synthesis'.

One directory holds one power half. Two power runs with the same `phys-run`, such as a static and a dynamic analysis of one design, overwrite each other in turn, and a failed rerun withdraws the rows. Leave `phys-run` off runs whose breakdowns must stay apart.

The manifest header names whichever run published last, and that name is what `rb phys runs` lists. Each half's block names its own producer. Changing or removing `phys-run` leaves the previous directory's power half in place; rerun the analysis or delete the directory.

`phys-run` does not force a merge. Halves merge only when both producers recorded the same netlist sha256. If the module rows were counted off a different netlist, the run warns and publishes its half alone. Rerun the synthesis and the power analysis over one netlist. See [Read a model with only one half](phys.md#read-a-model-with-only-one-half).

## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl` | Generated OpenROAD script |
| `power.log` | OpenROAD output |
| `power.rpt` | Raw `report_power` report (worst corner on a multi-corner platform) |
| `power.<corner>.rpt` | Each corner's report, multi-corner platforms only |
| `power_netlist.v` | This run's copy of the upstream netlist, the file OpenROAD reads |
| `power_instances.rpt` | Raw `report_power -instances`, one line per leaf instance |
| `power_instances.cells` | Instance path to Liberty cell, the module column that report lacks |
| `phys-model.json` | Physical model: per-instance rows plus design totals |
| `phys-manifest.json` | Which physical artefacts this run produced, and where |
| `phys-publish.lock` | Held while the model and manifest are rewritten; empty otherwise |

With `phys-run` set, `phys-model.json` and `phys-manifest.json` are written to the synthesis run's directory instead. Query the model with `rb phys`; see [Physical Metrics](phys.md).

An FPGA run and a power run must not share a name within one suite. Both own `artefacts/<name>/power.rpt`, and the second to run overwrites the first.

## Per-instance rows and netlist matching

The per-instance half of the model is a by-product, not a gate. Totals are parsed and reported first, and the hierarchy walk runs inside a Tcl `catch`. If OpenSTA cannot produce it, the model's `instances` block is `null`, a warning says so, and the run still passes.

Halves from `rb synth` and `rb power` travel together only when both recorded the same netlist hash:

- A `synth`-source power run keeps the per-module rows already in the directory when its netlist matches the one they were counted off.
- A later synthesis keeps the per-instance rows when its netlist hash equals the one the power run read.
- A rebuild between the two runs fails the check in whichever direction publishes second.
- A `pnr`-source run reads the routed database, records no hash, inherits no per-module rows, and its rows are never carried forward by a synthesis.

A `synth`-source run first copies its netlist to `power_netlist.v`. OpenROAD reads that copy and the hash identifies it, so a synthesis that lands mid-analysis is detected instead of recorded as a match. The manifest names the copy, so an archived result can re-check the hash against it.

## Recover from a failed run

Read `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals.

`power.rpt` and the netlist copy are deleted before each run and again if the run fails, so a failed run never leaves an earlier run's numbers. If the backend tool cannot be found, nothing is deleted.
