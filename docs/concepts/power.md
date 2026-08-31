---
description: Run OpenROAD gate-level power analysis from synthesis or P&R outputs using static, synthetic, SAIF, or VCD activity.
---

# Power Analysis

`rb power` runs OpenROAD `report_power` on a mapped design and reports total, internal, switching, and leakage power. FPGA runs report Vivado power directly; they do not use this command.

## Choose the design source

| Source | Input | Timing and parasitics | Required upstream runs |
| --- | --- | --- | --- |
| `netlist-source: synth` | `synth_netlist.v` | User SDC, no wire parasitics or clock tree | `rb synth` |
| `netlist-source: pnr` | `<top>.routed.odb` | Routed SDC, CTS, and global-routing parasitic estimates | `rb synth`, then `rb pnr` |

The default `synth` source is useful for early leakage and activity comparisons but underestimates switching because it has no routed wire capacitance. Use `pnr` for a more representative post-route estimate.

If the routed ODB is missing, rerun `rb pnr`.

## Install OpenROAD

OpenROAD 25Q1 or newer must be on `PATH` or configured under `cfg-power-tools`. Power runs currently support only `tool: openroad`; unsupported tools report `SKIP`.

The selected `cfg-pnr-platforms` entry supplies the PDK and Liberty corner. See [Place-and-Route](pnr.md#configure-the-physical-platform).

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

Paths resolve from `power.yaml`. A synth-source run requires `synth`, `synth-path`, and `constraints`. A P&R-source run requires `pnr` and `pnr-path`; it uses the routed SDC unless `constraints` overrides it.

See [YAML Formats: power.yaml](../reference/yaml.md#poweryaml) for all fields.

## Select activity

| Mode and fields | Applied activity |
| --- | --- |
| `mode: static` | No activity command |
| `mode: dynamic` with `activity.saif` | Per-signal SAIF activity |
| `mode: dynamic` with `activity.vcd` | Per-signal VCD activity |
| `mode: dynamic` with no trace | Global synthetic toggle rate and static probability |

`activity.saif` and `activity.vcd` are mutually exclusive. Set `activity.scope` only with a trace; use the hierarchy containing the design, such as `tb_top/u_dut`.

Synthetic defaults are a 0.1 toggle rate and 0.5 static probability. Override them with `activity.default-toggle-rate` and `activity.default-static-prob`.

## Capture SAIF activity

Convert a debug waveform with the built-in `rb saif` command:

```bash
rb -M debug test csr_smoke
rb saif verif/demo/artefacts/csr_smoke/dump.fst \
  verif/demo/artefacts/csr_smoke/dump.saif
rb power demo_power_saif -c power/demo/power.yaml -l 1000
```

The converter accepts FST or VCD and writes SAIF v2.0. If the trace starts at the testbench, configure `activity.scope` so OpenROAD can map the design top.

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

## Interpret results

The summary identifies the selected design source and resolved activity source, then reports total, internal, switching, and leakage power with readable SI scaling.

A run passes when OpenROAD exits 0, emits no `[ERROR ...]` line, and produces a parseable `Total` row in `power.rpt`. It skips when filtered by `reglvl` or when its tool has no registered backend.

## Inspect artefacts

Outputs land under `<power-dir>/artefacts/<run>/`:

| File | Purpose |
| --- | --- |
| `power.tcl` | Generated OpenROAD script |
| `power.log` | OpenROAD output |
| `power.rpt` | Raw `report_power` report |

On failure, inspect `power.log` for tool and input errors, then `power.rpt` for missing or malformed totals. `power.rpt` is deleted before each run, so a run that never got as far as `report_power` leaves none — read `power.log` in that case rather than an earlier run's numbers.
