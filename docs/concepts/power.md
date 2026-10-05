---
description: Run OpenROAD gate-level power analysis from synthesis or P&R outputs using static, synthetic, SAIF, or VCD activity.
---

# Power Analysis

`rb power` runs OpenROAD `report_power` on a mapped design and reports total, internal, switching, and leakage power. FPGA runs report Vivado power directly and do not use this command.

## Install OpenROAD

OpenROAD 25Q1 or newer must be on `PATH` or configured under `cfg-power-tools`. Only `tool: openroad` is supported; other tools report `SKIP`.

The run's `platform` names a `cfg-pnr-platforms` entry that supplies the PDK and Liberty corner; see [Place-and-Route: Configure the physical platform](pnr.md#configure-the-physical-platform). A platform with `corners:` analyses every listed corner in one session; see [Place-and-Route: Sign off at several corners](pnr.md#sign-off-at-several-corners).

## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` (default) | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and parasitics | `rb synth`, then `rb pnr` |

The `synth` source suits early leakage and activity comparisons but underestimates switching, which lacks wire capacitance. Use `pnr` for a post-route estimate; if the routed ODB is missing, rerun `rb pnr`.

## Extracted parasitics

A `pnr` source reads the P&R run's extracted `<top>.routed.spef` when the PDK sets [`rcx-rules`](pnr.md#tune-the-process-dependent-steps). Otherwise it estimates parasitics from global routing, with the PDK's [`layer-rc-tcl`](pnr.md#source-platform-tcl-hooks) sourced first when set.

The SPEF is used only if its `pnr.tcl` contains `write_spef` and the SPEF is no older than that `pnr.tcl`. Otherwise the run logs a `power.spef_rejected` warning naming the reason and uses the estimate. The `parasitics` result field (`spef` or `estimated`) and the summary's Parasitics column show which was used, and one ODB measured both ways counts as two experiments.

Extraction usually raises switching power and lowers slack compared with the estimate.

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

## Select activity

| Mode and fields | Applied activity |
| --- | --- |
| `mode: static` | No activity command |
| `mode: dynamic` with `activity.saif` | Per-signal SAIF activity |
| `mode: dynamic` with `activity.vcd` | Per-signal VCD activity |
| `mode: dynamic` with no trace | Global synthetic toggle rate and static probability |

`activity.saif` and `activity.vcd` are mutually exclusive. Set `activity.scope` only with a trace, to the hierarchy containing the design, such as `tb_top/u_dut`. Synthetic defaults are a 0.1 toggle rate and 0.5 static probability; override them with `activity.default-toggle-rate` and `activity.default-static-prob`.

## Capture SAIF activity

Convert a debug waveform with `rb saif`, then run the analysis:

```bash
rb -M debug test csr_smoke
rb saif verif/demo/artefacts/csr_smoke/dump.fst \
  verif/demo/artefacts/csr_smoke/dump.saif
rb power demo_power_saif -c power/demo/power.yaml -l 1000
```

The converter accepts FST or VCD. If the trace starts at the testbench, set `activity.scope` to the design top.

## Give hard macros a library

The `platform` corner characterises standard cells only. A hard macro such as an SRAM, PLL or hardened partition needs its own Liberty. Without one, the macro stays in `power_instances.rpt` but contributes exactly zero:

```text
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
```

The power run inherits the libraries of the run it references, so the usual case needs no configuration:

| `netlist-source` | Inherited from the upstream entry |
| --- | --- |
| `pnr` | `lib-paths` |
| `synth` | `lib-paths` and `lef-paths` |

A hardened block named under [`blocks:`](pnr.md#assemble-hardened-blocks) is resolved too. A block with no abstract fails the run before OpenROAD starts and names the block. Do not add the abstract's `.lib` to `lib-paths`: it has timing arcs but no power tables, and loading it makes OpenROAD ignore SAIF, VCD and `set_power_activity` values on the block's outputs. Without it the block reports 0 W and appears in the [unpowered-cell warning](#read-the-unpowered-cell-warning).

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

Paths resolve from `power.yaml`.

## Read the unpowered-cell warning

An instance whose master no Liberty covers reports `0.00e+00` in every column, the same as a cell that burns nothing. A run that finds one warns:

```text
power run "demo_power_macro": 1 instance(s) have no Liberty power data and report 0 W — sram_32x256.
```

`--machine` output adds `unpowered_cells`, `unpowered_cell_count` and `unpowered_instance_count`; all three are absent when nothing was found. To fix it, supply the cell's Liberty as described in [Give hard macros a library](#give-hard-macros-a-library).

The verdict does not change: the reported watts are a real measurement of everything that had a library, and a design with a LEF-only macro on purpose, such as a `fakeram45` placeholder, could not otherwise pass. Gate on `unpowered_cell_count` if your flow signs off on power. Fill cells listed in the PDK's `fill-cells` are not reported.

## Run power analysis

```bash
rb power --list -c power/demo/power.yaml
rb power demo_power_saif -c power/demo/power.yaml
rb power-regression -c power_regression.yaml -l 1000
```

A regression manifest lists power configs relative to itself:

```yaml
rtl-buddy-filetype: power_reg_config
power-configs:
  - power/block_a/power.yaml
  - power/block_b/power.yaml
```

OpenROAD runs on one thread unless the run sets `threads:` to a positive integer or `auto`. See [OpenROAD threads](pnr.md#openroad-threads).

## Interpret results

The summary names the design source and activity source, then reports total, internal, switching, and leakage power in SI units.

On a multi-corner platform the reported watts are those of the worst corner, the one with the highest design total. `worst_corner` names it, `corners` holds every corner's four figures, and `power.rpt`, `power_instances.rpt` and the physical model describe that corner.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`; on a multi-corner platform every corner's report must parse. It skips when filtered by `reglvl` or when its tool has no backend.

## Pair the model with a synthesis run

A complete physical model needs the synthesis run's per-module cells and area and the power run's per-instance watts in one artefact directory. Each run publishes into its own directory by default. If the suites are split into `synth/` and `power/`, or the runs have different names, the halves land apart: `rb phys instance` exits 2 on the synthesis directory, and `rb phys module` finds no modules in the power run's.

`phys-run` pairs them explicitly:

```yaml
runs:
  - name: demo_power_static
    synth: demo_synth_nangate45
    synth-path: ../../synth/demo/synth.yaml
    phys-run: demo_synth_nangate45
```

- The value names a run in the `synth.yaml` that `synth-path` points at. The model and manifest are published into that run's `artefacts/<run>/`; the log, reports and netlist copy stay in the power run's own directory. The synthesis directory need not exist yet.
- A `phys-run` that names no entry in `synth.yaml`, or resolves outside the project, is a configuration error.
- `phys-run` requires `netlist-source: synth`.
- One directory holds one power half. Two power runs with the same `phys-run`, such as a static and a dynamic analysis of one design, overwrite each other. Leave `phys-run` off runs whose breakdowns must stay apart.
- Removing or changing `phys-run` leaves the old directory's power half in place. Rerun the analysis or delete the directory.

`phys-run` does not force a merge. The halves merge only when the synthesis and the power run used the same netlist. Otherwise the run warns and publishes its half alone; rerun the synthesis and the power analysis over one netlist. A later synthesis likewise keeps the power rows only if its netlist is unchanged. A `pnr` source never merges. See [Read a model with only one half](phys.md#read-a-model-with-only-one-half).

## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl`, `power.log` | Generated OpenROAD script and its output |
| `power.rpt` | Raw `report_power` report (worst corner on a multi-corner platform) |
| `power.<corner>.rpt` | Each corner's report, multi-corner platforms only |
| `power_netlist.v` | This run's copy of the upstream netlist, the file OpenROAD reads |
| `power_instances.rpt` | Raw `report_power -instances`, one line per leaf instance |
| `phys-model.json`, `phys-manifest.json` | Physical model and its manifest; query with `rb phys` ([Physical Metrics](phys.md)) |

With `phys-run` set, the model and manifest are written to the synthesis run's directory instead.

An FPGA run and a power run must not share a name within one suite. Both own `artefacts/<name>/power.rpt`, and the second to run overwrites the first.

## Troubleshooting

Read `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals. A failed run deletes `power.rpt` and the netlist copy, so it never leaves an earlier run's numbers.

- **`N instance(s) have no Liberty power data and report 0 W`:** see [Read the unpowered-cell warning](#read-the-unpowered-cell-warning).
- **`N configured macro input(s) not on disk`:** the run stops. Fix the listed `lib-paths` or `lef-paths` on this entry or the upstream run.
- **`power.spef_rejected`:** the SPEF is unreadable or stale and the estimate was used. Rerun `rb pnr`, then the power run.
- **`phys-model.json has no per-instance breakdown`:** the per-instance report was missing or unreadable. Totals are still recorded and the run passes, but `rb phys instance` has nothing for it.
- **`phys-model.json was not written`:** publishing failed. Any model in the directory belongs to an earlier run.
- **Previous run's per-instance rows could not be withdrawn:** the run stops rather than leave stale rows over cleared reports. Fix the error the message names and rerun.
